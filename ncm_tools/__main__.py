from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .api import ApiClient, ApiError
from .auth import (edge_driver, find_edge, load_cookies, login_edge, login_qr,
                   parse_cookies, save_cookies)
from .runner import CHINA, next_run, run_batch

ROOT = Path(__file__).resolve().parent.parent
USER_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT


@contextmanager
def local_api(url, state_dir, enabled=True):
    parts = urlsplit(url)
    process = None
    log = None
    # Only manage our bundled server, at its fixed loopback address.
    if enabled and parts.hostname == "127.0.0.1" and (parts.port or 80) == 3000 and not parts.path.strip("/"):
        try:
            with socket.create_connection(("127.0.0.1", 3000), timeout=0.5):
                pass
        except OSError:
            launcher = ROOT / "api-server/server.cjs"
            installed = ROOT / "api-server/node_modules/@neteasecloudmusicapienhanced/api"
            bundled_node = ROOT / "node.exe"
            node = str(bundled_node) if bundled_node.exists() else shutil.which("node")
            if node and installed.exists():
                state_dir.mkdir(parents=True, exist_ok=True)
                log = (state_dir / "api.log").open("ab")
                environment = dict(os.environ, PORT="3000", HOST="127.0.0.1")
                # API dependencies use a writable cache directory for anonymous/session state.
                environment["NCM_API_TEMP_PATH"] = str(state_dir / "api-cache")
                process = subprocess.Popen([node, str(launcher)], cwd=ROOT / "api-server",
                    env=environment, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                try:
                    deadline = time.monotonic() + 30
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError("本地 API 启动失败，请查看 .state/api.log")
                        try:
                            with socket.create_connection(("127.0.0.1", 3000), timeout=0.5):
                                break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise RuntimeError("本地 API 启动超时")
                            time.sleep(0.3)
                except BaseException:
                    process.terminate()
                    process.wait(timeout=10)
                    log.close()
                    raise
    try:
        yield
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if log:
            log.close()


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("请输入正整数")
    return number


def parser():
    p = argparse.ArgumentParser(description="网易云升级助手 3.0 · Edge 兼容版")
    p.add_argument("--api", default="http://127.0.0.1:3000")
    p.add_argument("--state-dir", type=Path, default=USER_ROOT / ".state")
    p.add_argument("--no-start-api", action="store_true", help="不自动启动随附本地 API")
    sub = p.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="检查环境和接口")
    doctor.add_argument("--edge-smoke", action="store_true", help="实际启动无头 Edge 测试")
    doctor.add_argument("--edge-binary")
    doctor.add_argument("--driver")
    login = sub.add_parser("login", help="在 Edge 扫码或网页登录")
    login.add_argument("--method", choices=("qr", "edge"), default="qr")
    login.add_argument("--edge-binary")
    login.add_argument("--driver")
    login.add_argument("--timeout", type=positive, default=180)
    imp = sub.add_parser("import-cookie", help="从本地 Cookie 文件导入并验证登录")
    imp.add_argument("file", type=Path)
    sub.add_parser("status", help="读取服务端统计及等级进度")
    sub.add_parser("signin", help="安卓和网页分别签到")
    for name in ("run", "watch"):
        run = sub.add_parser(name, help="运行一轮" if name == "run" else "每日定时运行（需保持程序开启）")
        run.add_argument("--playlist-id", type=positive)
        run.add_argument("--song-id", type=positive)
        run.add_argument("--max-songs", type=positive, default=20)
        run.add_argument("--target", type=positive, default=250)
        run.add_argument("--interval", type=float, default=2.0)
        run.add_argument("--verify-wait", type=float, default=15)
        run.add_argument("--endpoint", choices=("auto", "v1", "legacy"), default="auto")
        if name == "watch":
            run.add_argument("--hour", type=int, choices=range(24), default=9)
    sub.add_parser("menu", help="交互菜单")
    return p


def doctor(client, args):
    print("Python：" + sys.version.split()[0])
    print("Edge：" + (find_edge(args.edge_binary) or "未找到"))
    print("Node.js：" + (shutil.which("node") or "未找到"))
    print("API：" + client.base_url)
    healthy = True
    try:
        client.request("/login/status", read_only=True)
        print("登录状态接口：已连通（是否已登录请执行 status）")
    except ApiError as error:
        print(str(error))
        healthy = False
    if args.edge_smoke:
        driver = edge_driver(args.state_dir, args.edge_binary, args.driver, headless=True)
        try:
            driver.get("data:text/html,<title>NCM Edge smoke</title><h1>OK</h1>")
            if driver.title != "NCM Edge smoke":
                raise RuntimeError("Edge 页面加载测试未通过")
            print("Edge 实际启动测试：通过；浏览器版本 " + driver.capabilities.get("browserVersion", "未知"))
        finally:
            driver.quit()
    return 0 if healthy else 1


def menu(args):
    while True:
        print("\n1. Edge 扫码登录  2. 查看等级/听歌量  3. 签到  4. 运行一轮  5. 每日定时  0. 退出")
        choice = input("选择：").strip()
        commands = {"1": ["login"], "2": ["status"], "3": ["signin"], "4": ["run"], "5": ["watch"]}
        if choice == "0":
            return 0
        if choice not in commands:
            continue
        extra = []
        if choice in {"4", "5"}:
            playlist = input("歌单 ID（留空使用自己的第一个歌单）：").strip()
            count = input("本轮最多歌曲数（默认 20，最多 250）：").strip() or "20"
            extra = (["--playlist-id", playlist] if playlist else []) + ["--max-songs", count]
        try:
            main(["--api", args.api, "--state-dir", str(args.state_dir),
                  "--no-start-api", *commands[choice], *extra])
        except SystemExit:
            print("输入无效，请重新选择。")


def main(argv=None):
    args = parser().parse_args(argv)
    args.state_dir = args.state_dir.resolve()
    auth_path = args.state_dir / "auth.dat"
    try:
        client = ApiClient(args.api)
        # Login and diagnosis must remain available even when a saved credential is damaged.
        if args.command not in {"login", "import-cookie", "doctor", "menu"}:
            client.cookies = load_cookies(auth_path)
        with local_api(args.api, args.state_dir, not args.no_start_api):
            if args.command == "menu":
                return menu(args)
            if args.command == "doctor":
                return doctor(client, args)
            if args.command == "login":
                if args.method == "qr":
                    uid, nick = login_qr(client, auth_path, binary=args.edge_binary, timeout=args.timeout)
                else:
                    uid, nick = login_edge(client, auth_path, args.state_dir,
                        binary=args.edge_binary, driver_path=args.driver, timeout=args.timeout)
                print(f"登录已验证：{nick}（{uid}）")
                return 0
            if args.command == "import-cookie":
                client.cookies = parse_cookies(args.file.read_text(encoding="utf-8-sig"))
                uid, nick = client.identity()
                save_cookies(auth_path, client.cookies)
                print(f"导入并验证成功：{nick}（{uid}）")
                return 0
            if args.command == "status":
                uid, nick = client.identity()
                print(json.dumps({"uid": uid, "nickname": nick, **client.statistics(uid)}, ensure_ascii=False, indent=2))
                return 0
            if args.command == "signin":
                client.identity()
                print(json.dumps(client.signin(), ensure_ascii=False, indent=2))
                return 0
            if args.command in {"run", "watch"}:
                options = {k: getattr(args, k) for k in ("playlist_id", "song_id", "max_songs",
                           "target", "interval", "verify_wait", "endpoint")}
                if args.command == "run":
                    report = run_batch(client, args.state_dir, **options)
                    return 2 if report["failed"] or (report["accepted"] and report["observed_delta"] <= 0) else 0
                # Validate before waiting for the scheduled time.
                client.identity()
                if not 1 <= args.max_songs <= 250 or not 1 <= args.target <= 250 or args.interval < 1 or args.verify_wait < 0:
                    raise ValueError("歌曲数/目标必须在 1~250，间隔至少 1 秒，核验等待时间不能为负")
                while True:
                    scheduled = next_run(datetime.now(CHINA), args.hour)
                    print(f"下次运行：{scheduled.isoformat()}（UTC+8）；Ctrl+C 退出。", flush=True)
                    while datetime.now(CHINA) < scheduled:
                        time.sleep(min(1, max(0, (scheduled - datetime.now(CHINA)).total_seconds())))
                    run_batch(client, args.state_dir, **options)
    except KeyboardInterrupt:
        print("\n已停止；进度已保存在本地，未确认的上报不会自动重复。")
        return 130
    except (ApiError, ValueError, RuntimeError, OSError, KeyError) as error:
        if isinstance(error, KeyError):
            print("错误：接口缺少必要字段，请检查 API 版本。", file=sys.stderr)
        elif isinstance(error, OSError):
            print("错误：文件或网络操作失败，请检查路径和网络。", file=sys.stderr)
        else:
            print("错误：" + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
