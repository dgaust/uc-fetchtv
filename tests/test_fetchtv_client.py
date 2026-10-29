"""The FetchTV client against the fake service: sign-in, box state, guide, commands, reconnect."""
import asyncio

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import fetchtv
from fake_fetch import BEDROOM, LOUNGE, FakeFetch


async def wait_for(cond, timeout=5.0, what="condition"):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.02)


async def main():
    fake = FakeFetch()
    await fake.start()

    # Bad credentials raise AuthError from login().
    try:
        await fake.client(pin="0000").login()
        raise AssertionError("expected AuthError")
    except fetchtv.AuthError:
        pass

    client = fake.client()
    updates = []
    client.subscribe(lambda box: updates.append(box))
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.ready.wait(), 5)

    # Disabled terminals are skipped; names come from the account.
    assert set(client.boxes) == {LOUNGE, BEDROOM}, client.boxes
    lounge, bedroom = client.boxes[LOUNGE], client.boxes[BEDROOM]
    assert lounge.name == "Lounge"

    # Both boxes are asked ARE_YOU_ALIVE and answer with their state.
    await wait_for(lambda: lounge.has_state and bedroom.has_state)
    assert lounge.online and lounge.standby is False
    assert lounge.channel_id == "30411316" and lounge.play_state == "LIVE"
    assert bedroom.standby is True
    # HLS channel ids arrive as ints and are normalised to strings.
    assert bedroom.channel_id == "2319"
    assert None in updates and lounge in updates

    # Channel list: visible free-to-air channels (hidden one dropped), then
    # subscribed subscription channels only.
    labels = [c.label for c in client.channels_for(lounge)]
    assert labels == ["2 ABC TV", "23 ABC Entertains", "159 beIN SPORTS 3"], labels
    # Free-to-air channels get their logo from the guide.
    assert client.channels_for(lounge)[1].logo == "http://img/abc-ent.png"

    # A regional DVB id resolves to its guide channel and programme.
    channel = client.current_channel(lounge)
    assert channel.name == "ABC Entertains" and channel.epg_id == "411316"
    program = await client.refresh_program(lounge)
    assert program.title == "Hard Quiz" and program.episode_title == "Episode 19"
    assert program.synopsis == "A synopsis."
    assert client.cached_program(lounge).title == "Hard Quiz"
    # Listings are cached: a second lookup doesn't hit the API again.
    n = len([r for r in fake.http_requests if r[1] == "/v2/epg/programslist"])
    await client.refresh_program(lounge)
    assert len([r for r in fake.http_requests if r[1] == "/v2/epg/programslist"]) == n

    # A subscription channel's programme resolves too (pyfetchtv only did DVB).
    current = client.current_channel(bedroom)
    assert current.name == "Discovery Turbo", current  # not subscribed, but still named
    assert (await client.refresh_program(bedroom)).title == "Texas Metal"

    # Changing channel: PLAY_CHANNEL with an int id; the box's NOW_PLAYING
    # updates state, and CHANNEL_CHANGED / PLAYBACK_STOPPED trigger a
    # MEDIA_STATE request instead of being taken as "stopped".
    before = len(fake.received("MEDIA_STATE", LOUNGE))
    await client.play_channel(LOUNGE, "1850")
    await wait_for(lambda: lounge.channel_id == "1850")
    sent = fake.received("PLAY_CHANNEL", LOUNGE)[-1]["message"]
    assert sent["channelId"] == 1850 and sent["requiresSetTopBox"] is True
    await wait_for(lambda: len(fake.received("MEDIA_STATE", LOUNGE)) >= before + 2)
    assert lounge.play_state == "LIVE" and lounge.playback_type == "HLS"
    assert (await client.refresh_program(lounge)).title == "EFL Cup"

    # Remote keys go out as KEYEVENT with the key name.
    await client.send_key(LOUNGE, fetchtv.KEY_UP)
    await wait_for(lambda: fake.received("KEYEVENT", LOUNGE))
    assert fake.received("KEYEVENT", LOUNGE)[-1]["message"]["keyName"] == "KEY_UP"
    await client.send_keycode(LOUNGE, "7")
    await wait_for(lambda: fake.received("KEYCODE", LOUNGE))
    assert fake.received("KEYCODE", LOUNGE)[-1]["message"]["keyCode"] == ord("7")

    # Pause / unpause messages update the play state.
    await fake.push(LOUNGE, "PAUSED")
    await wait_for(lambda: lounge.play_state == "PAUSED")
    await fake.push(LOUNGE, "UNPAUSED")
    await wait_for(lambda: lounge.play_state == "PLAYING")

    # Messages from unknown terminals are ignored.
    await fake.push("999|unknown", "MEDIA_STATE", {"currentPlaybackMedia": {"channelId": "1"}})

    # The service dropping the socket: client goes disconnected, signs in
    # again and reconnects on its own.
    logins = fake.logins
    await fake.drop_connections()
    await wait_for(lambda: not client.connected)
    assert not lounge.online
    try:
        await client.send_key(LOUNGE, fetchtv.KEY_UP)
        raise AssertionError("expected NotConnectedError")
    except fetchtv.NotConnectedError:
        pass
    await wait_for(lambda: client.connected and lounge.online)
    assert fake.logins == logins + 1

    await client.stop()
    await asyncio.wait_for(task, 5)
    assert not client.connected
    await fake.stop()
    print("FETCHTV_CLIENT_TEST_PASSED")


asyncio.run(main())
