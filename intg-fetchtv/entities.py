"""
Maps FetchTV boxes to Unfolded Circle media-player entities.

Each box on the account becomes one media player. What it shows:

- **state**: OFF in standby, PLAYING for live TV or playback, PAUSED, ON when
  the box is up but idle (e.g. in a menu or app), UNAVAILABLE while the Fetch
  service is unreachable.
- **media**: the programme title from the guide (falling back to the box's own
  title, usually the channel name), the channel as "artist", the episode title
  or genre as "album", the channel logo as artwork, and the programme's start
  and end as position/duration.
- **source**: the current channel, with every channel the box can play
  (visible free-to-air channels, then subscription channels) as the list.
- **media browser**: the same channels as two folders (free to air,
  subscription), each with its logo as the thumbnail; playing one tunes the box.
  A third folder lists the box's recordings, newest first; playing one plays it
  on the box.

Each box also gets a **select** entity listing the channels, for dropdowns and
automations.

Command handlers look up the current client and box at command time (via the
``get_client`` callback) rather than closing over them, because entities are
built from the cached box list before the client has signed in, and a
re-setup creates a new client.

:license: MPL-2.0, see LICENSE for more details.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from ucapi import StatusCodes
from ucapi.api_definitions import Pagination
from ucapi.media_player import Attributes as Attr
from ucapi.media_player import BrowseMediaItem, BrowseOptions, BrowseResults
from ucapi.media_player import Commands as Cmd
from ucapi.media_player import DeviceClasses, MediaClass, MediaPlayer, Options
from ucapi.media_player import Features as Feat
from ucapi.media_player import MediaContentType, States
from ucapi.select import Attributes as SelAttr
from ucapi.select import Commands as SelCmd
from ucapi.select import Select
from ucapi.select import States as SelStates

import fetchtv
from fetchtv import Box, FetchTvClient, Program
from placeholder import CHANNEL_PLACEHOLDER

_LOG = logging.getLogger(__name__)

_PREFIX = "media_player."
_SELECT_PREFIX = "select."

# Media browser folders.
_ROOT, _FREE_TO_AIR, _SUBSCRIPTION, _RECORDINGS = "root", "fta", "subscription", "recordings"
_RECORDING_PREFIX = "recording:"
_SERIES_PREFIX = "series:"

ClientGetter = Callable[[], "FetchTvClient | None"]

FEATURES = [
    Feat.ON_OFF,
    Feat.TOGGLE,
    Feat.PLAY_PAUSE,
    Feat.STOP,
    Feat.FAST_FORWARD,
    Feat.REWIND,
    Feat.MUTE_TOGGLE,
    Feat.VOLUME,
    Feat.VOLUME_UP_DOWN,
    Feat.DPAD,
    Feat.NUMPAD,
    Feat.MENU,
    Feat.GUIDE,
    Feat.INFO,
    Feat.COLOR_BUTTONS,
    Feat.CHANNEL_SWITCHER,
    Feat.SELECT_SOURCE,
    Feat.RECORD,
    Feat.BROWSE_MEDIA,
    Feat.PLAY_MEDIA,
    Feat.MEDIA_TITLE,
    Feat.MEDIA_ARTIST,
    Feat.MEDIA_ALBUM,
    Feat.MEDIA_IMAGE_URL,
    Feat.MEDIA_TYPE,
    Feat.MEDIA_DURATION,
    Feat.MEDIA_POSITION,
]

# Keys with no matching media-player command, offered as simple commands.
SIMPLE_COMMANDS = {
    "EXIT": fetchtv.KEY_EXIT,
    "TEXT": fetchtv.KEY_TEXT,
}

# Offered as simple commands, but not key presses.
START_OVER = "START_OVER"
VOLUME_STEP = 5

_KEY_COMMANDS = {
    Cmd.PLAY_PAUSE: fetchtv.KEY_PLAY_PAUSE,
    Cmd.STOP: fetchtv.KEY_STOP,
    Cmd.FAST_FORWARD: fetchtv.KEY_FAST_FORWARD,
    Cmd.REWIND: fetchtv.KEY_REWIND,
    Cmd.MUTE_TOGGLE: fetchtv.KEY_MUTE,
    Cmd.CURSOR_UP: fetchtv.KEY_UP,
    Cmd.CURSOR_DOWN: fetchtv.KEY_DOWN,
    Cmd.CURSOR_LEFT: fetchtv.KEY_LEFT,
    Cmd.CURSOR_RIGHT: fetchtv.KEY_RIGHT,
    Cmd.CURSOR_ENTER: fetchtv.KEY_SELECT,
    Cmd.BACK: fetchtv.KEY_BACK,
    Cmd.MENU: fetchtv.KEY_MENU,
    Cmd.GUIDE: fetchtv.KEY_GUIDE,
    Cmd.INFO: fetchtv.KEY_INFO,
    # Guide and info were first offered as simple commands, and a Remote that
    # still has them in a UI page or activity sends those names.
    "GUIDE": fetchtv.KEY_GUIDE,
    "INFO": fetchtv.KEY_INFO,
    Cmd.FUNCTION_RED: fetchtv.KEY_RED,
    Cmd.FUNCTION_GREEN: fetchtv.KEY_GREEN,
    Cmd.FUNCTION_YELLOW: fetchtv.KEY_YELLOW,
    Cmd.FUNCTION_BLUE: fetchtv.KEY_BLUE,
    Cmd.RECORD: fetchtv.KEY_RECORD,
    **SIMPLE_COMMANDS,
}

_DIGITS = {getattr(Cmd, f"DIGIT_{n}"): str(n) for n in range(10)}


def entity_id(terminal_id: str) -> str:
    """Entity identifier for a box's media player."""
    return f"{_PREFIX}{terminal_id}"


def select_entity_id(terminal_id: str) -> str:
    """Entity identifier for a box's channel select."""
    return f"{_SELECT_PREFIX}{terminal_id}"


def terminal_id_from_entity(entity_id_: str) -> str | None:
    """The box terminal id an entity id belongs to."""
    for prefix in (_PREFIX, _SELECT_PREFIX):
        if entity_id_.startswith(prefix):
            return entity_id_[len(prefix):]
    return None


def box_cache_entry(box: Box) -> dict[str, Any]:
    """The persistable summary of a box."""
    return {"terminal_id": box.terminal_id, "name": box.name}


class FetchMediaPlayer(MediaPlayer):
    """A box's media player, which can also browse the box's channels."""

    def __init__(self, terminal_id: str, name: str, get_client: ClientGetter):
        super().__init__(
            entity_id(terminal_id),
            name,
            FEATURES,
            {Attr.STATE: States.UNAVAILABLE},
            device_class=DeviceClasses.SET_TOP_BOX,
            options={Options.SIMPLE_COMMANDS: [*SIMPLE_COMMANDS, START_OVER]},
            cmd_handler=_make_cmd_handler(terminal_id, get_client),
        )
        self._terminal_id = terminal_id
        self._get_client = get_client

    async def browse(self, options: BrowseOptions) -> BrowseResults | StatusCodes:
        """Browse the box's channels (logos as thumbnails)."""
        client = self._get_client()
        box = client.boxes.get(self._terminal_id) if client else None
        if client is None or box is None or not client.connected:
            return StatusCodes.SERVICE_UNAVAILABLE
        return await browse_channels(client, box, options)


def build_entity(terminal_id: str, name: str, get_client: ClientGetter) -> MediaPlayer:
    """Build the media player for a box. State is filled in once the box reports."""
    return FetchMediaPlayer(terminal_id, name, get_client)


def build_select(terminal_id: str, name: str, get_client: ClientGetter) -> Select:
    """Build the channel select for a box. Options are filled in once the box reports."""
    return Select(
        select_entity_id(terminal_id),
        f"{name} channel",
        {SelAttr.STATE: SelStates.UNAVAILABLE, SelAttr.OPTIONS: [], SelAttr.CURRENT_OPTION: ""},
        icon="uc:tv",
        cmd_handler=_make_select_handler(terminal_id, get_client),
    )


async def browse_channels(
    client: FetchTvClient, box: Box, options: BrowseOptions
) -> BrowseResults | StatusCodes:
    """
    The media browser: a root with a folder for free-to-air channels, one for
    subscription channels (each channel with its logo and what is on now) and
    one for the box's recordings.
    """
    channels = client.channels_for(box)
    free = [c for c in channels if not c.subscription]
    subscription = [c for c in channels if c.subscription]
    paging = options.paging
    start = (paging.page - 1) * paging.limit

    def page_of(items: list[BrowseMediaItem]) -> tuple[list[BrowseMediaItem], Pagination]:
        chunk = items[start:start + paging.limit]
        return chunk, Pagination(page=paging.page, limit=len(chunk), count=len(items))

    media_id = options.media_id or _ROOT
    if media_id == _ROOT:
        folders = [
            BrowseMediaItem(
                media_id=folder_id,
                title=title,
                subtitle=f"{len(members)} {noun}",
                media_class=MediaClass.DIRECTORY,
                can_browse=True,
                thumbnail=icon,
            )
            for folder_id, title, members, noun, icon in (
                (_FREE_TO_AIR, "Free to air", free, "channels", "icon://uc:tv"),
                (_SUBSCRIPTION, "Subscription", subscription, "channels", "icon://uc:tv"),
                (_RECORDINGS, "Recordings", box.recordings,
                 "recordings", "icon://uc:tv"),
            )
            if members
        ]
        chunk, pagination = page_of(folders)
        root = BrowseMediaItem(
            media_id=_ROOT,
            title=_clip(box.name),
            media_class=MediaClass.DIRECTORY,
            can_browse=True,
            items=chunk,
        )
        return BrowseResults(media=root, pagination=pagination)

    if media_id in (_FREE_TO_AIR, _SUBSCRIPTION):
        members = free if media_id == _FREE_TO_AIR else subscription
        shown = members[start:start + paging.limit]
        await client.refresh_programs(shown)
        now_ms = int(time.time() * 1000)
        chunk = [_channel_item(c, client.cached_channel_program(c, now_ms)) for c in shown]
        pagination = Pagination(page=paging.page, limit=len(chunk), count=len(members))
        folder = BrowseMediaItem(
            media_id=media_id,
            title="Free to air" if media_id == _FREE_TO_AIR else "Subscription",
            media_class=MediaClass.DIRECTORY,
            can_browse=True,
            items=chunk,
        )
        return BrowseResults(media=folder, pagination=pagination)

    if media_id == _RECORDINGS or media_id.startswith(_SERIES_PREFIX):
        await client.refresh_recordings(box)
        logos = {c.id: c.logo for c in channels}
        if media_id == _RECORDINGS:
            title, entries = "Recordings", _recording_entries(box.recordings)
            shown = entries[start:start + paging.limit]
            chunk = [
                _recording_item(e, logos.get(e.channel_id)) if isinstance(e, fetchtv.Recording)
                else _series_item(e, logos.get(e[0].channel_id))
                for e in shown
            ]
        else:
            series_id = media_id[len(_SERIES_PREFIX):]
            episodes = _newest_first([r for r in box.recordings if r.series_id == series_id])
            if not episodes:
                return StatusCodes.NOT_FOUND
            title, entries = episodes[0].title, episodes
            chunk = [_recording_item(r, logos.get(r.channel_id)) for r in episodes[start:start + paging.limit]]
        pagination = Pagination(page=paging.page, limit=len(chunk), count=len(entries))
        folder = BrowseMediaItem(
            media_id=media_id,
            title=_clip(title),
            media_class=MediaClass.DIRECTORY,
            can_browse=True,
            items=chunk,
        )
        return BrowseResults(media=folder, pagination=pagination)

    return StatusCodes.NOT_FOUND


def _clip(text: str, limit: int = 255) -> str:
    """``text`` cut to what the Remote accepts in a browse item (255 characters), never empty."""
    text = text.strip() or "-"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _day(start_ms: int) -> str:
    """The recording's day, like "Sat 14 Nov"; empty if the time is missing or not a real date."""
    if start_ms <= 0:
        return ""
    try:
        return datetime.fromtimestamp(start_ms / 1000, timezone.utc).astimezone().strftime("%a %d %b")
    except (OverflowError, OSError, ValueError):
        return ""


def _newest_first(recordings: list[fetchtv.Recording]) -> list[fetchtv.Recording]:
    # What you want to watch is usually what was just recorded.
    return sorted(recordings, key=lambda r: (r.start_ms, r.id), reverse=True)


def _recording_entries(
    recordings: list[fetchtv.Recording],
) -> list[fetchtv.Recording | list[fetchtv.Recording]]:
    """
    What the Recordings folder lists, newest first: each series with several
    recordings as one folder, and every other recording on its own.
    """
    by_series: dict[str, list[fetchtv.Recording]] = {}
    entries: list[fetchtv.Recording | list[fetchtv.Recording]] = []
    for recording in recordings:
        if recording.series_id:
            by_series.setdefault(recording.series_id, []).append(recording)
        else:
            entries.append(recording)
    for group in by_series.values():
        group = _newest_first(group)
        entries.append(group if len(group) > 1 else group[0])

    def newest(entry: fetchtv.Recording | list[fetchtv.Recording]) -> tuple[int, str]:
        latest = entry[0] if isinstance(entry, list) else entry
        return latest.start_ms, latest.id

    return sorted(entries, key=newest, reverse=True)


def _series_item(episodes: list[fetchtv.Recording], logo: str | None) -> BrowseMediaItem:
    """A series with several recordings, as a folder of its episodes."""
    latest = episodes[0]
    return BrowseMediaItem(
        media_id=f"{_SERIES_PREFIX}{latest.series_id}",
        title=_clip(latest.title),
        subtitle=f"{len(episodes)} recordings",
        media_class=MediaClass.DIRECTORY,
        can_browse=True,
        thumbnail=logo or CHANNEL_PLACEHOLDER,
    )


def _recording_item(recording: fetchtv.Recording, logo: str | None) -> BrowseMediaItem:
    """A recording as a list entry; playing it tells the box to play it."""
    subtitle = " · ".join(part for part in (recording.label, _day(recording.start_ms)) if part) or None
    return BrowseMediaItem(
        media_id=f"{_RECORDING_PREFIX}{recording.id}",
        title=_clip(recording.title),
        subtitle=_clip(subtitle) if subtitle else None,
        media_class=MediaClass.EPISODE if recording.episode else MediaClass.VIDEO,
        media_type=MediaContentType.EPISODE if recording.episode else MediaContentType.VIDEO,
        can_play=True,
        thumbnail=logo or CHANNEL_PLACEHOLDER,
    )


def _channel_item(channel: fetchtv.Channel, program: Program | None) -> BrowseMediaItem:
    # What is on now, or the channel number when the guide has nothing.
    if program and program.title.strip():
        subtitle = _clip(program.title)
    else:
        subtitle = str(channel.number) if channel.number else None
    return BrowseMediaItem(
        media_id=channel.id,
        title=_clip(channel.name),
        subtitle=subtitle,
        media_class=MediaClass.CHANNEL,
        media_type=MediaContentType.CHANNEL,
        can_play=True,
        thumbnail=channel.logo or CHANNEL_PLACEHOLDER,
    )


def select_attributes(client: FetchTvClient | None, box: Box) -> dict[str, Any]:
    """Current channel-select attributes for a box."""
    if client is None or not client.connected or not box.online or not box.has_state:
        return {SelAttr.STATE: SelStates.UNAVAILABLE}
    channels = client.channels_for(box)
    channel = None if box.standby else client.current_channel(box)
    return {
        SelAttr.STATE: SelStates.ON,
        SelAttr.OPTIONS: [c.label for c in channels],
        SelAttr.CURRENT_OPTION: _source_label(channel, channels),
    }


def attributes(
    client: FetchTvClient | None, box: Box, program: Program | None, now_ms: int
) -> dict[str, Any]:
    """Current media-player attributes for a box."""
    if client is None or not client.connected or not box.online or not box.has_state:
        return {Attr.STATE: States.UNAVAILABLE}
    if box.standby:
        return {
            Attr.STATE: States.OFF,
            Attr.MEDIA_TITLE: "",
            Attr.MEDIA_ARTIST: "",
            Attr.MEDIA_ALBUM: "",
            Attr.MEDIA_IMAGE_URL: "",
            Attr.MEDIA_DURATION: 0,
            Attr.MEDIA_POSITION: 0,
        }

    channels = client.channels_for(box)
    channel = client.current_channel(box)
    channel_name = channel.name if channel else ""
    live = box.play_state == fetchtv.PLAY_STATE_LIVE
    if box.recording_id:
        return _recording_attributes(box, channel)

    attrs: dict[str, Any] = {
        Attr.STATE: _state(box),
        Attr.SOURCE_LIST: [c.label for c in channels],
        Attr.SOURCE: _source_label(channel, channels),
        Attr.MEDIA_TYPE: MediaContentType.CHANNEL if channel else MediaContentType.VIDEO,
        Attr.MEDIA_ARTIST: channel_name if channel_name != box.media_title or program else "",
        Attr.MEDIA_IMAGE_URL: (channel.logo if channel else None) or CHANNEL_PLACEHOLDER,
    }
    _add_volume(attrs, box)
    if program and live:
        attrs[Attr.MEDIA_TITLE] = program.title
        attrs[Attr.MEDIA_ALBUM] = program.episode_title or program.genre
        attrs[Attr.MEDIA_DURATION] = max(0, (program.end_ms - program.start_ms) // 1000)
        attrs[Attr.MEDIA_POSITION] = max(0, (now_ms - program.start_ms) // 1000)
        attrs[Attr.MEDIA_POSITION_UPDATED_AT] = datetime.fromtimestamp(
            now_ms / 1000, tz=timezone.utc
        ).isoformat()
    else:
        attrs[Attr.MEDIA_TITLE] = box.media_title or channel_name
        attrs[Attr.MEDIA_ALBUM] = ""
        attrs[Attr.MEDIA_DURATION] = 0
        attrs[Attr.MEDIA_POSITION] = 0
    return attrs


def _add_volume(attrs: dict[str, Any], box: Box) -> None:
    if box.volume is not None:
        attrs[Attr.VOLUME] = box.volume
    if box.muted is not None:
        attrs[Attr.MUTED] = box.muted


def _recording_attributes(box: Box, channel: fetchtv.Channel | None) -> dict[str, Any]:
    """What a box playing one of its recordings shows (not the channel's live guide)."""
    recorded = next((r for r in box.recordings if r.id == box.recording_id), None)
    episode = recorded or fetchtv.Recording(
        id=box.recording_id, title=box.media_title, season=box.season, episode=box.episode
    )
    attrs = {
        Attr.STATE: _state(box),
        Attr.SOURCE: "",
        Attr.MEDIA_TYPE: MediaContentType.EPISODE if episode.episode else MediaContentType.VIDEO,
        Attr.MEDIA_TITLE: box.media_title,
        Attr.MEDIA_ARTIST: channel.name if channel else "",
        Attr.MEDIA_ALBUM: episode.label,
        Attr.MEDIA_IMAGE_URL: (channel.logo if channel else None) or CHANNEL_PLACEHOLDER,
        Attr.MEDIA_DURATION: box.recording_duration_s or (recorded.duration_s if recorded else 0),
        Attr.MEDIA_POSITION: 0,
    }
    _add_volume(attrs, box)
    return attrs


def _state(box: Box) -> States:
    if box.play_state in (fetchtv.PLAY_STATE_LIVE, fetchtv.PLAY_STATE_PLAYING):
        return States.PLAYING
    if box.play_state == fetchtv.PLAY_STATE_PAUSED:
        return States.PAUSED
    return States.ON


def _source_label(channel: fetchtv.Channel | None, channels: list[fetchtv.Channel]) -> str:
    if channel is None:
        return ""
    for c in channels:
        if c.id == channel.id:
            return c.label
    return channel.name


def _make_cmd_handler(terminal_id: str, get_client: ClientGetter):
    async def _handler(
        _entity: MediaPlayer, cmd_id: str, params: dict[str, Any] | None, websocket: Any
    ) -> StatusCodes:
        client = get_client()
        box = client.boxes.get(terminal_id) if client else None
        label = fetchtv.describe_command(cmd_id, params)
        if client is None or box is None or not client.connected:
            _LOG.debug("Media player command %s for %s: not connected to FetchTV (503)", label, terminal_id)
            return StatusCodes.SERVICE_UNAVAILABLE
        try:
            result = await _run_command(client, box, cmd_id, params or {})
            _LOG.debug("Media player command %s for %s -> %s", label, box.name, result.name)
            return result
        except fetchtv.NotConnectedError:
            _LOG.debug("Media player command %s for %s: connection closed while sending (503)", label, box.name)
            return StatusCodes.SERVICE_UNAVAILABLE
        except Exception:  # noqa: BLE001 - answer the Remote rather than leave it waiting
            _LOG.exception("Command %s failed", cmd_id)
            return StatusCodes.SERVER_ERROR

    return _handler


def _make_select_handler(terminal_id: str, get_client: ClientGetter):
    async def _handler(
        _entity: Select, cmd_id: str, params: dict[str, Any] | None, websocket: Any
    ) -> StatusCodes:
        client = get_client()
        box = client.boxes.get(terminal_id) if client else None
        label = fetchtv.describe_command(cmd_id, params)
        if client is None or box is None or not client.connected:
            _LOG.debug("Channel select command %s for %s: not connected to FetchTV (503)", label, terminal_id)
            return StatusCodes.SERVICE_UNAVAILABLE
        try:
            result = await _run_select_command(client, box, cmd_id, params or {})
            _LOG.debug("Channel select command %s for %s -> %s", label, box.name, result.name)
            return result
        except fetchtv.NotConnectedError:
            _LOG.debug("Channel select command %s for %s: connection closed while sending (503)", label, box.name)
            return StatusCodes.SERVICE_UNAVAILABLE
        except Exception:  # noqa: BLE001 - answer the Remote rather than leave it waiting
            _LOG.exception("Select command %s failed", cmd_id)
            return StatusCodes.SERVER_ERROR

    return _handler


async def _run_select_command(
    client: FetchTvClient, box: Box, cmd_id: str, params: dict[str, Any]
) -> StatusCodes:
    channels = client.channels_for(box)
    if not channels:
        return StatusCodes.SERVICE_UNAVAILABLE
    if cmd_id == SelCmd.SELECT_OPTION:
        channel = next((c for c in channels if c.label == params.get("option")), None)
    elif cmd_id == SelCmd.SELECT_FIRST:
        channel = channels[0]
    elif cmd_id == SelCmd.SELECT_LAST:
        channel = channels[-1]
    elif cmd_id in (SelCmd.SELECT_NEXT, SelCmd.SELECT_PREVIOUS):
        channel = next_channel(client, box, 1 if cmd_id == SelCmd.SELECT_NEXT else -1)
    else:
        return StatusCodes.NOT_IMPLEMENTED
    if channel is None:
        return StatusCodes.BAD_REQUEST
    await client.play_channel(box.terminal_id, channel.id)
    return StatusCodes.OK


async def _run_command(
    client: FetchTvClient, box: Box, cmd_id: str, params: dict[str, Any]
) -> StatusCodes:
    tid = box.terminal_id
    if cmd_id in _KEY_COMMANDS:
        await client.send_key(tid, _KEY_COMMANDS[cmd_id])
    elif cmd_id in _DIGITS:
        await client.send_keycode(tid, _DIGITS[cmd_id])
    elif cmd_id == START_OVER:
        if not await client.start_over_current(box):
            return StatusCodes.SERVICE_UNAVAILABLE
    elif cmd_id == Cmd.VOLUME:
        try:
            level = int(params.get("volume"))
        except (TypeError, ValueError):
            return StatusCodes.BAD_REQUEST
        await client.set_volume(tid, level)
    elif cmd_id in (Cmd.VOLUME_UP, Cmd.VOLUME_DOWN):
        if box.volume is None:  # a step needs a level to start from
            return StatusCodes.SERVICE_UNAVAILABLE
        step = VOLUME_STEP if cmd_id == Cmd.VOLUME_UP else -VOLUME_STEP
        await client.set_volume(tid, box.volume + step)
    elif cmd_id in (Cmd.ON, Cmd.OFF, Cmd.TOGGLE):
        # The box only has a power toggle, so the client presses it only if it
        # changes something - and not at all while the box's state is unknown.
        if not await client.set_power(tid, None if cmd_id == Cmd.TOGGLE else cmd_id == Cmd.ON):
            return StatusCodes.SERVICE_UNAVAILABLE
    elif cmd_id == Cmd.SELECT_SOURCE:
        source = params.get("source")
        channel = next((c for c in client.channels_for(box) if c.label == source), None)
        if channel is None:
            return StatusCodes.BAD_REQUEST
        await client.play_channel(tid, channel.id)
    elif cmd_id == Cmd.PLAY_MEDIA and str(params.get("media_id", "")).startswith(_RECORDING_PREFIX):
        recording_id = str(params["media_id"])[len(_RECORDING_PREFIX):]
        if not any(r.id == recording_id for r in box.recordings):
            return StatusCodes.BAD_REQUEST
        await client.play_recording(tid, recording_id)
    elif cmd_id == Cmd.PLAY_MEDIA:
        channel = next((c for c in client.channels_for(box) if c.id == str(params.get("media_id"))), None)
        if channel is None:
            return StatusCodes.BAD_REQUEST
        await client.play_channel(tid, channel.id)
    elif cmd_id in (Cmd.CHANNEL_UP, Cmd.CHANNEL_DOWN):
        channel = next_channel(client, box, 1 if cmd_id == Cmd.CHANNEL_UP else -1)
        if channel is None:
            return StatusCodes.SERVICE_UNAVAILABLE
        await client.play_channel(tid, channel.id)
    else:
        return StatusCodes.NOT_IMPLEMENTED
    return StatusCodes.OK


def next_channel(client: FetchTvClient, box: Box, step: int) -> fetchtv.Channel | None:
    """The channel ``step`` places from the current one in the box's list, wrapping."""
    channels = client.channels_for(box)
    if not channels:
        return None
    current = client.current_channel(box)
    ids = [c.id for c in channels]
    if current is None or current.id not in ids:
        return channels[0]
    return channels[(ids.index(current.id) + step) % len(channels)]
