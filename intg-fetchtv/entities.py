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

Command handlers look up the current client and box at command time (via the
``get_client`` callback) rather than closing over them, because entities are
built from the cached box list before the client has signed in, and a
re-setup creates a new client.

:license: MPL-2.0, see LICENSE for more details.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from ucapi import StatusCodes
from ucapi.media_player import Attributes as Attr
from ucapi.media_player import Commands as Cmd
from ucapi.media_player import DeviceClasses, MediaPlayer, Options
from ucapi.media_player import Features as Feat
from ucapi.media_player import MediaContentType, States

import fetchtv
from fetchtv import Box, FetchTvClient, Program

_LOG = logging.getLogger(__name__)

_PREFIX = "media_player."

ClientGetter = Callable[[], "FetchTvClient | None"]

FEATURES = [
    Feat.ON_OFF,
    Feat.TOGGLE,
    Feat.PLAY_PAUSE,
    Feat.STOP,
    Feat.FAST_FORWARD,
    Feat.REWIND,
    Feat.MUTE_TOGGLE,
    Feat.DPAD,
    Feat.NUMPAD,
    Feat.MENU,
    Feat.COLOR_BUTTONS,
    Feat.CHANNEL_SWITCHER,
    Feat.SELECT_SOURCE,
    Feat.RECORD,
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


def terminal_id_from_entity(entity_id_: str) -> str | None:
    """The box terminal id an entity id belongs to."""
    return entity_id_[len(_PREFIX):] if entity_id_.startswith(_PREFIX) else None


def box_cache_entry(box: Box) -> dict[str, Any]:
    """The persistable summary of a box."""
    return {"terminal_id": box.terminal_id, "name": box.name}


def build_entity(terminal_id: str, name: str, get_client: ClientGetter) -> MediaPlayer:
    """Build the media player for a box. State is filled in once the box reports."""
    return MediaPlayer(
        entity_id(terminal_id),
        name,
        FEATURES,
        {Attr.STATE: States.UNAVAILABLE},
        device_class=DeviceClasses.SET_TOP_BOX,
        options={Options.SIMPLE_COMMANDS: list(SIMPLE_COMMANDS)},
        cmd_handler=_make_cmd_handler(terminal_id, get_client),
    )


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

    attrs: dict[str, Any] = {
        Attr.STATE: _state(box),
        Attr.SOURCE_LIST: [c.label for c in channels],
        Attr.SOURCE: _source_label(channel, channels),
        Attr.MEDIA_TYPE: MediaContentType.CHANNEL if channel else MediaContentType.VIDEO,
        Attr.MEDIA_ARTIST: channel_name if channel_name != box.media_title or program else "",
        Attr.MEDIA_IMAGE_URL: (channel.logo if channel else None) or "",
    }
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
        if client is None or box is None or not client.connected:
            return StatusCodes.SERVICE_UNAVAILABLE
        try:
            return await _run_command(client, box, cmd_id, params or {})
        except fetchtv.NotConnectedError:
            return StatusCodes.SERVICE_UNAVAILABLE

    return _handler


async def _run_command(
    client: FetchTvClient, box: Box, cmd_id: str, params: dict[str, Any]
) -> StatusCodes:
    tid = box.terminal_id
    if cmd_id in _KEY_COMMANDS:
        await client.send_key(tid, _KEY_COMMANDS[cmd_id])
    elif cmd_id in _DIGITS:
        await client.send_keycode(tid, _DIGITS[cmd_id])
    elif cmd_id == Cmd.TOGGLE:
        await client.send_key(tid, fetchtv.KEY_POWER)
    elif cmd_id in (Cmd.ON, Cmd.OFF):
        # The box only has a power toggle - press it only if it changes something.
        want_standby = cmd_id == Cmd.OFF
        if box.standby is not want_standby:
            await client.send_key(tid, fetchtv.KEY_POWER)
    elif cmd_id == Cmd.SELECT_SOURCE:
        source = params.get("source")
        channel = next((c for c in client.channels_for(box) if c.label == source), None)
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
