"""A fake FetchTV service for the tests: sign-in + guide over HTTP, boxes over a websocket.

The message shapes copy what the real service sends (captured from a real
account), with made-up ids and names.
"""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import websockets

ACTIVATION_CODE = "TESTCODE"
PIN = "1234"

LOUNGE = "111111111111111|00:11:22:33:44:55"
BEDROOM = "222222222222222|00:11:22:33:44:66"

# Guide channel list: one free-to-air channel (regional DVB variants) and two
# subscription channels, one of them not subscribed.
EPG_CHANNELS = {
    "411316": {
        "name": "ABC Entertains", "number": 23, "epg_id": 411316, "subscribed": True,
        "sources": [{"id": "10411316", "type": "dvb"}, {"id": "30411316", "type": "dvb"}],
        "images": {"channel_logo_focus": {"original": "http://img/abc-ent.png"}},
    },
    "18294": {
        "name": "ABC TV", "number": 2, "epg_id": 18294, "subscribed": True,
        "sources": [{"id": "30018294", "type": "dvb"}],
        "images": {"channel_logo_focus": {"original": "http://img/abc.png"}},
    },
    "1850": {
        "name": "beIN SPORTS 3", "number": 159, "epg_id": 1850, "subscribed": True,
        "sources": [{"id": 1850, "type": "multicast"}, {"id": 1850, "type": "hls"}],
        "images": {"channel_logo_focus": {"original": "http://img/bein3.png"}},
    },
    "2319": {
        "name": "Discovery Turbo", "number": 118, "epg_id": 2319, "subscribed": False,
        "sources": [{"id": 2319, "type": "hls"}],
    },
}

FTA = [
    {"id": "30018294", "epg_id": 18294, "name": "ABC TV", "number": 2},
    {"id": "30411316", "epg_id": 411316, "name": "ABC Entertains", "number": 23},
    {"id": "30999999", "epg_id": 999999, "name": "Hidden Channel", "number": 99},
]

# Recordings as the box reports them in I_AM_ALIVE (times in epoch ms).
RECORDINGS = [
    {"id": 11, "diskId": 11, "name": "Hard Quiz", "episodeTitle": "Series Finale", "seriesNumber": 6,
     "episodeNumber": 12, "channelId": "30411316", "programStartDate": 1_799_000_000_000},
    {"id": 12, "diskId": 12, "name": "The Drum", "channelId": "30018294",
     "programStartDate": 1_799_500_000_000},
    {"id": 13, "diskId": 13, "name": "Deleted One", "pendingDelete": True,
     "programStartDate": 1_799_600_000_000},
]

PROGRAM_FIELDS = ["id", "title", "start", "end", "synopsis_id", "rating", "warnings", "flags",
                  "genre", "series_link", "episode_title"]


def now_ms() -> int:
    return int(time.time() * 1000)


def programs_for(epg_id: str) -> list[list]:
    """A programme on air now, plus one before and one after."""
    t = now_ms()
    titles = {"411316": "Hard Quiz", "18294": "ABC News", "1850": "EFL Cup", "2319": "Texas Metal"}
    title = titles.get(epg_id, "Something")
    hour = 3600 * 1000
    return [
        [1, f"Before {title}", t - 2 * hour, t - hour, 0, 0, "", 0, "News", "", ""],
        [2, title, t - hour, t + hour, 7, 0, "", 0, "Sport", "", "Episode 19"],
        [3, f"After {title}", t + hour, t + 2 * hour, 0, 0, "", 0, "News", "", ""],
    ]


class FakeFetch:
    """Runs the fake HTTP API (in a thread) and websocket (on the test's loop)."""

    def __init__(self):
        self.http_requests: list[tuple[str, str]] = []
        self.http_headers: list[dict] = []
        self.ws_headers: list[dict] = []
        self.ws_paths: list[str] = []
        self.ws_received: list[dict] = []
        self.logins = 0
        self.reject_login = False
        self.boxes = {
            LOUNGE: {"label": "Lounge", "standby": False,
                     "media": {"playbackType": "DVB", "channelId": "30411316",
                               "mediaTitle": "ABC Entertains", "playBackState": "LIVE"}},
            BEDROOM: {"label": "Bedroom", "standby": True,
                      "media": {"playbackType": "HLS", "channelId": 2319,
                                "mediaTitle": "Discovery Turbo", "playBackState": "IDLE"}},
        }
        self._clients: set = set()
        self._http = None
        self._ws_server = None
        self.http_url = ""
        self.ws_url = ""

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, body, cookies=()):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for cookie in cookies:
                    self.send_header("Set-Cookie", cookie)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                fake.http_requests.append(("POST", self.path))
                fake.http_headers.append(dict(self.headers))
                if self.path != "/v3/authenticate":
                    return self._json(404, {})
                length = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(length).decode())
                ok = (not fake.reject_login
                      and form.get("activation_code") == [ACTIVATION_CODE]
                      and form.get("pin") == [PIN])
                if not ok:
                    return self._json(200, {"__meta__": {"error": "ERR_AUTH", "message": "bad pin"}})
                fake.logins += 1
                terminals = [
                    {"id": LOUNGE, "friendly_name": "Lounge", "status": "ENABLED", "type": "Mighty"},
                    {"id": BEDROOM, "friendly_name": "Bedroom", "status": "ENABLED", "type": "Mini"},
                    {"id": "333|gone", "friendly_name": "Old", "status": "DISABLED", "type": "Mini"},
                ]
                self._json(200, {"__meta__": {"error": None}, "terminals": terminals},
                           cookies=["auth=secret-token; Path=/", "user=u1; Path=/"])

            def do_GET(self):
                url = urlparse(self.path)
                fake.http_requests.append(("GET", url.path))
                fake.http_headers.append(dict(self.headers))
                if "auth=secret-token" not in (self.headers.get("Cookie") or ""):
                    return self._json(401, {})
                if url.path == "/v2/epg/channels":
                    return self._json(200, {"__meta__": {"error": None}, "channels": EPG_CHANNELS})
                if url.path == "/v2/epg/programslist":
                    ids = parse_qs(url.query).get("channel_ids", [""])[0].split(",")
                    return self._json(200, {
                        "__meta__": {"error": None, "program_fields": PROGRAM_FIELDS},
                        "synopses": {"7": "A synopsis."},
                        "channels": {i: programs_for(i) for i in ids if i},
                    })
                self._json(404, {})

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._http.serve_forever, daemon=True).start()
        self.http_url = f"http://127.0.0.1:{self._http.server_address[1]}"

        self._ws_server = await websockets.serve(self._ws_handler, "127.0.0.1", 0)
        port = next(iter(self._ws_server.sockets)).getsockname()[1]
        self.ws_url = f"ws://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        if self._http is not None:
            self._http.shutdown()

    async def drop_connections(self) -> None:
        """Close every client websocket (simulates the service dropping us)."""
        for ws in list(self._clients):
            await ws.close()

    def client(self, code: str = ACTIVATION_CODE, pin: str = PIN, **kwargs):
        """A FetchTvClient pointed at this fake (usable as box_manager.client_factory)."""
        import fetchtv
        kwargs.setdefault("reconnect_delay", 0.2)
        return fetchtv.FetchTvClient(code, pin, api_base=self.http_url, ws_url=self.ws_url, **kwargs)

    def received(self, msg_type: str, to: str | None = None) -> list[dict]:
        return [m for m in self.ws_received
                if m["message"]["type"] == msg_type and (to is None or m["to"] == to)]

    async def push(self, terminal_id: str, msg_type: str, data: dict | None = None) -> None:
        """Send a message from a box to every connected client."""
        for ws in list(self._clients):
            await ws.send(json.dumps({"sender": terminal_id,
                                      "message": {"type": msg_type, "data": data or {}}}))

    # --- websocket ------------------------------------------------------------

    def _settings(self, tid: str) -> dict:
        box = self.boxes[tid]
        return {"standby": box["standby"], "ipAddress": "10.0.0.2", "hiddenChannels": [30999999]}

    async def _ws_handler(self, ws) -> None:
        self.ws_headers.append(dict(ws.request.headers.items()))
        self.ws_paths.append(ws.request.path)
        if "auth=secret-token" not in ws.request.path:
            await ws.close(code=4001)
            return
        self._clients.add(ws)
        try:
            await ws.send(json.dumps({"message": {"frag": "SUBSCRIPTIONS_INITIALISED"}}))
            async for raw in ws:
                msg = json.loads(raw)
                self.ws_received.append(msg)
                await self._answer(ws, msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)

    async def _answer(self, ws, msg: dict) -> None:
        tid = msg["to"]
        if tid not in self.boxes:
            return
        box = self.boxes[tid]
        typ = msg["message"]["type"]

        async def reply(rtype: str, data: dict) -> None:
            await ws.send(json.dumps({"sender": tid, "message": {"type": rtype, "data": data}}))

        if typ == "ARE_YOU_ALIVE":
            await reply("I_AM_ALIVE", {**self._settings(tid), "state": box["media"], "ftaChannels": FTA,
                                       "recordings": RECORDINGS,
                                       "sysInfo": {"label": box["label"], "hardwareName": "Mighty"}})
        elif typ == "MEDIA_STATE":
            await reply("MEDIA_STATE", {"settings": self._settings(tid),
                                        "currentPlaybackMedia": box["media"]})
        elif typ == "PING":
            await reply("PONG", {})
        elif typ == "PLAY_CHANNEL":
            cid = msg["message"]["channelId"]
            raw = EPG_CHANNELS.get(str(cid))
            name = raw["name"] if raw else next((c["name"] for c in FTA if c["id"] == str(cid)), "?")
            hls = raw is not None
            box["media"] = {"playbackType": "HLS" if hls else "DVB",
                            "channelId": cid if hls else str(cid),
                            "mediaTitle": name, "playBackState": "LIVE"}
            await reply("NOW_PLAYING", {"currentPlaybackMedia": box["media"]})
            await reply("CHANNEL_CHANGED", {})
            await reply("PLAYBACK_STOPPED", {})
        elif typ == "KEYEVENT" and msg["message"].get("keyName") == "KEY_POWER":
            box["standby"] = not box["standby"]
