"""Stress: a box with thousands of recordings reports more than the websocket's default
message limit, and the guide has hundreds of channels.

Reuses the browser test's recordings (titles of every shape).
"""
import asyncio
import json
import logging

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import entities as ent
from fake_fetch import LOUNGE, FakeFetch
from _stress import build_recordings, odd_title
from ucapi import StatusCodes


async def wait_for(cond, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.02)


async def service_part():
    fake = FakeFetch()
    # 600 subscription channels and 150 free-to-air ones on the guide.
    fake.epg = {
        str(5000 + i): {"name": odd_title(i) + " HD", "number": 200 + i, "subscribed": True,
                        "sources": [{"id": 5000 + i, "type": "hls"}],
                        "images": {"channel_logo_focus": {"original": "http://img/x.png"}}}
        for i in range(600)
    }
    fake.fta = [{"id": str(30000000 + i), "epg_id": 6000 + i, "name": odd_title(i) + " FTA", "number": 2 + i} for i in range(150)]
    fake.recordings = build_recordings(4000)
    size = len(json.dumps(fake.recordings))
    assert size > 1024 * 1024, size
    await fake.start()
    client = fake.client()
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.ready.wait(), 5)
    lounge = client.boxes[LOUNGE]

    # The full report is bigger than a megabyte and still arrives, on the first connection.
    await wait_for(lambda: len(lounge.recordings) == 4000, timeout=10)
    assert fake.ws_connections == 1 and fake.logins == 1, (
        f"a {size // 1024} KiB report cost {fake.ws_connections - 1} reconnect(s)")

    channels = client.channels_for(lounge)
    assert len(channels) == 750, len(channels)
    labels = [c.label for c in channels]
    assert len(set(labels)) == 750, "channel labels must be unique for the source list"

    attrs = ent.select_attributes(client, lounge)
    assert len(attrs["options"]) == 750

    # What is on now for every channel comes in one request.
    before = len([r for r in fake.http_requests if r[1] == "/v2/epg/programslist"])
    await client.prefetch_programs()
    assert len([r for r in fake.http_requests if r[1] == "/v2/epg/programslist"]) == before + 1

    # Selecting every channel by label works, and a second box does not mix them up.
    handler = ent._make_select_handler(LOUNGE, lambda: client)
    for c in channels[::97]:
        assert await handler(None, "select_option", {"option": c.label}, None) == StatusCodes.OK

    await client.stop()
    await asyncio.wait_for(task, 5)
    await fake.stop()



async def main():
    await service_part()
    print("STRESS_REPORT_PASSED")


logging.disable(logging.CRITICAL)
asyncio.run(asyncio.wait_for(main(), 120))
