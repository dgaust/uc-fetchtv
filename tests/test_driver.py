"""End to end: the driver over the Remote's websocket API, against the fake FetchTV service.

Covers setup (bad PIN, then good), entity registration, live state with the
guide programme, a channel change from the Remote, and the restart path where
entities come back from the saved box cache before sign-in.
"""
import asyncio
import json
import os
import tempfile

os.environ["UC_DISABLE_MDNS_PUBLISH"] = "true"
os.environ["UC_INTEGRATION_HTTP_PORT"] = "9098"
CONFIG_DIR = tempfile.mkdtemp()
os.environ["UC_CONFIG_HOME"] = CONFIG_DIR

import _path  # noqa: E402 - puts intg-fetchtv on sys.path
os.chdir(_path.INTG_DIR)

import box_manager  # noqa: E402
import config  # noqa: E402
import driver  # noqa: E402
import entities as ent  # noqa: E402
import remote_entity  # noqa: E402
import sensors  # noqa: E402
import websockets  # noqa: E402
from fake_fetch import ACTIVATION_CODE, BEDROOM, LOUNGE, PIN, FakeFetch  # noqa: E402
from shared import api  # noqa: E402
from shared import loop as uc_loop  # noqa: E402

LOUNGE_ID = ent.entity_id(LOUNGE)
BEDROOM_ID = ent.entity_id(BEDROOM)
LOUNGE_SEL = ent.select_entity_id(LOUNGE)
BEDROOM_SEL = ent.select_entity_id(BEDROOM)
LOUNGE_REM = remote_entity.entity_id(LOUNGE)
BEDROOM_REM = remote_entity.entity_id(BEDROOM)


async def main():
    fake = FakeFetch()
    await fake.start()
    box_manager.client_factory = fake.client

    await driver.main()
    ws = None
    for _ in range(20):
        try:
            ws = await websockets.connect("ws://127.0.0.1:9098")
            break
        except OSError:
            await asyncio.sleep(0.25)
    assert ws is not None, "driver did not start"

    req_id = 0
    events: list[dict] = []

    async def request(msg, data=None, expect=None):
        nonlocal req_id
        req_id += 1
        await ws.send(json.dumps({"kind": "req", "id": req_id, "msg": msg, "msg_data": data or {}}))
        while True:
            resp = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if resp.get("kind") == "event":
                events.append(resp)
            if resp.get("kind") == "resp" and resp.get("req_id") == req_id:
                return resp

    async def wait_event(pred, timeout=10):
        async with asyncio.timeout(timeout):
            while True:
                for e in events:
                    if pred(e):
                        events.remove(e)
                        return e
                resp = json.loads(await ws.recv())
                events.append(resp)

    async def states():
        resp = await request("get_entity_states")
        return {s["entity_id"]: s["attributes"] for s in resp["msg_data"]}

    async with ws:
        resp = await request("get_available_entities")
        assert resp["msg_data"]["available_entities"] == []

        # Wrong PIN -> setup error, nothing saved.
        await request("setup_driver", {"setup_data": {"activation_code": ACTIVATION_CODE, "pin": "0"}})
        e = await wait_event(lambda e: e["msg"] == "driver_setup_change"
                             and e["msg_data"].get("state") == "ERROR")
        assert e["msg_data"]["error"] == "AUTHORIZATION_ERROR", e
        assert not config.is_configured()

        # Right PIN -> both enabled boxes become a media player and a channel select.
        await request("setup_driver", {"setup_data": {"activation_code": ACTIVATION_CODE, "pin": PIN}})
        await wait_event(lambda e: e["msg"] == "driver_setup_change" and e["msg_data"].get("state") == "OK")
        resp = await request("get_available_entities")
        found = {x["entity_id"]: x for x in resp["msg_data"]["available_entities"]}
        sensor_ids = {i for t in (LOUNGE, BEDROOM) for i in sensors.entity_ids(t)}
        assert len(sensor_ids) == 10
        assert set(found) == {LOUNGE_ID, BEDROOM_ID, LOUNGE_SEL, BEDROOM_SEL, LOUNGE_REM, BEDROOM_REM} | sensor_ids, found
        assert found[LOUNGE_REM]["name"] == {"en": "Lounge remote"}
        assert "GUIDE" in found[LOUNGE_REM]["options"]["simple_commands"]
        assert len(found[LOUNGE_REM]["options"]["user_interface"]["pages"]) == 2
        assert found[LOUNGE_ID]["name"] == {"en": "Lounge"}
        assert found[LOUNGE_ID]["device_class"] == "set_top_box"
        assert config.is_configured() and len(config.get_boxes()) == 2

        # Subscribing pushes live state; the guide programme follows shortly.
        await request("subscribe_events",
                      {"entity_ids": [LOUNGE_ID, BEDROOM_ID, LOUNGE_SEL, BEDROOM_SEL,
                                      LOUNGE_REM, BEDROOM_REM]})
        for _ in range(50):
            s = await states()
            if s.get(LOUNGE_ID, {}).get("media_title") == "Hard Quiz":
                break
            await asyncio.sleep(0.1)
        lounge = s[LOUNGE_ID]
        assert lounge["state"] == "PLAYING", lounge
        assert lounge["media_artist"] == "ABC Entertains"
        assert lounge["source"] == "23 ABC Entertains"
        assert "159 beIN SPORTS 3" in lounge["source_list"]
        assert s[BEDROOM_ID]["state"] == "OFF", s[BEDROOM_ID]
        assert s[LOUNGE_SEL]["state"] == "ON" and s[LOUNGE_SEL]["current_option"] == "23 ABC Entertains", s
        assert "159 beIN SPORTS 3" in s[LOUNGE_SEL]["options"]

        # Browse the channels with logos from the media browser.
        resp = await request("browse_media", {"entity_id": LOUNGE_ID, "media_id": "subscription",
                                              "paging": {"page": 1, "limit": 50}})
        assert resp["code"] == 200, resp
        item = next(i for i in resp["msg_data"]["media"]["items"] if i["media_id"] == "1850")
        assert item["title"] == "beIN SPORTS 3" and item["can_play"] and item["thumbnail"], item

        # Change channel from the Remote; the new programme comes through.
        resp = await request("entity_command", {"entity_type": "media_player", "entity_id": LOUNGE_ID,
                                                "cmd_id": "select_source",
                                                "params": {"source": "159 beIN SPORTS 3"}})
        assert resp["code"] == 200, resp
        await wait_event(lambda e: e["msg"] == "entity_change"
                         and e["msg_data"]["entity_id"] == LOUNGE_ID
                         and e["msg_data"]["attributes"].get("media_title") == "EFL Cup")
        assert fake.received("PLAY_CHANNEL", LOUNGE)[-1]["message"]["channelId"] == 1850

        # The remote entity sends the box's keys, including guide.
        assert s[LOUNGE_REM]["state"] == "ON" and s[BEDROOM_REM]["state"] == "OFF", s
        resp = await request("entity_command", {"entity_type": "remote", "entity_id": LOUNGE_REM,
                                                "cmd_id": "send_cmd", "params": {"command": "GUIDE"}})
        assert resp["code"] == 200, resp
        assert fake.received("KEYEVENT", LOUNGE)[-1]["message"]["keyName"] == "KEY_GUIDE"

        # Power the bedroom box on.
        resp = await request("entity_command", {"entity_type": "media_player", "entity_id": BEDROOM_ID,
                                                "cmd_id": "on"})
        assert resp["code"] == 200
        assert fake.received("KEYEVENT", BEDROOM)[-1]["message"]["keyName"] == "KEY_POWER"

    # Restart: entities are registered from the saved cache before sign-in.
    await box_manager.disconnect()
    api.available_entities.clear()
    config.init(CONFIG_DIR)
    box_manager.register_cached_entities()
    assert api.available_entities.contains(LOUNGE_ID) and api.available_entities.contains(BEDROOM_ID)

    await fake.stop()
    print("DRIVER_TEST_PASSED")


uc_loop.run_until_complete(asyncio.wait_for(main(), timeout=60))
