#!/command/with-contenv bashio

set -e

# Bashio runs this script under bash. Trim and reject the three settings that
# ship blank so a new install cannot start with an unnamed charge point.
require_setting() {
  local key="$1"
  local hint="$2"
  local value
  value="$(bashio::config "${key}")"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ -z "${value}" ]]; then
    bashio::log.error "${key} is empty. ${hint}"
    exit 1
  fi
}

require_setting "primary_cp_id" \
  "Set the Circle Link charge point id (<YOUR_CIRCLELINK_ID>) on the Configuration tab, then restart."
require_setting "secondary_cp_id" \
  "Set the Home Assistant charge point id (<CHARGER_ID>) on the Configuration tab, then restart."
require_setting "secondary_url" \
  "Set the Home Assistant OCPP base URL (ws://<HA_IP>:9000/ or ws://homeassistant.local:9000/), then restart."

bashio::log.info "Starting OCPP two-way proxy"
python3 /opt/ocpp-2w-proxy/render_config.py \
    --options /data/options.json \
    --output /tmp/ocpp-2w-proxy.ini
exec python3 /opt/ocpp-2w-proxy/ocpp-2w-proxy.py --config /tmp/ocpp-2w-proxy.ini
