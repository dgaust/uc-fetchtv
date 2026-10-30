"""
Status sensors for a box, from what its full report says about its recordings.

Per box there are three, for automations and dashboards:

- **recordings**: how many recordings are on the box.
- **scheduled**: how many recordings are scheduled.
- **recording now**: whether the box is recording at the moment.

They only change when the box sends its full report (at start-up and when the
Recordings folder is opened), so they are as fresh as the recordings list.

:license: MPL-2.0, see LICENSE for more details.
"""

from typing import Any, Callable

from ucapi.sensor import Attributes, DeviceClasses, Sensor, States

import fetchtv
from fetchtv import Box, FetchTvClient

_PREFIX = "sensor."

ClientGetter = Callable[[], "FetchTvClient | None"]

# (suffix, label, unit, how to read it from the box)
_SENSORS: list[tuple[str, str, str, Callable[[Box], Any]]] = [
    ("recordings", "recordings", "", lambda box: len(box.recordings)),
    ("scheduled", "scheduled recordings", "", lambda box: box.scheduled_count),
    ("recording_now", "recording now", "", lambda box: "Yes" if box.active_recording_ids else "No"),
]


def entity_ids(terminal_id: str) -> list[str]:
    """The identifiers of a box's sensors."""
    return [f"{_PREFIX}{terminal_id}.{suffix}" for suffix, *_ in _SENSORS]


def build_sensors(terminal_id: str, name: str) -> list[Sensor]:
    """The sensors for a box. Values are filled in once the box reports."""
    return [
        Sensor(
            f"{_PREFIX}{terminal_id}.{suffix}",
            f"{name} {label}",
            [],
            {Attributes.STATE: States.UNAVAILABLE},
            device_class=DeviceClasses.CUSTOM,
            icon="uc:tv",
        )
        for suffix, label, _unit, _read in _SENSORS
    ]


def attributes(client: FetchTvClient | None, box: Box) -> dict[str, dict[str, Any]]:
    """Current attributes of each of a box's sensors, by entity id."""
    available = client is not None and client.connected and box.online and box.recordings_at > 0
    result: dict[str, dict[str, Any]] = {}
    for suffix, _label, unit, read in _SENSORS:
        eid = f"{_PREFIX}{box.terminal_id}.{suffix}"
        if not available:
            result[eid] = {Attributes.STATE: States.UNAVAILABLE}
        else:
            result[eid] = {Attributes.STATE: States.ON, Attributes.VALUE: read(box), Attributes.UNIT: unit}
    return result
