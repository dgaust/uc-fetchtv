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
import os
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

# What the Fetch Android app (3.33.2, version code 6562) sends, so that this
# looks like an ordinary client to Fetch's service, which rejects requests
# without the capabilities header. The app is built on OkHttp 4.12.0 (its
# User-Agent) and sends the capabilities as "no_pin,android,v<version>.<build>".
APP_VERSION = "3.33.2.6562"
CAPABILITIES = f"no_pin,android,v{APP_VERSION},tenplay_v2"
USER_AGENT = "okhttp/4.12.0"
# The app sends its connect timeout in seconds with each request: 30 at first.
REQUEST_TIMEOUT_HEADER = "30"
# Stand-in when no device id is given (tests); the driver passes a per-install one.
DEFAULT_DEVICE_ID = "uc-intg-fetchtv"


def request_headers(device_id: str) -> dict[str, str]:
    """The headers the app puts on every API request."""
    return {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "X-FTV-Capabilities": CAPABILITIES,
        "X-FTV-Timeout": REQUEST_TIMEOUT_HEADER,
        "X-FTV-DeviceID": device_id,
    }

def experimental() -> bool:
    """
    True when FETCHTV_EXPERIMENTAL=1: turns on features that aren't finished
    (the Recordings folder, which can list but not play, and the raw
    ``PLAY_ASSET`` experiments), so users don't see them.
    """
    return os.environ.get("FETCHTV_EXPERIMENTAL") == "1"


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
# Waking a box: how long to wait for it to report it is awake, and how often to ask.
WAKE_TIMEOUT_S = 20.0
WAKE_POLL_S = 1.5
# Typing text into a box: a cap, and the pause between key codes.
MAX_TYPED_CHARS = 64
TYPE_DELAY_S = 0.15
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
    program_id: str = ""
    """The guide's id for the programme, which the box accepts for start over."""

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
    end_ms: int = 0
    series_id: str = ""

    @property
    def duration_s(self) -> int:
        return max(0, (self.end_ms - self.start_ms) // 1000)

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
    # Set while the box plays a recording: playback type PVR on the box that
    # recorded it, ENCRYPTED_VOD_STREAM (base type MULTIROOM_PVR) on another box.
    recording_id: str = ""
    recording_duration_s: int = 0
    season: str = ""
    episode: str = ""
    fta_channels: list[Channel] = field(default_factory=list)
    hidden_channel_ids: set[str] = field(default_factory=set)
    recordings: list[Recording] = field(default_factory=list)
    # Monotonic time of the last full box report, which carries the recordings;
    # 0 when they may be out of date (a recording started or ended).
    recordings_at: float = 0.0
    volume: int | None = None
    muted: bool | None = None
    scheduled_count: int = 0
    active_recording_ids: list[str] = field(default_factory=list)
    storage_info: Any = None

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
        if _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug("%s: recordings breakdown %s", self.name, _recordings_breakdown(data.get("recordings") or []))
            for raw in data.get("recordings") or []:
                if str(raw.get("id")) in ("969981881998843", "969981816334867", "978481670488756500"):
                    _LOG.debug(
                        "%s: watched recording %s",
                        self.name,
                        {k: v for k, v in raw.items() if k not in ("description", "nameLowered", "episodeTitleLowered")},
                    )
            _LOG.debug(
                "%s: recording signatures %s",
                self.name,
                _recordings_signatures(
                    data.get("recordings") or [], ("969981881998843", "969981816334867", "978481670488756500")
                ),
            )
        self.scheduled_count = len(data.get("currentFutureRecordings") or [])
        self.active_recording_ids = [str(i) for i in data.get("activeRecordings") or []]
        self.storage_info = data.get("storageInfo")
        _LOG.debug(
            "%s: volume %s mute %s bt %s/%s output %s, storage %s, %d scheduled, active %s",
            self.name,
            data.get("volume"),
            data.get("mute"),
            data.get("btVolume"),
            data.get("btMute"),
            data.get("volumeOuput"),
            self.storage_info,
            self.scheduled_count,
            self.active_recording_ids,
        )
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
        _LOG.debug("%s: playing %s", self.name, media)
        self.playback_type = str(media.get("playbackType") or "")
        channel_id = media.get("channelId")
        self.channel_id = "" if channel_id is None else str(channel_id)
        self.media_title = str(media.get("mediaTitle") or "")
        self.recording_id = str(media.get("recordingId") or "")
        self.recording_duration_s = _to_int(media.get("duration")) // 1000
        self.season = str(media.get("seasonNumber") or "")
        self.episode = str(media.get("episodeNumber") or "")
        self.play_state = str(media.get("playBackState") or "")
        if self.play_state == "UNPAUSED":
            self.play_state = PLAY_STATE_PLAYING

    def _apply_settings(self, data: dict[str, Any]) -> None:
        if "standby" in data:
            self.standby = bool(data["standby"])
        if isinstance(data.get("volume"), (int, float)):
            self.volume = int(data["volume"])
        if "mute" in data:
            self.muted = bool(data["mute"])
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


def _recordings_breakdown(raws: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts that show how the box's recordings differ (for debugging what can be played)."""
    counts: dict[str, Any] = {"total": len(raws), "owners": {}}
    for raw in raws:
        owner = (raw.get("remoteResource") or {}).get("stbId") or "local"
        counts["owners"][owner] = counts["owners"].get(owner, 0) + 1
        for key, hit in (
            ("pendingDelete", bool(raw.get("pendingDelete"))),
            ("id_over_2^53", isinstance(raw.get("id"), int) and raw["id"] > 2**53),
            ("hashId_differs", raw.get("hashId") not in (None, raw.get("id"))),
            ("has_dlnaId", raw.get("dlnaId") not in (None, -1)),
            ("tiny_or_partial", raw.get("size") != raw.get("dataReceived")),
        ):
            if hit:
                counts[key] = counts.get(key, 0) + 1
    return counts


def _recording_signature(raw: dict[str, Any]) -> tuple:
    return (
        "pendingDelete" if raw.get("pendingDelete") else "kept",
        "dlna" if raw.get("dlnaId") not in (None, -1) else "no-dlna",
        "complete" if raw.get("size") == raw.get("dataReceived") else "partial",
        raw.get("sourceType"),
        raw.get("flags"),
        "big-id" if isinstance(raw.get("id"), int) and raw["id"] > 2**53 else "",
    )


def _recordings_signatures(raws: list[dict[str, Any]], watch: tuple[str, ...]) -> dict[str, Any]:
    """Group a box's recordings by their status fields, with the signature of watched ids."""
    groups: dict[tuple, list[Any]] = {}
    for raw in raws:
        groups.setdefault(_recording_signature(raw), []).append(raw.get("id"))
    return {
        "groups": {str(k): (len(v), v[0]) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:14]},
        "watch": {
            w: _recording_signature(raw) for raw in raws for w in watch if str(raw.get("id")) == w
        },
    }


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
        end_ms=_to_int(raw.get("programEndDate") or raw.get("endDate")),
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
    i_synopsis, i_id = index("synopsis_id"), index("id")
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
                program_id=str(get(row, i_id) or ""),
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
        device_id: str = DEFAULT_DEVICE_ID,
        drm_id: str = "",
    ):
        self._device_id = device_id
        self._drm_id = drm_id
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
        self._tasks: set[asyncio.Task] = set()
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
        for task in list(self._tasks):
            task.cancel()
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

    async def wake(self, terminal_id: str, timeout_s: float = WAKE_TIMEOUT_S) -> bool:
        """
        Wake a box from standby and wait for it to say it is awake.

        The Fetch app does the same before it plays anything on a box in
        standby: ``WAKE_STB``, then it sends the play once the box reports it
        is awake. Returns False if the box is still in standby after
        ``timeout_s``. Does nothing if the box is not known to be in standby.
        """
        box = self.boxes.get(terminal_id)
        if box is None or box.standby is not True:
            return True
        await self._send(terminal_id, "WAKE_STB", requires_stb=True)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(WAKE_POLL_S)
            if box.standby is not True:
                return True
            await self.request_media_state(terminal_id)
        return False

    async def _play(self, terminal_id: str, msg_type: str, extra: dict[str, Any]) -> None:
        """
        Send a message that starts something playing.

        A box in standby is woken first, and the message goes out once it is
        awake. That wait can take many seconds, longer than the Remote waits for
        a command's reply, so it runs in the background and this returns at once.
        """
        box = self.boxes.get(terminal_id)
        if box is None or box.standby is not True:
            await self._send(terminal_id, msg_type, requires_stb=True, extra=extra)
            return
        task = asyncio.create_task(self._wake_then_send(terminal_id, msg_type, extra))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _wake_then_send(self, terminal_id: str, msg_type: str, extra: dict[str, Any]) -> None:
        try:
            if not await self.wake(terminal_id):
                _LOG.warning("%s did not wake up in time; sending %s anyway", terminal_id, msg_type)
            await self._send(terminal_id, msg_type, requires_stb=True, extra=extra)
        except NotConnectedError:
            _LOG.debug("Not connected: dropped %s for %s", msg_type, terminal_id)
        except Exception:  # a failed background send must not go unnoticed
            _LOG.exception("Could not send %s to %s after waking it", msg_type, terminal_id)

    async def play_channel(self, terminal_id: str, channel_id: str) -> None:
        """Tune a box to a channel id (as listed in ``channels_for``), waking it first if needed."""
        await self._play(terminal_id, "PLAY_CHANNEL", {"channelId": int(channel_id)})

    async def start_over(self, terminal_id: str, channel_id: str, program_id: str) -> None:
        """
        Restart the programme that is on a channel from its beginning, as the
        Fetch app's "start over": ``PLAY_ASSET`` with asset type ``START_OVER``.
        """
        await self._play(
            terminal_id,
            "PLAY_ASSET",
            {"assetType": "START_OVER", "channelId": str(channel_id), "programId": str(program_id)},
        )

    async def start_over_current(self, box: Box) -> bool:
        """
        Start over the programme that is on the box's current channel.

        Returns False if there is no known programme (not on a channel the guide
        knows, or the guide has no id for it) so there is nothing to start over.
        """
        program = self.cached_program(box) or await self.refresh_program(box)
        if program is None or not program.program_id or not box.channel_id:
            return False
        await self.start_over(box.terminal_id, box.channel_id, program.program_id)
        return True

    async def set_volume(self, terminal_id: str, level: int) -> None:
        """Set the box's volume (``SET_VOLUME``, 0 to 100 as on the app's slider)."""
        level = max(0, min(100, int(level)))
        await self._send(terminal_id, "SET_VOLUME", requires_stb=True, extra={"volume": level})
        box = self.boxes.get(terminal_id)
        if box is not None:
            box.volume = level

    async def type_text(self, terminal_id: str, text: str) -> None:
        """
        Type text into the box one key code at a time, as the app's keyboard does.

        The characters go out in the background, a short pause apart, so this
        returns at once rather than after the whole text has been sent.
        """
        task = asyncio.create_task(self._type_chars(terminal_id, text[:MAX_TYPED_CHARS]))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _type_chars(self, terminal_id: str, text: str) -> None:
        try:
            for char in text:
                await self.send_keycode(terminal_id, char)
                await asyncio.sleep(TYPE_DELAY_S)
        except NotConnectedError:
            _LOG.debug("Not connected: stopped typing to %s", terminal_id)

    async def play_recording(self, terminal_id: str, recording_id: str) -> None:
        """
        Play one of a box's recordings on it, waking it first if needed.

        The message is the one the Fetch Android app sends: ``PLAY_ASSET`` with
        an asset type and the recording's id, which is a string in the JSON.
        """
        await self._play(
            terminal_id, "PLAY_ASSET", {"assetType": "RECORDING", "recordingId": str(recording_id)}
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
        req = urllib.request.Request(url, method=method, headers=request_headers(self._device_id))
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
        # Connected the way the app does: the auth cookie in the URL, the
        # capabilities and device id as headers, and no User-Agent.
        url = f"{self._ws_url}?auth={urllib.parse.quote(self._cookies['auth'], safe='')}"
        if self._drm_id:
            url += f"&verimatrix_id={urllib.parse.quote(self._drm_id, safe='')}"
        headers = request_headers(self._device_id)
        async with websockets.connect(
            url,
            additional_headers={k: headers[k] for k in ("X-FTV-Capabilities", "X-FTV-DeviceID")},
            user_agent_header=None,
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
        elif msg_type == "VOLUME_SET":
            if isinstance(data.get("current"), (int, float)):
                box.volume = int(data["current"])
            if "mute" in data:
                box.muted = bool(data["mute"])
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
            extra = dict(extra)
            # A "data" object goes alongside the message id rather than replacing it.
            if isinstance(extra.get("data"), dict):
                message["data"].update(extra.pop("data"))
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
