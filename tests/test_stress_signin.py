"""Stress: sign-in failures, odd sign-in answers, HTTP 5xx and timeouts.

Whatever the service answers, ``login()`` raises only the client's own errors
(the setup flow turns those into a message for the user) and ``run()`` keeps
trying with a growing pause, without ever logging the code, the PIN or the
auth cookie.
"""
import asyncio
import io
import json
import logging

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import fetchtv
from fake_fetch import ACTIVATION_CODE, LOUNGE, PIN, FakeFetch

LOGIN = "/v3/authenticate"


async def wait_for(cond, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


async def odd_answers(fake):
    """login() raises FetchTvError (or AuthError) for every kind of bad answer, never anything else."""
    errors = {
        "http 500": (500, None),
        "http 502 html": (502, b"<html>Bad gateway</html>"),
        "http 503": (503, b""),
        "http 429": (429, b'{"error": "slow down"}'),
        "http 401": (401, b"{}"),
        "http 403": (403, b"{}"),
        "not json": (200, b"<html>maintenance</html>"),
        "empty": (200, b""),
        "truncated": (200, b'{"__meta__": {"error": null}, "terminals": [{"id": "1|a", "fri'),
        "list": (200, b"[]"),
        "null": (200, b"null"),
        "string": (200, b'"hello"'),
        "invalid utf-8": (200, b'{"a": "' + bytes([0xFF, 0xFE]) + b'"}'),
    }
    # Parsable answers with nothing (or only some of it) usable: the boxes that can be read are kept.
    lounge = {"id": LOUNGE, "friendly_name": "Lounge", "status": "ENABLED"}
    readable = {
        "meta string": ({"__meta__": "bad"}, set()),
        "terminals string": ({"__meta__": {}, "terminals": "x"}, set()),
        "terminals number": ({"__meta__": {}, "terminals": 5}, set()),
        "terminals dict": ({"__meta__": {}, "terminals": {"id": LOUNGE}}, set()),
        "terminal without id": ({"__meta__": {}, "terminals": [{"friendly_name": "x"}]}, set()),
        "terminal null": ({"__meta__": {}, "terminals": [None, 3, "x", [1]]}, set()),
        "terminal id null": ({"__meta__": {}, "terminals": [{"id": None}]}, set()),
        "partly bad": ({"__meta__": {}, "terminals": [None, {"friendly_name": "no id"}, lounge]}, {LOUNGE}),
        "name not text": ({"__meta__": {}, "terminals": [{**lounge, "friendly_name": ["x"]}]}, {LOUNGE}),
    }
    for name, (status, body) in errors.items():
        fake.http_status[LOGIN] = status
        if body is None:
            fake.http_body.pop(LOGIN, None)
        else:
            fake.http_body[LOGIN] = body
        try:
            boxes = await fake.client().login()
        except fetchtv.FetchTvError:
            continue
        except Exception as ex:  # noqa: BLE001
            raise AssertionError(f"{name}: login() raised {type(ex).__name__}: {ex}") from ex
        raise AssertionError(f"{name}: login() succeeded with {boxes}")
    fake.http_status[LOGIN] = 200
    for name, (body, expected) in readable.items():
        fake.http_body[LOGIN] = json.dumps(body).encode()
        try:
            boxes = await fake.client().login()
        except Exception as ex:  # noqa: BLE001
            raise AssertionError(f"{name}: login() raised {type(ex).__name__}: {ex}") from ex
        assert set(boxes) == expected, (name, boxes)
    fake.http_status.clear()
    fake.http_body.clear()

    # Timeouts: a service that never answers.
    saved = fetchtv.HTTP_TIMEOUT_S
    fetchtv.HTTP_TIMEOUT_S = 0.3
    fake.login_delay = 1.0
    try:
        await fake.client().login()
        raise AssertionError("expected FetchTvError")
    except fetchtv.FetchTvError as ex:
        assert not isinstance(ex, fetchtv.AuthError), "a timeout is not a bad PIN"
    finally:
        fetchtv.HTTP_TIMEOUT_S = saved
        fake.login_delay = 0.0
    # Nothing reachable at all.
    try:
        await fetchtv.FetchTvClient("c", "p", api_base="http://127.0.0.1:1").login()
        raise AssertionError("expected FetchTvError")
    except fetchtv.FetchTvError as ex:
        assert not isinstance(ex, fetchtv.AuthError)


async def retries(fake):
    """run() keeps trying through 5xx and bad PINs, backing off, and recovers by itself."""
    fetchtv.RECONNECT_DELAY_MAX_S = 0.4
    fake.http_status[LOGIN] = 503
    client = fake.client(reconnect_delay=0.05)
    task = asyncio.create_task(client.run())
    n = len([r for r in fake.http_requests if r == ("POST", LOGIN)])
    await asyncio.sleep(1.5)
    tries = len([r for r in fake.http_requests if r == ("POST", LOGIN)]) - n
    assert 3 <= tries <= 12, f"{tries} sign-in attempts in 1.5 s against a failing service"
    assert not client.connected and not client.auth_failed
    fake.http_status.clear()
    await wait_for(lambda: client.connected, timeout=3)
    await client.stop()
    await asyncio.wait_for(task, 3)

    # A rejected PIN is flagged, retried rarely, and never opens a socket.
    fetchtv.RECONNECT_DELAY_MAX_S = 5.0
    fake.reject_login = True
    connections = fake.ws_connections
    client = fake.client(reconnect_delay=0.05)
    task = asyncio.create_task(client.run())
    await wait_for(lambda: client.auth_failed)
    n = len([r for r in fake.http_requests if r == ("POST", LOGIN)])
    await asyncio.sleep(0.8)
    assert len([r for r in fake.http_requests if r == ("POST", LOGIN)]) == n, "a rejected PIN is retried at once"
    assert fake.ws_connections == connections and not client.connected
    await client.stop()
    await asyncio.wait_for(task, 3)
    fake.reject_login = False


async def expired_cookie(fake):
    """The auth cookie stops working while the websocket stays up: guide calls sign in again."""
    client = fake.client()
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.ready.wait(), 5)
    lounge = client.boxes[LOUNGE]
    await wait_for(lambda: lounge.has_state)
    assert (await client.refresh_program(lounge)).title == "Hard Quiz"
    logins = fake.logins
    client._guide._programs.clear()
    fake.expired = True
    program = await asyncio.wait_for(client.refresh_program(lounge), 10)
    assert program is not None and program.title == "Hard Quiz", "the guide stayed down after the cookie expired"
    assert fake.logins == logins + 1 and not fake.expired
    # And only once: a service that keeps saying 401 is not signed in to again and again.
    fake.expired = True
    fake.http_status["/v2/epg/programslist"] = 401
    logins = fake.logins
    client._guide._programs.clear()
    for _ in range(5):
        await client.refresh_program(lounge)
    assert fake.logins - logins <= 1, f"{fake.logins - logins} sign-ins for 5 failed lookups"
    assert client.connected
    await client.stop()
    await asyncio.wait_for(task, 3)


async def secrets_stay_out_of_logs(fake):
    """The activation code, PIN and auth cookie never reach a log record, even at DEBUG, even on errors."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, root.handlers[:]
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG)
    logging.disable(logging.NOTSET)
    logging.getLogger("websockets.server").setLevel(logging.WARNING)  # the fake service's own side
    try:
        client = fake.client(reconnect_delay=0.05)
        task = asyncio.create_task(client.run())
        await asyncio.wait_for(client.ready.wait(), 5)
        await wait_for(lambda: all(b.has_state for b in client.boxes.values()))
        await client.refresh_program(client.boxes[LOUNGE])
        await fake.drop_connections(code=1011)
        await wait_for(lambda: not client.connected)
        await wait_for(lambda: client.connected)
        fake.http_status[LOGIN] = 500
        await fake.drop_connections(code=1011)
        await asyncio.sleep(0.3)
        fake.http_status.clear()
        fake.reject_login = True
        await fake.drop_connections(code=1011)
        await wait_for(lambda: client.auth_failed, timeout=3)
        await client.stop()
        await asyncio.wait_for(task, 3)
        fake.reject_login = False
        try:
            await fake.client(pin="9999").login()
        except fetchtv.AuthError:
            pass
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging.disable(logging.CRITICAL)
    log = stream.getvalue()
    assert "Signed in to FetchTV" in log or "FetchTV" in log, "nothing was logged, the check proves nothing"
    for secret in (ACTIVATION_CODE, PIN, "secret-token", "9999"):
        assert secret not in log, f"{secret!r} reached the log:\n" + "\n".join(l for l in log.splitlines() if secret in l)


async def main():
    fake = FakeFetch()
    await fake.start()
    saved = (fetchtv.RECONNECT_DELAY_MAX_S, fetchtv.HTTP_TIMEOUT_S)
    try:
        await odd_answers(fake)
        await retries(fake)
        await secrets_stay_out_of_logs(fake)
    finally:
        fetchtv.RECONNECT_DELAY_MAX_S, fetchtv.HTTP_TIMEOUT_S = saved
    await fake.stop()
    print("STRESS_SIGNIN_PASSED")


logging.disable(logging.CRITICAL)
asyncio.run(asyncio.wait_for(main(), 120))
