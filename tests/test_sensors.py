"""The status sensors: what each shows, and when they are unavailable."""
import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import fetchtv
import sensors
from fetchtv import Box
from ucapi.sensor import Attributes, States

TID = "111|aa"


class Client:
    connected = True


box = Box(terminal_id=TID, name="Lounge", online=True, standby=False)
box.recordings = [fetchtv.Recording(id="1", title="A"), fetchtv.Recording(id="2", title="B")]
box.scheduled_count = 3
box.active_recording_ids = ["2"]
box.recordings_at = 1.0
box.storage_info = {"freeSize": 227.43, "hdTotalSize": 961.4, "recordingsAllocation": 801}

ids = sensors.entity_ids(TID)
assert ids == [f"sensor.{TID}.{s}" for s in
               ("recordings", "scheduled", "recording_now", "free_space", "disk_used")], ids
built = sensors.build_sensors(TID, "Lounge")
assert [s.id for s in built] == ids
assert built[0].name == {"en": "Lounge recordings"} and built[2].name == {"en": "Lounge recording now"}

attrs = sensors.attributes(Client(), box)
assert attrs[ids[0]][Attributes.VALUE] == 2 and attrs[ids[0]][Attributes.STATE] == States.ON
assert attrs[ids[1]][Attributes.VALUE] == 3
assert attrs[ids[2]][Attributes.VALUE] == "Yes"
assert attrs[ids[3]][Attributes.VALUE] == 227.4 and attrs[ids[3]][Attributes.UNIT] == "GB"
assert attrs[ids[4]][Attributes.VALUE] == 76.3 and attrs[ids[4]][Attributes.UNIT] == "%", attrs[ids[4]]
# Without storage figures (or with nonsense) just those two are unavailable.
for odd in (None, "x", {}, {"freeSize": "a"}, {"freeSize": 5, "hdTotalSize": 0}):
    box.storage_info = odd
    a = sensors.attributes(Client(), box)
    assert a[ids[4]][Attributes.STATE] == States.UNAVAILABLE, odd
    assert a[ids[0]][Attributes.STATE] == States.ON
box.storage_info = {"freeSize": 5, "hdTotalSize": 4}
assert sensors.attributes(Client(), box)[ids[4]][Attributes.VALUE] == 0.0, "clamped, never negative"
box.storage_info = {"freeSize": 227.43, "hdTotalSize": 961.4}
box.active_recording_ids = []
assert sensors.attributes(Client(), box)[ids[2]][Attributes.VALUE] == "No"

# Unavailable until the box has sent its report, when offline, or with no connection.
for broken, client in ((dict(recordings_at=0.0), Client()), (dict(online=False), Client()), ({}, None)):
    b = Box(terminal_id=TID, name="Lounge", online=True, standby=False, recordings_at=1.0)
    for k, v in broken.items():
        setattr(b, k, v)
    assert all(a[Attributes.STATE] == States.UNAVAILABLE for a in sensors.attributes(client, b).values())

print("SENSORS_TEST_PASSED")
