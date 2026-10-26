import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from ncm_tools.api import ApiError
from ncm_tools.repeat import parse_song_ids, run_multi_repeat, run_playlist_rounds


class RecordingClient:
    def __init__(self, barrier=None, fail_id=None, cancel=None):
        self.counts = Counter({1: 10, 2: 20, 3: 30})
        self.calls = []
        self.lock = threading.Lock()
        self.barrier = barrier
        self.fail_id = fail_id
        self.cancel = cancel
        self.active = 0
        self.max_active = 0

    def identity(self):
        return 123, 'Test'

    def songs(self, uid, playlist_id, song_id, limit):
        return [({'id': song_id, 'name': f'Song {song_id}', 'dt': 180000}, 0)]

    def statistics(self, uid):
        return {'listenSongs': 100, 'level': 6}

    def song_play_count(self, uid, song_id):
        return self.counts[song_id]

    def scrobble(self, song, source, endpoint):
        song_id = song['id']
        with self.lock:
            self.calls.append(song_id)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.barrier:
                self.barrier.wait(timeout=3)
            if song_id == self.fail_id:
                raise ApiError('/scrobble/v1', 503, 'test failure')
            with self.lock:
                self.counts[song_id] += 1
            if self.cancel:
                self.cancel.set()
            return '/scrobble/v1'
        finally:
            with self.lock:
                self.active -= 1


class RepeatTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)

    def run_job(self, client, **options):
        with contextlib.redirect_stdout(io.StringIO()), patch('ncm_tools.repeat._pause'):
            return run_multi_repeat(client, self.path, verify_wait=0, **options)

    def test_song_list_parses_multiple_separators_and_deduplicates(self):
        self.assertEqual(parse_song_ids('1, 2，3\n1; 4；5'), [1, 2, 3, 4, 5])
        for value in ('', '1,bad', '-1', '0', '1.5', '１２３'):
            with self.assertRaises(ValueError):
                parse_song_ids(value)

    def test_three_songs_two_rounds_each_ten_is_sixty_requests(self):
        client = RecordingClient()
        result = self.run_job(client, song_ids=[1, 2, 3], repetitions=10, rounds=2, concurrency=3)
        self.assertEqual(Counter(client.calls), {1: 20, 2: 20, 3: 20})
        self.assertEqual(result['requested'], 60)
        self.assertEqual(result['accepted'], 60)
        self.assertEqual(result['rounds_completed'], 2)
        self.assertEqual(result['playback_observed_delta'], 60)
        self.assertEqual(result['observed_delta'], 0)
        self.assertEqual({item['observed_delta'] for item in result['songs'].values()}, {20})
        states = [attempt['round'] for attempt in result['attempts']]
        self.assertEqual(states, [1] * 30 + [2] * 30)
        self.assertEqual(json.loads((self.path / 'last-report.json').read_text(encoding='utf-8'))['accepted'], 60)

    def test_three_songs_are_actually_concurrent(self):
        client = RecordingClient(barrier=threading.Barrier(3))
        result = self.run_job(client, song_ids=[1, 2, 3], repetitions=2, rounds=2, concurrency=3)
        self.assertEqual(client.max_active, 3)
        self.assertEqual(result['accepted'], 12)
        self.assertEqual(result['rounds_completed'], 2)

    def test_single_concurrency_never_overlaps(self):
        client = RecordingClient()
        result = self.run_job(client, song_ids=[1, 2, 3], repetitions=2, concurrency=1)
        self.assertEqual(client.max_active, 1)
        self.assertEqual(result['accepted'], 6)

    def test_error_stops_queued_songs_and_later_rounds(self):
        client = RecordingClient(fail_id=2)
        result = self.run_job(client, song_ids=[1, 2, 3], repetitions=2, rounds=3, concurrency=1)
        self.assertEqual(client.calls, [1, 1, 2])
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['rounds_completed'], 0)
        self.assertEqual(result['attempts'][-1]['state'], 'unconfirmed')

    def test_cancellation_persists_progress_and_prevents_later_writes(self):
        cancel = threading.Event()
        client = RecordingClient(cancel=cancel)
        with self.assertRaises(KeyboardInterrupt):
            self.run_job(client, song_ids=[1, 2, 3], repetitions=10, rounds=3, concurrency=1, cancel=cancel)
        self.assertEqual(client.calls, [1])
        saved = json.loads((self.path / 'last-report.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['result'], 'interrupted')
        self.assertEqual(saved['accepted'], 1)

    def test_invalid_limits_do_not_send_any_writes(self):
        client = RecordingClient()
        for options in ({'rounds': 0}, {'rounds': 1001}, {'rounds': True}, {'repetitions': 101},
                        {'concurrency': 9}, {'concurrency': 0}):
            with self.assertRaises(ValueError):
                self.run_job(client, song_ids=[1], **options)
        self.assertFalse(client.calls)

    def test_playlist_rounds_stop_when_no_new_songs_remain(self):
        reports = [dict(accepted=2, failed=0, before={'listenSongs': 100}, after={'listenSongs': 102}),
                   dict(accepted=0, failed=0, before={'listenSongs': 102}, after={'listenSongs': 102})]
        with patch('ncm_tools.repeat.run_batch', side_effect=reports) as run, contextlib.redirect_stdout(io.StringIO()):
            result = run_playlist_rounds(RecordingClient(), self.path, rounds=10)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result['accepted'], 2)
        self.assertEqual(result['observed_delta'], 2)


if __name__ == '__main__':
    unittest.main()
