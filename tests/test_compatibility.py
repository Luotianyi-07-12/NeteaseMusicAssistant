import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ncm_tools.api import ApiClient, ApiError, LoginRequired
from ncm_tools.auth import load_cookies, parse_cookies, save_cookies, login_qr
from ncm_tools.runner import CHINA, RunLock, next_run, run_batch, run_repeat


class Fixture:
    def __init__(self):
        self.calls = []
        self.urls = []
        self.headers = []
        self.total = 100
        self.song_total = 10
        self.song_growth = False
        self.record_available = True
        self.growth = False
        self.legacy = False
        self.bad_subrequest = False
        self.fail_write = False
        self.authenticated = True
        self.qr_code = 803
        self.read_failures = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                fields = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
                owner.urls.append(self.path)
                owner.headers.append(dict(self.headers))
                self.path = urlsplit(self.path).path
                owner.calls.append((self.path, fields))
                status = 200
                if self.path == '/login/status':
                    body = {'data': {'code': 200, 'profile': {'userId': 123, 'nickname': 'Test'}
                                    if owner.authenticated else None, 'account': None}}
                elif self.path == '/daily_signin':
                    body = {'code': 200 if fields['type'] == ['0'] else -2}
                    if fields['type'] == ['1']:
                        status = 400
                elif self.path == '/user/detail':
                    if owner.read_failures:
                        owner.read_failures -= 1
                        status, body = 503, {'code': 503}
                    else:
                        body = {'code': 200, 'listenSongs': owner.total, 'level': 6}
                elif self.path == '/user/level':
                    body = {'code': 200, 'data': {'level': 6, 'nowPlayCount': owner.total, 'nowLoginCount': 10}}
                elif self.path == '/user/record':
                    body = {'code': 200, 'weekData': [{'song': {'id': 1}, 'playCount': owner.song_total}]
                            if owner.record_available else []}
                elif self.path == '/user/playlist':
                    body = {'code': 200, 'playlist': [{'id': 999, 'creator': {'userId': 123}}]}
                elif self.path == '/playlist/detail':
                    body = {'code': 200, 'playlist': {'trackIds': [{'id': i} for i in [1, 1, 2, 3, 4]]}}
                elif self.path == '/song/detail':
                    body = {'code': 200, 'songs': [{'id': int(i), 'dt': 245678, 'name': 'Test song', 'ar': []}
                                                 for i in fields['ids'][0].split(',')]}
                elif self.path.startswith('/scrobble'):
                    if owner.fail_write:
                        status, body = 503, {'code': 503, 'cookie': 'DO_NOT_PRINT'}
                    elif owner.legacy and self.path == '/scrobble/v1':
                        status, body = 404, {'code': 404}
                    elif owner.bad_subrequest:
                        body = {'code': 200, 'details': {'play': {'code': 500}}}
                    else:
                        owner.total += int(owner.growth)
                        owner.song_total += int(owner.song_growth)
                        body = {'code': 200, 'data': 'success'}
                elif self.path == '/login/qr/key':
                    body = {'code': 200, 'data': {'unikey': 'test-key'}}
                elif self.path == '/login/qr/create':
                    body = {'code': 200, 'data': {'qrimg': 'data:image/png;base64,aGVsbG8=',
                                                 'qrurl': 'https://music.163.com/login?codekey=test-key'}}
                elif self.path == '/login/qr/check':
                    body = {'code': owner.qr_code, 'cookie': 'MUSIC_U=test; Path=/; HttpOnly; __csrf=abc;'}
                elif self.path == '/redirect':
                    self.send_response(307)
                    self.send_header('Location', '/captured')
                    self.end_headers()
                    return
                else:
                    status, body = 404, {'code': 404}
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = ApiClient(f'http://127.0.0.1:{self.server.server_port}', {'MUSIC_U': 'secret'})

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)

    def run_batch(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return run_batch(self.fixture.client, self.path, verify_wait=0, **kwargs)

    def test_nested_login_status_and_account_fallback(self):
        self.assertEqual(self.fixture.client.identity(), (123, 'Test'))
        with patch.object(self.fixture.client, 'request', return_value={'code': 200, 'account': {'id': 55}}):
            self.assertEqual(self.fixture.client.identity(), (55, ''))

    def test_requests_bypass_post_cache_without_cookie_in_url(self):
        self.fixture.client.identity()
        self.fixture.client.identity()
        self.assertNotEqual(self.fixture.urls[0], self.fixture.urls[1])
        self.assertTrue(all('timestamp=' in url for url in self.fixture.urls))
        self.assertTrue(all('secret' not in url and 'MUSIC_U' not in url for url in self.fixture.urls))
        self.assertTrue(all(h.get('X-Apicache-Bypass') == '1' for h in self.fixture.headers))

    def test_logged_out_stops_instead_of_looping(self):
        self.fixture.authenticated = False
        with self.assertRaises(LoginRequired):
            self.fixture.client.identity()
        self.assertEqual(len(self.fixture.calls), 1)

    def test_signin_both_types_and_already_signed_http_error(self):
        self.assertEqual(self.fixture.client.signin(), {'安卓签到': '签到成功', '网页签到': '已签到'})
        self.assertEqual([c[1]['type'][0] for c in self.fixture.calls], ['0', '1'])

    def test_no_growth_is_not_reported_as_success(self):
        report = self.run_batch(max_songs=1)
        self.assertEqual(report['accepted'], 1)
        self.assertEqual(report['observed_delta'], 0)
        self.assertEqual(report['result'], 'no_observed_growth')

    def test_real_statistic_delta_and_deduplicated_playlist(self):
        self.fixture.growth = True
        with patch('ncm_tools.runner.time.sleep'):
            report = self.run_batch(max_songs=3)
        self.assertEqual(report['accepted'], 3)
        self.assertEqual(report['observed_delta'], 3)
        self.assertEqual(report['result'], 'verified_growth')
        calls = [c for c in self.fixture.calls if c[0] == '/scrobble/v1']
        self.assertEqual([c[1]['id'][0] for c in calls], ['1', '2', '3'])
        self.assertEqual(calls[0][1]['time'], ['245'])
        self.assertNotIn('secret', (self.path / 'last-report.json').read_text(encoding='utf-8'))

    def test_restart_skips_previous_songs_and_advances(self):
        self.run_batch(max_songs=1)
        self.run_batch(max_songs=1)
        ids = [c[1]['id'][0] for c in self.fixture.calls if c[0] == '/scrobble/v1']
        self.assertEqual(ids, ['1', '2'])

    def test_missing_v1_falls_back_only_once(self):
        self.fixture.legacy = True
        report = self.run_batch(max_songs=1)
        self.assertEqual(report['used_endpoint'], '/scrobble')

    def test_write_error_is_not_retried_or_fallen_back(self):
        self.fixture.fail_write = True
        report = self.run_batch(max_songs=3)
        self.assertEqual(report['failed'], 1)
        self.assertEqual(len([c for c in self.fixture.calls if c[0].startswith('/scrobble')]), 1)
        self.assertNotIn('DO_NOT_PRINT', json.dumps(report))
        self.fixture.fail_write = False
        self.run_batch(max_songs=1)
        ids = [c[1]['id'][0] for c in self.fixture.calls if c[0] == '/scrobble/v1']
        self.assertEqual(ids, ['1', '2'])

    def test_child_error_is_not_counted_as_accepted(self):
        self.fixture.bad_subrequest = True
        report = self.run_batch(max_songs=1)
        self.assertEqual(report['accepted'], 0)
        self.assertEqual(report['failed'], 1)

    def test_v1_failure_with_top_level_code_200_is_not_success(self):
        with patch.object(self.fixture.client, 'request', return_value={
            'code': 200, 'msg': 'PLV 上报失败', 'details': {'code': 200, 'data': {'rate': 0}}}):
            with self.assertRaises(ApiError):
                self.fixture.client.scrobble({'id': 1, 'dt': 180000}, 2)

    def test_transient_reads_are_retried(self):
        self.fixture.read_failures = 1
        with patch('ncm_tools.api.time.sleep'):
            self.assertEqual(self.fixture.client.statistics(123)['listenSongs'], 100)
        self.assertEqual(len([c for c in self.fixture.calls if c[0] == '/user/detail']), 2)

    def test_cookie_header_and_browser_export(self):
        self.assertEqual(parse_cookies('Cookie: MUSIC_U=a=b; __csrf=c;')['MUSIC_U'], 'a=b')
        result = parse_cookies(json.dumps([{'name': 'MUSIC_U', 'value': 'token', 'domain': '.music.163.com'},
                                          {'name': 'foreign', 'value': 'no', 'domain': 'example.com'}]))
        self.assertNotIn('foreign', result)
        with self.assertRaises(ValueError):
            parse_cookies('foo=bar;')

    def test_joined_set_cookie_headers_with_empty_segments(self):
        raw = ('NMTID=guest; Max-Age=315360000; Path=/;;MUSIC_U=real=token; '
               'Expires=Sat, 24 Oct 2026 14:00:00 GMT; Domain=.music.163.com; '
               'HttpOnly; Secure; SameSite=None;; __csrf=csrf-token; Path=/;')
        cookies = parse_cookies(raw)
        self.assertEqual(cookies['MUSIC_U'], 'real=token')
        self.assertEqual(cookies['__csrf'], 'csrf-token')
        self.assertNotIn('Expires', cookies)
        self.assertNotIn('Path', cookies)

    def test_local_cookie_roundtrip(self):
        file = self.path / 'auth.dat'
        if not save_cookies(file, {'MUSIC_U': 'private-token', '__csrf': 'test'}):
            self.skipTest('Windows DPAPI unavailable in this execution host; no plaintext credential saved')
        self.assertEqual(load_cookies(file)['MUSIC_U'], 'private-token')
        import os
        if os.name == 'nt':
            self.assertNotIn(b'private-token', file.read_bytes())

    def test_qr_login_checks_identity_before_persisting(self):
        with contextlib.redirect_stdout(io.StringIO()), patch('ncm_tools.auth.save_cookies') as save:
            identity = login_qr(self.fixture.client, self.path / 'auth.dat', open_browser=False)
        self.assertEqual(identity[0], 123)
        self.assertEqual(save.call_args.args[1]['MUSIC_U'], 'test')

    def test_expired_qr_does_not_save_cookie(self):
        self.fixture.qr_code = 800
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            login_qr(self.fixture.client, self.path / 'auth.dat', open_browser=False)
        self.assertFalse((self.path / 'auth.dat').exists())

    def test_redirect_does_not_forward_secret(self):
        with self.assertRaises(ApiError):
            self.fixture.client.request('/redirect')
        self.assertFalse(any(c[0] == '/captured' for c in self.fixture.calls))

    def test_lock_prevents_concurrent_runs(self):
        with RunLock(self.path / 'run.lock'):
            with self.assertRaises(RuntimeError):
                with RunLock(self.path / 'run.lock'):
                    pass

    def test_daily_schedule_avoids_repeat_in_9am_hour(self):
        now = datetime(2026, 10, 24, 9, 1, tzinfo=CHINA)
        self.assertEqual(next_run(now).day, 25)
        self.assertEqual(next_run(now.replace(hour=8)).day, 24)

    def test_rejects_unsafe_api_url_and_unbounded_run(self):
        for url in ['http://example.com', 'https://user:pass@example.com', 'file:///tmp', 'https://example.com?cookie=bad']:
            with self.assertRaises(ValueError):
                ApiClient(url)
        for options in [{'max_songs': 251}, {'interval': 0.1}, {'target': 0}, {'interval': float('nan')}]:
            with self.assertRaises(ValueError):
                self.run_batch(**options)

    def test_single_song_100_times_is_exactly_bounded(self):
        self.fixture.song_growth = True
        with contextlib.redirect_stdout(io.StringIO()), patch('ncm_tools.runner.time.sleep'):
            report = run_repeat(self.fixture.client, self.path, song_id=1, repetitions=100, verify_wait=0)
        calls = [c for c in self.fixture.calls if c[0] == '/scrobble/v1']
        self.assertEqual(len(calls), 100)
        self.assertTrue(all(c[1]['id'] == ['1'] for c in calls))
        self.assertEqual(report['accepted'], 100)
        self.assertEqual(report['single_observed_delta'], 100)
        self.assertEqual(report['observed_delta'], 0)

    def test_single_song_101_is_rejected_before_any_request(self):
        for count in (0, 101, -1, True):
            with self.assertRaises(ValueError):
                run_repeat(self.fixture.client, self.path, song_id=1, repetitions=count)
        self.assertEqual(len(self.fixture.calls), 0)

    def test_single_song_failure_stops_without_repeating_ambiguous_write(self):
        self.fixture.fail_write = True
        with contextlib.redirect_stdout(io.StringIO()):
            report = run_repeat(self.fixture.client, self.path, song_id=1, repetitions=100, verify_wait=0)
        self.assertEqual(report['accepted'], 0)
        self.assertEqual(report['failed'], 1)
        self.assertEqual(len(report['attempts']), 1)

    def test_missing_song_in_ranking_is_unverified_not_zero(self):
        self.fixture.record_available = False
        with contextlib.redirect_stdout(io.StringIO()):
            report = run_repeat(self.fixture.client, self.path, song_id=1, repetitions=1, verify_wait=0)
        self.assertIsNone(report['single_observed_delta'])
        self.assertEqual(report['result'], 'count_unavailable')


if __name__ == '__main__':
    unittest.main()
