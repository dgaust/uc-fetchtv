#!/usr/bin/env python3
"""
Remote Two/3 integration driver for FetchTV set-top boxes.

Signs in to the FetchTV service with the account's activation code and PIN
(via the built-in fetchtv client) and shows each box as a media player. See
README.md for the architecture.

:license: MPL-2.0, see LICENSE for more details.
"""

import json
import logging
import os
import re

import ucapi

import box_manager
import config
import logsetup
import setup_flow
from shared import api, loop

_LOG = logging.getLogger("driver")


@api.listens_to(ucapi.Events.CONNECT)
async def on_connect() -> None:
    """
    Remote wants us to connect: sign in to FetchTV.

    The Remote waits for a ``device_state`` event after every ``connect``.
    ``connect()`` is a no-op when already signed in, so the device state is
    re-asserted explicitly - otherwise the integration card stays
    "Disconnected" despite a healthy connection.
    """
    _LOG.debug("Remote event: connect")
    await box_manager.connect()
    await box_manager.report_device_state()


@api.listens_to(ucapi.Events.DISCONNECT)
async def on_disconnect() -> None:
    """Remote wants us to disconnect: close the FetchTV connection after a grace period."""
    _LOG.debug("Remote event: disconnect")
    box_manager.disconnect_later()


@api.listens_to(ucapi.Events.ENTER_STANDBY)
async def on_enter_standby() -> None:
    """Remote entered standby: close the connection after a grace period, not at once."""
    _LOG.debug("Remote event: enter_standby")
    box_manager.disconnect_later()


@api.listens_to(ucapi.Events.EXIT_STANDBY)
async def on_exit_standby() -> None:
    """Remote woke up: reconnect and re-assert device state."""
    _LOG.debug("Remote event: exit_standby")
    await box_manager.connect()
    await box_manager.report_device_state()


@api.listens_to(ucapi.Events.SUBSCRIBE_ENTITIES)
async def on_subscribe_entities(entity_ids: list[str]) -> None:
    """Entities got subscribed: make sure we're connected and push current state."""
    _LOG.debug("Subscribe entities: %s", entity_ids)
    await box_manager.connect()
    box_manager.push_subscribed_state(entity_ids)


def _driver_version() -> str:
    try:
        with open("driver.json", encoding="utf-8") as f:
            return str(json.load(f).get("version", "?"))
    except (OSError, ValueError):
        return "?"


def _redact_setup_data(record: logging.LogRecord) -> bool:
    """Blank the activation code, PIN and typed text out of ucapi's message logging."""
    message = record.getMessage()
    if "setup_data" in message:
        message = re.sub(r'("setup_data"\s*:\s*)\{[^}]*\}', r'\1{"redacted": true}', message)
        record.msg, record.args = message, None
    # Text typed into a box (a TYPE: command) can be a password.
    if "TYPE:" in message:
        record.msg, record.args = re.sub(r'"TYPE:[^"]*"', '"TYPE:<redacted>"', message), None
    return True


async def main() -> None:
    """Load configuration and start the integration driver."""
    logging.basicConfig(
        format="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # ucapi logs every websocket message at DEBUG, including the setup request
    # that carries the activation code and PIN, and any typed text. Keep it quiet
    # unless asked for, and never let those values reach the log.
    logging.getLogger("ucapi.api").addFilter(_redact_setup_data)
    env_level, bad_level = logsetup.environment_level()
    logsetup.apply(env_level or "INFO")  # until the saved settings are read

    config.init(api.config_dir_path)

    level = logsetup.choose()
    logsetup.apply(level)
    _LOG.info(
        "FetchTV driver %s starting (log level %s from %s, config in %s, session grace %.0f s, %s handshake)",
        _driver_version(),
        level,
        logsetup.source(),
        api.config_dir_path,
        box_manager.SESSION_GRACE_S,
        "original" if os.getenv("UC_FETCH_LEGACY_HANDSHAKE", "").strip().lower() in ("1", "true", "yes", "on") else "app-style",
    )
    if bad_level:
        _LOG.warning("Ignoring UC_LOG_LEVEL=%r: use DEBUG, INFO, WARNING or ERROR", os.getenv("UC_LOG_LEVEL"))
    if level == "DEBUG":
        _LOG.info("Debug logging is on: every message to and from the boxes is logged (sign-in details and typed text are not)")

    # Register entities from the cached box list BEFORE starting the API
    # server, so they are already available when the Remote reconnects and
    # immediately re-subscribes (which it does from its own memory).
    box_manager.register_cached_entities()

    await api.init("driver.json", setup_flow.driver_setup_handler)

    if config.is_configured():
        await box_manager.connect()


if __name__ == "__main__":
    loop.run_until_complete(main())
    loop.run_forever()
