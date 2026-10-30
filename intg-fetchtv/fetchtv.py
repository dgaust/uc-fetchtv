"""
Async client for the FetchTV cloud service.

FetchTV boxes are not controlled over the LAN: the official mobile app talks to
Fetch's own service, and so does this client.

- **Sign-in** is an HTTPS form post of the activation code and PIN (the ones
  used by the Fetch mobile app) to ``/v3/authenticate``. The response lists the
  account's boxes ("terminals") and its subscriptions, and sets an ``auth``
  cookie.
- **Box state and commands** go over one websocket at
  ``messages.fetchtv.com.au``, authenticated by that cookie. Each box answers
  ``ARE_YOU_ALIVE`` with its full state and channel list, and pushes
  ``NOW_PLAYING`` / ``MEDIA_STATE`` / ``PAUSED`` / ... as things change.
  Remote keys (``KEYEVENT``) and channel changes (``PLAY_CHANNEL``) are sent
  over the same socket.
- **Channel names, logos and the programme guide** come from ``/v2/epg``.
  Subscription (HLS) channels use their guide id as their channel id, while
  free-to-air (DVB) channel ids are regional variants of a guide id, so both
  are resolved through the guide's channel list.

The protocol isn't documented by Fetch; this follows what the mobile app does,
as worked out by the pyfetchtv project (https://github.com/jinxo13/pyfetchtv).

:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import websockets

_LOG = logging.getLogger(__name__)

API_BASE = "https://apis.fetchtv.com.au"
WS_URL = "wss://messages.fetchtv.com.au/v2/message/ws/messages"

# Headers the Fetch Android app sends; the service rejects requests without
# the capabilities header.
_HEADERS = {
    "Accept": "application/json",
    "X-FTV-Capabilities": "no_pin,android,v3.21.1.4988,tenplay_v2",
    "X-FTV-Timeout": "3",
    "X-FTV-DeviceID": "uc-intg-fetchtv",
}

# Messages that mean the list of recordings has changed.
_RECORDING_EVENTS = frozenset({
    "RECORDINGS_UPDATE",
    "RECORDING_UPDATED",
    "RECORD_PROGRAM_SUCCESS",
    "RECORD_PROGRAM_STOP",
    "PENDING_DELETE_RECORDINGS_BY_ID_SUCCESS",
    "RESTORE_RECORDINGS_BY_ID_SUCCESS",
})

HTTP_TIMEOUT_S = 20.0
WS_OPEN_TIMEOUT_S = 15.0
# How often each box is pinged and asked for its state. The box pushes changes
# on its own, but not reliably when it goes into or out of standby.
POLL_INTERVAL_S = 60.0
RECONNECT_DELAY_S = 10.0
RECONNECT_DELAY_MAX_S = 300.0
# Where Fetch's public site serves channel logos.
PUBLIC_URL = "https://www.fetchtv.com.au"
# The programme cache is topped up when a listing would run out within this long.
PREFETCH_MARGIN_MS = 15 * 60 * 1000
# Guide channel list (names, logos) changes rarely.
CHANNEL_LIST_MAX_AGE_S = 24 * 3600

# Remote key names the box accepts in a KEYEVENT message.
KEY_BACK = "KEY_BACK"
KEY_BLUE = "KEY_BLUE"
KEY_DOWN = "KEY_DOWN"
KEY_EXIT = "KEY_EXIT"
KEY_FAST_FORWARD = "KEY_FAST_FORWARD"
KEY_GREEN = "KEY_GREEN"
# Not in pyfetchtv's key list, but the box accepts them (tried on a real box).
KEY_GUIDE = "KEY_GUIDE"
KEY_INFO = "KEY_INFO"
KEY_LEFT = "KEY_LEFT"
KEY_MENU = "KEY_MENU"
KEY_MUTE = "KEY_MUTE"
KEY_PLAY_PAUSE = "KEY_PLAY_PAUSE"
KEY_POWER = "KEY_POWER"
KEY_RECORD = "KEY_RECORD"
KEY_RED = "KEY_RED"
KEY_REWIND = "KEY_REWIND"
KEY_RIGHT = "KEY_RIGHT"
KEY_SELECT = "KEY_SELECT"
KEY_STOP = "KEY_STOP"
KEY_TEXT = "KEY_TEXT"
KEY_UP = "KEY_UP"
KEY_YELLOW = "KEY_YELLOW"

PLAY_STATE_LIVE = "LIVE"
PLAY_STATE_PLAYING = "PLAYING"
PLAY_STATE_PAUSED = "PAUSED"
PLAY_STATE_IDLE = "IDLE"


class FetchTvError(Exception):
    """Base exception for the FetchTV client."""


class AuthError(FetchTvError):
    """The activation code / PIN were rejected."""


class NotConnectedError(FetchTvError):
    """A command was sent while the websocket is down."""


@dataclass
class Channel:
    """A channel a box can play."""

    id: str
    """The id the box reports in its state and accepts in PLAY_CHANNEL."""
    epg_id: str
    """Key of the channel in the programme guide."""
    number: int
    name: str
    logo: str | None = None
    subscription: bool = False

    @property
    def label(self) -> str:
        """How the channel is shown in the Remote's source list."""
        return f"{self.number} {self.name}" if self.number else self.name


@dataclass
class Program:
    """A programme guide entry."""

    title: str
    start_ms: int
    end_ms: int
    episode_title: str = ""
    genre: str = ""
    synopsis: str = ""

    def covers(self, at_ms: int) -> bool:
        """True if the programme is on air at ``at_ms``."""
        return self.start_ms <= at_ms < self.end_ms


@dataclass
class Recording:
    """A programme recorded on a box's disk."""

    id: str
    title: str
    episode_title: str = ""
    season: str = ""
    episode: str = ""
    channel_id: str = ""
    synopsis: str = ""
    start_ms: int = 0
    series_id: str = ""

    @property
    def label(self) -> str:
        """Season / episode numbers and the episode title, as far as known."""
        numbers = " ".join(
            part for part in (f"S{self.season}" if self.season else "", f"E{self.episode}" if self.episode else "") if part
        )
        return " · ".join(part for part in (numbers, self.episode_title) if part)


@dataclass
class Box:
    """A FetchTV set-top box on the account, and its last known state."""

    terminal_id: str
    name: str
    online: bool = False
    standby: bool | None = None
    ip_address: str = ""
    model: str = ""
    playback_type: str = ""
    channel_id: str = ""
    media_title: str = ""
    play_state: str = ""
    fta_channels: list[Channel] = field(default_factory=list)
    hidden_channel_ids: set[str] = field(default_factory=set)
    recordings: list[Recording] = field(default_factory=list)
    # Monotonic time of the last full box report, which carries the recordings;
    # 0 when they may be out of date (a recording started or ended).
    recordings_at: float = 0.0

    @property
    def has_state(self) -> bool:
        """True once the box has reported what it is doing."""
        return self.standby is not None

    def apply_alive(self, data: dict[str, Any]) -> None:
        """Update from an I_AM_ALIVE reply (full box details + state)."""
        self.online = True
        sys_info = data.get("sysInfo") or {}
        self.model = sys_info.get("hardwareName") or self.model
        self._apply_settings(data)
        self.fta_channels = [
            c for c in (_fta_channel(raw) for raw in data.get("ftaChannels") or []) if c
        ]
        self.recordings = [
            r for r in (_recording(raw) for raw in data.get("recordings") or []) if r
        ]
        self.recordings_at = time.monotonic()
        _LOG.debug(
            "%s: %d recordings; report keys %s; first recording %s",
            self.name,
            len(self.recordings),
            sorted(data),
            (data.get("recordings") or [None])[0],
        )
        self.apply_playback(data.get("state"))

    def apply_media_state(self, data: dict[str, Any]) -> None:
        """Update from a MEDIA_STATE / NOW_PLAYING message."""
        self.online = True
        self._apply_settings(data.get("settings") or {})
        self.apply_playback(data.get("currentPlaybackMedia"))

    def apply_playback(self, media: dict[str, Any] | None) -> None:
        """Update the playing media from a ``currentPlaybackMedia`` dict."""
        if not media:
            return
        self.playback_type = str(media.get("playbackType") or "")
        channel_id = media.get("channelId")
        self.channel_id = "" if channel_id is None else str(channel_id)
        self.media_title = str(media.get("mediaTitle") or "")
        self.play_state = str(media.get("playBackState") or "")
        if self.play_state == "UNPAUSED":
            self.play_state = PLAY_STATE_PLAYING

    def _apply_settings(self, data: dict[str, Any]) -> None:
        if "standby" in data:
            self.standby = bool(data["standby"])
        if data.get("ipAddress"):
            self.ip_address = data["ipAddress"]
        if "hiddenChannels" in data:
            self.hidden_channel_ids = {str(c) for c in data["hiddenChannels"] or []}


def _fta_channel(raw: dict[str, Any]) -> Channel | None:
    if raw.get("id") is None or raw.get("epg_id") is None:
        return None
    return Channel(
        id=str(raw["id"]),
        epg_id=str(raw["epg_id"]),
        number=_to_int(raw.get("number")),
        name=str(raw.get("name") or raw["id"]),
    )


def _recording(raw: dict[str, Any]) -> Recording | None:
    """A recording from the box's report; None for deleted or unnamed ones."""
    if raw.get("id") is None or raw.get("pendingDelete") or not raw.get("name"):
        return None
    start = _to_int(raw.get("programStartDate") or raw.get("startDate") or raw.get("creationDate"))
    # Fetch times are epoch milliseconds; tolerate seconds.
    return Recording(
        id=str(raw["id"]),
        title=str(raw["name"]),
        episode_title=str(raw.get("episodeTitle") or ""),
        season=str(raw.get("seriesNumber") or ""),
        episode=str(raw.get("episodeNumber") or ""),
        channel_id=str(raw.get("channelId") or ""),
        synopsis=str(raw.get("description") or ""),
        start_ms=start * 1000 if 0 < start < 10**11 else start,
        series_id=str(raw.get("seriesLinkId") or ""),
    )


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _logo_url(key: str, raw: dict[str, Any]) -> str | None:
    """
    A logo URL that works outside Fetch's network, or None.

    The guide's own image URLs point at ``static.lb.i.fetchtv.com.au``, a name
    that only exists inside Fetch's network, so a Remote can't load them. The
    public site serves subscription (streaming) channels' logos by guide key
    instead. Free-to-air channels have no logo there, so they get none.
    """
    streaming = any(s.get("type") == "hls" for s in raw.get("sources") or [])
    if not streaming or not _logo(raw):
        return None
    return f"{PUBLIC_URL}/v2/epg/channels/{key}/image"


def _logo(raw: dict[str, Any]) -> str | None:
    images = raw.get("images") or {}
    for key in ("channel_logo_focus", "channel_logo"):
        url = (images.get(key) or {}).get("original")
        if url:
            return url
    return None


class Guide:
    """The guide's channel list plus cached programme listings."""

    def __init__(self, channels: dict[str, dict[str, Any]]):
        self.loaded_at = time.monotonic()
        self._raw = channels
        # Every id a box may report (DVB source ids, HLS ids) -> guide key.
        self._by_source_id: dict[str, str] = {}
        for key, raw in channels.items():
            self._by_source_id[str(key)] = str(key)
            for source in raw.get("sources") or []:
                if source.get("id") is not None:
                    self._by_source_id.setdefault(str(source["id"]), str(key))
        self.subscription_channels = sorted(
            (
                Channel(
                    id=str(key),
                    epg_id=str(key),
                    number=_to_int(raw.get("number")),
                    name=str(raw.get("name") or key),
                    logo=_logo_url(str(key), raw),
                    subscription=True,
                )
                for key, raw in channels.items()
                if raw.get("subscribed")
                and any(s.get("type") == "hls" for s in raw.get("sources") or [])
            ),
            key=lambda c: (c.number, c.name),
        )
        self._programs: dict[str, list[Program]] = {}
        # Where free-to-air logos live isn't known yet - show what the guide has.
        for key, raw in channels.items():
            if not raw.get("subscribed") and raw.get("images"):
                _LOG.debug("Guide free-to-air sample: key=%s images=%s", key, raw["images"])
                break

    def epg_key(self, channel_id: str) -> str | None:
        """Guide key for a channel id as reported by a box."""
        return self._by_source_id.get(str(channel_id))

    def logo(self, epg_key: str) -> str | None:
        """Channel logo URL for a guide key."""
        raw = self._raw.get(epg_key)
        return _logo_url(epg_key, raw) if raw else None

    def name(self, epg_key: str) -> str | None:
        """Channel name for a guide key."""
        raw = self._raw.get(epg_key)
        return raw.get("name") if raw else None

    def cached_program(self, epg_key: str, at_ms: int) -> Program | None:
        """The cached programme on air at ``at_ms``, without fetching."""
        for program in self._programs.get(epg_key, []):
            if program.covers(at_ms):
                return program
        return None

    def has_listings(self, epg_key: str, at_ms: int) -> bool:
        """True if the cache covers ``at_ms`` for this channel (even if off air)."""
        programs = self._programs.get(epg_key)
        return bool(programs) and programs[0].start_ms <= at_ms < programs[-1].end_ms

    def set_programs(self, epg_key: str, programs: list[Program]) -> None:
        self._programs[epg_key] = sorted(programs, key=lambda p: p.start_ms)


def parse_programs(response: dict[str, Any]) -> dict[str, list[Program]]:
    """Parse a ``/v2/epg/programslist`` response into programmes per guide key."""
    fields = (response.get("__meta__") or {}).get("program_fields") or []

    def index(name: str) -> int | None:
        return fields.index(name) if name in fields else None

    i_title, i_start, i_end = index("title"), index("start"), index("end")
    i_episode, i_genre = index("episode_title"), index("genre")
    i_synopsis = index("synopsis_id")
    synopses = response.get("synopses") or {}
    if i_title is None or i_start is None or i_end is None:
        return {}

    def get(row: list, i: int | None) -> Any:
        return row[i] if i is not None and i < len(row) else None

    result: dict[str, list[Program]] = {}
    for key, rows in (response.get("channels") or {}).items():
        result[str(key)] = [
            Program(
                title=str(get(row, i_title) or ""),
                start_ms=_to_int(get(row, i_start)),
                end_ms=_to_int(get(row, i_end)),
                episode_title=str(get(row, i_episode) or ""),
                genre=str(get(row, i_genre) or ""),
                synopsis=str(synopses.get(str(get(row, i_synopsis)), "")),
            )
            for row in rows
        ]
    return result


Callback = Callable[[Box | None], Awaitable[None] | None]


class FetchTvClient:
    """One signed-in FetchTV account: its boxes, their state, and commands."""

    def __init__(
        self,
        activation_code: str,
        pin: str,
        *,
        api_base: str = API_BASE,
        ws_url: str = WS_URL,
        poll_interval: float = POLL_INTERVAL_S,
        reconnect_delay: float = RECONNECT_DELAY_S,
    ):
        self._activation_code = activation_code
        self._pin = pin
        self._api_base = api_base.rstrip("/")
        self._ws_url = ws_url
        self._poll_interval = poll_interval
        self._reconnect_delay = reconnect_delay
        self._cookies: dict[str, str] = {}
        self._ws: Any = None
        self._running = False
        self._callbacks: list[Callback] = []
        self._guide: Guide | None = None
        self._guide_lock = asyncio.Lock()
        self.boxes: dict[str, Box] = {}
        self._alive_waiters: dict[str, asyncio.Event] = {}
        self.connected = False
        self.ready = asyncio.Event()
        """Set once signed in and the websocket is open."""
        self.auth_failed = False

    # --- public API -----------------------------------------------------------

    def subscribe(self, callback: Callback) -> None:
        """Call ``callback(box)`` on box changes, ``callback(None)`` on connectivity changes."""
        self._callbacks.append(callback)

    @property
    def guide(self) -> Guide | None:
        return self._guide

    async def login(self) -> dict[str, Box]:
        """Sign in and return the account's boxes. Raises ``AuthError`` on bad credentials."""
        try:
            status, headers, body = await asyncio.to_thread(
                self._http,
                "POST",
                "/v3/authenticate",
                data={"activation_code": self._activation_code, "pin": self._pin},
            )
        except urllib.error.HTTPError as ex:
            if ex.code in (401, 403):
                raise AuthError(f"sign-in rejected ({ex.code})") from ex
            raise FetchTvError(f"sign-in failed: HTTP {ex.code}") from ex
        except (OSError, ValueError) as ex:
            raise FetchTvError(f"sign-in failed: {ex}") from ex
        meta = body.get("__meta__") or {}
        if meta.get("error"):
            raise AuthError(f"sign-in rejected: {meta.get('error')}: {meta.get('message')}")
        cookies = {}
        for cookie in headers.get_all("Set-Cookie") or []:
            name, _, value = cookie.split(";", 1)[0].partition("=")
            cookies[name.strip()] = value.strip()
        if "auth" not in cookies:
            raise AuthError("sign-in returned no auth cookie")
        self._cookies = cookies
        self.auth_failed = False

        for terminal in body.get("terminals") or []:
            if terminal.get("status", "ENABLED") != "ENABLED":
                continue
            tid = str(terminal["id"])
            name = terminal.get("friendly_name") or tid
            if tid in self.boxes:
                self.boxes[tid].name = name
            else:
                self.boxes[tid] = Box(terminal_id=tid, name=name)
        _LOG.info("Signed in to FetchTV: %d box(es)", len(self.boxes))
        return self.boxes

    async def run(self) -> None:
        """Stay connected until ``stop()``: sign in, open the websocket, reconnect on failure."""
        self._running = True
        delay = self._reconnect_delay
        while self._running:
            try:
                await self.login()
                await self._run_socket()
                delay = self._reconnect_delay
            except AuthError as ex:
                _LOG.error("FetchTV sign-in rejected: %s", ex)
                self.auth_failed = True
                delay = RECONNECT_DELAY_MAX_S
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001 - keep the loop alive on anything
                _LOG.warning("FetchTV connection lost: %s", ex)
            await self._set_connected(False)
            if not self._running:
                break
            _LOG.info("Reconnecting to FetchTV in %.0fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX_S)

    async def stop(self) -> None:
        """Disconnect and stop reconnecting."""
        self._running = False
        ws = self._ws
        if ws is not None:
            await ws.close()
        await self._set_connected(False)

    async def send_key(self, terminal_id: str, key: str) -> None:
        """Press a remote key (one of the ``KEY_*`` names) on a box."""
        await self._send(terminal_id, "KEYEVENT", requires_stb=True, extra={"keyName": key})
        if key == KEY_STOP:
            await self.request_media_state(terminal_id)

    async def send_keycode(self, terminal_id: str, char: str) -> None:
        """Type a character (used for channel number digits)."""
        await self._send(terminal_id, "KEYCODE", requires_stb=True, extra={"keyCode": ord(char[0])})

    async def play_channel(self, terminal_id: str, channel_id: str) -> None:
        """Tune a box to a channel id (as listed in ``channels_for``)."""
        await self._send(
            terminal_id, "PLAY_CHANNEL", requires_stb=True, extra={"channelId": int(channel_id)}
        )

    async def send_raw(self, terminal_id: str, msg_type: str, extra: dict[str, Any]) -> None:
        """Send an arbitrary message to a box (experiments only: see remote_entity)."""
        _LOG.info("Experimental message to %s: %s %s", terminal_id, msg_type, extra)
        await self._send(terminal_id, msg_type, requires_stb=True, extra=extra)

    async def request_media_state(self, terminal_id: str) -> None:
        """Ask a box to report what it is playing."""
        await self._send(terminal_id, "MEDIA_STATE", requires_stb=True)

    async def refresh_recordings(self, box: Box, max_age_s: float = 60.0, timeout_s: float = 3.0) -> None:
        """
        Make the box's recordings reasonably fresh.

        Recordings come in the box's full report (the reply to ``ARE_YOU_ALIVE``),
        which the driver only asks for at start-up, so ask again when the last
        one is older than ``max_age_s`` or a recording has started or ended
        since. Waits up to ``timeout_s`` for the reply and carries on with what
        it has if the box doesn't answer.
        """
        fresh = box.recordings_at and time.monotonic() - box.recordings_at < max_age_s
        if fresh or not box.online:
            return
        waiter = self._alive_waiters.setdefault(box.terminal_id, asyncio.Event())
        waiter.clear()
        try:
            await self._send(box.terminal_id, "ARE_YOU_ALIVE", queueable=True, paired_only=True)
            await asyncio.wait_for(waiter.wait(), timeout_s)
        except (NotConnectedError, asyncio.TimeoutError):
            pass

    def channels_for(self, box: Box) -> list[Channel]:
        """Channels the box can play: its visible free-to-air channels, then subscriptions."""
        fta = [c for c in box.fta_channels if c.id not in box.hidden_channel_ids]
        guide = self._guide
        if guide is not None:
            for channel in fta:
                if channel.logo is None:
                    channel.logo = guide.logo(channel.epg_id)
        subs = guide.subscription_channels if guide else []
        return sorted(fta, key=lambda c: (c.number, c.name)) + list(subs)

    def current_channel(self, box: Box) -> Channel | None:
        """The channel the box is on, if it is on a known channel."""
        if not box.channel_id:
            return None
        for channel in self.channels_for(box):
            if channel.id == box.channel_id:
                return channel
        guide = self._guide
        key = guide.epg_key(box.channel_id) if guide else None
        if guide and key:
            return Channel(
                id=box.channel_id,
                epg_id=key,
                number=0,
                name=guide.name(key) or box.media_title,
                logo=guide.logo(key),
            )
        return None

    def cached_program(self, box: Box, at_ms: int | None = None) -> Program | None:
        """What is on air on the box's channel, from the cache (no network)."""
        channel = self.current_channel(box)
        if channel is None or self._guide is None:
            return None
        return self._guide.cached_program(channel.epg_id, at_ms or _now_ms())

    async def refresh_program(self, box: Box) -> Program | None:
        """Fetch guide listings for the box's channel if the cache doesn't cover now."""
        await self._ensure_guide()
        channel = self.current_channel(box)
        guide = self._guide
        if channel is None or guide is None:
            return None
        now = _now_ms()
        if not guide.has_listings(channel.epg_id, now):
            try:
                _, _, body = await asyncio.to_thread(
                    self._http,
                    "GET",
                    "/v2/epg/programslist",
                    params={
                        "channel_ids": channel.epg_id,
                        "count": 42,
                        "extended": 1,
                        "off_air_catchup": 0,
                        "include_catchup": 0,
                    },
                )
            except (OSError, ValueError) as ex:
                _LOG.warning("Guide lookup for %s failed: %s", channel.name, ex)
                return None
            for key, programs in parse_programs(body).items():
                guide.set_programs(key, programs)
        return guide.cached_program(channel.epg_id, now)

    def cached_channel_program(self, channel: Channel, at_ms: int | None = None) -> Program | None:
        """What is on air on a channel, from the cache (no network)."""
        if self._guide is None:
            return None
        return self._guide.cached_program(channel.epg_id, at_ms or _now_ms())

    async def prefetch_programs(self) -> None:
        """
        Keep what is on now cached for every channel any box can play.

        One request covers them all, and only when some listing is about to
        run out, so the media browser never waits on a lookup.
        """
        channels = {c.epg_id: c for box in self.boxes.values() for c in self.channels_for(box)}
        await self.refresh_programs(list(channels.values()), margin_ms=PREFETCH_MARGIN_MS, count=6)

    async def refresh_programs(
        self, channels: list[Channel], margin_ms: int = 0, count: int = 3
    ) -> None:
        """
        Fetch what is on now for several channels in one request.

        Used by the media browser, so every channel on a page can show its
        programme. Channels whose cached listings already cover ``margin_ms``
        from now are skipped.
        """
        await self._ensure_guide()
        guide = self._guide
        if guide is None:
            return
        at = _now_ms() + margin_ms
        keys = sorted({c.epg_id for c in channels if not guide.has_listings(c.epg_id, at)})
        if not keys:
            return
        try:
            _, _, body = await asyncio.to_thread(
                self._http,
                "GET",
                "/v2/epg/programslist",
                params={
                    "channel_ids": ",".join(keys),
                    "count": count,
                    "extended": 1,
                    "off_air_catchup": 0,
                    "include_catchup": 0,
                },
            )
        except (OSError, ValueError) as ex:
            _LOG.warning("Guide lookup for %d channel(s) failed: %s", len(keys), ex)
            return
        for key, programs in parse_programs(body).items():
            guide.set_programs(key, programs)

    # --- internals ------------------------------------------------------------

    def _http(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> tuple[int, Any, dict[str, Any]]:
        url = self._api_base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method, headers=dict(_HEADERS))
        if data is not None:
            req.data = urllib.parse.urlencode(data).encode()
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        if self._cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self._cookies.items()))
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status, resp.headers, json.loads(resp.read())

    async def _ensure_guide(self) -> None:
        async with self._guide_lock:
            if self._guide and time.monotonic() - self._guide.loaded_at < CHANNEL_LIST_MAX_AGE_S:
                return
            try:
                _, _, body = await asyncio.to_thread(self._http, "GET", "/v2/epg/channels")
            except (OSError, ValueError) as ex:
                _LOG.warning("Could not load the FetchTV channel list: %s", ex)
                return
            self._guide = Guide(body.get("channels") or {})
            _LOG.info(
                "Loaded FetchTV guide: %d subscription channel(s)",
                len(self._guide.subscription_channels),
            )

    async def _run_socket(self) -> None:
        async with websockets.connect(
            self._ws_url,
            additional_headers={"Cookie": f"auth={self._cookies['auth']}"},
            open_timeout=WS_OPEN_TIMEOUT_S,
            ping_interval=30,
        ) as ws:
            self._ws = ws
            await self._set_connected(True)
            poller = asyncio.create_task(self._poll())
            try:
                await self._ensure_guide()
                async for raw in ws:
                    await self._on_message(raw)
            finally:
                poller.cancel()
                self._ws = None
                for box in self.boxes.values():
                    box.online = False

    async def _poll(self) -> None:
        while True:
            for tid, box in list(self.boxes.items()):
                try:
                    if box.online:
                        await self.request_media_state(tid)
                    else:
                        await self._send(tid, "ARE_YOU_ALIVE", queueable=True, paired_only=True)
                except NotConnectedError:
                    return
            await asyncio.sleep(self._poll_interval)

    async def _on_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            _LOG.debug("Ignoring non-JSON message")
            return
        body = msg.get("message") or {}
        msg_type = body.get("type")
        sender = msg.get("sender")
        if not msg_type or not sender:
            return
        box = self.boxes.get(str(sender))
        if box is None:
            _LOG.debug("Ignoring %s from unknown terminal", msg_type)
            return
        data = body.get("data") or {}
        _LOG.debug("%s <- %s", box.name, msg_type)

        if msg_type == "I_AM_ALIVE":
            box.apply_alive(data)
            waiter = self._alive_waiters.get(box.terminal_id)
            if waiter:
                waiter.set()
        elif msg_type in _RECORDING_EVENTS:
            # The report is the only place the list of recordings comes from.
            box.recordings_at = 0.0
            return
        elif msg_type in ("MEDIA_STATE", "NOW_PLAYING"):
            box.apply_media_state(data)
        elif msg_type in ("PAUSED", "UNPAUSED"):
            box.play_state = PLAY_STATE_PAUSED if msg_type == "PAUSED" else PLAY_STATE_PLAYING
        elif msg_type in ("CHANNEL_CHANGED", "PLAYBACK_STOPPED"):
            # These follow every channel change and carry no reliable state of
            # their own (PLAYBACK_STOPPED is the *old* stream stopping) - ask
            # the box for the truth instead.
            await self.request_media_state(box.terminal_id)
            return
        elif msg_type == "PONG":
            if not box.has_state:
                await self._send(box.terminal_id, "ARE_YOU_ALIVE", queueable=True, paired_only=True)
            return
        else:
            # Anything else is not acted on, but its content is what to look at
            # when working out a message the box sends (command errors, say).
            _LOG.debug("%s <- %s data: %s", box.name, msg_type, data)
            return
        await self._notify(box)

    async def _send(
        self,
        terminal_id: str,
        msg_type: str,
        *,
        queueable: bool = False,
        requires_stb: bool = False,
        paired_only: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        ws = self._ws
        if ws is None:
            raise NotConnectedError("not connected to FetchTV")
        message: dict[str, Any] = {
            "data": {"messageId": f"{terminal_id}_{_now_ms()}"},
            "type": msg_type,
            "isQueueable": queueable,
            "requiresSetTopBox": requires_stb,
            "onlyPairedSetTopBox": paired_only,
        }
        if extra:
            message.update(extra)
        try:
            await ws.send(json.dumps({"to": terminal_id, "message": message}))
        except websockets.ConnectionClosed as ex:
            raise NotConnectedError("FetchTV connection closed") from ex

    async def _set_connected(self, connected: bool) -> None:
        if connected:
            self.ready.set()
        else:
            self.ready.clear()
        if connected == self.connected:
            return
        self.connected = connected
        await self._notify(None)

    async def _notify(self, box: Box | None) -> None:
        for callback in list(self._callbacks):
            try:
                result = callback(box)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                _LOG.exception("FetchTV update callback failed")


def _now_ms() -> int:
    return int(time.time() * 1000)
