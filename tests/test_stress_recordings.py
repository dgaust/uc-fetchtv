"""Stress: recording events, stale reports and the sensors that follow them.

A recording starting or ending changes what the sensors say, and the only place
that comes from is the box's full report, so the client asks for one shortly
after the event - once for a burst of events - and the sensors stay available
meanwhile.
"""
import asyncio
import logging

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import entities as ent
import fetchtv
import sensors
from fake_fetch import BEDROOM, LOUNGE, RECORDINGS, FakeFetch
from ucapi.sensor import Attributes, States

fetchtv.RECORDING_REPORT_DELAY_S = 0.6
EVENTS = ["RECORDINGS_UPDATE", "RECORDING_UPDATED", "RECORD_PROGRAM_SUCCESS", "RECORD_PROGRAM_STOP",
          "PENDING_DELETE_RECORDINGS_BY_ID_SUCCESS", "RESTORE_RECORDINGS_BY_ID_SUCCESS"]


async def wait_for(cond, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


def reports(fake, tid):
    return len([m for m in fake.ws_received if m["to"] == tid and m["message"]["type"] == "ARE_YOU_ALIVE"])


async def main():
    fake = FakeFetch()
    await fake.start()
    client = fake.client()
    notified = []
    client.subscribe(lambda box: notified.append(box) if box is not None else None)
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.ready.wait(), 5)
    lounge = client.boxes[LOUNGE]
    await wait_for(lambda: lounge.has_state and lounge.has_report)
    ids = sensors.entity_ids(LOUNGE)

    def sensor_states():
        return {k: v[Attributes.STATE] for k, v in sensors.attributes(client, lounge).items()}

    # (the fake's report has no storage figures, so those two have nothing to show)
    assert [sensor_states()[i] for i in ids] == [States.ON] * 3 + [States.UNAVAILABLE] * 2, sensor_states()
    assert sensors.attributes(client, lounge)[ids[2]][Attributes.VALUE] == "Yes"

    # A burst of every kind of recording event is answered by one report request.
    before = reports(fake, LOUNGE)
    fake.recordings = RECORDINGS + [{"id": 99, "name": "New Recording", "programStartDate": 1_800_000_000_000}]
    for _ in range(50):
        for event in EVENTS:
            await fake.push(LOUNGE, event, {})
    await wait_for(lambda: lounge.recordings_at == 0.0)
    # Out of date, but the sensors are still there with their last values.
    assert [sensor_states()[i] for i in ids[:3]] == [States.ON] * 3, sensor_states()
    assert sensors.attributes(client, lounge)[ids[0]][Attributes.VALUE] == 2
    await wait_for(lambda: len(lounge.recordings) == 3)
    assert reports(fake, LOUNGE) == before + 1, f"{reports(fake, LOUNGE) - before} report requests for one burst"
    assert lounge.recordings_at > 0 and lounge in notified[-3:], "listeners hear about the new report"
    assert sensors.attributes(client, lounge)[ids[0]][Attributes.VALUE] == 3
    # Events a while later are a new burst.
    await fake.push(LOUNGE, "RECORD_PROGRAM_STOP", {})
    await wait_for(lambda: reports(fake, LOUNGE) == before + 2)
    # Another box's events only ask that box.
    other = reports(fake, BEDROOM)
    await fake.push(BEDROOM, "RECORDINGS_UPDATE", {})
    await wait_for(lambda: reports(fake, BEDROOM) == other + 1)
    assert reports(fake, LOUNGE) == before + 2

    # A box that is playing a recording the list no longer has is shown without error.
    await fake.push(LOUNGE, "NOW_PLAYING", {"currentPlaybackMedia": {
        "playbackType": "PVR", "recordingId": "424242", "mediaTitle": "Gone", "playBackState": "UNPAUSED",
        "duration": 1_800_000, "channelId": "30411316"}})
    await wait_for(lambda: lounge.recording_id == "424242")
    attrs = ent.attributes(client, lounge, None, 1_800_000_000_000)
    assert attrs["media_title"] == "Gone" and attrs["state"] == "PLAYING", attrs

    # Messages about a box before it has sent anything (or out of order) change nothing that breaks.
    fresh = fetchtv.Box(terminal_id="x|y", name="New")
    client.boxes["x|y"] = fresh
    for typ, data in (("PAUSED", {}), ("VOLUME_SET", {"current": 5}), ("RECORD_PROGRAM_STOP", {}),
                      ("NOW_PLAYING", {"currentPlaybackMedia": {"channelId": 5}}), ("PONG", {})):
        await fake.push("x|y", typ, data)
    await asyncio.sleep(0.3)
    assert client.connected and not fresh.has_report
    assert all(a[Attributes.STATE] == States.UNAVAILABLE for a in sensors.attributes(client, fresh).values())

    # Many callers wanting a fresh report at once (the Remote opening several pages)
    # share one request, and a box that doesn't answer is not asked again by each of them.
    lounge.recordings_at = 0.0
    before = reports(fake, LOUNGE)
    await asyncio.gather(*(client.refresh_recordings(lounge) for _ in range(40)))
    assert reports(fake, LOUNGE) == before + 1, f"{reports(fake, LOUNGE) - before} report requests for 40 callers"
    fake.silent.add(LOUNGE)
    lounge.recordings_at = 0.0
    before = reports(fake, LOUNGE)
    t0 = asyncio.get_running_loop().time()
    await asyncio.gather(*(client.refresh_recordings(lounge, timeout_s=0.4) for _ in range(10)))
    assert asyncio.get_running_loop().time() - t0 < 1.5 and reports(fake, LOUNGE) == before + 1
    fake.silent.discard(LOUNGE)
    saved = fetchtv.REPORT_REQUEST_TTL_S
    fetchtv.REPORT_REQUEST_TTL_S = 0.1  # an unanswered request is asked again later
    await asyncio.sleep(0.2)
    await client.refresh_recordings(lounge)
    fetchtv.REPORT_REQUEST_TTL_S = saved
    assert reports(fake, LOUNGE) == before + 2 and lounge.recordings_at > 0

    # A request that could not be sent (no connection) leaves nothing "in flight".
    calls = []
    real_send = client._send

    async def failing(*args, **kwargs):
        calls.append(args)
        raise fetchtv.NotConnectedError("down")

    client._send = failing
    lounge.recordings_at = 0.0
    await client.refresh_recordings(lounge)
    await client.refresh_recordings(lounge)
    client._send = real_send
    assert len(calls) == 2, "the second caller was left waiting on a request that was never sent"

    # Stopped with a report request pending: it is never sent, nothing is left over.
    fetchtv.RECORDING_REPORT_DELAY_S = 1.0
    before = reports(fake, LOUNGE)
    await fake.push(LOUNGE, "RECORDINGS_UPDATE", {})
    await wait_for(lambda: client._report_pending)
    await client.stop()
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(1.2)
    assert reports(fake, LOUNGE) == before, (reports(fake, LOUNGE), before)
    assert not client._tasks and not client._report_pending, (client._tasks, client._report_pending)
    await fake.stop()
    print("STRESS_RECORDINGS_PASSED")


logging.disable(logging.CRITICAL)
asyncio.run(asyncio.wait_for(main(), 60))
