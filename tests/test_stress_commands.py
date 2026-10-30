"""Stress: many rapid commands, concurrent browse/play/select, wake-then-play races and typing.

Covers a box that never wakes, the client being stopped while a wake or typing
task is pending, two texts typed at once, and the standby state flapping.
"""
import asyncio
import gc
import logging

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import entities as ent
import fetchtv
import remote_entity
from fake_fetch import BEDROOM, LOUNGE, FakeFetch
from ucapi import StatusCodes
from ucapi.api_definitions import Paging
from ucapi.media_player import BrowseOptions

fetchtv.WAKE_POLL_S = 0.05
fetchtv.TYPE_DELAY_S = 0.01
unretrieved: list[str] = []


async def wait_for(cond, timeout=20.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


async def idle(client):
    """Let background tasks finish; nothing may be left pending."""
    await wait_for(lambda: not client._tasks, timeout=10)


async def connected_client(fake, **kwargs):
    client = fake.client(**kwargs)
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.ready.wait(), 5)
    await wait_for(lambda: all(b.has_state for b in client.boxes.values()))
    return client, task


async def stop(client, task):
    await client.stop()
    await asyncio.wait_for(task, 5)


def types(fake, tid, *names):
    return [m["message"] for m in fake.ws_received if m["to"] == tid and m["message"]["type"] in names]


async def rapid_commands(fake):
    client, task = await connected_client(fake)
    fake.ws_received.clear()
    keys = [fetchtv.KEY_UP, fetchtv.KEY_DOWN, fetchtv.KEY_LEFT, fetchtv.KEY_RIGHT, fetchtv.KEY_SELECT]
    await asyncio.gather(*(client.send_key(LOUNGE, keys[i % 5]) for i in range(600)))
    await asyncio.gather(*(client.send_keycode(LOUNGE, str(i % 10)) for i in range(200)))
    await asyncio.gather(*(client.set_volume(LOUNGE, i) for i in range(150)))
    await wait_for(lambda: len(types(fake, LOUNGE, "KEYEVENT", "KEYCODE", "SET_VOLUME")) == 950, timeout=10)
    sent = types(fake, LOUNGE, "KEYEVENT")
    # Presses are delivered in the order they were issued.
    assert [m["keyName"] for m in sent] == [keys[i % 5] for i in range(600)]
    assert [m["volume"] for m in types(fake, LOUNGE, "SET_VOLUME")] == list(range(100)) + [100] * 50
    assert client.connected and client.boxes[LOUNGE].volume == 100
    await stop(client, task)


async def concurrent_calls(fake):
    """Browse, play and select at the same time, through the entity handlers."""
    client, task = await connected_client(fake)
    lounge = client.boxes[LOUNGE]
    media = ent._make_cmd_handler(LOUNGE, lambda: client)
    select = ent._make_select_handler(LOUNGE, lambda: client)
    remote = remote_entity._make_handler(LOUNGE, lambda: client)
    fake.ws_received.clear()

    async def browse(media_id, page):
        return await ent.browse_channels(client, lounge, BrowseOptions(media_id=media_id, paging=Paging(page=page, limit=2)))

    jobs = []
    for i in range(60):
        jobs += [
            media(None, "select_source", {"source": "159 beIN SPORTS 3"}, None),
            media(None, "play_media", {"media_id": "recording:11"}, None),
            media(None, "play_media", {"media_id": "1850"}, None),
            media(None, "channel_up", None, None),
            media(None, "volume_up", None, None),
            media(None, "START_OVER", None, None),
            select(None, "select_next", None, None),
            select(None, "select_option", {"option": "2 ABC TV"}, None),
            remote(None, "send_cmd", {"command": "CHANNEL_DOWN", "repeat": 2, "delay": 0}, None),
            remote(None, "send_cmd_sequence", {"sequence": ["GUIDE", "CURSOR_UP"], "delay": 0}, None),
            browse("recordings", 1 + i % 2), browse("subscription", 1), browse("fta", 1), browse(None, 1),
        ]
    results = await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), 30)
    bad = [r for r in results if isinstance(r, BaseException)]
    assert not bad, bad[:3]
    codes = {r for r in results if isinstance(r, StatusCodes)}
    assert codes <= {StatusCodes.OK, StatusCodes.SERVICE_UNAVAILABLE}, codes
    assert client.connected
    await idle(client)
    # Browsing the recordings while stale asks the box again: not once per caller.
    asks = len(types(fake, LOUNGE, "ARE_YOU_ALIVE"))
    assert asks <= 2, f"{asks} full reports requested for concurrent browses"
    await stop(client, task)


async def wake_races(fake):
    bedroom_standby = lambda: fake.boxes[BEDROOM].__setitem__("standby", True)
    client, task = await connected_client(fake)
    bedroom = client.boxes[BEDROOM]
    bedroom_standby()
    await fake.push(BEDROOM, "MEDIA_STATE", {"settings": fake._settings(BEDROOM), "currentPlaybackMedia": fake.boxes[BEDROOM]["media"]})
    await wait_for(lambda: bedroom.standby is True)

    # Twenty plays at once on a sleeping box: the calls return at once, every play is
    # delivered once the box is awake, in the order they were made.
    fake.ws_received.clear()
    await asyncio.gather(*(client.play_channel(BEDROOM, "1850" if i % 2 else "2319") for i in range(20)))
    # (the calls returned before any play was sent: those wait for the box to wake)
    assert not types(fake, BEDROOM, "PLAY_CHANNEL"), "play_channel waited for the wake"
    await idle(client)
    plays = types(fake, BEDROOM, "PLAY_CHANNEL")
    assert [m["channelId"] for m in plays] == [1850 if i % 2 else 2319 for i in range(20)], [m["channelId"] for m in plays]
    wakes = len(types(fake, BEDROOM, "WAKE_STB"))
    assert 1 <= wakes <= 20, wakes
    assert bedroom.standby is False
    # Nothing else was sent to the other box.
    assert not types(fake, LOUNGE, "WAKE_STB", "PLAY_CHANNEL")

    # A press racing with a wake: the box falls asleep again while the plays are queued.
    bedroom_standby()
    await fake.push(BEDROOM, "MEDIA_STATE", {"settings": fake._settings(BEDROOM), "currentPlaybackMedia": fake.boxes[BEDROOM]["media"]})
    await wait_for(lambda: bedroom.standby is True)
    fake.ws_received.clear()
    await client.play_recording(BEDROOM, "11")
    await client.send_key(BEDROOM, fetchtv.KEY_UP)  # a key is not held back by the wake
    await idle(client)
    assert [m["type"] for m in types(fake, BEDROOM, "KEYEVENT", "PLAY_ASSET", "WAKE_STB")][0] in ("KEYEVENT", "WAKE_STB")
    assert len(types(fake, BEDROOM, "PLAY_ASSET")) == 1

    # A box that never wakes: the play still goes out after the wait, and nothing is left behind.
    fake.wakes = False
    bedroom_standby()
    await fake.push(BEDROOM, "MEDIA_STATE", {"settings": fake._settings(BEDROOM), "currentPlaybackMedia": fake.boxes[BEDROOM]["media"]})
    await wait_for(lambda: bedroom.standby is True)
    fake.ws_received.clear()
    saved = fetchtv.WAKE_TIMEOUT_S
    fetchtv.WAKE_TIMEOUT_S = 0.4
    try:
        # wake()'s default timeout is bound when the method is defined, so pass it through _play's path:
        assert await client.wake(BEDROOM, timeout_s=0.3) is False
        await client.play_channel(BEDROOM, "1850")
        await idle(client)
    finally:
        fetchtv.WAKE_TIMEOUT_S = saved
    assert len(types(fake, BEDROOM, "PLAY_CHANNEL")) == 1
    assert len(types(fake, BEDROOM, "MEDIA_STATE")) >= 2, "the box is asked for its state while waiting"
    fake.wakes = True
    await stop(client, task)


async def stopped_while_pending(fake):
    # Stopped while a wake is pending: the play is never sent, the task is gone, no error is left over.
    fake.wakes = False
    client, task = await connected_client(fake)
    bedroom = client.boxes[BEDROOM]
    fake.boxes[BEDROOM]["standby"] = True
    await fake.push(BEDROOM, "MEDIA_STATE", {"settings": fake._settings(BEDROOM), "currentPlaybackMedia": fake.boxes[BEDROOM]["media"]})
    await wait_for(lambda: bedroom.standby is True)
    fake.ws_received.clear()
    await client.play_channel(BEDROOM, "1850")
    await client.play_recording(BEDROOM, "11")
    await client.type_text(LOUNGE, "a" * 64)
    await wait_for(lambda: types(fake, BEDROOM, "WAKE_STB") and types(fake, LOUNGE, "KEYCODE"))
    assert client._tasks
    await stop(client, task)
    await asyncio.sleep(0.1)
    assert not client._tasks, "tasks left after stop()"
    keycodes = len(types(fake, LOUNGE, "KEYCODE"))
    await asyncio.sleep(0.3)
    assert len(types(fake, LOUNGE, "KEYCODE")) == keycodes, "typing went on after stop()"
    assert keycodes < 64
    assert not types(fake, BEDROOM, "PLAY_CHANNEL", "PLAY_ASSET"), "a play went out after stop()"
    gc.collect()
    assert not unretrieved, unretrieved
    fake.wakes = True

    # Stopped before the wake task has even started: plays are not held for ever by a dead wake.
    client, task = await connected_client(fake)
    fake.boxes[BEDROOM]["standby"] = True
    await fake.push(BEDROOM, "MEDIA_STATE", {"settings": fake._settings(BEDROOM), "currentPlaybackMedia": fake.boxes[BEDROOM]["media"]})
    await wait_for(lambda: client.boxes[BEDROOM].standby is True)
    await client.play_channel(BEDROOM, "1850")  # (no await between this and stop(): the task never runs)
    await client.stop()
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(0.05)
    assert not client._pending_plays and not client._tasks, client._pending_plays
    fake.wakes = True
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.ready.wait(), 5)
    await wait_for(lambda: client.boxes[BEDROOM].standby is True)
    fake.ws_received.clear()
    await client.play_channel(BEDROOM, "2319")
    await wait_for(lambda: types(fake, BEDROOM, "PLAY_CHANNEL"))
    await stop(client, task)
    fake.wakes = False

    # Connection lost while typing and while a wake is pending: both end quietly.
    client, task = await connected_client(fake, reconnect_delay=5.0)
    fake.wakes = False
    fake.boxes[BEDROOM]["standby"] = True
    await fake.push(BEDROOM, "MEDIA_STATE", {"settings": fake._settings(BEDROOM), "currentPlaybackMedia": fake.boxes[BEDROOM]["media"]})
    await wait_for(lambda: client.boxes[BEDROOM].standby is True)
    await client.play_channel(BEDROOM, "1850")
    await client.type_text(LOUNGE, "b" * 64)
    await asyncio.sleep(0.1)
    await fake.drop_connections(code=1011)
    await wait_for(lambda: not client.connected)
    await idle(client)
    gc.collect()
    assert not unretrieved, unretrieved
    fake.wakes = True
    await stop(client, task)


async def typing(fake):
    client, task = await connected_client(fake)
    # Two texts typed at once come out one after the other, not mixed.
    fake.ws_received.clear()
    await client.type_text(LOUNGE, "aaaaaaaa")
    await client.type_text(LOUNGE, "bbbbbbbb")
    await idle(client)
    typed = "".join(chr(m["keyCode"]) for m in types(fake, LOUNGE, "KEYCODE"))
    assert typed == "aaaaaaaabbbbbbbb", f"texts were mixed: {typed!r}"
    # Over the cap, the extra is dropped.
    fake.ws_received.clear()
    await client.type_text(LOUNGE, "x" * 500)
    await idle(client)
    assert len(types(fake, LOUNGE, "KEYCODE")) == fetchtv.MAX_TYPED_CHARS
    # What the remote entity lets through.
    remote = remote_entity._make_handler(LOUNGE, lambda: client)
    fake.ws_received.clear()
    for text, code in (("hello world", StatusCodes.OK), ("", StatusCodes.BAD_REQUEST), ("x" * 65, StatusCodes.BAD_REQUEST),
                       ("café", StatusCodes.BAD_REQUEST), ("a\nb", StatusCodes.BAD_REQUEST), ("a\x00b", StatusCodes.BAD_REQUEST),
                       ("日本", StatusCodes.BAD_REQUEST), ("a‮b", StatusCodes.BAD_REQUEST), ("!@#$%^&*()", StatusCodes.OK)):
        assert await remote(None, "send_cmd", {"command": "TYPE:" + text}, None) == code, repr(text)
    await idle(client)
    typed = "".join(chr(m["keyCode"]) for m in types(fake, LOUNGE, "KEYCODE"))
    assert typed == "hello world!@#$%^&*()", typed
    await stop(client, task)


async def standby_flapping(fake):
    """The box flips between standby and awake faster than anything can follow."""
    client, task = await connected_client(fake)
    lounge = client.boxes[LOUNGE]
    updates = []
    client.subscribe(lambda box: updates.append(box))
    async def push_state(i):
        asleep = i % 2 == 0
        await fake.push(LOUNGE, "MEDIA_STATE", {"settings": {"standby": asleep, "volume": i % 100, "mute": asleep},
                                                "currentPlaybackMedia": {"channelId": "30411316", "mediaTitle": f"t{i}",
                                                                         "playBackState": "IDLE" if asleep else "LIVE"}})

    for i in range(300):
        await push_state(i)
        if i % 7 == 0:
            await client.play_channel(LOUNGE, "1850")
        if i % 11 == 0:
            await fake.push(LOUNGE, "PAUSED")
    # The plays above make the fake answer with the box's own state (and the client asks again after each
    # channel change), which on a slow machine can land after the last push. Let all of that settle -
    # the client sends nothing more for a while - and then the last state is the last word.
    await idle(client)
    seen = -1
    while seen != len(fake.ws_received):
        seen = len(fake.ws_received)
        await asyncio.sleep(0.5)
    await push_state(299)
    await wait_for(lambda: lounge.media_title == "t299")
    assert lounge.standby is False and lounge.volume == 299 % 100 and lounge.muted is False
    assert client.connected and len(updates) >= 300
    await stop(client, task)


async def main():
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: unretrieved.append(str(context.get("message"))))
    fake = FakeFetch()
    await fake.start()
    for part in (rapid_commands, concurrent_calls, wake_races, stopped_while_pending, typing, standby_flapping):
        fake.boxes[BEDROOM]["standby"] = True
        fake.wakes = True
        await part(fake)
    await fake.stop()
    assert not unretrieved, unretrieved
    print("STRESS_COMMANDS_PASSED")


logging.disable(logging.CRITICAL)
asyncio.run(asyncio.wait_for(main(), 120))
