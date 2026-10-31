"""Stress: the programme guide returns odd, empty, slow or failing answers.

The guide is a convenience: whatever it does, the box connection stays up, the
channels the boxes report stay usable, lookups never raise into the driver, and
a guide that is down is not asked again on every box update.
"""
import asyncio
import json
import logging

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import fetchtv
from fake_fetch import LOUNGE, PROGRAM_FIELDS, FakeFetch

CHANNELS = "/v2/epg/channels"
PROGRAMS = "/v2/epg/programslist"
GOOD = {"name": "Good", "number": 7, "subscribed": True, "sources": [{"id": 777, "type": "hls"}],
        "images": {"channel_logo_focus": {"original": "http://img/x.png"}}}


async def wait_for(cond, timeout=20.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


def count(fake, path):
    return len([r for r in fake.http_requests if r[1] == path])


async def connect(fake, **kwargs):
    client = fake.client(**kwargs)
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.ready.wait(), 5)
    await wait_for(lambda: all(b.has_state for b in client.boxes.values()))
    return client, task


async def stop(client, task):
    await client.stop()
    await asyncio.wait_for(task, 5)


async def odd_channel_lists(fake):
    """A channel list that is partly or wholly unreadable costs only those channels."""
    bodies = {
        "partly bad entries": {"channels": {
            "1": "x", "2": None, "3": [1], "4": GOOD, "5": {"sources": "x", "subscribed": True, "name": 5},
            "6": {"subscribed": True, "sources": [None, "x", {"id": None}, {"type": "hls"}], "images": "x"},
            "7": {"subscribed": True, "sources": [{"id": 8, "type": "hls"}], "images": {"channel_logo_focus": "x"}},
            "8": {"subscribed": True, "number": "ten", "name": ["x"], "sources": [{"id": 9, "type": "hls"}]}}},
        "channels is a list": {"channels": [1, 2]},
        "channels is text": {"channels": "x"},
        "channels is null": {"channels": None},
        "no channels": {},
    }
    for name, body in bodies.items():
        fake.http_body[CHANNELS] = json.dumps(body).encode()
        connections = fake.ws_connections
        client, task = await connect(fake)
        lounge = client.boxes[LOUNGE]
        await client.refresh_program(lounge)  # asks for the guide
        assert client.connected and fake.ws_connections == connections + 1, f"{name}: the connection was lost"
        assert lounge.online
        labels = [c.label for c in client.channels_for(lounge)]
        assert "2 ABC TV" in labels and "23 ABC Entertains" in labels, (name, labels)  # the box's own channels
        if name == "partly bad entries":
            assert "7 Good" in labels, labels
        client.current_channel(lounge)
        await stop(client, task)
    fake.http_body.pop(CHANNELS)
    fake.http_body[CHANNELS] = b"[]"  # not even an object
    client, task = await connect(fake)
    assert client.connected
    await stop(client, task)
    fake.http_body.clear()


async def odd_listings(fake):
    """Programme listings that cannot be read give no programme, not an exception."""
    fields = {"__meta__": {"program_fields": PROGRAM_FIELDS}}
    good_row = [1, "Show", 0, 10**14, 0, 0, "", 0, "News", "", ""]
    bodies = [
        b"[]", b"null", b'"x"', b"{}", b"not json", b"",
        json.dumps({**fields, "channels": []}), json.dumps({**fields, "channels": "x"}),
        json.dumps({**fields, "channels": {"411316": 5}}), json.dumps({**fields, "channels": {"411316": "text"}}),
        json.dumps({**fields, "channels": {"411316": [None, 3, "x", [], good_row[:2]]}}),
        json.dumps({**fields, "channels": {"411316": [[1, "T", "soon", "later"] + good_row[4:]]}}),
        json.dumps({**fields, "channels": {"411316": [[1, {"a": 1}, None, [1]] + good_row[4:]]}}),
        json.dumps({"__meta__": {"program_fields": "title"}, "channels": {"411316": [good_row]}}),
        json.dumps({"__meta__": {"program_fields": [1, 2, 3]}, "channels": {"411316": [good_row]}}),
        json.dumps({"__meta__": "x", "channels": {"411316": [good_row]}}),
        json.dumps({**fields, "synopses": [1], "channels": {"411316": [good_row]}}),
        json.dumps({**fields, "synopses": {"0": ["x"]}, "channels": {"411316": [good_row]}}),
    ]
    client, task = await connect(fake)
    lounge = client.boxes[LOUNGE]
    for body in bodies:
        fake.http_body[PROGRAMS] = body if isinstance(body, bytes) else body.encode()
        client._guide._programs.clear()
        client._guide._empty_until.clear()
        client._programs_retry_at = 0.0
        try:
            await client.refresh_program(lounge)
            await client.refresh_programs(client.channels_for(lounge))
            await client.prefetch_programs()
        except Exception as ex:  # noqa: BLE001
            raise AssertionError(f"{body[:70]!r}: raised {type(ex).__name__}: {ex}") from ex
    assert client.connected
    await stop(client, task)
    fake.http_body.clear()


async def failing_guide(fake):
    """A guide that errors is not asked again on every call; it is tried again later and recovers."""
    saved = fetchtv.GUIDE_RETRY_S
    fetchtv.GUIDE_RETRY_S = 0.5
    fake.http_status[CHANNELS] = 500
    fake.http_status[PROGRAMS] = 500
    try:
        client, task = await connect(fake)
        lounge = client.boxes[LOUNGE]
        n_channels, n_programs = count(fake, CHANNELS), count(fake, PROGRAMS)
        for _ in range(20):
            assert await client.refresh_program(lounge) is None
            await client.refresh_programs(client.channels_for(lounge))
            await client.prefetch_programs()
        assert count(fake, CHANNELS) - n_channels <= 2, f"{count(fake, CHANNELS) - n_channels} channel list requests"
        assert count(fake, PROGRAMS) - n_programs <= 2, f"{count(fake, PROGRAMS) - n_programs} listing requests"
        assert client.connected
        fake.http_status.clear()
        await asyncio.sleep(0.6)
        program = await client.refresh_program(lounge)
        assert program is not None and program.title == "Hard Quiz", "the guide did not come back"
        assert [c.label for c in client.channels_for(lounge)][-1] == "159 beIN SPORTS 3"
        await stop(client, task)
    finally:
        fetchtv.GUIDE_RETRY_S = saved
        fake.http_status.clear()


async def nothing_on(fake):
    """A channel the guide has no listings for is not asked about on every update."""
    fields = {"__meta__": {"program_fields": PROGRAM_FIELDS}}
    fake.http_body[PROGRAMS] = json.dumps({**fields, "channels": {"411316": []}}).encode()
    client, task = await connect(fake)
    lounge = client.boxes[LOUNGE]
    n = count(fake, PROGRAMS)
    for _ in range(15):
        assert await client.refresh_program(lounge) is None
    assert count(fake, PROGRAMS) - n <= 1, f"{count(fake, PROGRAMS) - n} requests for a channel with no listings"
    fake.http_body.clear()
    await stop(client, task)


async def slow_guide(fake):
    """Concurrent lookups while the guide is slow: the channel list is fetched once."""
    fake.http_delay = 0.4
    client, task = await connect(fake)
    lounge = client.boxes[LOUNGE]
    n_channels, n_programs = count(fake, CHANNELS), count(fake, PROGRAMS)
    client._guide = None
    results = await asyncio.gather(*(client.refresh_program(lounge) for _ in range(12)))
    assert all(r is not None and r.title == "Hard Quiz" for r in results)
    assert count(fake, CHANNELS) - n_channels == 1, "the channel list was fetched more than once"
    assert count(fake, PROGRAMS) - n_programs <= 2, f"{count(fake, PROGRAMS) - n_programs} listing requests for 12 callers"
    # Timeouts: a guide that never answers gives nothing, in about the timeout.
    saved = fetchtv.HTTP_TIMEOUT_S
    fetchtv.HTTP_TIMEOUT_S = 0.2
    fake.http_delay = 1.0
    client._guide._programs.clear()
    client._programs_retry_at = 0.0
    t0 = asyncio.get_running_loop().time()
    assert await client.refresh_program(lounge) is None
    assert asyncio.get_running_loop().time() - t0 < 0.95  # (the guide would answer after 1.0)
    fetchtv.HTTP_TIMEOUT_S = saved
    fake.http_delay = 0.0
    assert client.connected
    await stop(client, task)


async def main():
    fake = FakeFetch()
    await fake.start()
    for part in (odd_channel_lists, odd_listings, failing_guide, nothing_on, slow_guide):
        await part(fake)
    await fake.stop()
    print("STRESS_GUIDE_PASSED")


logging.disable(logging.CRITICAL)
asyncio.run(asyncio.wait_for(main(), 120))
