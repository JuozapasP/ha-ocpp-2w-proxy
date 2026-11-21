# OCPP Two-Way Proxy

One OCPP 1.6J charger connects to this add-on. The add-on connects onward to two central systems at once:

- **Primary:** Circle Link. Its replies are the ones the charger sees.
- **Secondary:** the Home Assistant [OCPP integration](https://github.com/lbbrhzn/ocpp). It receives every charger call and can send its own commands. Its replies to charger-initiated calls are not sent back to the charger, so they cannot fight Circle Link.

Point the charger at Home Assistant's address on port **9100**:

`ws://<HA_IP>:9100/<CHARGER_ID>`

The Home Assistant integration stays on port **9000**. `primary_cp_id`, `secondary_cp_id`, and `secondary_url` are blank until you set them. The add-on exits with a configuration error if any of those three is empty. Install and rollback steps are in the Documentation tab.

## Why upstream is patched

The image installs [ocpp-2w-proxy](https://github.com/ocpp-balanz/ocpp-2w-proxy) at commit `5e02d2ce3becb4c334c57e21f386906376ff537c` and applies `patches/0001-ha-id-remap-and-pings.patch`. Unpatched, that project cannot serve a charger whose two backends use different ids:

- It appends the **incoming** path id to both CSMS URLs. Circle Link must be called as `wss://ocpp.circlelink.app/<YOUR_CIRCLELINK_ID>` while Home Assistant must be called as `ws://<HA_IP>:9000/<CHARGER_ID>`.
- It rejects any charge point id that is not strictly alphanumeric, so a hyphenated `<CHARGER_ID>` never gets past the handshake check.
- It sets `ping_timeout` but not `ping_interval`. The websockets library then pings the charger every 20 seconds and closes the socket when the charger does not answer. Charger-facing pings are disabled. Connections toward the CSMS still ping every 20 seconds.
- A dropped secondary connection used to close the primary session as well. Home Assistant being down now only drops the secondary link; Circle Link stays up and the secondary is retried.

The patch also pins the WebSocket subprotocol to `ocpp1.6`, turns off permessage-deflate (it breaks some chargers), and opens `wss://` with the system CA bundle.

Upstream `requirements.txt` lists `cryptography` and `pydantic`. The proxy never imports them, so the image does not install them. The runtime dependency is `websockets==15.0.1`.

## How add-on options map to the proxy ini

On start, `run.sh` rejects an empty `primary_cp_id`, `secondary_cp_id`, or `secondary_url`. `render_config.py` reads `/data/options.json` and writes `/tmp/ocpp-2w-proxy.ini` (mode `0600`). The password is not printed in the log.

| Add-on option | Ini key | What it does |
| --- | --- | --- |
| `listen_port` | `host.port` | Bind address is `0.0.0.0`. Default `9100`, which is the published container port. |
| `primary_url` | `ext-server.server` | Base URL only. Default `wss://ocpp.circlelink.app/`. The id is not part of this value. |
| `primary_cp_id` | `ext-server.server_cp_id` | Required. Appended to the primary URL. Blank by default. |
| `primary_auth_password` | `ext-server.server_auth_password` | Optional. Sent as HTTP Basic auth, username = `primary_cp_id`. Omit it for Circle Link. The key is absent when the option is empty. |
| `secondary_url` | `ext-server.secondary_server` | Required base URL of the Home Assistant OCPP integration. Blank by default. |
| `secondary_cp_id` | `ext-server.secondary_cp_id` | Required. Appended to the secondary URL. Blank by default. |
| `log_level` | `logging.proxy` | `debug`, `info`, `warning`, or `error`. The websockets loggers never go above `info`, so debug does not dump HTTP headers. |

These are written as well, and are not add-on options:

| Ini key | Value | Why |
| --- | --- | --- |
| `host.ping_interval` / `host.ping_timeout` | `none` | Some chargers, including the Elinta HomeBox, do not answer WebSocket PINGs. |
| `ext-server.upstream_ping_interval` / `upstream_ping_timeout` | `20` | Keepalive toward Circle Link and Home Assistant. |

Any path on port 9100 is accepted (`ws://<HA_IP>:9100/<CHARGER_ID>` or another id). That path is only the charger's local name. Upstream paths always use the two configured ids. After you fill in the options the proxy dials:

- `wss://ocpp.circlelink.app/<YOUR_CIRCLELINK_ID>`
- `ws://<HA_IP>:9000/<CHARGER_ID>`

Message rules, unchanged from upstream: charger calls go to both CSMS; primary replies and errors go back to the charger; secondary replies to those calls are dropped; a call from either CSMS is sent to the charger and the matching reply returns only to the CSMS that sent it.

OCPP JSON bodies are not edited. Only the WebSocket path changes per backend.

## License

Add-on glue is MIT, see the repository `LICENSE`. The proxy itself is MIT, copyright Jens Vedel Markussen, see `UPSTREAM_LICENSE`.
