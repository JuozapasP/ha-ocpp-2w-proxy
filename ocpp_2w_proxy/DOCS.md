# OCPP Two-Way Proxy

An OCPP 1.6J charger can talk to Circle Link and to the Home Assistant OCPP integration at the same time. This add-on sits in front of the charger.

Leave the Home Assistant OCPP integration on port **9000**. Point the charger at this add-on on port **9100**.

## Install

1. In Home Assistant, open **Settings → Add-ons → Add-on Store**. On current Home Assistant releases the same screens are labeled **Settings → Apps → App store**.
2. Open the three-dot menu in the top right and choose **Repositories**.
3. Add this repository URL and confirm:

   `https://github.com/JuozapasP/ha-ocpp-2w-proxy`

4. Close the dialog and choose **Check for updates** from the same menu (refresh the page if the new repository does not show up).
5. Install **OCPP Two-Way Proxy**. The first install builds the image on the Home Assistant machine, which needs outbound access to `ghcr.io`, `github.com`, and `pypi.org`.

## Configure

Open the add-on's **Configuration** tab. Three fields are intentionally blank. The add-on logs an error and exits until they are set.

| Option | Default | Meaning |
| --- | --- | --- |
| Listen port | `9100` | Port inside the add-on. Leave it at 9100. |
| Primary CSMS URL | `wss://ocpp.circlelink.app/` | Circle Link base URL, with no charge point id. |
| Primary charge point id | empty | Required. The Circle Link id, `<YOUR_CIRCLELINK_ID>`. |
| Primary password | empty | Circle Link has no password. Fill this in only if a CSMS requires HTTP Basic auth. |
| Secondary CSMS URL | empty | Required. Home Assistant OCPP base URL, with no charge point id. Use `ws://<HA_IP>:9000/` or `ws://homeassistant.local:9000/`. |
| Secondary charge point id | empty | Required. The id configured in the OCPP integration, `<CHARGER_ID>`. |
| Log level | `info` | Raise to `debug` while bringing the link up. Payloads are written to the log. |

`<HA_IP>` is the address the charger already uses to reach Home Assistant. `<YOUR_CIRCLELINK_ID>` comes from Circle Link. `<CHARGER_ID>` is the charge point id in the Home Assistant OCPP integration. Do not put the charge point id in either URL.

Save, then start the add-on. **Start on boot** is on by default.

If a required field is still empty, the log contains `primary_cp_id is empty`, `secondary_cp_id is empty`, or `secondary_url is empty`, and the add-on stops. Fill in the field and start it again.

Watch the **Log** tab until it says the proxy is ready and that charger-facing WebSocket pings are disabled. It should name the two upstream URLs:

- `wss://ocpp.circlelink.app/<YOUR_CIRCLELINK_ID>`
- `ws://<HA_IP>:9000/<CHARGER_ID>`

The published host port is **9100/tcp**. That is what the charger connects to. `listen_port` is the matching port inside the container; keep it at 9100. If 9100 is already taken on the Home Assistant host, change the **host** port on the add-on's **Network** page and use that host port in the charger URL. Do not change `listen_port` to do this.

### Point the charger at the proxy

On the charger, set the OCPP server URL to:

`ws://<HA_IP>:9100/<CHARGER_ID>`

The path can be any id. It is not sent upstream. The proxy always uses `<YOUR_CIRCLELINK_ID>` for Circle Link and `<CHARGER_ID>` for Home Assistant. Keeping the charger's existing path makes the setting obvious.

The subprotocol negotiated with the charger and with both backends is `ocpp1.6`.

After the charger connects, the log shows the incoming path and both upstream connections. Circle Link should show the charge point online. Home Assistant's OCPP integration should show the same charger it showed before, because the secondary connection still uses `<CHARGER_ID>` on port 9000.

Meter values, status, and transactions are calls from the charger, so both backends receive them. A command from either backend (remote start, reset, and so on) is forwarded to the charger, and the charger's answer goes back to the backend that asked.

### If Home Assistant is not reached

From inside an add-on, the host's LAN address is not always reachable, even when the charger can open it. The log then says the secondary CSMS is unavailable and that the primary stays up.

Set **Secondary CSMS URL** to the Home Assistant OS gateway address and restart the add-on:

`ws://172.30.32.1:9000/`

`172.30.32.1` is the gateway address add-ons use on Home Assistant OS, not a LAN address of a particular site. The charge point id stays `<CHARGER_ID>`. Circle Link is unaffected while the secondary link retries.

## Rollback

Point the charger OCPP URL back to the Home Assistant integration:

`ws://<HA_IP>:9000/<CHARGER_ID>`

The OCPP integration is still listening there; this add-on does not change it. Stop or uninstall the add-on afterwards if you want port 9100 closed. No change is required in the OCPP integration's charge point id or port.

## Behavior and limits

- One charger at a time. A new connection closes the previous session.
- Primary (Circle Link) answers are the ones the charger applies to its own calls, such as BootNotification and MeterValues. Home Assistant still sees those calls. Its answers to them are dropped on purpose.
- Commands that Home Assistant itself sends are forwarded, and the charger's reply is returned to Home Assistant.
- If Home Assistant is down, Circle Link stays connected. The add-on retries the secondary link every few seconds.
- WebSocket PINGs toward the charger are off, because some chargers (including the Elinta HomeBox) do not answer them. PINGs toward Circle Link and Home Assistant are sent every 20 seconds.
- The charger URL is plain `ws://` on the LAN. Only the Circle Link link is `wss://`. The image trusts the Alpine CA bundle.
- OCPP message bodies are not rewritten. A backend that checks a serial number inside BootNotification still sees whatever the charger sends.
- If both backends reuse the same message id at the same time, the reply is delivered to the primary. OCPP message ids are normally unique.
- `info` and `debug` logs include OCPP payloads (energy and id tags). `warning` and `error` do not. The primary password is not written to the log. It is stored by Home Assistant in the add-on options and copied into `/tmp/ocpp-2w-proxy.ini` inside the container only.

## Help

Issues and the source: https://github.com/JuozapasP/ha-ocpp-2w-proxy

The proxy program is https://github.com/ocpp-balanz/ocpp-2w-proxy (MIT). This add-on's glue code is MIT.
