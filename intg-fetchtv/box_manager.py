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
import math
import os
import sys
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


def _grace_from_environment() -> float:
    """
    How long the FetchTV session outlives the Remote's connection, in seconds.

    The Remote wakes and sleeps all day; signing in again each time is more
    often than the Fetch app would, and a Remote back within this time just
    picks up the session that is already running. On a server that costs
    nothing, so it is 10 minutes. The on-Remote package (built with PyInstaller,
    unlike the container image) shares the Remote's battery and Wi-Fi, so it
    closes the session at once, as the Remote's own advice for standby is to
    act on it. ``UC_SESSION_GRACE_S`` overrides either (0 = close at once).
    """
    raw = os.getenv("UC_SESSION_GRACE_S")
    if raw is not None:
        try:
            value = float(raw)
        except ValueError:
            value = math.nan
        if math.isfinite(value) and value >= 0:
            return value
        _LOG.warning("Ignoring UC_SESSION_GRACE_S=%r: not a number of seconds", raw)
    return 0.0 if getattr(sys, "frozen", False) else 600.0


SESSION_GRACE_S = _grace_from_environment()

_client: fetchtv.FetchTvClient | None = None
_client_task: asyncio.Task | None = None
_refresh_task: asyncio.Task | None = None
_grace_task: asyncio.Task | None = None
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
    if _grace_task is not None:
        _LOG.debug("The Remote is back: keeping the FetchTV session that was left running")
    _cancel_grace()
    if _client is not None:
        return
    code, pin = config.get_credentials()
    if not code or not pin:
        _LOG.debug("Cannot connect: no Fetch account configured yet")
        return
    await _start(_new_client(code, pin))


def _new_client(code: str, pin: str) -> fetchtv.FetchTvClient:
    # UC_FETCH_LEGACY_HANDSHAKE: introduce the driver to Fetch the way versions
    # before 0.4.1 did, to tell whether a problem comes from mirroring the app.
    legacy = os.getenv("UC_FETCH_LEGACY_HANDSHAKE", "").strip().lower() in ("1", "true", "yes", "on")
    if legacy:
        _LOG.info("UC_FETCH_LEGACY_HANDSHAKE is set: connecting the way versions before 0.4.1 did")
    return client_factory(
        code, pin, device_id=config.get_device_id(), drm_id=config.get_drm_id(), legacy_handshake=legacy
    )


async def connect_for_setup(activation_code: str, pin: str) -> list[fetchtv.Box]:
    """
    Sign in with new credentials for driver setup and return the account's boxes.

    Raises ``fetchtv.AuthError`` if the credentials are rejected and
    ``fetchtv.FetchTvError`` if the service can't be reached. On success the
    new client replaces any existing one and stays running, so there is only
    ever one connection for the lifetime of the driver.
    """
    client = _new_client(activation_code, pin)
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


def _cancel_grace() -> None:
    global _grace_task
    task, _grace_task = _grace_task, None
    if task is not None and task is not asyncio.current_task():
        task.cancel()


def disconnect_later(grace_s: float | None = None) -> None:
    """
    Close the FetchTV session after a grace period, unless the Remote comes back first.

    For the Remote going to sleep or disconnecting: it is usually back soon, and
    keeping the session means no new sign-in each time.
    """
    global _grace_task
    if _client is None:
        return
    _cancel_grace()
    delay = SESSION_GRACE_S if grace_s is None else grace_s
    if delay > 0:
        _LOG.info("Remote went away: keeping the FetchTV session for %.0fs", delay)
    _grace_task = loop.create_task(_disconnect_after(delay))


async def _disconnect_after(delay: float) -> None:
    await asyncio.sleep(delay)
    _LOG.info("The Remote has been away for %.0f s: closing the FetchTV session", delay)
    await disconnect()


async def disconnect() -> None:
    """Disconnect from FetchTV, if connected."""
    global _client, _client_task, _refresh_task
    _cancel_grace()
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
        if client is None or not client.connected or _grace_task is not None:
            continue  # (nobody is looking while the Remote sleeps)
        try:
            await client.prefetch_programs()
            for box in list(client.boxes.values()):
                if box.has_state and not box.standby:
                    await client.refresh_program(box)
                _push_box(box)
        except Exception:  # noqa: BLE001 - never let a hiccup stop the refresh loop
            _LOG.exception("Periodic refresh failed")


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


def _short(value: Any) -> Any:
    """A value for the log: long ones (a source list, an image) as their size."""
    text = str(value)
    return value if len(text) <= 80 else f"<{len(text)} characters>"


def _push_entity(eid: str, attrs: dict[str, Any]) -> None:
    # Only subscribed entities live in configured_entities; update_attributes()
    # would just log "not found" for the rest.
    if not api.configured_entities.contains(eid):
        return
    comparable = {k: v for k, v in attrs.items() if k not in _MOVING_ATTRS}
    previous = _last_pushed.get(eid)
    if previous == comparable:
        return
    _last_pushed[eid] = comparable
    if _LOG.isEnabledFor(logging.DEBUG):
        changed = {k: v for k, v in comparable.items() if previous is None or previous.get(k) != v}
        _LOG.debug("Pushing %s: %s", eid, {k: _short(v) for k, v in changed.items()})
    api.configured_entities.update_attributes(eid, attrs)


def _save_box_cache(client: fetchtv.FetchTvClient) -> None:
    entries = [ent.box_cache_entry(b) for b in client.boxes.values()]
    if entries and entries != config.get_boxes():
        config.set_boxes(entries)
