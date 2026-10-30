"""The remote entity: state, power, send_cmd / send_cmd_sequence, and the default layout."""
import asyncio
import os

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import remote_entity as rem
from fetchtv import Box, Channel, FetchTvClient
from ucapi import StatusCodes
from ucapi.remote import Attributes, States

TID = "111|aa"


class FakeClient(FetchTvClient):
    """No network: records what would be sent."""

    def __init__(self):
        super().__init__("code", "pin")
        self.connected = True
        self.sent: list[tuple] = []

    async def send_key(self, terminal_id, key):
        self.sent.append(("key", key))

    async def send_keycode(self, terminal_id, char):
        self.sent.append(("keycode", char))

    async def send_raw(self, terminal_id, msg_type, extra):
        self.sent.append(("raw", msg_type, extra))

    async def play_channel(self, terminal_id, channel_id):
        self.sent.append(("play", channel_id))
        box.channel_id = box.channel_id  # the box's own report lags behind, so don't move it

    def channels_for(self, box):
        return CHANNELS


CHANNELS = [Channel(id=str(i), epg_id=str(i), number=i, name=f"Ch {i}") for i in (2, 7, 9, 10)]
box = Box(terminal_id=TID, name="Lounge", online=True, standby=False)
box.channel_id = "7"
client = FakeClient()
client.boxes = {TID: box}
remote = rem.build_remote(TID, "Lounge", lambda: client)


def run(cmd, params=None):
    return asyncio.run(remote.command(cmd, params, websocket=None))


# State follows the box.
assert rem.attributes(client, box) == {Attributes.STATE: States.ON}
box.standby = True
assert rem.attributes(client, box) == {Attributes.STATE: States.OFF}
box.standby = False
assert rem.attributes(client, Box(TID, "x", online=False, standby=False)) == {Attributes.STATE: States.UNAVAILABLE}
assert rem.attributes(None, box) == {Attributes.STATE: States.UNAVAILABLE}

# Power: on/off press the toggle only when it changes something.
assert run("on") == StatusCodes.OK and client.sent == []
assert run("off") == StatusCodes.OK and client.sent == [("key", "KEY_POWER")]
box.standby = True
client.sent.clear()
run("off"); assert client.sent == []
run("on"); assert client.sent == [("key", "KEY_POWER")]
run("toggle"); assert client.sent[-1] == ("key", "KEY_POWER")
box.standby = False
client.sent.clear()

# send_cmd: one key, a repeat, a digit.
assert run("send_cmd", {"command": "GUIDE"}) == StatusCodes.OK and client.sent == [("key", "KEY_GUIDE")]
client.sent.clear()
run("send_cmd", {"command": "CURSOR_DOWN", "repeat": 3, "delay": 1})
assert client.sent == [("key", "KEY_DOWN")] * 3
client.sent.clear()
run("send_cmd", {"command": "DIGIT_7"}); assert client.sent == [("keycode", "7")]
client.sent.clear()
run("send_cmd", {"command": "RED"}); run("send_cmd", {"command": "MUTE_TOGGLE"})
assert client.sent == [("key", "KEY_RED"), ("key", "KEY_MUTE")]

# Unknown commands are rejected before anything is pressed.
client.sent.clear()
assert run("send_cmd", {"command": "NOPE"}) == StatusCodes.BAD_REQUEST
assert run("send_cmd", {}) == StatusCodes.BAD_REQUEST
assert run("send_cmd_sequence", {"sequence": ["GUIDE", "NOPE"], "delay": 1}) == StatusCodes.BAD_REQUEST
assert run("send_cmd_sequence", {"sequence": []}) == StatusCodes.BAD_REQUEST
assert client.sent == []

# send_cmd_sequence: in order, each command repeated in turn.
assert run("send_cmd_sequence", {"sequence": ["GUIDE", "CURSOR_DOWN", "CURSOR_ENTER"], "delay": 1}) == StatusCodes.OK
assert client.sent == [("key", "KEY_GUIDE"), ("key", "KEY_DOWN"), ("key", "KEY_SELECT")]
client.sent.clear()
run("send_cmd_sequence", {"sequence": ["DIGIT_1", "DIGIT_2"], "repeat": 2, "delay": 1})
assert client.sent == [("keycode", "1"), ("keycode", "1"), ("keycode", "2"), ("keycode", "2")]

# Channel up/down step from the channel they last tuned, not the box's lagging report.
client.sent.clear()
run("send_cmd", {"command": "CHANNEL_UP", "repeat": 2, "delay": 1})
assert client.sent == [("play", "9"), ("play", "10")], client.sent
client.sent.clear()
run("send_cmd", {"command": "CHANNEL_DOWN"}); assert client.sent == [("play", "2")]  # box is on 7
box.channel_id = "2"
client.sent.clear()
run("send_cmd", {"command": "CHANNEL_DOWN"}); assert client.sent == [("play", "10")]  # wraps

# Repeats are capped, so a bad request can't hammer the box.
client.sent.clear()
run("send_cmd", {"command": "CURSOR_UP", "repeat": 5000, "delay": 0})
assert len(client.sent) == rem.MAX_REPEAT

# Unsupported command, and a client that is down.
assert run("seek") == StatusCodes.NOT_IMPLEMENTED
client.connected = False
assert run("send_cmd", {"command": "GUIDE"}) == StatusCodes.SERVICE_UNAVAILABLE
client.connected = True

# The default layout: every command it uses exists, nothing sits off the grid or overlaps.
options = remote.options
commands = set(options["simple_commands"])
assert {"GUIDE", "INFO", "TEXT", "EXIT", "DIGIT_0", "DIGIT_9", "CHANNEL_UP", "RED", "BLUE"} <= commands
assert not commands & {"on", "off", "toggle", "send_cmd", "send_cmd_sequence"}, "reserved names"
assert all(len(c) <= 20 and " " not in c for c in commands)

buttons = {m["button"]: m for m in options["button_mapping"]}
# Home and Power belong to the Remote; Back has a short press only.
assert "HOME" not in buttons and "POWER" not in buttons
assert buttons["BACK"]["short_press"]["cmd_id"] == "BACK" and not buttons["BACK"].get("long_press")
assert buttons["DPAD_MIDDLE"]["short_press"]["cmd_id"] == "CURSOR_ENTER"
for m in buttons.values():
    for press in (m.get("short_press"), m.get("long_press")):
        if press:
            assert press["cmd_id"] in commands or press["cmd_id"] == "toggle", press

pages = options["user_interface"]["pages"]
assert [p["page_id"] for p in pages] == ["numbers", "keys"]
assert next(p for p in pages if p["page_id"] == "numbers")["grid"]["width"] == 3, "number pad is three columns"
assert buttons["MENU"]["short_press"]["cmd_id"] == "GUIDE" and buttons["MENU"]["long_press"]["cmd_id"] == "MENU"
# Nothing a physical button already does is repeated on screen...
mapped = {press["cmd_id"] for m in buttons.values()
          for press in (m.get("short_press"), m.get("long_press")) if press}
on_screen = {i["command"]["cmd_id"] for p in pages for i in p["items"]}
assert not on_screen & mapped, on_screen & mapped
# ...and nothing is left off both: every command is on a button or on the screen.
assert commands <= mapped | on_screen, commands - mapped - on_screen
for page in pages:
    grid = page["grid"]
    cells = [(i["location"]["x"], i["location"]["y"]) for i in page["items"]]
    assert len(cells) == len(set(cells)), f"{page['page_id']} has overlapping items"
    assert all(0 <= x < grid["width"] and 0 <= y < grid["height"] for x, y in cells), page["page_id"]
    assert all(i["command"]["cmd_id"] in commands for i in page["items"]), page["page_id"]

# Experiments (working out how to play a recording): off unless enabled, and
# only a play message can be sent this way.
client.sent.clear()
exp = 'EXPERIMENT PLAY_ASSET {"assetId": 5}'
os.environ.pop("FETCHTV_EXPERIMENTAL", None)
assert run("send_cmd", {"command": exp}) == StatusCodes.BAD_REQUEST and client.sent == []
os.environ["FETCHTV_EXPERIMENTAL"] = "1"
assert run("send_cmd", {"command": exp}) == StatusCodes.OK
assert client.sent == [("raw", "PLAY_ASSET", {"assetId": 5})], client.sent
for bad in ('EXPERIMENT PENDING_DELETE_RECORDINGS_BY_ID {"recordingIds": [1]}',
            'EXPERIMENT PLAY_ASSET nope', 'EXPERIMENT PLAY_ASSET [1]'):
    assert run("send_cmd", {"command": bad}) == StatusCodes.BAD_REQUEST, bad
assert len(client.sent) == 1
os.environ.pop("FETCHTV_EXPERIMENTAL")

print("REMOTE_ENTITY_TEST_PASSED")
