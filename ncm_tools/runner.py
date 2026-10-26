from __future__ import annotations

import json
import math
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import ApiError, LoginRequired

CHINA = timezone(timedelta(hours=8))


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class RunLock:
    """OS-backed lock: automatically released even when the process crashes."""
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if os.fstat(self.file.fileno()).st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("已有任务正在使用此状态目录，请先关闭另一个任务") from None
        return self

    def __exit__(self, *args):
        self.file.close()


def run_batch(client, state_dir, *, playlist_id=None, song_id=None, max_songs=20,
              interval=2.0, endpoint="auto", verify_wait=15, target=250, cancel=None):
    if (not 1 <= max_songs <= 250 or not 1 <= target <= 250
            or not math.isfinite(interval) or interval < 1
            or not math.isfinite(verify_wait) or not 0 <= verify_wait <= 600):
        raise ValueError("歌曲数/目标必须在 1~250，间隔至少 1 秒，核验等待为 0~600 秒")
    state_dir = Path(state_dir)
    with RunLock(state_dir / "run.lock"):
        uid, nickname = client.identity()
        before = client.statistics(uid)
        day = datetime.now(CHINA).date().isoformat()
        checkpoint = state_dir / f"progress-{uid}-{day}.json"
        progress = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {
            "uid": uid, "date": day, "baseline": before["listenSongs"], "songs": {}}
        report = {"uid": uid, "date": day, "before": before, "accepted": 0, "failed": 0,
                  "skipped": 0, "signin": {}, "endpoint": endpoint}
        report_path = state_dir / "last-report.json"
        print(f"用户：{nickname}（{uid}），累计听歌量：{before['listenSongs']}", flush=True)
        report["signin"] = client.signin()
        for label, status in report["signin"].items():
            print(f"{label}：{status}", flush=True)
        budget = min(max_songs, max(0, 250 - len(progress["songs"])))
        if budget and before["listenSongs"] - progress["baseline"] < target:
            songs = client.songs(uid, playlist_id, song_id, min(250, budget + len(progress["songs"])))
            attempted = 0
            for index, (song, source) in enumerate(songs):
                if cancel is not None and cancel.is_set():
                    raise KeyboardInterrupt
                if datetime.now(CHINA).date().isoformat() != day:
                    print("已跨日，本轮停止，请开始新一天的任务。", flush=True)
                    break
                key = str(song["id"])
                if key in progress["songs"]:
                    report["skipped"] += 1
                    continue
                if attempted >= budget:
                    break
                attempted += 1
                # Persist intent first: interrupted/ambiguous writes must not be silently repeated.
                progress["songs"][key] = "pending"
                atomic_json(checkpoint, progress)
                try:
                    used = client.scrobble(song, source, endpoint)
                    progress["songs"][key] = "accepted"
                    report["accepted"] += 1
                    report["used_endpoint"] = used
                    print(f"上报已受理：{song.get('name', key)}（尚未确认计入等级）", flush=True)
                except LoginRequired:
                    atomic_json(report_path, report)
                    raise
                except ApiError as error:
                    progress["songs"][key] = "unconfirmed"
                    report["failed"] += 1
                    report["last_error"] = str(error)
                    print(str(error), flush=True)
                    # Do not continue producing traffic after an error or a lost write response.
                    atomic_json(checkpoint, progress)
                    break
                atomic_json(checkpoint, progress)
                atomic_json(report_path, report)
                if (index + 1) % 10 == 0:
                    current = client.statistics(uid)
                    if current["listenSongs"] - progress["baseline"] >= target:
                        break
                if index + 1 < len(songs):
                    if cancel is not None:
                        if cancel.wait(interval):
                            raise KeyboardInterrupt
                    else:
                        time.sleep(interval)
        deadline = time.monotonic() + verify_wait
        after = client.statistics(uid)
        while (report["accepted"] and after["listenSongs"] <= before["listenSongs"]
               and time.monotonic() < deadline):
            pause = min(5, max(0, deadline - time.monotonic()))
            if cancel is not None:
                if cancel.wait(pause):
                    raise KeyboardInterrupt
            else:
                time.sleep(pause)
            after = client.statistics(uid)
        report.update(after=after, observed_delta=after["listenSongs"] - before["listenSongs"],
                      daily_observed_delta=after["listenSongs"] - progress["baseline"])
        report["result"] = ("verified_growth" if report["observed_delta"] > 0 else "no_observed_growth")
        atomic_json(report_path, report)
        print(f"上报受理 {report['accepted']} 首；未确认 {report['failed']} 首；跳过 {report['skipped']} 首。", flush=True)
        print(f"服务端累计听歌量：{before['listenSongs']} → {after['listenSongs']}；"
              f"本轮观测增量：{report['observed_delta']}。", flush=True)
        if report["observed_delta"] <= 0 and report["accepted"]:
            print("未观测到听歌量增长。本轮已停止，不会自动反复重刷；可稍后执行 status 核对。", flush=True)
        return report


def next_run(now, hour=9):
    scheduled = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return scheduled if scheduled > now else scheduled + timedelta(days=1)


def run_repeat(client, state_dir, *, song_id, repetitions=1, playlist_id=None,
               interval=2.0, endpoint="auto", verify_wait=15, cancel=None):
    """Backward-compatible one-song, one-round entry point."""
    from .repeat import run_multi_repeat
    return run_multi_repeat(client, state_dir, song_ids=[song_id], repetitions=repetitions,
                            rounds=1, concurrency=1, playlist_id=playlist_id, interval=interval,
                            endpoint=endpoint, verify_wait=verify_wait, cancel=cancel)
