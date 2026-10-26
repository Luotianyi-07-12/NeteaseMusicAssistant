from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from .api import ApiError


def parse_cookies(text):
    text = text.strip().removeprefix("Cookie:").strip()
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, list):
        result = {c["name"]: c["value"] for c in value
                  if c.get("name") and c.get("value") and
                  ("domain" not in c or c["domain"].lstrip(".") in {"music.163.com", "163.com"})}
    elif isinstance(value, dict):
        result = {str(k): str(v) for k, v in value.items()}
    else:
        # QR APIs join multiple Set-Cookie headers using semicolons. Empty segments,
        # attributes, and a leading NMTID header can make SimpleCookie drop MUSIC_U.
        result = {}
        attributes = {'path', 'domain', 'expires', 'max-age', 'samesite', 'secure',
                      'httponly', 'partitioned', 'priority', 'version', 'comment'}
        for part in text.split(';'):
            if '=' not in part:
                continue
            name, cookie_value = part.strip().split('=', 1)
            if name.lower() in attributes or not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name):
                continue
            result[name] = cookie_value.strip().strip('"')
    if not result.get("MUSIC_U"):
        raise ValueError("Cookie 中没有 MUSIC_U，请使用登录后的网易云 Cookie")
    for k, v in result.items():
        if any(c in k + v for c in "\r\n;"):
            raise ValueError("Cookie 格式不正确")
    return result


def _dpapi(data, decrypt=False):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    if decrypt:
        fn = crypt32.CryptUnprotectData
        args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        fn = crypt32.CryptProtectData
        args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not fn(*args):
        raise RuntimeError("Windows 无法保护/读取登录信息，请在当前 Windows 用户下重新登录")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def save_cookies(path, cookies):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(cookies).encode()
    if os.name == "nt":
        try:
            raw = b"DPAPI1\n" + base64.b64encode(_dpapi(raw))
        except RuntimeError:
            print("当前环境无法加密保存登录信息；本次会话仍可使用，关闭程序后需重新登录。", flush=True)
            return False
    else:
        raw = b"JSON1\n" + raw
    temp = path.with_suffix(".tmp")
    try:
        temp.write_bytes(raw)
        temp.chmod(0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return True


def load_cookies(path):
    path = Path(path)
    if not path.exists():
        return {}
    data = path.read_bytes()
    if data.startswith(b"DPAPI1\n"):
        if os.name != "nt":
            raise ValueError("此登录信息由 Windows 保护，请重新登录")
        data = _dpapi(base64.b64decode(data.split(b"\n", 1)[1]), decrypt=True)
    elif data.startswith(b"JSON1\n"):
        data = data.split(b"\n", 1)[1]
    else:
        raise ValueError("登录文件损坏，请重新登录")
    return parse_cookies(data.decode())


def find_edge(explicit=None):
    if explicit:
        if not Path(explicit).is_file():
            raise ValueError("指定的 Edge 程序不存在")
        return str(Path(explicit).resolve())
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        if os.environ.get(env):
            path = Path(os.environ[env]) / "Microsoft/Edge/Application/msedge.exe"
            if path.is_file():
                return str(path)
    return shutil.which("msedge") or shutil.which("microsoft-edge")


def edge_driver(state_dir, binary=None, driver_path=None, headless=False):
    try:
        from selenium import webdriver
        from selenium.webdriver.edge.service import Service
    except ImportError:
        raise RuntimeError("备用网页登录需要 Selenium 4，请先运行安装脚本") from None
    state_dir = Path(state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    # Keep this project's cache and browser profile away from the user's regular profile.
    os.environ["SE_CACHE_PATH"] = str(state_dir / "selenium")
    options = webdriver.EdgeOptions()
    if binary or find_edge():
        options.binary_location = find_edge(binary)
    options.add_argument("--user-data-dir=" + str(state_dir / "edge-profile"))
    options.add_argument("--no-first-run")
    if headless:
        options.add_argument("--headless=new")
    try:
        return webdriver.Edge(options=options, service=Service(driver_path) if driver_path else Service())
    except Exception:
        raise RuntimeError("Edge 启动失败：检查网络，或用 --driver 指定与 Edge 前三段版本匹配的 msedgedriver.exe") from None


def login_edge(client, auth_path, state_dir, *, binary=None, driver_path=None, timeout=180, cancel=None):
    driver = edge_driver(state_dir, binary, driver_path)
    try:
        driver.set_page_load_timeout(40)
        driver.get("https://music.163.com/")
        print("请在新打开的 Edge 窗口中登录网易云音乐，程序会自动等待。", flush=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise KeyboardInterrupt
            if not driver.window_handles:
                raise RuntimeError("登录窗口已关闭")
            # Only collect cookies while still on the expected service's origin.
            from urllib.parse import urlsplit
            if urlsplit(driver.current_url).hostname == "music.163.com":
                cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
                if cookies.get("MUSIC_U"):
                    client.cookies = cookies
                    identity = client.identity()
                    save_cookies(auth_path, cookies)
                    return identity
            time.sleep(2)
        raise TimeoutError("等待登录超时，未保存登录信息")
    finally:
        driver.quit()


def login_qr(client, auth_path, *, binary=None, timeout=180, open_browser=True, cancel=None):
    key = client.request("/login/qr/key", ua="pc")["data"]["unikey"]
    qr = client.request("/login/qr/create", key=key, platform="web", ua="pc", qrimg="true")["data"]
    from urllib.parse import parse_qs, urlsplit
    qr_url = urlsplit(qr.get("qrurl", ""))
    if (qr_url.scheme != "https" or qr_url.hostname != "music.163.com"
            or parse_qs(qr_url.query).get("codekey") != [key]):
        raise ApiError("/login/qr/create", None, "二维码与本次登录密钥不匹配，请检查 API 缓存")
    image = qr.get("qrimg", "")
    if not image.startswith("data:image/png;base64,"):
        raise ApiError("/login/qr/create", None, "API 未返回二维码图片")
    token = secrets.token_urlsafe(24)
    state = {"message": "请用网易云音乐 App 扫码并确认登录"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/" + token:
                self.send_error(404)
                return
            html = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                    '<meta http-equiv="refresh" content="3"><title>网易云音乐登录</title>'
                    '<body style="font:18px system-ui;text-align:center;margin:60px">'
                    '<h1>网易云音乐登录</h1><img width="240" height="240" src="' + image + '">'
                    '<p>' + state["message"] + '</p><p>完成后查看程序窗口</p></body></html>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; img-src data:; style-src 'unsafe-inline'")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/{token}"
        if open_browser:
            edge = find_edge(binary)
            if not edge:
                raise RuntimeError("未找到 Edge，请用 --edge-binary 指定 msedge.exe")
            subprocess.Popen([edge, "--new-window", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("扫码登录页：" + url, flush=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise KeyboardInterrupt
            data = client.request("/login/qr/check", key=key, ua="pc", allowed=(800, 801, 802, 803))
            code = data["code"]
            if code == 800:
                raise RuntimeError("二维码已过期，请重新执行 login")
            if code == 802:
                state["message"] = "已扫码，请在手机上确认登录"
            if code == 803:
                cookies = parse_cookies(data.get("cookie", ""))
                client.cookies = cookies
                identity = client.identity()
                save_cookies(auth_path, cookies)
                return identity
            if cancel is not None:
                cancel.wait(2)
            else:
                time.sleep(2)
        raise TimeoutError("等待扫码超时，请重新执行 login")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
