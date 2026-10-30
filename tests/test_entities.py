"""Mapping of box state to media-player attributes, and Remote commands to box commands."""
import asyncio
import os
import time

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import entities as ent
import fetchtv
from placeholder import CHANNEL_PLACEHOLDER
from fetchtv import Box, Channel, Guide, Program
from ucapi import StatusCodes
from ucapi.api_definitions import Paging
from ucapi.media_player import Attributes as Attr
from ucapi.media_player import BrowseOptions
from ucapi.media_player import Commands as Cmd
from ucapi.media_player import States
from ucapi.select import Attributes as SelAttr
from ucapi.select import Commands as SelCmd
from ucapi.select import States as SelStates

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

        # What is on now, so browsing needs no network. Relative to the real
        # clock because the browser asks for "now".
        now = int(time.time() * 1000)
        for key, title in (("1850", "EFL Cup"), ("411316", "Hard Quiz"), ("18294", "The Drum")):
            self._guide.set_programs(key, [Program(title=title, start_ms=now - 600_000,
                                                   end_ms=now + 1_200_000)])

    async def send_key(self, terminal_id, key):
        self.sent.append(("key", terminal_id, key))

    async def send_keycode(self, terminal_id, char):
        self.sent.append(("keycode", terminal_id, char))

    async def play_channel(self, terminal_id, channel_id):
        self.sent.append(("play", terminal_id, channel_id))

    async def play_recording(self, terminal_id, recording_id):
        self.sent.append(("recording", terminal_id, recording_id))


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
assert a[Attr.MEDIA_IMAGE_URL] == CHANNEL_PLACEHOLDER  # no public free-to-air logo
assert CHANNEL_PLACEHOLDER.startswith("data:image/png;base64,iVBOR")
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
assert a[Attr.MEDIA_IMAGE_URL] == "https://www.fetchtv.com.au/v2/epg/channels/1850/image"
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
# The names they had as simple commands in 0.2.0 still work.
run("GUIDE"); assert client.sent[-1][2] == "KEY_GUIDE"
run("INFO"); assert client.sent[-1][2] == "KEY_INFO"
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
client.connected = True


def run_select(cmd, params=None):
    return asyncio.run(select.command(cmd, params, websocket=None))


# Channel select: options mirror the source list, the current channel is selected.
sel_attrs = ent.select_attributes(client, make_box())
assert sel_attrs[SelAttr.STATE] == SelStates.ON
assert sel_attrs[SelAttr.OPTIONS] == ["2 ABC TV", "23 ABC Entertains", "159 beIN SPORTS 3"]
assert sel_attrs[SelAttr.CURRENT_OPTION] == "23 ABC Entertains"
assert ent.select_attributes(client, make_box(standby=True))[SelAttr.CURRENT_OPTION] == ""
assert ent.select_attributes(client, make_box(online=False)) == {SelAttr.STATE: SelStates.UNAVAILABLE}
assert ent.select_attributes(None, make_box()) == {SelAttr.STATE: SelStates.UNAVAILABLE}

select = ent.build_select(TID, "Lounge", lambda: client)
assert select.id == ent.select_entity_id(TID) and ent.terminal_id_from_entity(select.id) == TID
assert run_select(SelCmd.SELECT_OPTION, {"option": "159 beIN SPORTS 3"}) == StatusCodes.OK
assert client.sent[-1] == ("play", TID, "1850")
assert run_select(SelCmd.SELECT_OPTION, {"option": "nope"}) == StatusCodes.BAD_REQUEST
run_select(SelCmd.SELECT_FIRST); assert client.sent[-1] == ("play", TID, "30018294")
run_select(SelCmd.SELECT_LAST); assert client.sent[-1] == ("play", TID, "1850")
box = client.boxes[TID]
box.channel_id = "30411316"
run_select(SelCmd.SELECT_NEXT); assert client.sent[-1] == ("play", TID, "1850")
run_select(SelCmd.SELECT_PREVIOUS); assert client.sent[-1] == ("play", TID, "30018294")

# Playing a recording shows the recording, not the channel's live guide entry.
rec_box = make_box()
rec_box.apply_playback({"playbackType": "PVR", "channelId": "30411316", "recordingId": 55,
                        "duration": 3_862_000, "seasonNumber": "5", "episodeNumber": "17",
                        "mediaTitle": "Dirty Jobs", "playBackState": "PLAYING"})
rec_box.recordings = [fetchtv.Recording(id="55", title="Dirty Jobs", episode_title="Cedar Log Peeler",
                                        season="5", episode="17")]
a = ent.attributes(client, rec_box, Program(title="Live Show", start_ms=NOW - 1, end_ms=NOW + 1), NOW)
assert a[Attr.STATE] == States.PLAYING and a[Attr.MEDIA_TITLE] == "Dirty Jobs", a
assert a[Attr.MEDIA_ALBUM] == "S5 E17 · Cedar Log Peeler" and a[Attr.MEDIA_DURATION] == 3862, a
assert a[Attr.SOURCE] == "" and a[Attr.MEDIA_TYPE] == "episode", a
# A box playing another box's recording reports it differently, with no duration.
rec_box.apply_playback({"playbackType": "ENCRYPTED_VOD_STREAM", "baseType": "MULTIROOM_PVR",
                        "channelId": "30411316", "recordingId": 55, "mediaTitle": "Dirty Jobs",
                        "playBackState": "PLAYING"})
rec_box.recordings[0].start_ms, rec_box.recordings[0].end_ms = 1_000_000, 3_700_000
a = ent.attributes(client, rec_box, None, NOW)
assert a[Attr.MEDIA_TITLE] == "Dirty Jobs" and a[Attr.MEDIA_DURATION] == 2700 and a[Attr.SOURCE] == "", a
rec_box.apply_playback({"playbackType": "DVB", "channelId": "30411316", "mediaTitle": "ABC Entertains",
                        "playBackState": "LIVE"})
assert rec_box.recording_id == "" and rec_box.recording_duration_s == 0

# Media browser: root has the two folders; each lists channels with logos, paged.
def browse(media_id=None, page=1, limit=10):
    return asyncio.run(entity.browse(BrowseOptions(media_id=media_id, paging=Paging(page=page, limit=limit))))


root = browse()
assert [i.media_id for i in root.media.items] == ["fta", "subscription"], root
assert root.media.items[0].subtitle == "2 channels" and root.pagination.count == 2
fta = browse("fta")
assert [i.media_id for i in fta.media.items] == ["30018294", "30411316"]
assert fta.media.items[1].can_play
assert fta.media.items[1].thumbnail == CHANNEL_PLACEHOLDER, "no public logo -> placeholder"
sub = browse("subscription")
assert sub.media.items[0].thumbnail == "https://www.fetchtv.com.au/v2/epg/channels/1850/image"
# The subtitle is what is on now, not the channel number.
assert sub.media.items[0].subtitle == "EFL Cup", sub.media.items[0]
assert [i.subtitle for i in fta.media.items] == ["The Drum", "Hard Quiz"]
# With nothing in the guide it falls back to the channel number.
beins = client.channels_for(make_box())[-1]
assert ent._channel_item(beins, None).subtitle == "159"
assert ent._channel_item(Channel(id="9", epg_id="9", number=0, name="X"), None).subtitle is None
paged = browse("fta", page=2, limit=1)
assert [i.media_id for i in paged.media.items] == ["30411316"] and paged.pagination.count == 2
assert browse("nope") == StatusCodes.NOT_FOUND
assert run(Cmd.PLAY_MEDIA, {"media_id": "1850", "media_type": "channel"}) == StatusCodes.OK
assert client.sent[-1] == ("play", TID, "1850")
assert run(Cmd.PLAY_MEDIA, {"media_id": "nope"}) == StatusCodes.BAD_REQUEST

# Recordings: a third folder, newest first, with episode details and the channel's logo.
# The folder only appears when the box has recordings.
assert client.boxes[TID].recordings == []
assert [i.media_id for i in browse().media.items] == ["fta", "subscription"], "no recordings, no folder"
client.boxes[TID].recordings = [
    fetchtv.Recording(id="11", title="Hard Quiz", episode_title="Series Finale", season="6",
                      episode="12", channel_id="1850", start_ms=1_799_000_000_000),
    fetchtv.Recording(id="12", title="The Drum", start_ms=1_799_500_000_000),
]
root = browse()
assert [i.media_id for i in root.media.items] == ["fta", "subscription", "recordings"], root
assert root.media.items[2].subtitle == "2 recordings"
recs = browse("recordings")
assert [i.title for i in recs.media.items] == ["The Drum", "Hard Quiz"], recs
assert recs.pagination.count == 2
quiz = recs.media.items[1]
assert quiz.media_id == "recording:11" and quiz.can_play
assert quiz.subtitle.startswith("S6 E12 · Series Finale"), quiz.subtitle
assert quiz.thumbnail == "https://www.fetchtv.com.au/v2/epg/channels/1850/image"
assert recs.media.items[0].thumbnail == CHANNEL_PLACEHOLDER
assert browse("recordings", page=2, limit=1).media.items[0].title == "Hard Quiz"
# Playing a recording asks the box to play it; ids it doesn't have are refused.
assert run(Cmd.PLAY_MEDIA, {"media_id": "recording:11", "media_type": "episode"}) == StatusCodes.OK
assert client.sent[-1] == ("recording", TID, "11"), client.sent[-1]
assert run(Cmd.PLAY_MEDIA, {"media_id": "recording:999"}) == StatusCodes.BAD_REQUEST
client.boxes[TID].recordings = []

client.connected = False
assert browse() == StatusCodes.SERVICE_UNAVAILABLE
client.connected = False

assert asyncio.run(ent.build_entity(TID, "x", lambda: None).command(Cmd.STOP, None, websocket=None)) \
    == StatusCodes.SERVICE_UNAVAILABLE

print("ENTITIES_TEST_PASSED")
