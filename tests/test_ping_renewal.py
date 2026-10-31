"""The event subscription a PING gives is renewed just before it lapses, and started afresh on every connection."""
import asyncio

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import fetchtv
from fake_fetch import BEDROOM, LOUNGE, FakeFetch


async def wait_for(cond, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.02)


async def main():
    # The real interval is 14,380 s (as the app's); shrink it so the test can see a renewal.
    assert fetchtv.SUBSCRIPTION_RENEW_S == 14380.0
    fetchtv.SUBSCRIPTION_RENEW_S = 0.5

    fake = FakeFetch()
    await fake.start()
    client = fake.client(poll_interval=0.1)
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.ready.wait(), 5)
    await wait_for(lambda: all(b.has_state for b in client.boxes.values()))

    # A box is asked for its media state as well as its full report (a box that has
    # no recordings, like the Mini, only ever answers the first).
    assert fake.received("MEDIA_STATE", LOUNGE), "no MEDIA_STATE asked of a new box"
    kinds = [m["message"]["type"] for m in fake.ws_received if m["to"] == LOUNGE]
    assert kinds.index("PING") < kinds.index("MEDIA_STATE"), kinds

    pings = lambda tid: len(fake.received("PING", tid))
    first = {t: pings(t) for t in (LOUNGE, BEDROOM)}
    assert all(n >= 1 for n in first.values()), first

    # Once a box answers, it is pinged only when the subscription is due again:
    # about every 0.5 s here, not once per 0.1 s poll.
    await asyncio.sleep(1.7)
    for tid in (LOUNGE, BEDROOM):
        renewed = pings(tid) - first[tid]
        assert 2 <= renewed <= 5, (tid, renewed)

    # A new connection is a new session: it subscribes again at once, however
    # recently the last connection did.
    fetchtv.SUBSCRIPTION_RENEW_S = 600.0
    before = pings(LOUNGE)
    await fake.drop_connections()
    await wait_for(lambda: not client.connected)
    await wait_for(lambda: client.connected and client.boxes[LOUNGE].online)
    assert pings(LOUNGE) > before, "no PING on the new connection"

    await client.stop()
    await asyncio.wait_for(task, 5)
    await fake.stop()
    print("PING_RENEWAL_TEST_PASSED")


asyncio.run(main())
