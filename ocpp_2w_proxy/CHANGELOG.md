# Changelog

## 1.1.0

- `primary_cp_id`, `secondary_cp_id`, and `secondary_url` default to empty. Startup fails with a log error until each one is set. The repository no longer ships a charge point id or a LAN address.
- `primary_url` still defaults to `wss://ocpp.circlelink.app/`.

## 1.0.0

- First release of the Home Assistant add-on.
- Installs ocpp-2w-proxy at commit `5e02d2ce3becb4c334c57e21f386906376ff537c`.
- Patches that commit so one charger path can use a different charge point id on each CSMS, hyphenated ids are accepted, charger-facing WebSocket pings are disabled, and a down secondary CSMS does not drop Circle Link.
