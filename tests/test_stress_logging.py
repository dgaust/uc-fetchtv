"""The activation code, PIN and auth cookie never reach the log, with the driver at DEBUG.

Runs the driver the way a user does with UC_LOG_LEVEL=DEBUG (which makes the
Unfolded Circle library log every websocket message, including the setup
request that carries the code and PIN), does a setup, subscribes, sends
commands, drops the connection, and searches everything logged.
"""
import asyncio
import io
import json
import logging
import os
import tempfile

os.environ["UC_DISABLE_MDNS_PUBLISH"] = "true"
os.environ["UC_INTEGRATION_HTTP_PORT"] = "9096"
os.environ["UC_LOG_LEVEL"] = "DEBUG"
CONFIG_DIR = tempfile.mkdtemp()
os.environ["UC_CONFIG_HOME"] = CONFIG_DIR

import _path  # noqa: E402 - puts intg-fetchtv on sys.path
os.chdir(_path.INTG_DIR)

import box_manager  # noqa: E402
import driver  # noqa: E402
import fake_fetch  # noqa: E402
import websockets  # noqa: E402
from shared import loop as uc_loop  # noqa: E402

# Distinctive values, so a match can only be a leak.
CODE, PIN = "ACTSECRET7731", "PINSECRET4482"
TYPED = "HUNTER2TYPED"  # text typed into a box (it can be a password)
fake_fetch.ACTIVATION_CODE, fake_fetch.PIN = CODE, PIN


async def main():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    fake = fake_fetch.FakeFetch()
    await fake.start()
    box_manager.client_factory = fake.client
    startup = io.StringIO()  # the driver's own start-up lines come before the handler below
    logging.getLogger("driver").addHandler(logging.StreamHandler(startup))
    await driver.main()  # sets the logging up from UC_LOG_LEVEL
    logging.getLogger().addHandler(handler)
    logging.getLogger("websockets.server").setLevel(logging.WARNING)  # the fake service's side
    assert logging.getLogger("fetchtv").level == logging.DEBUG and logging.getLogger("ucapi.api").level == logging.DEBUG

    ws = None
    for _ in range(40):
        try:
            ws = await websockets.connect("ws://127.0.0.1:9096")
            break
        except OSError:
            await asyncio.sleep(0.25)
    n = 0

    async def request(msg, data=None):
        nonlocal n
        n += 1
        await ws.send(json.dumps({"kind": "req", "id": n, "msg": msg, "msg_data": data or {}}))
        while True:
            resp = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if resp.get("kind") == "resp" and resp.get("req_id") == n:
                return resp

    await request("setup_driver", {"setup_data": {"activation_code": CODE, "pin": "WRONGPIN5521"}})  # rejected
    await request("setup_driver", {"setup_data": {"activation_code": CODE, "pin": PIN}})
    for _ in range(100):
        if box_manager.is_connected():
            break
        await asyncio.sleep(0.1)
    entities = [e["entity_id"] for e in (await request("get_available_entities"))["msg_data"]["available_entities"]]
    await request("subscribe_events", {"entity_ids": entities})
    lounge = next(e for e in entities if e.startswith("media_player."))
    await request("entity_command", {"entity_type": "media_player", "entity_id": lounge, "cmd_id": "channel_up"})
    await request("browse_media", {"entity_id": lounge, "media_id": "recordings", "paging": {"page": 1, "limit": 5}})
    remote = next(e for e in entities if e.startswith("remote."))
    await request("entity_command", {"entity_type": "remote", "entity_id": remote,
                                     "cmd_id": "send_cmd", "params": {"command": "TYPE:" + TYPED}})
    await fake.drop_connections(code=1011)
    for _ in range(100):
        await asyncio.sleep(0.1)
        if box_manager.is_connected():
            break
    await asyncio.sleep(0.5)
    await ws.close()
    await box_manager.disconnect()
    await fake.stop()

    log = stream.getvalue()
    assert "setup_data" in log or "FetchTV" in log, "nothing was logged, so the check proves nothing"
    assert len(log.splitlines()) > 30, len(log.splitlines())
    assert "\"redacted\": true" in log, "the setup request was not logged (or not redacted), so the check proves less"
    # With debug on, the log shows what the driver does: start-up, HTTP calls (path only),
    # the connection, every message each way, and each command with its outcome.
    assert "FetchTV driver " in startup.getvalue() and "log level DEBUG" in startup.getvalue(), startup.getvalue()
    assert "Debug logging is on" in startup.getvalue()
    for expected in ("HTTP POST /v3/authenticate -> 200",
                     "Connected to FetchTV's message service", " -> PING", " -> ARE_YOU_ALIVE", " -> MEDIA_STATE",
                     " <- I_AM_ALIVE", " is reachable (first answer:", "Pushing media_player.",
                     "Media player command channel_up for ", "Remote command send_cmd {'command': 'TYPE:<"):
        assert expected in log, f"{expected!r} missing from the debug log"
    for secret in (CODE, PIN, "secret-token", "WRONGPIN5521", TYPED):
        leaked = [line for line in log.splitlines() if secret in line]
        assert not leaked, f"{secret!r} reached the log:\n" + "\n".join(leaked[:5])
    with open(os.path.join(CONFIG_DIR, "config.json"), encoding="utf-8") as f:
        assert json.load(f)["pin"] == PIN  # (it is saved, in the config file only)
    print("STRESS_LOGGING_PASSED")


uc_loop.run_until_complete(asyncio.wait_for(main(), 60))
