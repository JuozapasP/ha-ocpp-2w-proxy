#!/usr/bin/env python3
"""Write ocpp-2w-proxy.ini from the add-on's /data/options.json.

The ini is the upstream project's config format, plus the keys added by
patches/0001-ha-id-remap-and-pings.patch (separate charge point ids, ping
intervals, and an optional primary password).
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
from io import StringIO
from pathlib import Path
from urllib.parse import urlsplit

# Container port published by config.yaml. The process must bind this port
# for the add-on Network mapping to reach it.
PUBLISHED_PORT = 9100


_REQUIRED_HINTS = {
    "primary_cp_id": (
        "Set primary_cp_id to the Circle Link charge point id "
        "(<YOUR_CIRCLELINK_ID>) on the Configuration tab, then restart the add-on."
    ),
    "secondary_cp_id": (
        "Set secondary_cp_id to the charge point id the Home Assistant OCPP "
        "integration expects (<CHARGER_ID>), then restart the add-on."
    ),
    "secondary_url": (
        "Set secondary_url to the Home Assistant OCPP base URL, for example "
        "ws://<HA_IP>:9000/ or ws://homeassistant.local:9000/, with no charge "
        "point id, then restart the add-on."
    ),
}


def _require_text(options: dict, key: str) -> str:
    hint = _REQUIRED_HINTS.get(key)
    missing = key not in options or options[key] is None
    value = "" if missing else str(options[key]).strip()
    if missing or not value:
        if hint:
            raise SystemExit(f"ERROR: {key} is empty. {hint}")
        raise SystemExit(f"ERROR: {key} is empty.")
    if any(char in value for char in "\r\n\0"):
        raise SystemExit(f"ERROR: {key} contains a newline.")
    return value


def _check_base_url(url: str, key: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        raise SystemExit(f"{key} must be a ws:// or wss:// URL, got {url!r}")
    if parts.username or parts.password:
        raise SystemExit(
            f"{key} must not include a username or password. "
            "Set primary_auth_password for the primary CSMS."
        )
    if parts.query or parts.fragment:
        raise SystemExit(f"{key} must not include a query or fragment")
    if parts.path not in ("", "/"):
        raise SystemExit(
            f"{key} is the CSMS base URL. Leave the charge point id out of it "
            f"(path {parts.path!r} is not allowed)."
        )


def _check_cp_id(cp_id: str, key: str) -> None:
    if cp_id in (".", "..") or any(ord(char) < 32 or char in "/\\?# \t" for char in cp_id):
        raise SystemExit(f"Invalid {key}: {cp_id!r}")


def render(options: dict) -> tuple[str, bool]:
    """Return (ini_text, primary_auth_enabled)."""
    try:
        port = int(options.get("listen_port", PUBLISHED_PORT))
    except (TypeError, ValueError) as exc:
        raise SystemExit("listen_port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise SystemExit("listen_port must be between 1 and 65535")

    primary_url = _require_text(options, "primary_url")
    secondary_url = _require_text(options, "secondary_url")
    primary_cp_id = _require_text(options, "primary_cp_id")
    secondary_cp_id = _require_text(options, "secondary_cp_id")
    _check_base_url(primary_url, "primary_url")
    _check_base_url(secondary_url, "secondary_url")
    _check_cp_id(primary_cp_id, "primary_cp_id")
    _check_cp_id(secondary_cp_id, "secondary_cp_id")

    level = str(options.get("log_level", "info")).strip().lower()
    if level not in ("debug", "info", "warning", "error"):
        raise SystemExit("log_level must be one of debug, info, warning, error")
    proxy_level = level.upper()
    # Cap the library loggers at INFO. websockets DEBUG prints HTTP headers,
    # including an Authorization value when primary auth is set.
    ws_level = proxy_level if proxy_level in ("WARNING", "ERROR") else "INFO"

    raw_password = options.get("primary_auth_password")
    password = "" if raw_password is None else str(raw_password).strip()
    if any(char in password for char in "\r\n\0"):
        raise SystemExit("primary_auth_password contains a newline")

    parser = configparser.ConfigParser(interpolation=None)
    parser["logging"] = {
        "proxy": proxy_level,
        "websockets.client": ws_level,
        "websockets.server": ws_level,
    }
    parser["host"] = {
        "addr": "0.0.0.0",
        "port": str(port),
        # Elinta HomeBox Slim does not answer WebSocket PINGs.
        "ping_interval": "none",
        "ping_timeout": "none",
    }
    ext = {
        "server": primary_url,
        "server_cp_id": primary_cp_id,
        "secondary_server": secondary_url,
        "secondary_cp_id": secondary_cp_id,
        "upstream_ping_interval": "20",
        "upstream_ping_timeout": "20",
    }
    if password:
        ext["server_auth_password"] = password
    parser["ext-server"] = ext

    buffer = StringIO()
    buffer.write(
        "; Generated from /data/options.json. Rewritten every time the add-on starts.\n"
        "; Charger-facing WebSocket pings are off. Upstream pings stay at 20 seconds.\n"
    )
    parser.write(buffer)

    if port != PUBLISHED_PORT:
        print(
            f"WARNING: listen_port is {port}, but the add-on publishes container "
            f"port {PUBLISHED_PORT}. Leave listen_port at {PUBLISHED_PORT} and "
            "change the host port on the add-on Network page instead.",
            file=sys.stderr,
        )
    return buffer.getvalue(), bool(password)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--options", required=True, help="Path to /data/options.json")
    parser.add_argument("--output", required=True, help="Where to write the ini file")
    args = parser.parse_args(argv)

    options_path = Path(args.options)
    if not options_path.is_file():
        print(f"Options file not found: {options_path}", file=sys.stderr)
        return 1
    try:
        options = json.loads(options_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Invalid options.json: {exc}", file=sys.stderr)
        return 1
    if not isinstance(options, dict):
        print("options.json must be a JSON object", file=sys.stderr)
        return 1

    rendered, auth_enabled = render(options)
    output = Path(args.output)
    output.write_text(rendered, encoding="utf-8")
    os.chmod(output, 0o600)
    # The password stays in the ini. This line only says whether auth is on.
    print(
        f"Wrote proxy config listen_port={options.get('listen_port', PUBLISHED_PORT)} "
        f"primary_auth={'on' if auth_enabled else 'off'}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
