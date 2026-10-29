"""
Persisted driver configuration.

Stores the Fetch account's activation code and PIN (needed to sign in again
after every restart) plus a cached list of its boxes (terminal id + name). The
box cache exists so that, on a plain restart, entities can be registered
*immediately* - before the (async) sign-in completes. The Remote re-subscribes
to its remembered entities the instant it reconnects to the driver, so if the
entities aren't already registered at that moment the subscribe fails with
"entity is not available" and everything shows up disconnected/unknown.

:license: MPL-2.0, see LICENSE for more details.
"""

import json
import logging
import os

_LOG = logging.getLogger(__name__)

_FILENAME = "config.json"

_config_path: str | None = None
_activation_code: str | None = None
_pin: str | None = None
_boxes: list[dict] = []


def init(config_dir: str) -> None:
    """Set the configuration directory and load any existing configuration."""
    global _config_path, _activation_code, _pin, _boxes
    _config_path = os.path.join(config_dir, _FILENAME)
    _LOG.info("Config file: %s", _config_path)
    _activation_code, _pin, _boxes = _load()
    if is_configured():
        _LOG.info("Loaded saved config: %d cached box(es)", len(_boxes))
    else:
        _LOG.info("No saved Fetch account found - setup is required")


def _load() -> tuple[str | None, str | None, list[dict]]:
    if not _config_path or not os.path.exists(_config_path):
        return None, None, []
    try:
        with open(_config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("activation_code"), data.get("pin"), data.get("boxes", [])
    except (OSError, ValueError) as ex:
        _LOG.error("Cannot read config file %s: %s", _config_path, ex)
        return None, None, []


def is_configured() -> bool:
    """True once an activation code and PIN have been saved."""
    return bool(_activation_code and _pin)


def get_credentials() -> tuple[str | None, str | None]:
    """Return the saved (activation code, PIN)."""
    return _activation_code, _pin


def get_boxes() -> list[dict]:
    """Return the cached box list (each: terminal_id, name)."""
    return list(_boxes)


def set_account(activation_code: str, pin: str, boxes: list[dict]) -> None:
    """Persist the account credentials and its box list."""
    global _activation_code, _pin, _boxes
    _activation_code, _pin, _boxes = activation_code, pin, list(boxes)
    _write()


def set_boxes(boxes: list[dict]) -> None:
    """Persist the cached box list, keeping the credentials."""
    global _boxes
    _boxes = list(boxes)
    _write()


def _write() -> None:
    if not _config_path:
        return
    try:
        with open(_config_path, "w", encoding="utf-8") as f:
            json.dump({"activation_code": _activation_code, "pin": _pin, "boxes": _boxes}, f)
            f.flush()
            # The sandboxed custom-driver process can be killed shortly after a
            # setup completes - fsync so the write survives that.
            os.fsync(f.fileno())
        _LOG.info("Saved config: %s", _config_path)
    except OSError as ex:
        _LOG.error("Cannot write config file %s: %s", _config_path, ex)
