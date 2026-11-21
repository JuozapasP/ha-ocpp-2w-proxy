#!/usr/bin/env python3
"""Behavior tests for the patched ocpp-2w-proxy and the add-on config mapping.

Run from the repository root:

    python3 tests/test_proxy.py
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import json
import logging
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets
import yaml
from websockets.exceptions import ConnectionClosed

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_COMMIT = "5e02d2ce3becb4c334c57e21f386906376ff537c"
PATCH = ROOT / "ocpp_2w_proxy" / "patches" / "0001-ha-id-remap-and-pings.patch"
FIXTURE = ROOT / "tests" / "fixtures" / "ocpp-2w-proxy.py"
RENDER_PATH = ROOT / "ocpp_2w_proxy" / "render_config.py"

PRIMARY_ID = "CLTESTID01"
SECONDARY_ID = "EB-TEST-01"
HA_HOST = "192.0.2.10"
# Fake password used only to prove it is sent on the wire and not logged.
TEST_PASSWORD = "s3cret%value"


def fail(message: str) -> None:
    raise AssertionError(message)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        fail(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_patch(directory: Path) -> Path:
    shutil.copy(FIXTURE, directory / "ocpp-2w-proxy.py")
    subprocess.check_call(
        ["patch", "-p1", "-d", str(directory), "-i", str(PATCH)],
        stdout=subprocess.DEVNULL,
    )
    return directory / "ocpp-2w-proxy.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class Csms:
    def __init__(self) -> None:
        self.connections: list = []
        self.inbox: list[str] = []
        self.server = None
        self._arrived = asyncio.Condition()

    async def handler(self, websocket) -> None:
        async with self._arrived:
            self.connections.append(websocket)
            self._arrived.notify_all()
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    message = message.decode("utf-8")
                async with self._arrived:
                    self.inbox.append(message)
                    self._arrived.notify_all()
        except ConnectionClosed:
            pass

    async def start(self, ssl_context=None) -> int:
        self.server = await websockets.serve(
            self.handler,
            "127.0.0.1",
            0,
            subprotocols=["ocpp1.6"],
            ping_interval=None,
            compression=None,
            ssl=ssl_context,
        )
        return self.server.sockets[0].getsockname()[1]

    async def wait_until(self, predicate, detail: str, timeout: float = 4) -> None:
        deadline = time.monotonic() + timeout
        async with self._arrived:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    fail(
                        f"{detail}; connections={len(self.connections)} "
                        f"inbox={self.inbox!r}"
                    )
                try:
                    await asyncio.wait_for(self._arrived.wait(), remaining)
                except asyncio.TimeoutError:
                    fail(
                        f"{detail}; connections={len(self.connections)} "
                        f"inbox={self.inbox!r}"
                    )

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None


def header_map(websocket) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in websocket.request.headers.items()}


def basic_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


async def start_proxy(module, ini_path: Path, logs: Capture):
    module.active_server = None
    module.OCPP2WProxy.proxy_list.clear()
    logging.getLogger("proxy").addHandler(logs)
    logging.getLogger("proxy").setLevel(logging.DEBUG)
    sys.argv = ["ocpp-2w-proxy.py", "--config", str(ini_path)]
    task = asyncio.create_task(module.main())
    for _ in range(100):
        server = module.active_server
        if server is not None and server.sockets:
            return task
        if task.done():
            exc = task.exception()
            fail(f"proxy exited during startup: {exc!r}")
        await asyncio.sleep(0.05)
    fail("proxy did not bind a port")


async def stop_proxy(module, task) -> None:
    server = module.active_server
    if server is not None:
        server.close()
        await server.wait_closed()
    if not task.done():
        try:
            await asyncio.wait_for(task, timeout=3)
        except asyncio.TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    module.active_server = None
    module.OCPP2WProxy.proxy_list.clear()


def write_runtime_ini(render, directory: Path, listen_port: int, primary_url: str,
                      secondary_url: str, password: str | None, log_level: str = "info") -> Path:
    options = {
        "listen_port": listen_port,
        "primary_url": primary_url,
        "primary_cp_id": PRIMARY_ID,
        "secondary_url": secondary_url,
        "secondary_cp_id": SECONDARY_ID,
        "log_level": log_level,
    }
    if password is not None:
        options["primary_auth_password"] = password
    options_path = directory / "options.json"
    ini_path = directory / "ocpp-2w-proxy.ini"
    options_path.write_text(json.dumps(options), encoding="utf-8")
    rc = render.main(["--options", str(options_path), "--output", str(ini_path)])
    if rc != 0:
        fail(f"render_config returned {rc}")
    # Speed the secondary retry up. These keys are optional in the patched proxy.
    import configparser

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ini_path)
    parser.set("ext-server", "secondary_retry_interval", "0.2")
    parser.set("ext-server", "secondary_open_timeout", "1")
    parser.set("ext-server", "primary_open_timeout", "2")
    with ini_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    return ini_path


async def charger_connect(port: int, path: str, headers: dict[str, str] | None = None):
    return await websockets.connect(
        f"ws://127.0.0.1:{port}/{path}",
        subprotocols=["ocpp1.6"],
        additional_headers=headers,
        ping_interval=None,
        compression=None,
        open_timeout=3,
    )


async def test_routing(module, render, logs: Capture) -> None:
    primary = Csms()
    secondary = Csms()
    primary_port = await primary.start()
    secondary_port = await secondary.start()
    listen = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        ini = write_runtime_ini(
            render,
            Path(tmp),
            listen,
            f"ws://127.0.0.1:{primary_port}/",
            f"ws://127.0.0.1:{secondary_port}/",
            TEST_PASSWORD,
            log_level="debug",
        )
        mode = ini.stat().st_mode & 0o777
        if mode != 0o600:
            fail(f"ini mode is {oct(mode)}, expected 0o600")
        text = ini.read_text(encoding="utf-8")
        if f"server_auth_password = {TEST_PASSWORD}" not in text:
            fail("password was not preserved in the ini (percent signs must round-trip)")
        if "ping_interval = none" not in text:
            fail("charger ping_interval was not disabled in the generated ini")
        if "upstream_ping_interval = 20" not in text:
            fail("upstream ping interval missing")
        # debug must not turn on websockets DEBUG (that logs HTTP headers)
        if "websockets.client = INFO" not in text or "proxy = DEBUG" not in text:
            fail(f"unexpected log levels in ini:\n{text}")

        task = await start_proxy(module, ini, logs)
        charger = None
        try:
            charger = await charger_connect(
                listen,
                SECONDARY_ID,
                {
                    "Authorization": "Basic from-charger",
                    "User-Agent": "ElintaHomeBox/1.0",
                },
            )
            if charger.subprotocol != "ocpp1.6":
                fail(f"charger subprotocol is {charger.subprotocol}")

            await primary.wait_until(lambda: primary.connections, "primary did not connect")
            await secondary.wait_until(lambda: secondary.connections, "secondary did not connect")
            primary_ws = primary.connections[0]
            secondary_ws = secondary.connections[0]
            if primary_ws.request.path != f"/{PRIMARY_ID}":
                fail(f"primary path {primary_ws.request.path}")
            if secondary_ws.request.path != f"/{SECONDARY_ID}":
                fail(f"secondary path {secondary_ws.request.path}")
            if primary_ws.subprotocol != "ocpp1.6" or secondary_ws.subprotocol != "ocpp1.6":
                fail("upstream subprotocol is not ocpp1.6")

            primary_headers = header_map(primary_ws)
            secondary_headers = header_map(secondary_ws)
            expected_auth = basic_header(PRIMARY_ID, TEST_PASSWORD)
            if primary_headers.get("authorization") != expected_auth:
                fail(f"primary auth header {primary_headers.get('authorization')!r}")
            if "from-charger" in primary_headers.get("authorization", ""):
                fail("charger Authorization was forwarded to the primary CSMS")
            if secondary_headers.get("authorization") != "Basic from-charger":
                fail("charger Authorization was not forwarded to the secondary CSMS")
            if primary_headers.get("user-agent") != "ElintaHomeBox/1.0":
                fail(f"primary user-agent {primary_headers.get('user-agent')!r}")
            if secondary_headers.get("user-agent") != "ElintaHomeBox/1.0":
                fail("secondary did not get the charger user-agent")
            if "sec-websocket-extensions" in primary_headers or "sec-websocket-extensions" in secondary_headers:
                fail("proxy offered permessage-deflate to a CSMS")

            proxy = None
            for _ in range(50):
                candidate = module.OCPP2WProxy.proxy_list.get(SECONDARY_ID)
                if (
                    candidate is not None
                    and candidate.primary_connection is not None
                    and candidate.secondary_connection is not None
                ):
                    proxy = candidate
                    break
                await asyncio.sleep(0.05)
            if proxy is None:
                fail("upstream connections were not stored on the session")
            if proxy.ws.ping_interval is not None:
                fail(f"charger ping_interval is {proxy.ws.ping_interval}")
            if getattr(proxy.ws, "keepalive_task", None) is not None:
                fail("charger keepalive task is running")
            if proxy.primary_connection.ping_interval != 20:
                fail(f"primary ping_interval is {proxy.primary_connection.ping_interval}")
            if proxy.primary_connection.keepalive_task is None:
                fail("primary keepalive is not running")
            if proxy.secondary_connection.ping_interval != 20:
                fail("secondary ping_interval is not 20")

            from_charger: asyncio.Queue[str] = asyncio.Queue()

            async def read_charger(socket) -> None:
                try:
                    async for message in socket:
                        if isinstance(message, bytes):
                            message = message.decode("utf-8")
                        await from_charger.put(message)
                except ConnectionClosed:
                    pass

            reader = asyncio.create_task(read_charger(charger))
            boot = json.dumps(
                [2, "boot-1", "BootNotification",
                 {"chargePointVendor": "Elinta", "chargePointModel": "HomeBox Slim"}]
            )
            await charger.send(boot)
            await primary.wait_until(lambda: boot in primary.inbox, "primary missed BootNotification")
            await secondary.wait_until(
                lambda: boot in secondary.inbox, "secondary missed BootNotification"
            )

            primary_conf = json.dumps(
                [3, "boot-1", {"status": "Accepted", "interval": 60, "from": "primary"}]
            )
            secondary_conf = json.dumps(
                [3, "boot-1", {"status": "Accepted", "interval": 300, "from": "secondary"}]
            )
            await primary_ws.send(primary_conf)
            seen = json.loads(await asyncio.wait_for(from_charger.get(), timeout=2))
            if seen[0] != 3 or seen[2].get("from") != "primary":
                fail(f"charger did not get the primary BootNotification conf: {seen}")
            await secondary_ws.send(secondary_conf)
            await asyncio.sleep(0.4)
            if not from_charger.empty():
                fail(f"secondary BootNotification conf was forwarded: {from_charger.get_nowait()}")

            sec_cmd = json.dumps(
                [2, "sec-cmd", "RemoteStopTransaction", {"transactionId": 7}]
            )
            await secondary_ws.send(sec_cmd)
            seen = json.loads(await asyncio.wait_for(from_charger.get(), timeout=2))
            if seen[1] != "sec-cmd":
                fail(f"charger did not get the secondary command: {seen}")
            sec_result = json.dumps([3, "sec-cmd", {"status": "Accepted"}])
            await charger.send(sec_result)
            await secondary.wait_until(
                lambda: sec_result in secondary.inbox, "secondary missed its command result"
            )
            if any("sec-cmd" in item and item != sec_cmd for item in primary.inbox):
                fail(f"primary received the secondary command result: {primary.inbox}")

            pri_cmd = json.dumps([2, "pri-cmd", "Reset", {"type": "Soft"}])
            await primary_ws.send(pri_cmd)
            seen = json.loads(await asyncio.wait_for(from_charger.get(), timeout=2))
            if seen[1] != "pri-cmd":
                fail(f"charger did not get the primary command: {seen}")
            pri_result = json.dumps([3, "pri-cmd", {"status": "Accepted"}])
            await charger.send(pri_result)
            await primary.wait_until(
                lambda: pri_result in primary.inbox, "primary missed its command result"
            )
            if any(item == pri_result for item in secondary.inbox):
                fail("secondary received the primary command result")
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader

            # Still alive after a pause, which is the Elinta "no PONG" case:
            # the server keepalive task is absent, so the socket is not dropped.
            await asyncio.sleep(0.5)
            heartbeat = json.dumps([2, "hb-1", "Heartbeat", {}])
            await charger.send(heartbeat)
            await primary.wait_until(lambda: heartbeat in primary.inbox, "heartbeat was not proxied")

            # A different incoming path still maps to the configured ids.
            await charger.close()
            charger = None
            await asyncio.sleep(0.2)
            other = await charger_connect(listen, "not-the-configured-id")
            charger = other
            await primary.wait_until(
                lambda: len(primary.connections) >= 2, "primary did not accept the remapped path"
            )
            await secondary.wait_until(
                lambda: len(secondary.connections) >= 2,
                "secondary did not accept the remapped path",
            )
            if primary.connections[-1].request.path != f"/{PRIMARY_ID}":
                fail("remapped primary path changed")
            if secondary.connections[-1].request.path != f"/{SECONDARY_ID}":
                fail("remapped secondary path changed")
        finally:
            if charger is not None:
                await charger.close()
            await stop_proxy(module, task)
            await primary.stop()
            await secondary.stop()

    joined = "\n".join(logs.messages)
    if TEST_PASSWORD in joined or basic_header(PRIMARY_ID, TEST_PASSWORD) in joined:
        fail("primary password or its Basic token was written to the log")
    if "Charger-facing WebSocket pings disabled" not in joined:
        fail(f"ping disable was not logged; logs={joined!r}")
    if "ocpp1.6" not in joined:
        fail("negotiated subprotocol was not logged")


async def test_secondary_outage_keeps_primary(module, render, logs: Capture) -> None:
    primary = Csms()
    secondary = Csms()
    primary_port = await primary.start()
    secondary_port = await secondary.start()
    listen = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        ini = write_runtime_ini(
            render,
            Path(tmp),
            listen,
            f"ws://127.0.0.1:{primary_port}/",
            f"ws://127.0.0.1:{secondary_port}/",
            None,
        )
        task = await start_proxy(module, ini, logs)
        charger = None
        try:
            charger = await charger_connect(listen, SECONDARY_ID)
            await primary.wait_until(lambda: primary.connections, "primary connect")
            await secondary.wait_until(lambda: secondary.connections, "secondary connect")
            await secondary.connections[0].close()
            # Give the proxy a moment to notice, then prove primary still works.
            await asyncio.sleep(0.3)
            first = json.dumps([2, "while-down", "Heartbeat", {}])
            await charger.send(first)
            await primary.wait_until(lambda: first in primary.inbox, "primary died with the secondary")
            await secondary.wait_until(
                lambda: len(secondary.connections) >= 2,
                "secondary did not reconnect",
                timeout=4,
            )
            second = json.dumps([2, "after-reconnect", "Heartbeat", {}])
            await charger.send(second)
            await secondary.wait_until(
                lambda: second in secondary.inbox, "secondary missed the message after reconnect"
            )
            await primary.wait_until(
                lambda: second in primary.inbox, "primary missed the message after reconnect"
            )
        finally:
            if charger is not None:
                await charger.close()
            await stop_proxy(module, task)
            await primary.stop()
            await secondary.stop()


async def test_primary_down_then_up(module, render, logs: Capture) -> None:
    """A refused primary closes that charger session and the listener stays up."""
    primary_port = free_port()
    secondary = Csms()
    secondary_port = await secondary.start()
    listen = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        ini = write_runtime_ini(
            render,
            Path(tmp),
            listen,
            f"ws://127.0.0.1:{primary_port}/",
            f"ws://127.0.0.1:{secondary_port}/",
            None,
        )
        task = await start_proxy(module, ini, logs)
        try:
            dropped = False
            try:
                async with await charger_connect(listen, SECONDARY_ID) as charger:
                    await asyncio.wait_for(charger.recv(), timeout=2)
            except (ConnectionClosed, asyncio.TimeoutError, OSError):
                dropped = True
            if not dropped:
                fail("charger session stayed open without a primary CSMS")

            primary = Csms()
            # Bind the port the proxy is already configured to dial.
            # free_port() released it; take it again for the real server.
            # If something else grabbed it, serve on port 0 would not match.
            try:
                primary.server = await websockets.serve(
                    primary.handler,
                    "127.0.0.1",
                    primary_port,
                    subprotocols=["ocpp1.6"],
                    ping_interval=None,
                    compression=None,
                )
            except OSError as exc:
                fail(f"could not bind the primary port after the refused attempt: {exc}")

            async with await charger_connect(listen, SECONDARY_ID) as charger:
                await primary.wait_until(lambda: primary.connections, "primary did not come up")
                call = json.dumps([2, "recovered", "Heartbeat", {}])
                await charger.send(call)
                await primary.wait_until(lambda: call in primary.inbox, "recovered session did not proxy")
            await primary.stop()
        finally:
            await stop_proxy(module, task)
            await secondary.stop()


def make_certs(directory: Path) -> tuple[Path, Path, Path]:
    ca_key = directory / "ca.key"
    ca_cert = directory / "ca.pem"
    key = directory / "server.key"
    csr = directory / "server.csr"
    cert = directory / "server.pem"
    ext = directory / "san.cnf"
    ext.write_text(
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
        "basicConstraints=CA:FALSE\n"
        "keyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n",
        encoding="utf-8",
    )
    subprocess.check_call(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "2",
            "-nodes", "-keyout", str(ca_key), "-out", str(ca_cert),
            "-subj", "/CN=OCPP Test CA",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.check_call(
        [
            "openssl", "req", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(csr), "-subj", "/CN=127.0.0.1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.check_call(
        [
            "openssl", "x509", "-req", "-in", str(csr),
            "-CA", str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial",
            "-out", str(cert), "-days", "2", "-sha256", "-extfile", str(ext),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return ca_cert, cert, key


async def test_wss_primary(module, render, logs: Capture) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        ca_cert, cert, key = make_certs(tmp_path)
        server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ssl.load_cert_chain(cert, key)
        primary = Csms()
        primary_port = await primary.start(ssl_context=server_ssl)
        secondary = Csms()
        secondary_port = await secondary.start()
        listen = free_port()
        previous = os.environ.get("SSL_CERT_FILE")
        os.environ["SSL_CERT_FILE"] = str(ca_cert)
        ini = write_runtime_ini(
            render,
            tmp_path,
            listen,
            f"wss://127.0.0.1:{primary_port}/",
            f"ws://127.0.0.1:{secondary_port}/",
            None,
        )
        task = await start_proxy(module, ini, logs)
        charger = None
        try:
            charger = await charger_connect(listen, SECONDARY_ID)
            await primary.wait_until(lambda: primary.connections, "wss primary did not complete TLS")
            if primary.connections[0].request.path != f"/{PRIMARY_ID}":
                fail("wss primary path was not remapped")
            call = json.dumps([2, "tls-1", "Heartbeat", {}])
            await charger.send(call)
            await primary.wait_until(lambda: call in primary.inbox, "wss primary missed the call")
        finally:
            if previous is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = previous
            if charger is not None:
                await charger.close()
            await stop_proxy(module, task)
            await primary.stop()
            await secondary.stop()


def test_render_and_repo(render, module) -> None:
    if module.join_cp_url("wss://ocpp.circlelink.app/", PRIMARY_ID) != (
        f"wss://ocpp.circlelink.app/{PRIMARY_ID}"
    ):
        fail("Circle Link URL was not joined correctly")
    if module.join_cp_url(f"ws://{HA_HOST}:9000/", SECONDARY_ID) != (
        f"ws://{HA_HOST}:9000/{SECONDARY_ID}"
    ):
        fail("Home Assistant URL was not joined correctly")
    if module.extract_charger_id(f"/{SECONDARY_ID}") != SECONDARY_ID:
        fail("hyphenated incoming id was rejected")
    try:
        module.extract_charger_id("/")
        fail("empty path was accepted")
    except ValueError:
        pass

    defaults = {
        "listen_port": 9100,
        "primary_url": "wss://ocpp.circlelink.app/",
        "primary_cp_id": PRIMARY_ID,
        "secondary_url": f"ws://{HA_HOST}:9000/",
        "secondary_cp_id": SECONDARY_ID,
        "log_level": "info",
    }
    text, auth = render.render(defaults)
    if auth:
        fail("empty password option was treated as enabled")
    if "server_auth_password" in text:
        fail("password key written without a password")
    if "ping_interval = none" not in text or "ping_timeout = none" not in text:
        fail("default ini still pings the charger")

    try:
        render.render({**defaults, "primary_url": f"wss://ocpp.circlelink.app/{PRIMARY_ID}"})
        fail("primary URL containing the charge point id was accepted")
    except SystemExit:
        pass
    try:
        render.render({**defaults, "primary_url": "http://ocpp.circlelink.app/"})
        fail("non-websocket URL was accepted")
    except SystemExit:
        pass

    repo = yaml.safe_load((ROOT / "repository.yaml").read_text(encoding="utf-8"))
    if repo["url"] != "https://github.com/JuozapasP/ha-ocpp-2w-proxy":
        fail(f"repository url {repo['url']}")
    if repo["maintainer"] != "JuozapasP" or not repo.get("name"):
        fail(f"repository.yaml metadata {repo}")

    config = yaml.safe_load((ROOT / "ocpp_2w_proxy" / "config.yaml").read_text(encoding="utf-8"))
    if config["arch"] != ["aarch64", "amd64"]:
        fail(f"arch {config['arch']}")
    if config["startup"] != "services" or config["boot"] != "auto" or config["init"] is not False:
        fail("startup/boot/init do not match the add-on requirements")
    if config["ports"]["9100/tcp"] != 9100:
        fail(f"ports {config['ports']}")
    if config["slug"] != "ocpp_2w_proxy":
        fail("slug does not match the add-on folder")
    options = config["options"]
    if options["primary_url"] != "wss://ocpp.circlelink.app/":
        fail("primary_url default")
    if options["primary_cp_id"] != "" or options["secondary_cp_id"] != "":
        fail("charge point id defaults must be empty")
    if options["secondary_url"] != "":
        fail("secondary_url default must be empty")
    if config["version"] != "1.1.0":
        fail(f"version {config['version']}")
    for key in ("primary_cp_id", "secondary_cp_id", "secondary_url"):
        try:
            render.render({**defaults, key: "   "})
        except SystemExit as exc:
            if key not in str(exc) or "empty" not in str(exc).lower():
                fail(f"unclear empty-value error for {key}: {exc}")
        else:
            fail(f"blank {key} was accepted")
    if options["listen_port"] != 9100 or options["log_level"] != "info":
        fail("listen_port or log_level default")
    if "primary_auth_password" in options:
        fail("a default password was committed")
    schema = config["schema"]
    for key in ("primary_cp_id", "secondary_cp_id", "secondary_url", "primary_url"):
        if schema[key] != "str":
            fail(f"{key} schema is {schema[key]!r}, expected str")
    if schema["primary_auth_password"] != "password?":
        fail("primary password is not an optional password field")
    if schema["log_level"] != "list(debug|info|warning|error)":
        fail("log_level schema")
    if schema["listen_port"] != "int":
        fail("listen_port schema")

    build = yaml.safe_load((ROOT / "ocpp_2w_proxy" / "build.yaml").read_text(encoding="utf-8"))
    for arch in ("aarch64", "amd64"):
        image = build["build_from"][arch]
        if f"{arch}-base-python:" not in image:
            fail(f"build_from {arch} is not a HA base-python image: {image}")
    if build["args"]["UPSTREAM_COMMIT"] != UPSTREAM_COMMIT:
        fail("build.yaml commit pin drifted")

    translations = yaml.safe_load(
        (ROOT / "ocpp_2w_proxy" / "translations" / "en.yaml").read_text(encoding="utf-8")
    )
    if "9100/tcp" not in translations["network"]:
        fail("translation is missing the port description")
    if set(translations["configuration"]) != set(schema):
        fail("translation keys do not match the schema")

    dockerfile = (ROOT / "ocpp_2w_proxy" / "Dockerfile").read_text(encoding="utf-8")
    for needle in (
        "ARG BUILD_FROM",
        UPSTREAM_COMMIT,
        "websockets==15.0.1",
        "ca-certificates",
        "SSL_CERT_FILE",
        "0001-ha-id-remap-and-pings.patch",
    ):
        if needle not in dockerfile:
            fail(f"Dockerfile is missing {needle}")

    docs = (ROOT / "ocpp_2w_proxy" / "DOCS.md").read_text(encoding="utf-8")
    for needle in (
        "https://github.com/JuozapasP/ha-ocpp-2w-proxy",
        "ws://<HA_IP>:9100/<CHARGER_ID>",
        "ws://<HA_IP>:9000/<CHARGER_ID>",
        "Add-on Store",
        "Repositories",
    ):
        if needle not in docs:
            fail(f"DOCS.md is missing {needle}")

    run_sh = (ROOT / "ocpp_2w_proxy" / "run.sh").read_text(encoding="utf-8")
    if not run_sh.startswith("#!/command/with-contenv bashio\n"):
        fail("run.sh shebang is not the Home Assistant s6/bashio shebang")
    if "\r" in run_sh:
        fail("run.sh has CRLF line endings")
    if "/data/options.json" not in run_sh:
        fail("run.sh does not read options.json")
    for key in ("primary_cp_id", "secondary_cp_id", "secondary_url"):
        if key not in run_sh or "bashio::log.error" not in run_sh:
            fail("run.sh does not log an error for an empty required setting")

    # Kept split so the site-specific values are not stored in this file.
    banned = ("U0017193" + "SPJMJAYZ", "23-HBS-" + "07-02", "192.168.0." + "114")
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        blob = path.read_text(encoding="utf-8")
        for item in banned:
            if item in blob:
                fail(f"{path.relative_to(ROOT)} still contains {item}")


async def async_main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        patched = apply_patch(Path(tmp))
        module = load_module("ocpp_2w_proxy_runtime", patched)
    render = load_module("ocpp_render_config", RENDER_PATH)
    test_render_and_repo(render, module)

    logs = Capture()
    await test_routing(module, render, logs)
    await test_secondary_outage_keeps_primary(module, render, logs)
    await test_primary_down_then_up(module, render, logs)
    await test_wss_primary(module, render, logs)
    print("ok")


def main() -> int:
    try:
        asyncio.run(async_main())
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
