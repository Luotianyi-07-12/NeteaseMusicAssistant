from __future__ import annotations

import math
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .api import ApiError, LoginRequired
from .runner import RunLock, atomic_json, run_batch


def parse_song_ids(text):
    parts = [part for part in re.split(r'[,，;；\s]+', text.strip()) if part]
    if not parts or any(not part.isascii() or not part.isdecimal() or int(part) <= 0 for part in parts):
        raise ValueError('请填写正整数歌曲 ID，多个 ID 可用逗号、空格或换行分隔')
    ids = list(dict.fromkeys(map(int, parts)))
    if len(ids) > 250:
        raise ValueError('一次最多选择 250 首歌曲')
    return ids


def _pause(seconds, stop, cancel):
    if cancel is None:
        if not stop.is_set():
            time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while not stop.is_set() and time.monotonic() < deadline:
        if cancel.wait(min(0.2, max(0, deadline - time.monotonic()))):
            raise KeyboardInterrupt


def run_multi_repeat(client, state_dir, *, song_ids, repetitions=1, rounds=1,
                     concurrency=3, playlist_id=None, interval=2.0,
                     endpoint='auto', verify_wait=15, cancel=None):
    if (not isinstance(repetitions, int) or isinstance(repetitions, bool) or not 1 <= repetitions <= 100
            or not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 1000
            or not isinstance(concurrency, int) or isinstance(concurrency, bool) or not 1 <= concurrency <= 8
            or not math.isfinite(interval) or interval < 1
            or not math.isfinite(verify_wait) or not 0 <= verify_wait <= 600):
        raise ValueError('每首每轮 1–100 次；循环 1–1000 轮；同时处理 1–8 首；间隔至少 1 秒')
    if (not isinstance(song_ids, (list, tuple)) or not song_ids
            or any(not isinstance(id_, int) or isinstance(id_, bool) or id_ <= 0 for id_ in song_ids)):
        raise ValueError('歌曲 ID 必须为正整数列表')
    song_ids = list(dict.fromkeys(song_ids))
    if len(song_ids) > 250:
        raise ValueError('一次最多选择 250 首歌曲')
    state_dir = Path(state_dir)
    with RunLock(state_dir / 'run.lock'):
        uid, _ = client.identity()
        # Resolve every ID before the first write, so an invalid entry cannot start a partial job.
        songs = []
        for id_ in song_ids:
            found = client.songs(uid, playlist_id, id_, 1)
            if not found or found[0][0].get('id') != id_:
                raise ApiError('/song/detail', None, f'未找到歌曲 {id_}，本次尚未上报')
            songs.append(found[0])
        before = client.statistics(uid)
        def counts():
            result = {}
            for id_ in song_ids:
                try:
                    result[str(id_)] = client.song_play_count(uid, id_)
                except LoginRequired:
                    raise
                except ApiError:
                    result[str(id_)] = None
            return result
        count_before = counts()
        report = {'mode': 'single' if len(songs) == 1 else 'multi', 'job_id': uuid.uuid4().hex,
                  'uid': uid, 'song_ids': song_ids, 'song_id': song_ids[0],
                  'requested': len(songs) * repetitions * rounds, 'repetitions': repetitions,
                  'rounds': rounds, 'rounds_completed': 0, 'concurrency': min(concurrency, len(songs)),
                  'accepted': 0, 'failed': 0, 'before': before, 'attempts': [], 'result': 'running',
                  'songs': {str(song['id']): {'name': song.get('name', str(song['id'])),
                            'accepted': 0, 'failed': 0, 'before': count_before[str(song['id'])]}
                            for song, _ in songs}}
        report['song_name'] = songs[0][0].get('name', str(song_ids[0]))
        job_path = state_dir / ('repeat-' + report['job_id'] + '.json')
        last_path = state_dir / 'last-report.json'
        lock, stop = threading.Lock(), threading.Event()
        def save():
            # Caller holds lock whenever workers are active.
            atomic_json(job_path, report)
            atomic_json(last_path, report)
        save()
        print(f"计划：{len(songs)} 首 × 每首每轮 {repetitions} 次 × {rounds} 轮 = {report['requested']} 次。", flush=True)
        print(f"同时处理 {report['concurrency']} 首；每首独立计数，全部完成才进入下一轮。", flush=True)

        def worker(song, source, round_number):
            key = str(song['id'])
            for number in range(1, repetitions + 1):
                if cancel is not None and cancel.is_set():
                    raise KeyboardInterrupt
                with lock:
                    if stop.is_set():
                        return
                    attempt = {'round': round_number, 'song_id': song['id'], 'number': number, 'state': 'pending'}
                    report['attempts'].append(attempt)
                    save()
                try:
                    used = client.scrobble(song, source, endpoint)
                except ApiError as error:
                    with lock:
                        stop.set()
                        attempt['state'] = 'unconfirmed'
                        report['failed'] += 1
                        report['songs'][key]['failed'] += 1
                        report['last_error'] = str(error)
                        save()
                        print(str(error), flush=True)
                    if isinstance(error, LoginRequired):
                        raise
                    return
                with lock:
                    attempt['state'] = 'accepted'
                    report['accepted'] += 1
                    report['songs'][key]['accepted'] += 1
                    report['used_endpoint'] = used
                    save()
                    print(f"第 {round_number}/{rounds} 轮 · {report['songs'][key]['name']}：{number}/{repetitions} 已受理", flush=True)
                if number < repetitions:
                    _pause(interval, stop, cancel)
        try:
            for round_number in range(1, rounds + 1):
                if cancel is not None and cancel.is_set():
                    raise KeyboardInterrupt
                print(f'开始第 {round_number}/{rounds} 轮。', flush=True)
                with ThreadPoolExecutor(max_workers=report['concurrency'], thread_name_prefix='ncm-song') as pool:
                    futures = [pool.submit(worker, song, source, round_number) for song, source in songs]
                    try:
                        for future in as_completed(futures):
                            future.result()
                    except BaseException:
                        stop.set()
                        raise
                if stop.is_set():
                    break
                report['rounds_completed'] = round_number
                save()
                if round_number < rounds:
                    _pause(interval, stop, cancel)
            deadline = time.monotonic() + verify_wait
            after = client.statistics(uid)
            count_after = counts()
            def pending_growth():
                return any(report['songs'][key]['accepted'] and count_before[key] is not None
                           and count_after[key] is not None and count_after[key] <= count_before[key]
                           for key in count_before)
            while pending_growth() and time.monotonic() < deadline:
                _pause(min(5, max(0, deadline - time.monotonic())), threading.Event(), cancel)
                count_after = counts()
                after = client.statistics(uid)
            deltas = []
            for key, song_result in report['songs'].items():
                start, end = count_before[key], count_after[key]
                delta = end - start if start is not None and end is not None else None
                song_result.update(after=end, observed_delta=delta)
                deltas.append(delta)
                print(f"{song_result['name']}：上报受理 {song_result['accepted']} 次；"
                      + (f'周次数增量 {delta}。' if delta is not None else '周次数未核验。'), flush=True)
            total_delta = sum(deltas) if all(delta is not None for delta in deltas) else None
            report.update(after=after, observed_delta=after['listenSongs'] - before['listenSongs'],
                          single_count_before=count_before[str(song_ids[0])], single_count_after=count_after[str(song_ids[0])],
                          single_observed_delta=total_delta, playback_observed_delta=total_delta,
                          result='verified_growth' if total_delta is not None and total_delta > 0
                          else 'no_observed_growth' if total_delta is not None else 'count_unavailable')
            save()
            print(f"本次完成 {report['rounds_completed']}/{rounds} 轮；受理 {report['accepted']}/{report['requested']} 次。", flush=True)
            if total_delta is None:
                print('部分歌曲不在可见周排行中，实际次数标为未核验。', flush=True)
            return report
        except BaseException:
            stop.set()
            with lock:
                report['result'] = 'interrupted'
                save()
            raise


def run_playlist_rounds(client, state_dir, *, rounds=1, cancel=None, **options):
    if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 1000:
        raise ValueError('循环轮数必须为 1–1000')
    reports = []
    for round_number in range(1, rounds + 1):
        if cancel is not None and cancel.is_set():
            raise KeyboardInterrupt
        print(f'歌单模式：第 {round_number}/{rounds} 轮。', flush=True)
        report = run_batch(client, state_dir, cancel=cancel, **options)
        reports.append(report)
        if report['failed'] or not report['accepted']:
            print('本轮失败、没有新歌曲或已达当天上限，结束循环。', flush=True)
            break
    result = dict(reports[-1], mode='playlist', before=reports[0]['before'],
                  accepted=sum(r['accepted'] for r in reports), failed=sum(r['failed'] for r in reports),
                  rounds=rounds, rounds_completed=len(reports))
    result['observed_delta'] = result['after']['listenSongs'] - result['before']['listenSongs']
    atomic_json(Path(state_dir) / 'last-report.json', result)
    return result
