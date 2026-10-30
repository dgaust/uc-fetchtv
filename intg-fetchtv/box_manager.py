"""
Owns the single FetchTV client for the configured account.

Signs in with ``fetchtv.FetchTvClient``, keeps one media-player entity per box
in sync with what the box reports, and translates the service connection into
the driver's device state.

Entities are registered from the cached box list at startup (see
``register_cached_entities``) so they exist the moment the Remote reconnects
and re-subscribes, before the sign-in has finished.

:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
import logging
import time
from typing import Any

import ucapi
from ucapi.media_player import Attributes as Attr

import config
import entities as ent
import fetchtv
import remote_entity
import sensors
from shared import api, loop

_LOG = logging.getLogger(__name__)

# How often the shown programme is re-checked, so the title moves on when a
# programme ends even if the box doesn't report anything.
REFRESH_INTERVAL_S = 30.0
# During setup, how long to wait for the websocket and the boxes' first reports.
_SETUP_BOX_WAIT_S = 8.0

_client: fetchtv.FetchTvClient | None = None
_client_task: asyncio.Task | None = None
_refresh_task: asyncio.Task | None = None
# Last attributes pushed per entity (without the always-moving position).
_last_pushed: dict[str, dict[str, Any]] = {}

_MOVING_ATTRS = (Attr.MEDIA_POSITION, Attr.MEDIA_POSITION_UPDATED_AT)

# Creates the client; the tests point it at a fake service.
client_factory = fetchtv.FetchTvClient


def get_client() -> fetchtv.FetchTvClient | None:
    """The current client, for entity command handlers."""
    return _client


def is_connected() -> bool:
    """True while signed in with the websocket open."""
    return _client is not None and _client.connected


async def report_device_state() -> None:
    """
    (Re)assert the current device state to the Remote.

    The Remote treats the integration as connected only once it receives a
    ``device_state`` event after its ``connect`` request.
    """
    if is_connected():
        state = ucapi.DeviceStates.CONNECTED
    elif _client is not None:
        state = ucapi.DeviceStates.CONNECTING
    else:
        state = ucapi.DeviceStates.DISCONNECTED
    await api.set_device_state(state)


def register_cached_entities() -> None:
    """Register available entities from the cached box list (called at startup)."""
    for entry in config.get_boxes():
        _ensure_entity(entry["terminal_id"], entry.get("name") or entry["terminal_id"])
    _LOG.info("Registered %d cached box(es) as available entities", len(config.get_boxes()))


async def connect() -> None:
    """Sign in with the saved account, if not already running."""
    if _client is not None:
        return
    code, pin = config.get_credentials()
    if not code or not pin:
        _LOG.debug("Cannot connect: no Fetch account configured yet")
        return
    await _start(client_factory(code, pin, device_id=config.get_device_id(), drm_id=config.get_drm_id()))


async def connect_for_setup(activation_code: str, pin: str) -> list[fetchtv.Box]:
    """
    Sign in with new credentials for driver setup and return the account's boxes.

    Raises ``fetchtv.AuthError`` if the credentials are rejected and
    ``fetchtv.FetchTvError`` if the service can't be reached. On success the
    new client replaces any existing one and stays running, so there is only
    ever one connection for the lifetime of the driver.
    """
    client = client_factory(
        activation_code, pin, device_id=config.get_device_id(), drm_id=config.get_drm_id()
    )
    boxes = await client.login()
    await disconnect()
    await _start(client)
    try:
        async with asyncio.timeout(_SETUP_BOX_WAIT_S):
            await client.ready.wait()
            while not all(b.has_state for b in boxes.values()):
                await asyncio.sleep(0.25)
    except TimeoutError:
        _LOG.info("Not every box answered during setup; they will appear when they do")
    for box in boxes.values():
        _ensure_entity(box.terminal_id, box.name)
    return list(boxes.values())


async def _start(client: fetchtv.FetchTvClient) -> None:
    global _client, _client_task, _refresh_task
    _client = client
    _last_pushed.clear()
    client.subscribe(_on_client_update)
    _client_task = loop.create_task(client.run())
    _refresh_task = loop.create_task(_refresh_periodically())
    await api.set_device_state(ucapi.DeviceStates.CONNECTING)


async def disconnect() -> None:
    """Disconnect from FetchTV, if connected."""
    global _client, _client_task, _refresh_task
    client, task, refresh = _client, _client_task, _refresh_task
    _client = _client_task = _refresh_task = None
    if refresh is not None:
        refresh.cancel()
    try:
        if client is not None:
            _LOG.info("Disconnecting from FetchTV")
            await client.stop()
    finally:
        if task is not None:
            task.cancel()
    # Closing the old connection can take a while. If a new one was started
    # meanwhile (the Remote woke straight after sleeping), it is not disconnected.
    if _client is None:
        await api.set_device_state(ucapi.DeviceStates.DISCONNECTED)


def push_subscribed_state(entity_ids: list[str]) -> None:
    """Push current state for freshly subscribed entities."""
    for entity_id in entity_ids:
        _last_pushed.pop(entity_id, None)
    if _client is not None:
        for box in _client.boxes.values():
            _push_box(box)


async def _on_client_update(box: fetchtv.Box | None) -> None:
    client = _client
    if client is None:
        return
    if box is None:
        await api.set_device_state(
            ucapi.DeviceStates.CONNECTED if client.connected else ucapi.DeviceStates.CONNECTING
        )
        for b in client.boxes.values():
            _push_box(b)
        _save_box_cache(client)
        return
    _ensure_entity(box.terminal_id, box.name)
    _push_box(box)
    # Then fetch the guide for the new channel if needed and push again.
    if not box.standby and await client.refresh_program(box):
        _push_box(box)


async def _refresh_periodically() -> None:
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_S)
        client = _client
        if client is None or not client.connected:
            continue
        try:
            await client.prefetch_programs()
        except Exception:  # noqa: BLE001 - never let a guide hiccup stop the refresh loop
            _LOG.exception("Guide prefetch failed")
        for box in list(client.boxes.values()):
            if box.has_state and not box.standby:
                await client.refresh_program(box)
            _push_box(box)


def _ensure_entity(terminal_id: str, name: str) -> None:
    if not api.available_entities.contains(ent.entity_id(terminal_id)):
        api.available_entities.add(ent.build_entity(terminal_id, name, get_client))
    if not api.available_entities.contains(ent.select_entity_id(terminal_id)):
        api.available_entities.add(ent.build_select(terminal_id, name, get_client))
    if not api.available_entities.contains(remote_entity.entity_id(terminal_id)):
        api.available_entities.add(remote_entity.build_remote(terminal_id, name, get_client))
    for sensor in sensors.build_sensors(terminal_id, name):
        if not api.available_entities.contains(sensor.id):
            api.available_entities.add(sensor)


def _push_box(box: fetchtv.Box) -> None:
    client = _client
    now_ms = int(time.time() * 1000)
    program = client.cached_program(box, now_ms) if client else None
    _push_entity(ent.entity_id(box.terminal_id), ent.attributes(client, box, program, now_ms))
    _push_entity(ent.select_entity_id(box.terminal_id), ent.select_attributes(client, box))
    _push_entity(remote_entity.entity_id(box.terminal_id), remote_entity.attributes(client, box))
    for eid, attrs in sensors.attributes(client, box).items():
        _push_entity(eid, attrs)


def _push_entity(eid: str, attrs: dict[str, Any]) -> None:
    # Only subscribed entities live in configured_entities; update_attributes()
    # would just log "not found" for the rest.
    if not api.configured_entities.contains(eid):
        return
    comparable = {k: v for k, v in attrs.items() if k not in _MOVING_ATTRS}
    if _last_pushed.get(eid) == comparable:
        return
    _last_pushed[eid] = comparable
    api.configured_entities.update_attributes(eid, attrs)


def _save_box_cache(client: fetchtv.FetchTvClient) -> None:
    entries = [ent.box_cache_entry(b) for b in client.boxes.values()]
    if entries and entries != config.get_boxes():
        config.set_boxes(entries)
