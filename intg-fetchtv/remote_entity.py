"""
A remote entity per FetchTV box: the box's keys as commands, with a default
button mapping and screens for the Remote.

This is additional to the media player (see ``entities.py``), which it doesn't
touch. Where the media player offers the keys a media player has, this offers
every key the box accepts - the d-pad, guide, info, text, colours, number pad,
channel up/down - so activities can use them, and ships a default layout:

- **Buttons**: the Remote 3's d-pad, Back, channel rocker, Mute, Menu (guide,
  hold for menu), Record and the transport row. Home and Power are left to the
  Remote itself, and Back has no long press.
- **Pages**: Numbers (number pad, Text) and Keys (colours, Info, Exit) -
  everything the buttons don't cover, so nothing is repeated on screen.
- **State**: ON, or OFF in standby, and on / off / toggle press power only when
  that changes something.

:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
from typing import Any, Callable

from ucapi import StatusCodes
from ucapi.remote import Attributes, Commands, Features, Remote, States
from ucapi.ui import (
    Buttons,
    EntityCommand,
    Size,
    UiItem,
    UiPage,
    create_btn_mapping,
    create_ui_text,
)

import fetchtv
from fetchtv import Box, FetchTvClient

_PREFIX = "remote."

ClientGetter = Callable[[], "FetchTvClient | None"]

# The box's keys, under the usual command names.
KEYS = {
    "CURSOR_UP": fetchtv.KEY_UP,
    "CURSOR_DOWN": fetchtv.KEY_DOWN,
    "CURSOR_LEFT": fetchtv.KEY_LEFT,
    "CURSOR_RIGHT": fetchtv.KEY_RIGHT,
    "CURSOR_ENTER": fetchtv.KEY_SELECT,
    "BACK": fetchtv.KEY_BACK,
    "EXIT": fetchtv.KEY_EXIT,
    "MENU": fetchtv.KEY_MENU,
    "GUIDE": fetchtv.KEY_GUIDE,
    "INFO": fetchtv.KEY_INFO,
    "TEXT": fetchtv.KEY_TEXT,
    "PLAY_PAUSE": fetchtv.KEY_PLAY_PAUSE,
    "STOP": fetchtv.KEY_STOP,
    "FAST_FORWARD": fetchtv.KEY_FAST_FORWARD,
    "REWIND": fetchtv.KEY_REWIND,
    "RECORD": fetchtv.KEY_RECORD,
    "MUTE_TOGGLE": fetchtv.KEY_MUTE,
    "RED": fetchtv.KEY_RED,
    "GREEN": fetchtv.KEY_GREEN,
    "YELLOW": fetchtv.KEY_YELLOW,
    "BLUE": fetchtv.KEY_BLUE,
}
DIGITS = {f"DIGIT_{n}": str(n) for n in range(10)}
CHANNEL_STEPS = {"CHANNEL_UP": 1, "CHANNEL_DOWN": -1}
VOLUME_STEPS = {"VOLUME_UP": 5, "VOLUME_DOWN": -5}
START_OVER = "START_OVER"
# send_cmd with free text "TYPE:hello" types the text into the box (search boxes etc.).
TYPE_PREFIX = "TYPE:"

# Every command the remote offers (the Remote's configurator lists these).
SIMPLE_COMMANDS = [*KEYS, *DIGITS, *CHANNEL_STEPS, *VOLUME_STEPS, START_OVER]

# Pauses the box needs between key presses, in ms, when the Remote gives none.
DEFAULT_REPEAT_DELAY_MS = 150
DEFAULT_SEQUENCE_DELAY_MS = 300
MAX_REPEAT = 20


def entity_id(terminal_id: str) -> str:
    """Entity identifier for a box's remote."""
    return f"{_PREFIX}{terminal_id}"


def _button_mapping() -> list:
    """
    The Remote 3's physical buttons for the keys that suit them.

    Home and Power are left alone: they belong to the Remote itself. Back has a
    short press only. Volume and Voice are unmapped (a Fetch box has neither).
    """
    b = Buttons
    return [
        create_btn_mapping(b.BACK, "BACK"),
        create_btn_mapping(b.DPAD_UP, "CURSOR_UP"),
        create_btn_mapping(b.DPAD_DOWN, "CURSOR_DOWN"),
        create_btn_mapping(b.DPAD_LEFT, "CURSOR_LEFT"),
        create_btn_mapping(b.DPAD_RIGHT, "CURSOR_RIGHT"),
        create_btn_mapping(b.DPAD_MIDDLE, "CURSOR_ENTER"),
        create_btn_mapping(b.CHANNEL_UP, "CHANNEL_UP"),
        create_btn_mapping(b.CHANNEL_DOWN, "CHANNEL_DOWN"),
        create_btn_mapping(b.MUTE, "MUTE_TOGGLE"),
        create_btn_mapping(b.MENU, "GUIDE", "MENU"),
        create_btn_mapping(b.RECORD, "RECORD"),
        create_btn_mapping(b.PREV, "REWIND"),
        create_btn_mapping(b.PLAY, "PLAY_PAUSE"),
        create_btn_mapping(b.NEXT, "FAST_FORWARD"),
        create_btn_mapping(b.STOP, "STOP"),
    ]


def _pages() -> list[UiPage]:
    """
    The keys the buttons above don't cover, in two pages sized to fit.

    - **Numbers**: the number pad (three columns) and Text.
    - **Keys**: the four colour keys (the Remote 3 has no colour buttons),
      Info and Exit, and Start over (restart the programme on now).

    VOLUME_UP and VOLUME_DOWN are offered as commands for activities but not
    placed: the Remote 3's volume buttons normally control the TV or amplifier.
    """
    def text(label: str, cmd: str, x: int, y: int) -> UiItem:
        return create_ui_text(label, x, y, None, cmd)

    numbers = UiPage("numbers", "Numbers", grid=Size(3, 4), items=[
        *(text(str(n), f"DIGIT_{n}", (n - 1) % 3, (n - 1) // 3) for n in range(1, 10)),
        text("Text", "TEXT", 0, 3), text("0", "DIGIT_0", 1, 3),
    ])
    keys = UiPage("keys", "Keys", grid=Size(3, 3), items=[
        text("Red", "RED", 0, 0), text("Green", "GREEN", 1, 0), text("Yellow", "YELLOW", 2, 0),
        text("Blue", "BLUE", 0, 1), text("Info", "INFO", 1, 1), text("Exit", "EXIT", 2, 1),
        create_ui_text("Start over", 0, 2, Size(3, 1), START_OVER),
    ])
    return [numbers, keys]


def build_remote(terminal_id: str, name: str, get_client: ClientGetter) -> Remote:
    """Build the remote for a box. State is filled in once the box reports."""
    return Remote(
        entity_id(terminal_id),
        f"{name} remote",
        [Features.ON_OFF, Features.TOGGLE, Features.SEND_CMD],
        {Attributes.STATE: States.UNAVAILABLE},
        simple_commands=SIMPLE_COMMANDS,
        button_mapping=_button_mapping(),
        ui_pages=_pages(),
        icon="uc:tv",
        cmd_handler=_make_handler(terminal_id, get_client),
    )


def attributes(client: FetchTvClient | None, box: Box) -> dict[str, Any]:
    """Current remote attributes for a box."""
    if client is None or not client.connected or not box.online or not box.has_state:
        return {Attributes.STATE: States.UNAVAILABLE}
    return {Attributes.STATE: States.OFF if box.standby else States.ON}


def _make_handler(terminal_id: str, get_client: ClientGetter):
    async def _handler(
        _entity: Remote, cmd_id: str, params: dict[str, Any] | None, websocket: Any
    ) -> StatusCodes:
        client = get_client()
        box = client.boxes.get(terminal_id) if client else None
        if client is None or box is None or not client.connected:
            return StatusCodes.SERVICE_UNAVAILABLE
        try:
            return await _run(client, box, cmd_id, params or {})
        except fetchtv.NotConnectedError:
            return StatusCodes.SERVICE_UNAVAILABLE

    return _handler


def _valid(command: Any) -> bool:
    return command in SIMPLE_COMMANDS


def _is_typing(command: Any) -> bool:
    return isinstance(command, str) and command.startswith(TYPE_PREFIX)


def _count(value: Any, default: int) -> int:
    try:
        return max(1, min(MAX_REPEAT, int(value)))
    except (TypeError, ValueError):
        return default


def _delay_s(value: Any, default_ms: int) -> float:
    try:
        return max(0, int(value)) / 1000 if value is not None else default_ms / 1000
    except (TypeError, ValueError):
        return default_ms / 1000


async def _run(client: FetchTvClient, box: Box, cmd_id: str, params: dict[str, Any]) -> StatusCodes:
    tid = box.terminal_id
    if cmd_id in (Commands.ON, Commands.OFF):
        # The box only has a power toggle - press it only if it changes something.
        if box.standby is not (cmd_id == Commands.OFF):
            await client.send_key(tid, fetchtv.KEY_POWER)
        return StatusCodes.OK
    if cmd_id == Commands.TOGGLE:
        await client.send_key(tid, fetchtv.KEY_POWER)
        return StatusCodes.OK

    if cmd_id == Commands.SEND_CMD and _is_typing(params.get("command")):
        text = params["command"][len(TYPE_PREFIX):]
        if not text or len(text) > fetchtv.MAX_TYPED_CHARS or not (text.isascii() and text.isprintable()):
            return StatusCodes.BAD_REQUEST
        await client.type_text(box.terminal_id, text)
        return StatusCodes.OK

    if cmd_id == Commands.SEND_CMD:
        sequence = [params.get("command")]
        repeat = _count(params.get("repeat"), 1)
        delay = _delay_s(params.get("delay"), DEFAULT_REPEAT_DELAY_MS)
    elif cmd_id == Commands.SEND_CMD_SEQUENCE:
        sequence = params.get("sequence")
        if not isinstance(sequence, list) or not sequence:
            return StatusCodes.BAD_REQUEST
        repeat = _count(params.get("repeat"), 1)
        delay = _delay_s(params.get("delay"), DEFAULT_SEQUENCE_DELAY_MS)
    else:
        return StatusCodes.NOT_IMPLEMENTED

    # Check everything before pressing anything.
    if not all(_valid(c) for c in sequence):
        return StatusCodes.BAD_REQUEST
    # Each command is repeated in turn, as the Remote's send_cmd_sequence defines.
    presses = [c for c in sequence for _ in range(repeat)]
    tuned: list[fetchtv.Channel] = []
    done = True
    for i, command in enumerate(presses):
        if i:
            await asyncio.sleep(delay)
        done = await _press(client, box, command, tuned) and done
    return StatusCodes.OK if done else StatusCodes.SERVICE_UNAVAILABLE


async def _press(
    client: FetchTvClient, box: Box, command: str, tuned: list[fetchtv.Channel]
) -> bool:
    """Do one command. False if it couldn't be done (there was nothing to act on)."""
    if command in KEYS:
        await client.send_key(box.terminal_id, KEYS[command])
    elif command in DIGITS:
        await client.send_keycode(box.terminal_id, DIGITS[command])
    elif command == START_OVER:
        return await client.start_over_current(box)
    elif command in VOLUME_STEPS:
        await client.set_volume(box.terminal_id, (box.volume or 0) + VOLUME_STEPS[command])
    else:
        # Step from the channel this command last tuned: the box's own report
        # lags behind, so repeated presses would otherwise all pick the same one.
        channels = client.channels_for(box)
        if not channels:
            return False
        current = tuned[-1] if tuned else client.current_channel(box)
        ids = [c.id for c in channels]
        if current is None or current.id not in ids:
            channel = channels[0]
        else:
            channel = channels[(ids.index(current.id) + CHANNEL_STEPS[command]) % len(channels)]
        await client.play_channel(box.terminal_id, channel.id)
        tuned.append(channel)
    return True
