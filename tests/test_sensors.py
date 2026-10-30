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

ids = sensors.entity_ids(TID)
assert ids == [f"sensor.{TID}.recordings", f"sensor.{TID}.scheduled", f"sensor.{TID}.recording_now"], ids
built = sensors.build_sensors(TID, "Lounge")
assert [s.id for s in built] == ids
assert built[0].name == {"en": "Lounge recordings"} and built[2].name == {"en": "Lounge recording now"}

attrs = sensors.attributes(Client(), box)
assert attrs[ids[0]][Attributes.VALUE] == 2 and attrs[ids[0]][Attributes.STATE] == States.ON
assert attrs[ids[1]][Attributes.VALUE] == 3
assert attrs[ids[2]][Attributes.VALUE] == "Yes"
box.active_recording_ids = []
assert sensors.attributes(Client(), box)[ids[2]][Attributes.VALUE] == "No"

# Unavailable until the box has sent its report, when offline, or with no connection.
for broken, client in ((dict(recordings_at=0.0), Client()), (dict(online=False), Client()), ({}, None)):
    b = Box(terminal_id=TID, name="Lounge", online=True, standby=False, recordings_at=1.0)
    for k, v in broken.items():
        setattr(b, k, v)
    assert all(a[Attributes.STATE] == States.UNAVAILABLE for a in sensors.attributes(client, b).values())

print("SENSORS_TEST_PASSED")
