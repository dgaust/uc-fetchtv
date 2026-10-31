"""Stress: the Remote connecting, disconnecting and entering standby over and over.

End to end through the driver's websocket API against the fake FetchTV
service: the Remote's connection coming and going (some abruptly), connect,
disconnect and standby events racing each other, many Remotes at once, a slow
shutdown of the old FetchTV connection, a command that fails, and a
time-compressed soak with the boxes changing all the time.
"""
import asyncio
import gc
import json
import logging
import os
import tempfile
import tracemalloc

os.environ["UC_DISABLE_MDNS_PUBLISH"] = "true"
os.environ["UC_INTEGRATION_HTTP_PORT"] = "9097"
CONFIG_DIR = tempfile.mkdtemp()
os.environ["UC_CONFIG_HOME"] = CONFIG_DIR

import _path  # noqa: E402 - puts intg-fetchtv on sys.path
os.chdir(_path.INTG_DIR)

import box_manager  # noqa: E402
import driver  # noqa: E402
import entities as ent  # noqa: E402
import fetchtv  # noqa: E402
import remote_entity  # noqa: E402
import sensors  # noqa: E402
import websockets  # noqa: E402
from fake_fetch import ACTIVATION_CODE, BEDROOM, LOUNGE, PIN, FakeFetch  # noqa: E402
from shared import loop as uc_loop  # noqa: E402

URL = "ws://127.0.0.1:9097"
ALL = [ent.entity_id(LOUNGE), ent.entity_id(BEDROOM), ent.select_entity_id(LOUNGE), ent.select_entity_id(BEDROOM),
       remote_entity.entity_id(LOUNGE), remote_entity.entity_id(BEDROOM),
       *sensors.entity_ids(LOUNGE), *sensors.entity_ids(BEDROOM)]
LOUNGE_ID = ent.entity_id(LOUNGE)
loop_errors: list[str] = []


async def wait_for(cond, timeout=20.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.02)


class Remote:
    """What the Remote does over the driver's websocket API."""

    def __init__(self):
        self.ws = None
        self.events: list[dict] = []
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader = None

    async def open(self):
        self.ws = await websockets.connect(URL)
        self._reader = asyncio.create_task(self._read())
        return self

    async def _read(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg.get("kind") == "resp" and msg.get("req_id") in self._pending:
                    self._pending.pop(msg["req_id"]).set_result(msg)
                else:
                    self.events.append(msg)
        except websockets.ConnectionClosed:
            pass

    async def request(self, msg, data=None, timeout=10):
        self._id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self.ws.send(json.dumps({"kind": "req", "id": self._id, "msg": msg, "msg_data": data or {}}))
        return await asyncio.wait_for(fut, timeout)

    async def event(self, msg):
        await self.ws.send(json.dumps({"kind": "event", "msg": msg, "msg_data": {}}))

    async def states(self):
        return {s["entity_id"]: s["attributes"] for s in (await self.request("get_entity_states"))["msg_data"]}

    def device_states(self):
        return [e["msg_data"]["state"] for e in self.events if e.get("msg") == "device_state"]

    async def close(self, abrupt=False):
        if abrupt:
            self.ws.transport.abort()
        else:
            await self.ws.close()
        await asyncio.gather(self._reader, return_exceptions=True)


class SlowStop(fetchtv.FetchTvClient):
    """A client whose connection takes a while to shut down, as when the service is slow to answer a close."""

    stop_delay = 0.0

    finished = 0  # shutdowns completed

    async def stop(self):
        await asyncio.sleep(self.stop_delay)
        await super().stop()
        SlowStop.finished += 1


async def setup(fake):
    remote = await Remote().open()
    assert (await remote.request("setup_driver", {"setup_data": {"activation_code": ACTIVATION_CODE, "pin": PIN}}))["code"] == 200
    await wait_for(lambda: any(e.get("msg") == "driver_setup_change" and e["msg_data"].get("state") == "OK" for e in remote.events))
    await remote.request("subscribe_events", {"entity_ids": ALL})
    await wait_for(lambda: box_manager.is_connected() and all(b.has_state for b in box_manager.get_client().boxes.values()))
    await remote.close()


async def remote_comes_and_goes(fake):
    """The Remote's websocket opens and closes 30 times, half of them abruptly; events race each other."""
    logins = fake.logins
    for i in range(30):
        remote = await Remote().open()
        await remote.event("connect")
        resp = await remote.request("subscribe_events", {"entity_ids": ALL})
        assert resp["code"] == 200
        states = await remote.states()
        assert set(states) >= set(ALL), set(ALL) - set(states)
        resp = await remote.request("entity_command", {"entity_type": "remote", "entity_id": remote_entity.entity_id(LOUNGE),
                                                       "cmd_id": "send_cmd", "params": {"command": "CURSOR_UP"}})
        assert resp["code"] in (200, 503), resp
        if i % 3 == 0:
            await remote.event("enter_standby")
            await remote.event("exit_standby")
        if i % 5 == 0:
            await remote.event("disconnect")
            await remote.event("connect")
        await remote.close(abrupt=i % 2 == 0)
    await wait_for(lambda: box_manager.is_connected(), timeout=15)
    # Whatever the churn, there is one client, signed in a bounded number of times.
    assert fake.logins - logins <= 30, f"{fake.logins - logins} sign-ins for 30 sessions"
    remote = await Remote().open()
    assert (await remote.request("get_device_state"))["msg_data"]["state"] == "CONNECTED"
    await remote.close()


async def racing_standby(fake):
    """The Remote sleeps and wakes within the time the old connection takes to close."""
    # No grace period, so standby closes the connection (and starts the race) straight away.
    grace, box_manager.SESSION_GRACE_S = box_manager.SESSION_GRACE_S, 0.0
    box_manager.client_factory = lambda *a, **k: _slow(fake, *a, **k)
    remote = await Remote().open()
    await remote.request("subscribe_events", {"entity_ids": ALL})
    for n in range(1, 4):
        await remote.event("enter_standby")
        await asyncio.sleep(0.05)
        await remote.event("exit_standby")
        await wait_for(lambda: box_manager.is_connected(), timeout=15)
        # Past the old connection's shutdown (which is what used to report "disconnected").
        await wait_for(lambda: SlowStop.finished >= n - 1, timeout=15)
        await asyncio.sleep(0.5)
        state = (await remote.request("get_device_state"))["msg_data"]["state"]
        assert state == "CONNECTED", f"the driver reports {state} though it is connected"
        assert box_manager.is_connected()
    assert remote.device_states()[-1] == "CONNECTED", remote.device_states()
    await remote.close()
    box_manager.client_factory = fake.client
    box_manager.SESSION_GRACE_S = grace


def _slow(fake, *args, **kwargs):
    kwargs.setdefault("reconnect_delay", 0.2)
    client = SlowStop(*args, api_base=fake.http_url, ws_url=fake.ws_url, **kwargs)
    client.stop_delay = 0.6
    return client


async def many_remotes(fake):
    """Ten Remotes at once, each subscribing, commanding and dropping."""
    async def one(n):
        remote = await Remote().open()
        await remote.request("subscribe_events", {"entity_ids": ALL})
        for _ in range(5):
            await asyncio.gather(
                remote.request("entity_command", {"entity_type": "media_player", "entity_id": LOUNGE_ID, "cmd_id": "channel_up"}),
                remote.request("browse_media", {"entity_id": LOUNGE_ID, "media_id": "recordings", "paging": {"page": 1, "limit": 5}}),
                remote.request("browse_media", {"entity_id": LOUNGE_ID, "media_id": "subscription", "paging": {"page": 1, "limit": 5}}),
                remote.states(),
            )
        await remote.close(abrupt=n % 2 == 0)
        return True
    results = await asyncio.wait_for(asyncio.gather(*(one(n) for n in range(10))), 60)
    assert all(results)


async def failing_command(fake):
    """A command that raises inside the driver is answered with an error, not left hanging."""
    remote = await Remote().open()
    await remote.request("subscribe_events", {"entity_ids": ALL})
    client = box_manager.get_client()
    lounge = client.boxes[LOUNGE]
    saved = client.play_channel

    async def boom(*args):
        raise RuntimeError("boom")

    client.play_channel = boom
    try:
        resp = await remote.request("entity_command", {"entity_type": "media_player", "entity_id": LOUNGE_ID,
                                                       "cmd_id": "select_source", "params": {"source": "159 beIN SPORTS 3"}}, timeout=5)
    finally:
        client.play_channel = saved
    assert resp["code"] == 500, resp
    # The same through a channel the box reports with an id that is not a number.
    lounge.fta_channels.append(fetchtv.Channel(id="abc", epg_id="abc", number=77, name="Odd"))
    resp = await remote.request("entity_command", {"entity_type": "media_player", "entity_id": LOUNGE_ID,
                                                   "cmd_id": "select_source", "params": {"source": "77 Odd"}}, timeout=5)
    assert resp["code"] in (400, 500), resp
    lounge.fta_channels.pop()
    assert (await remote.request("get_device_state"))["code"] == 200
    await remote.close()


async def refresh_loop_survives(fake):
    """A guide error in the periodic refresh does not end the refresh for good."""
    box_manager._refresh_task.cancel()
    box_manager.REFRESH_INTERVAL_S = 0.05
    task = box_manager._refresh_task = uc_loop.create_task(box_manager._refresh_periodically())
    client = box_manager.get_client()
    saved = client.refresh_program
    calls = []

    async def flaky(box):
        calls.append(box)
        if len(calls) == 1:
            raise RuntimeError("guide exploded")
        return await saved(box)

    client.refresh_program = flaky
    try:
        await wait_for(lambda: len(calls) >= 3 or task.done(), timeout=5)
        assert not task.done(), "the periodic refresh stopped after one error"
    finally:
        client.refresh_program = saved
        box_manager.REFRESH_INTERVAL_S = 30.0


async def soak(fake):
    """Compressed time: polls every 20 ms, the boxes changing all the time, a Remote watching."""
    await box_manager.disconnect()
    box_manager.REFRESH_INTERVAL_S = 0.05
    saved_factory = box_manager.client_factory
    box_manager.client_factory = lambda *a, **k: fake.client(*a, poll_interval=0.02, **k)
    remote = await Remote().open()
    await remote.event("connect")
    await remote.request("subscribe_events", {"entity_ids": ALL})
    await wait_for(lambda: box_manager.is_connected() and all(b.has_state for b in box_manager.get_client().boxes.values()))
    gc.collect()
    tracemalloc.start()
    tasks_before, base = len(asyncio.all_tasks()), tracemalloc.take_snapshot()
    received = 0
    for i in range(300):
        await fake.push(LOUNGE, "MEDIA_STATE", {"settings": {"standby": i % 40 == 0, "volume": i % 100, "mute": i % 7 == 0},
                                                "currentPlaybackMedia": {"channelId": "30411316" if i % 3 else "30018294",
                                                                         "mediaTitle": f"Title {i % 20}", "playBackState": "LIVE",
                                                                         "playbackType": "DVB"}})
        if i % 50 == 0:
            await fake.push(LOUNGE, "RECORDINGS_UPDATE", {})
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.5)
    # Quiet: nothing changes, so nothing is pushed to the Remote however often the client polls.
    remote.events.clear()
    await asyncio.sleep(1.0)
    changes = [e for e in remote.events if e.get("msg") == "entity_change"]
    assert len(changes) <= 6, f"{len(changes)} entity changes in 1 s with nothing changing"
    gc.collect()
    growth = sum(s.size_diff for s in tracemalloc.take_snapshot().compare_to(base, "filename") if s.size_diff > 0)
    tracemalloc.stop()
    assert len(asyncio.all_tasks()) - tasks_before <= 3, "tasks piled up"
    assert growth < 6_000_000, f"memory grew by {growth / 1e6:.1f} MB over the soak"
    assert len(box_manager._last_pushed) <= len(ALL)
    assert len(remote.events) < 5000
    await remote.close()
    box_manager.client_factory = saved_factory
    box_manager.REFRESH_INTERVAL_S = 30.0


async def main():
    uc_loop.set_exception_handler(lambda _loop, context: loop_errors.append(str(context.get("message"))))
    fake = FakeFetch()
    await fake.start()
    box_manager.client_factory = fake.client
    await driver.main()
    for _ in range(40):
        try:
            await (await websockets.connect(URL)).close()
            break
        except OSError:
            await asyncio.sleep(0.25)
    await setup(fake)
    for part in (remote_comes_and_goes, racing_standby, many_remotes, failing_command, refresh_loop_survives, soak):
        await part(fake)
    await box_manager.disconnect()
    await fake.stop()
    assert not [e for e in loop_errors if "never retrieved" in e], loop_errors
    print("STRESS_DRIVER_PASSED")


logging.disable(logging.CRITICAL)
uc_loop.run_until_complete(asyncio.wait_for(main(), 240))
