"""
Driver setup flow.

The activation code and PIN are collected on the first driver.json setup
screen, so this just signs in, waits briefly for the boxes to report, and
saves the account. It drives the *same* persistent client that
``box_manager`` uses for the rest of the driver's life.

:license: MPL-2.0, see LICENSE for more details.
"""

import logging

from ucapi import (
    DriverSetupRequest,
    IntegrationSetupError,
    SetupAction,
    SetupComplete,
    SetupDriver,
    SetupError,
)

import box_manager
import config
import entities as ent
import fetchtv
import logsetup
import remote_entity
import sensors
from shared import api

_LOG = logging.getLogger(__name__)


async def driver_setup_handler(msg: SetupDriver) -> SetupAction:
    """Dispatch driver setup requests."""
    if isinstance(msg, DriverSetupRequest):
        return await _handle_setup(msg)
    return SetupError(error_type=IntegrationSetupError.OTHER)


def _apply_debug_option(msg: DriverSetupRequest) -> None:
    """
    Take the Debug logging tick box from the setup screen, first, so a setup that fails is logged too.

    It is the only way to turn debug logging on for a driver installed on the
    Remote (which cannot set environment variables); ``UC_LOG_LEVEL`` still wins.
    """
    debug = str(msg.setup_data.get("debug_logging", "")).strip().lower() == "true"
    if debug == config.get_debug_logging():
        return
    config.set_debug_logging(debug)
    logsetup.apply(logsetup.choose())
    _LOG.info("Debug logging turned %s on the setup screen (log level now %s)", "on" if debug else "off", logsetup.choose())


async def _handle_setup(msg: DriverSetupRequest) -> SetupAction:
    _apply_debug_option(msg)
    code =(msg.setup_data.get("activation_code") or "").strip()
    pin = (msg.setup_data.get("pin") or "").strip()
    if not code or not pin:
        return SetupError(error_type=IntegrationSetupError.AUTHORIZATION_ERROR)

    try:
        boxes = await box_manager.connect_for_setup(code, pin)
    except fetchtv.AuthError as ex:
        _LOG.error("FetchTV rejected the activation code / PIN: %s", ex)
        return SetupError(error_type=IntegrationSetupError.AUTHORIZATION_ERROR)
    except fetchtv.FetchTvError as ex:
        _LOG.error("Could not reach FetchTV: %s", ex)
        return SetupError(error_type=IntegrationSetupError.CONNECTION_REFUSED)

    if not boxes:
        _LOG.warning("Signed in, but the account has no boxes")
        await box_manager.disconnect()
        return SetupError(error_type=IntegrationSetupError.NOT_FOUND)

    # Drop entities of boxes no longer on the account (e.g. a different account).
    ids = (ent.entity_id, ent.select_entity_id, remote_entity.entity_id)
    keep = {f(b.terminal_id) for b in boxes for f in ids}
    keep |= {eid for b in boxes for eid in sensors.entity_ids(b.terminal_id)}
    for entity in list(api.available_entities.get_all()):
        if entity["entity_id"] not in keep:
            api.available_entities.remove(entity["entity_id"])

    config.set_account(code, pin, [ent.box_cache_entry(b) for b in boxes])
    _LOG.info("Setup complete: %d FetchTV box(es)", len(boxes))
    return SetupComplete()
