"""Stress: malformed, truncated, oversized and out-of-order messages from the service.

One bad message must never cost the connection (a reconnect signs in again
and re-asks every box for its full report), and a box with thousands of
recordings must still get its report through.
"""
import asyncio
import json
import logging

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import fetchtv
from fake_fetch import BEDROOM, LOUNGE, FakeFetch


async def wait_for(cond, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.02)


def msg(sender, typ, data):
    return json.dumps({"sender": sender, "message": {"type": typ, "data": data}})


# Each of these is valid JSON (or nearly) that the service might send. None may
# drop the connection or leave the client unable to handle the next message.
JUNK = [
    "[]", "null", "123", '"text"', "true", "{}", '{"message": null}', '{"message": "x"}',
    '{"message": [1, 2]}', '{"message": {}}', json.dumps({"sender": LOUNGE}),
    json.dumps({"sender": LOUNGE, "message": {"type": None}}),
    json.dumps({"sender": LOUNGE, "message": {"type": ["a", "list"]}}),
    json.dumps({"sender": LOUNGE, "message": {"type": {"a": 1}}}),
    json.dumps({"sender": [LOUNGE], "message": {"type": "MEDIA_STATE"}}),
    json.dumps({"sender": {"a": 1}, "message": {"type": "MEDIA_STATE"}}),
    "{not json", '{"sender": "' + LOUNGE, "", " ", "\x00\x01\x02",
    json.dumps({"sender": LOUNGE, "message": {"type": "I_AM_ALIVE", "data": None}}),
    msg(LOUNGE, "I_AM_ALIVE", [1, 2]),
    msg(LOUNGE, "I_AM_ALIVE", "text"),
    msg(LOUNGE, "I_AM_ALIVE", {"sysInfo": "text"}),
    msg(LOUNGE, "I_AM_ALIVE", {"sysInfo": [1]}),
    msg(LOUNGE, "I_AM_ALIVE", {"ftaChannels": 5}),
    msg(LOUNGE, "I_AM_ALIVE", {"ftaChannels": ["a", None, 3, [1], {"id": 1}, {"epg_id": 1}]}),
    msg(LOUNGE, "I_AM_ALIVE", {"ftaChannels": {"a": 1}}),
    msg(LOUNGE, "I_AM_ALIVE", {"recordings": 5}),
    msg(LOUNGE, "I_AM_ALIVE", {"recordings": ["a", None, 3, [1], {"id": 1}, {"name": "x"}]}),
    msg(LOUNGE, "I_AM_ALIVE", {"recordings": [{"id": 1, "name": "x", "programStartDate": "soon"}]}),
    msg(LOUNGE, "I_AM_ALIVE", {"recordings": [{"id": 1, "name": "x", "programStartDate": 10**30}]}),
    msg(LOUNGE, "I_AM_ALIVE", {"recordings": [{"id": 1, "name": "x", "programStartDate": -5}]}),
    msg(LOUNGE, "I_AM_ALIVE", {"recordings": [{"id": 1, "name": "x", "programStartDate": 1.5e12}]}),
    msg(LOUNGE, "I_AM_ALIVE", {"recordings": [{"id": 1, "name": "x", "programStartDate": [1]}]}),
    msg(LOUNGE, "I_AM_ALIVE", {"currentFutureRecordings": 5}),
    msg(LOUNGE, "I_AM_ALIVE", {"activeRecordings": 5}),
    msg(LOUNGE, "I_AM_ALIVE", {"activeRecordings": [[1], {"a": 1}]}),
    msg(LOUNGE, "I_AM_ALIVE", {"state": "text"}),
    msg(LOUNGE, "I_AM_ALIVE", {"state": [1]}),
    msg(LOUNGE, "I_AM_ALIVE", {"standby": "yes", "volume": "loud", "mute": [1], "hiddenChannels": 5}),
    msg(LOUNGE, "I_AM_ALIVE", {"hiddenChannels": [[1], {"a": 1}, None]}),
    msg(LOUNGE, "I_AM_ALIVE", {"volume": 1e999}),
    msg(LOUNGE, "I_AM_ALIVE", {"storageInfo": "full"}),
    msg(LOUNGE, "I_AM_ALIVE", {"storageInfo": {"freeSize": "lots", "hdTotalSize": [1]}}),
    msg(LOUNGE, "MEDIA_STATE", "text"),
    msg(LOUNGE, "MEDIA_STATE", {"settings": "text", "currentPlaybackMedia": "text"}),
    msg(LOUNGE, "MEDIA_STATE", {"settings": [1], "currentPlaybackMedia": [1]}),
    msg(LOUNGE, "MEDIA_STATE", {"currentPlaybackMedia": {"duration": "long", "channelId": [1]}}),
    msg(LOUNGE, "NOW_PLAYING", {"currentPlaybackMedia": {"mediaTitle": {"a": 1}, "playBackState": [1]}}),
    msg(LOUNGE, "VOLUME_SET", [1]),
    msg(LOUNGE, "VOLUME_SET", {"current": "loud"}),
    msg(LOUNGE, "VOLUME_SET", {"current": 1e999, "mute": None}),
    msg(LOUNGE, "VOLUME_SET", {"current": float("nan")}),
    msg(LOUNGE, "PAUSED", None),
    msg(LOUNGE, "SOMETHING_NEW", {"a": ["b"] * 100}),
    msg(LOUNGE, "RECORDINGS_UPDATE", "text"),
    msg("999|unknown", "I_AM_ALIVE", {}),
]


async def main():
    fake = FakeFetch()
    await fake.start()
    client = fake.client(reconnect_delay=0.05)
    seen = []

    def on_update(box):
        seen.append(box)

    client.subscribe(on_update)
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.ready.wait(), 5)
    lounge = client.boxes[LOUNGE]
    await wait_for(lambda: lounge.has_state)
    connections = fake.ws_connections
    logins = fake.logins

    # --- junk: one at a time, each followed by a good message --------------------
    dropped = []
    for i, junk in enumerate(JUNK):
        await fake.push_raw(junk)
        await fake.push(LOUNGE, "MEDIA_STATE", {"settings": {"volume": 10 + i % 50, "standby": False},
                                                "currentPlaybackMedia": {"channelId": "30411316", "mediaTitle": f"ok {i}",
                                                                         "playBackState": "LIVE", "playbackType": "DVB"}})
        try:
            await wait_for(lambda: lounge.media_title == f"ok {i}", timeout=2)
        except TimeoutError:
            dropped.append(i)
            await wait_for(lambda: client.connected and lounge.online, timeout=10)
    # The binary form of a good message is handled like the text one.
    await fake.push_raw(msg(LOUNGE, "MEDIA_STATE", {"currentPlaybackMedia": {"mediaTitle": "binary"}}).encode())
    await wait_for(lambda: lounge.media_title == "binary")
    # Invalid UTF-8 bytes are ignored.
    await fake.push_raw(b"\xff\xfe\xfd")
    await fake.push(LOUNGE, "MEDIA_STATE", {"currentPlaybackMedia": {"mediaTitle": "after bytes"}})
    await wait_for(lambda: lounge.media_title == "after bytes")

    # A list with some malformed entries keeps the good ones.
    good = {"id": 5, "name": "Keeper", "programStartDate": 1_799_000_000_000}
    await fake.push(LOUNGE, "I_AM_ALIVE", {
        "standby": False, "ftaChannels": ["x", None, {"id": "30018294", "epg_id": 18294, "name": "ABC TV", "number": 2}],
        "recordings": ["x", None, [1], good, {"id": 6, "name": "Also", "programStartDate": "soon"}],
        "currentFutureRecordings": 5, "activeRecordings": None, "state": {"channelId": "30018294", "mediaTitle": "mixed"}})
    await wait_for(lambda: lounge.media_title == "mixed")
    assert [r.title for r in lounge.recordings] == ["Keeper", "Also"], lounge.recordings
    assert [c.name for c in lounge.fta_channels] == ["ABC TV"] and lounge.scheduled_count == 0
    await fake.push(LOUNGE, "VOLUME_SET", {"current": float("inf")})
    await fake.push(LOUNGE, "MEDIA_STATE", {"currentPlaybackMedia": {"mediaTitle": "after inf"}})
    await wait_for(lambda: lounge.media_title == "after inf")
    assert lounge.volume != float("inf")

    assert not dropped, f"junk messages that broke message handling: {[JUNK[i][:90] for i in dropped]}"
    assert fake.ws_connections == connections and fake.logins == logins, (
        f"reconnected {fake.ws_connections - connections}x / signed in {fake.logins - logins}x because of bad messages")

    # --- an oversized message that is still valid --------------------------------
    await fake.push(LOUNGE, "SOMETHING_NEW", {"blob": "x" * (900 * 1024)})
    await fake.push(LOUNGE, "MEDIA_STATE", {"currentPlaybackMedia": {"mediaTitle": "after blob"}})
    await wait_for(lambda: lounge.media_title == "after blob")

    await client.stop()
    await asyncio.wait_for(task, 5)
    await fake.stop()
    print("STRESS_MESSAGES_PASSED")


logging.disable(logging.CRITICAL)
asyncio.run(main())
