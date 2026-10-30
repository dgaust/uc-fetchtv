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

    # Requests look like the Fetch Android app's: its capabilities string and
    # User-Agent, and the device id the client was given.
    assert fake.http_headers, "no HTTP requests seen"
    lower = lambda h: {k.lower(): v for k, v in h.items()}  # header names are case-insensitive
    for h in map(lower, fake.http_headers):
        assert h["x-ftv-capabilities"] == "no_pin,android,v3.33.2.6562,tenplay_v2", h
        assert h["user-agent"] == "okhttp/4.12.0" and h["x-ftv-deviceid"] == "uc-intg-fetchtv", h
        assert h["x-ftv-timeout"] == "30", h
    # The websocket is opened as the app does: the cookie in the URL, the
    # capabilities and device id as headers, no Cookie header and no User-Agent.
    assert fake.ws_paths and all("?auth=secret-token" in p for p in fake.ws_paths), fake.ws_paths
    ws_h = lower(fake.ws_headers[-1])
    assert ws_h["x-ftv-capabilities"] == "no_pin,android,v3.33.2.6562,tenplay_v2", ws_h
    assert ws_h["x-ftv-deviceid"] == "uc-intg-fetchtv" and "cookie" not in ws_h, ws_h
    assert "user-agent" not in ws_h, ws_h
    own = fake.client(device_id="0123456789abcdef")
    await own.login()
    assert any(lower(h)["x-ftv-deviceid"] == "0123456789abcdef" for h in fake.http_headers)

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

    # Recordings arrive in the full report; the one pending deletion is dropped,
    # and a time in milliseconds is kept as is.
    assert [(r.title, r.label) for r in lounge.recordings] == [
        ("Hard Quiz", "S6 E12 · Series Finale"), ("The Drum", "")], lounge.recordings
    assert lounge.recordings[0].start_ms == 1_799_000_000_000
    assert fetchtv._recording({"id": 1, "name": "X", "programStartDate": 1_799_000_000}).start_ms == 1_799_000_000_000

    # A recording event marks them stale; refreshing asks the box again, once
    # (a fresh list is left alone).
    asks = lambda: len([m for m in fake.ws_received if m["message"]["type"] == "ARE_YOU_ALIVE" and m["to"] == LOUNGE])
    before = asks()
    await client.refresh_recordings(lounge)
    assert asks() == before, "fresh list not re-requested"
    lounge.recordings_at = 0.0
    lounge.recordings = []
    await client.refresh_recordings(lounge)
    assert asks() == before + 1 and len(lounge.recordings) == 2, "stale list re-requested"

    # Channel list: visible free-to-air channels (hidden one dropped), then
    # subscribed subscription channels only.
    labels = [c.label for c in client.channels_for(lounge)]
    assert labels == ["2 ABC TV", "23 ABC Entertains", "159 beIN SPORTS 3"], labels
    # Free-to-air logos aren't served publicly; the guide's own URLs are internal.
    assert client.channels_for(lounge)[1].logo is None

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

    # The media browser fetches a whole page of channels in one request, and
    # skips channels whose listings are already cached (411316 is).
    page = client.channels_for(lounge)
    n = len([r for r in fake.http_requests if r[1] == "/v2/epg/programslist"])
    await client.refresh_programs(page)
    assert len([r for r in fake.http_requests if r[1] == "/v2/epg/programslist"]) == n + 1
    assert {c.name: client.cached_channel_program(c).title for c in page} == {
        "ABC TV": "ABC News", "ABC Entertains": "Hard Quiz", "beIN SPORTS 3": "EFL Cup"}
    await client.refresh_programs(page)  # everything cached now
    assert len([r for r in fake.http_requests if r[1] == "/v2/epg/programslist"]) == n + 1

    # The background prefetch warms every channel in one request, then idles.
    client._guide._programs.clear()
    await client.prefetch_programs()
    assert len([r for r in fake.http_requests if r[1] == "/v2/epg/programslist"]) == n + 2
    assert all(client.cached_channel_program(c) for c in page)
    await client.prefetch_programs()
    assert len([r for r in fake.http_requests if r[1] == "/v2/epg/programslist"]) == n + 2

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

    # Playing a recording: the Fetch app's PLAY_ASSET message, with the id as a string.
    await client.play_recording(LOUNGE, 11)
    await wait_for(lambda: fake.received("PLAY_ASSET", LOUNGE))
    sent = fake.received("PLAY_ASSET", LOUNGE)[-1]["message"]
    assert sent["assetType"] == "RECORDING" and sent["recordingId"] == "11", sent
    assert sent["requiresSetTopBox"] is True

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
