"""UC_FETCH_LEGACY_HANDSHAKE: the connection style of versions before 0.4.1, to compare against the app-style one."""
import asyncio
import os
import tempfile

os.environ["UC_DISABLE_MDNS_PUBLISH"] = "true"
os.environ["UC_CONFIG_HOME"] = tempfile.mkdtemp()

import _path  # noqa: E402 - puts intg-fetchtv on sys.path
os.chdir(_path.INTG_DIR)

import box_manager  # noqa: E402
import fetchtv  # noqa: E402
from fake_fetch import LOUNGE, FakeFetch  # noqa: E402

lower = lambda h: {k.lower(): v for k, v in h.items()}  # header names are case-insensitive


async def connect(fake, **kwargs):
    client = fake.client(**kwargs)
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.ready.wait(), 5)
    async with asyncio.timeout(5):
        while not client.boxes[LOUNGE].has_state:
            await asyncio.sleep(0.02)
    await client.stop()
    await asyncio.wait_for(task, 5)
    return client


async def main():
    fake = FakeFetch()
    await fake.start()

    # The original style: the old capabilities and one shared device id on every
    # request, no User-Agent of ours, and the websocket opened with just the cookie.
    await connect(fake, legacy_handshake=True, device_id="0123456789abcdef",
                  drm_id="11111111-2222-3333-4444-555555555555")
    for h in map(lower, fake.http_headers):
        assert h["x-ftv-capabilities"] == "no_pin,android,v3.21.1.4988,tenplay_v2", h
        assert h["x-ftv-deviceid"] == "uc-intg-fetchtv" and h["x-ftv-timeout"] == "3", h
        assert not h["user-agent"].startswith("okhttp"), h
    ws_h = lower(fake.ws_headers[-1])
    assert ws_h["cookie"] == "auth=secret-token", ws_h
    assert "x-ftv-capabilities" not in ws_h and "x-ftv-deviceid" not in ws_h, ws_h
    assert fake.ws_paths[-1] in ("/", ""), fake.ws_paths  # nothing in the URL
    assert not any("verimatrix_id" in p or "auth=" in p for p in fake.ws_paths), fake.ws_paths

    # The default is the app's style, and mixing them up would fail here.
    fake.http_headers.clear()
    await connect(fake, device_id="0123456789abcdef", drm_id="11111111-2222-3333-4444-555555555555")
    for h in map(lower, fake.http_headers):
        assert h["x-ftv-capabilities"] == "no_pin,android,v3.33.2.6562,tenplay_v2", h
        assert h["user-agent"] == "okhttp/4.12.0" and h["x-ftv-deviceid"] == "0123456789abcdef", h
    assert "auth=secret-token" in fake.ws_paths[-1] and "verimatrix_id=" in fake.ws_paths[-1], fake.ws_paths
    assert "cookie" not in lower(fake.ws_headers[-1])

    # The driver passes the setting on from the environment.
    box_manager.client_factory = fake.client
    for value, expected in (("1", True), ("true", True), ("YES", True), ("", False), ("0", False), ("no", False)):
        os.environ["UC_FETCH_LEGACY_HANDSHAKE"] = value
        assert box_manager._new_client("code", "pin")._legacy is expected, value
    del os.environ["UC_FETCH_LEGACY_HANDSHAKE"]
    assert box_manager._new_client("code", "pin")._legacy is False

    await fake.stop()
    print("LEGACY_HANDSHAKE_TEST_PASSED")


asyncio.run(main())
