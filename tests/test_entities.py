"""Mapping of box state to media-player attributes, and Remote commands to box commands."""
import asyncio

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import entities as ent
import fetchtv
from fetchtv import Box, Channel, Guide, Program
from ucapi import StatusCodes
from ucapi.media_player import Attributes as Attr
from ucapi.media_player import Commands as Cmd
from ucapi.media_player import States

TID = "111|aa"
NOW = 1_800_000_000_000


class FakeClient(fetchtv.FetchTvClient):
    """A client with a guide but no network; records commands instead of sending."""

    def __init__(self):
        super().__init__("code", "pin")
        self.connected = True
        self.sent: list[tuple] = []
        self._guide = Guide({
            "1850": {"name": "beIN SPORTS 3", "number": 159, "subscribed": True,
                     "sources": [{"id": 1850, "type": "hls"}],
                     "images": {"channel_logo_focus": {"original": "http://img/bein3.png"}}},
            "411316": {"name": "ABC Entertains", "number": 23,
                       "sources": [{"id": "30411316", "type": "dvb"}],
                       "images": {"channel_logo_focus": {"original": "http://img/abc-ent.png"}}},
        })

    async def send_key(self, terminal_id, key):
        self.sent.append(("key", terminal_id, key))

    async def send_keycode(self, terminal_id, char):
        self.sent.append(("keycode", terminal_id, char))

    async def play_channel(self, terminal_id, channel_id):
        self.sent.append(("play", terminal_id, channel_id))


def make_box(**kw) -> Box:
    box = Box(terminal_id=TID, name="Lounge", online=True, standby=False)
    box.fta_channels = [
        Channel(id="30018294", epg_id="18294", number=2, name="ABC TV"),
        Channel(id="30411316", epg_id="411316", number=23, name="ABC Entertains"),
    ]
    box.apply_playback({"playbackType": "DVB", "channelId": "30411316",
                        "mediaTitle": "ABC Entertains", "playBackState": "LIVE"})
    for k, v in kw.items():
        setattr(box, k, v)
    return box


client = FakeClient()
program = Program(title="Hard Quiz", start_ms=NOW - 600_000, end_ms=NOW + 1_200_000,
                  episode_title="Episode 19", genre="Quiz")

# Live TV with a guide entry: programme as title, channel as artist, logo as art.
a = ent.attributes(client, make_box(), program, NOW)
assert a[Attr.STATE] == States.PLAYING, a
assert a[Attr.MEDIA_TITLE] == "Hard Quiz"
assert a[Attr.MEDIA_ARTIST] == "ABC Entertains"
assert a[Attr.MEDIA_ALBUM] == "Episode 19"
assert a[Attr.MEDIA_IMAGE_URL] == "http://img/abc-ent.png"
assert a[Attr.MEDIA_DURATION] == 1800 and a[Attr.MEDIA_POSITION] == 600
assert a[Attr.SOURCE] == "23 ABC Entertains"
assert a[Attr.SOURCE_LIST] == ["2 ABC TV", "23 ABC Entertains", "159 beIN SPORTS 3"]

# Subscription channel without a guide entry: the box's own title, no duplicate artist.
box = make_box()
box.apply_playback({"playbackType": "HLS", "channelId": 1850, "mediaTitle": "beIN SPORTS 3",
                    "playBackState": "LIVE"})
a = ent.attributes(client, box, None, NOW)
assert a[Attr.MEDIA_TITLE] == "beIN SPORTS 3" and a[Attr.MEDIA_ARTIST] == "", a
assert a[Attr.SOURCE] == "159 beIN SPORTS 3"
assert a[Attr.MEDIA_IMAGE_URL] == "http://img/bein3.png"
assert a[Attr.MEDIA_DURATION] == 0

# Paused / idle / standby / unreachable.
box.play_state = "PAUSED"
assert ent.attributes(client, box, program, NOW)[Attr.STATE] == States.PAUSED
box.play_state = "IDLE"
assert ent.attributes(client, box, program, NOW)[Attr.STATE] == States.ON
assert ent.attributes(client, make_box(standby=True), program, NOW)[Attr.STATE] == States.OFF
assert ent.attributes(client, make_box(online=False), program, NOW)[Attr.STATE] == States.UNAVAILABLE
assert ent.attributes(client, make_box(standby=None), program, NOW)[Attr.STATE] == States.UNAVAILABLE
assert ent.attributes(None, make_box(), program, NOW)[Attr.STATE] == States.UNAVAILABLE

# Commands.
box = make_box()
client.boxes = {TID: box}
entity = ent.build_entity(TID, "Lounge", lambda: client)
assert entity.id == ent.entity_id(TID) and ent.terminal_id_from_entity(entity.id) == TID


def run(cmd, params=None):
    return asyncio.run(entity.command(cmd, params, websocket=None))


assert run(Cmd.CURSOR_UP) == StatusCodes.OK and client.sent[-1] == ("key", TID, "KEY_UP")
run(Cmd.CURSOR_ENTER); assert client.sent[-1][2] == "KEY_SELECT"
run(Cmd.FUNCTION_RED); assert client.sent[-1][2] == "KEY_RED"
run("EXIT"); assert client.sent[-1][2] == "KEY_EXIT"
run(Cmd.GUIDE); assert client.sent[-1][2] == "KEY_GUIDE"
run(Cmd.INFO); assert client.sent[-1][2] == "KEY_INFO"
run(Cmd.DIGIT_7); assert client.sent[-1] == ("keycode", TID, "7")
assert run(Cmd.SELECT_SOURCE, {"source": "159 beIN SPORTS 3"}) == StatusCodes.OK
assert client.sent[-1] == ("play", TID, "1850")
assert run(Cmd.SELECT_SOURCE, {"source": "nope"}) == StatusCodes.BAD_REQUEST

# Channel up/down step through the list and wrap.
run(Cmd.CHANNEL_UP); assert client.sent[-1] == ("play", TID, "1850")
run(Cmd.CHANNEL_DOWN); assert client.sent[-1] == ("play", TID, "30018294")
box.channel_id = "1850"
run(Cmd.CHANNEL_UP); assert client.sent[-1] == ("play", TID, "30018294")

# Power: ON/OFF only press the toggle when it changes something.
n = len(client.sent)
run(Cmd.ON); assert len(client.sent) == n  # already on
run(Cmd.OFF); assert client.sent[-1] == ("key", TID, "KEY_POWER")
box.standby = True
n = len(client.sent)
run(Cmd.OFF); assert len(client.sent) == n
run(Cmd.ON); assert client.sent[-1] == ("key", TID, "KEY_POWER")
run(Cmd.TOGGLE); assert client.sent[-1] == ("key", TID, "KEY_POWER")

assert run(Cmd.SEEK) == StatusCodes.NOT_IMPLEMENTED
client.connected = False
assert run(Cmd.CURSOR_UP) == StatusCodes.SERVICE_UNAVAILABLE
assert asyncio.run(ent.build_entity(TID, "x", lambda: None).command(Cmd.STOP, None, websocket=None)) \
    == StatusCodes.SERVICE_UNAVAILABLE

print("ENTITIES_TEST_PASSED")
