"""Stress: websocket drops and reconnect storms.

Checks that reconnecting backs off but recovers once a connection has been
good for a while, that a service accepting and closing at once is not hammered,
that nothing leaks across many reconnects, that ``connected`` always matches
reality, and that stop() is prompt whatever the client is doing.
"""
import asyncio
import logging
import threading
import time

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import fetchtv
from fake_fetch import BEDROOM, LOUNGE, FakeFetch


async def wait_for(cond, timeout=20.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


async def backoff_resets_after_a_good_connection(fake):
    """Dropped with an error after a long enough connection: the next retry is quick again."""
    fetchtv.STABLE_CONNECTION_S = 0.2
    fetchtv.RECONNECT_DELAY_MAX_S = 2.0
    client = fake.client(reconnect_delay=0.1)
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.ready.wait(), 5)
    gaps = []
    for _ in range(6):
        await asyncio.sleep(0.3)  # a good connection
        n = fake.ws_connections
        dropped = time.monotonic()
        await fake.drop_connections(code=1011)  # an error close, as a network failure gives
        await wait_for(lambda: fake.ws_connections > n and client.connected)
        gaps.append(fake.ws_times[n] - dropped)
    # 0.1 s each time (plus the time to sign in), not 0.1, 0.2, 0.4, 0.8, 1.6, 2.0.
    assert max(gaps) < 0.7, f"retry delays kept growing after good connections: {[round(g, 2) for g in gaps]}"
    await client.stop()
    await asyncio.wait_for(task, 5)


async def storm_is_not_hammered(fake):
    """A service that accepts the websocket and closes it at once is retried with growing pauses."""
    fetchtv.STABLE_CONNECTION_S = 60.0
    fetchtv.RECONNECT_DELAY_MAX_S = 0.4
    fake.close_at_once = True
    n, logins = fake.ws_connections, fake.logins
    client = fake.client(reconnect_delay=0.05)
    task = asyncio.create_task(client.run())
    await asyncio.sleep(2.0)
    tries = fake.ws_connections - n
    # 0.05 + 0.1 + 0.2 + 0.4 + 0.4 ... gives about 8 tries in 2 s; hammering at the base delay gives 30+.
    assert 3 <= tries <= 12, f"{tries} reconnects in 2 s"
    assert fake.logins - logins == tries, "one sign-in per try"
    assert not client.connected or tries
    fake.close_at_once = False
    await wait_for(lambda: client.connected, timeout=3)
    await client.stop()
    await asyncio.wait_for(task, 5)


async def no_leaks_over_many_reconnects(fake):
    fetchtv.STABLE_CONNECTION_S = 0.0
    fetchtv.RECONNECT_DELAY_MAX_S = 0.05
    client = fake.client(reconnect_delay=0.01, poll_interval=0.05)
    events = []
    client.subscribe(lambda box: events.append(client.connected) if box is None else None)
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.ready.wait(), 5)
    await wait_for(lambda: all(b.has_state for b in client.boxes.values()))
    tasks_before, threads_before = len(asyncio.all_tasks()), threading.active_count()
    lounge = client.boxes[LOUNGE]
    for i in range(40):
        n = fake.ws_connections
        await fake.drop_connections(code=1000 if i % 2 else 1011)
        # While down: commands fail cleanly, and the box is not claimed to be online.
        await wait_for(lambda: not client.connected)
        assert not lounge.online and not client.ready.is_set()
        try:
            await client.send_key(LOUNGE, fetchtv.KEY_UP)
            raise AssertionError("expected NotConnectedError")
        except fetchtv.NotConnectedError:
            pass
        await wait_for(lambda: fake.ws_connections > n and client.connected and lounge.online)
    await asyncio.sleep(0.2)
    # connected went False then True once per drop, never twice in a row.
    assert events == [True] + [False, True] * 40, events
    assert len(fake._clients) == 1, f"{len(fake._clients)} sockets open at the service for one client"
    leaked = len(asyncio.all_tasks()) - tasks_before
    assert leaked <= 1, f"{leaked} more tasks after 40 reconnects"
    assert threading.active_count() - threads_before <= 2, "threads grew across reconnects"
    assert client._ws is not None and not client._tasks
    await client.stop()
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(0.1)
    assert client._ws is None and not client._tasks and not fake._clients
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                and t.get_coro().__qualname__.startswith(("FetchTvClient", "FakeFetch._ws"))], asyncio.all_tasks()


async def stop_is_prompt(fake):
    """stop() returns quickly and nothing is left running, whatever the client is doing."""
    fetchtv.STABLE_CONNECTION_S = 60.0
    fetchtv.RECONNECT_DELAY_MAX_S = 300.0

    # While waiting out a long retry delay.
    fake.close_at_once = True
    client = fake.client(reconnect_delay=30.0)
    task = asyncio.create_task(client.run())
    await wait_for(lambda: fake.ws_connections >= 1)
    await asyncio.sleep(0.3)
    t0 = time.monotonic()
    await client.stop()
    await asyncio.wait_for(task, 2)
    assert time.monotonic() - t0 < 1.5, "run() sat out its retry delay after stop()"
    fake.close_at_once = False

    # While signing in: no websocket may be opened afterwards.
    fake.login_delay = 0.6
    n = fake.ws_connections
    client = fake.client()
    task = asyncio.create_task(client.run())
    await asyncio.sleep(0.2)
    await client.stop()
    await asyncio.wait_for(task, 3)
    assert fake.ws_connections == n, "a websocket was opened after stop()"
    assert not client.connected and client._ws is None
    fake.login_delay = 0.0

    # Stopped twice, and run again afterwards.
    await client.stop()
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.ready.wait(), 5)
    await client.stop()
    await asyncio.wait_for(task, 3)


async def main():
    fake = FakeFetch()
    await fake.start()
    saved = (fetchtv.STABLE_CONNECTION_S, fetchtv.RECONNECT_DELAY_MAX_S)
    try:
        await backoff_resets_after_a_good_connection(fake)
        await storm_is_not_hammered(fake)
        await no_leaks_over_many_reconnects(fake)
        await stop_is_prompt(fake)
    finally:
        fetchtv.STABLE_CONNECTION_S, fetchtv.RECONNECT_DELAY_MAX_S = saved
    await fake.stop()
    print("STRESS_RECONNECT_PASSED")


logging.disable(logging.CRITICAL)
asyncio.run(asyncio.wait_for(main(), 120))
