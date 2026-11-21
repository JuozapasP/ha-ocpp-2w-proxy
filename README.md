# Home Assistant add-on: OCPP two-way proxy

Add-on repository for proxying one OCPP 1.6J charger to two CSMS backends. The charger stays on the Home Assistant OCPP integration and also registers with Circle Link.

Add this URL in **Settings → Add-ons → Add-on Store → ⋮ → Repositories** (on current Home Assistant: **Settings → Apps → App store**):

`https://github.com/JuozapasP/ha-ocpp-2w-proxy`

The add-on is `ocpp_2w_proxy/`. Setup, the charger URL, and rollback are in [`ocpp_2w_proxy/DOCS.md`](ocpp_2w_proxy/DOCS.md).

The image installs [ocpp-2w-proxy](https://github.com/ocpp-balanz/ocpp-2w-proxy) at commit `5e02d2ce3becb4c334c57e21f386906376ff537c` and applies [`ocpp_2w_proxy/patches/0001-ha-id-remap-and-pings.patch`](ocpp_2w_proxy/patches/0001-ha-id-remap-and-pings.patch). Upstream cannot remap the two charge point ids, rejects a hyphen in `<CHARGER_ID>`, and leaves WebSocket pings on. Chargers that do not answer PINGs stay connected because the patch disables pings on the charger-facing socket only.

`primary_cp_id`, `secondary_cp_id`, and `secondary_url` are blank in the add-on defaults. Fill them in before the first start. With those set, the proxy listens on `ws://<HA_IP>:9100/<CHARGER_ID>` and connects to:

- `wss://ocpp.circlelink.app/<YOUR_CIRCLELINK_ID>` (primary, no password unless you set one)
- `ws://<HA_IP>:9000/<CHARGER_ID>` (Home Assistant OCPP, left on port 9000)

`tests/test_proxy.py` applies the patch and checks id remap, OCPP routing, charger ping disable, optional Basic auth, and `wss://` against a local CA. Docker is not required to run it.
