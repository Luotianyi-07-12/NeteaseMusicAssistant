from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ApiError(RuntimeError):
    def __init__(self, path: str, code: int | None, message: str):
        self.path, self.code = path, code
        super().__init__(f"{path}: {message}" + (f"（代码 {code}）" if code is not None else ""))


class LoginRequired(ApiError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never forward a POST body containing account cookies.
        return None


class ApiClient:
    def __init__(self, base_url="http://127.0.0.1:3000", cookies=None, timeout=15):
        parts = urlsplit(base_url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment):
            raise ValueError("API 地址必须为不含账号、查询参数的 http(s) 地址")
        if parts.scheme == "http" and parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("远程 API 必须使用 HTTPS；本机 API 可使用 HTTP")
        self.base_url = base_url.rstrip("/")
        self.cookies = dict(cookies or {})
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def request(self, path, *, allowed=(200,), read_only=False, **params):
        # Retry reads only. A lost response does not imply a mutation failed.
        for attempt in range(3 if read_only else 1):
            stamp = time.time_ns()
            fields = dict(params, timestamp=stamp // 1_000_000)
            if self.cookies:
                fields["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            # Upstream apicache keys POSTs by URL and header cookies, ignoring POST bodies.
            # Both a unique URL and its explicit bypass header are needed for fresh auth,
            # statistics, and distinct writes while keeping credentials in the POST body.
            query = urlencode({"timestamp": stamp // 1_000_000, "_ncm_request": stamp})
            req = Request(self.base_url + path + "?" + query, data=urlencode(fields).encode(), headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "NcmEdgeAssistant/3.0",
                "X-APICACHE-BYPASS": "1",
                "Cache-Control": "no-cache, no-store",
            })
            status = 200
            try:
                try:
                    response = self.opener.open(req, timeout=self.timeout)
                except HTTPError as error:
                    response = error
                    status = error.code
                with response:
                    raw = response.read(4_000_001)
                if len(raw) > 4_000_000:
                    raise ApiError(path, status, "接口响应过大")
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ApiError(path, status, "返回的不是 JSON；请检查 API 地址及版本") from None
                if not isinstance(result, dict):
                    raise ApiError(path, status, "接口返回格式不正确")
                nested = result.get("data")
                code = result.get("code", nested.get("code") if isinstance(nested, dict) else None)
                if code in {301, 302, 401} or status == 401:
                    raise LoginRequired(path, code or status, "登录已失效，请重新登录")
                if status == 429 or status >= 500:
                    if read_only and attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                if status >= 400 and code not in allowed:
                    raise ApiError(path, status, "HTTP 请求失败；不会自动重复有副作用的请求")
                if code not in allowed:
                    # Do not echo upstream bodies: they can contain credentials.
                    raise ApiError(path, code, "接口未确认成功，请检查 API 兼容性或账号状态")
                return result
            except (URLError, TimeoutError, OSError):
                if read_only and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(path, None, "连接失败或超时，请检查本地 API 服务") from None
        raise ApiError(path, None, "读取失败")

    def identity(self):
        result = self.request("/login/status", read_only=True)
        data = result.get("data", result)
        if not isinstance(data, dict):
            raise LoginRequired("/login/status", 301, "没有有效登录信息")
        profile, account = data.get("profile") or {}, data.get("account") or {}
        uid = profile.get("userId") or account.get("id")
        if not uid:
            raise LoginRequired("/login/status", 301, "尚未登录，请先执行 login")
        return int(uid), profile.get("nickname", "")

    def statistics(self, uid):
        detail = self.request("/user/detail", uid=uid, read_only=True)
        count = detail.get("listenSongs")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ApiError("/user/detail", None, "缺少有效的 listenSongs，不能核验听歌量")
        result = {"listenSongs": count, "level": detail.get("level")}
        try:
            data = self.request("/user/level", read_only=True).get("data", {})
            if isinstance(data, dict):
                result.update({k: data[k] for k in ("level", "nowPlayCount", "nowLoginCount",
                              "nextPlayCount", "nextLoginCount") if k in data})
        except LoginRequired:
            raise
        except ApiError:
            # Old compatible API servers may lack /user/level.
            pass
        return result

    def signin(self):
        result = {}
        for kind, label in ((0, "安卓签到"), (1, "网页签到")):
            data = self.request("/daily_signin", type=kind, allowed=(200, -2))
            result[label] = "已签到" if data.get("code") == -2 else "签到成功"
        return result

    def songs(self, uid, playlist_id=None, song_id=None, limit=20):
        if song_id:
            ids, source = [int(song_id)], int(playlist_id or 0)
        else:
            if not playlist_id:
                lists = self.request("/user/playlist", uid=uid, limit=100, read_only=True).get("playlist", [])
                # The first playlist created by the current user is normally their liked list.
                owned = [p for p in lists if str((p.get("creator") or {}).get("userId")) == str(uid)]
                if not owned:
                    raise ApiError("/user/playlist", None, "没有自己的歌单，请指定 --playlist-id")
                playlist_id = owned[0]["id"]
            source = int(playlist_id)
            detail = self.request("/playlist/detail", id=source, read_only=True)
            playlist = detail.get("playlist") or {}
            tracks = playlist.get("trackIds") or playlist.get("tracks") or []
            ids = list(dict.fromkeys(int(t["id"]) for t in tracks if t.get("id")))[:limit]
        if not ids:
            raise ApiError("/playlist/detail", None, "歌单为空")
        songs = self.request("/song/detail", ids=",".join(map(str, ids)), read_only=True).get("songs", [])
        by_id = {int(song["id"]): song for song in songs}
        return [(by_id[id_], source) for id_ in ids if id_ in by_id]

    def scrobble(self, song, source, endpoint="auto"):
        duration = int(song.get("dt", 0)) // 1000
        if duration <= 0:
            raise ApiError("/song/detail", None, "歌曲时长无效")
        params = {"id": song["id"], "sourceid": source, "time": duration, "total": duration,
                  "name": song.get("name", ""),
                  "artist": "/".join(a.get("name", "") for a in song.get("ar", []))}
        path = "/scrobble" if endpoint == "legacy" else "/scrobble/v1"
        try:
            result = self.request(path, **params)
        except ApiError as error:
            # Never switch methods after a timeout/auth/risk-control response: it may duplicate a write.
            if endpoint == "auto" and error.code in {404, 405}:
                path = "/scrobble"
                result = self.request(path, **params)
            else:
                raise
        details = result.get("details")
        if path == "/scrobble/v1" and result.get("data") not in {"success", "scrobble_v1 上报成功"}:
            # Enhanced 4.41.1 can forward an upstream code=200 even when PLV/PLD fails.
            # Its positive acknowledgement always carries this data field.
            raise ApiError(path, result.get("code"), "接口未给出上报完成确认，未计为成功")
        if isinstance(details, dict):
            for value in details.values():
                if isinstance(value, dict) and value.get("code", 200) != 200:
                    raise ApiError(path, value.get("code"), "子请求失败，未计为上报成功")
        return path

    def song_play_count(self, uid, song_id):
        """Read this user's weekly song play count, distinct from unique-song totals."""
        result = self.request("/user/record", uid=uid, type=1, read_only=True)
        records = result.get("weekData")
        if not isinstance(records, list):
            raise ApiError("/user/record", None, "当前账号未提供可读取的单曲周播放次数")
        for record in records:
            if str((record.get("song") or {}).get("id")) == str(song_id):
                count = record.get("playCount")
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    return count
                raise ApiError("/user/record", None, "单曲播放次数格式不正确")
        # A song absent from the returned ranking can be outside the ranking window;
        # absence is not evidence of zero plays.
        return None
