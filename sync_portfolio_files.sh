#!/bin/bash
# sync_portfolio_files.sh
# Pull the latest packages/portfolio.yaml and lovelace/dashboard.yaml from
# the GitHub repo, run a YAML config-check, and restart HA Core.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/reidyroo/ha-portfolio-collector/main/sync_portfolio_files.sh \
#     -o /config/sync_portfolio_files.sh
#   chmod +x /config/sync_portfolio_files.sh
#   /config/sync_portfolio_files.sh
#
# Since v2.5.0 the dashboard reaches the add-on only via http://localhost:8000
# from HA Core itself, so there's no LAN-IP rewrite needed for fresh deploys.
#
# If you're upgrading from an older snapshot of the YAML files that still
# references "homeassistant.local:8000", set HA_IP to your LAN address before
# running and the script will substitute it:
#   HA_IP=192.168.1.42 /config/sync_portfolio_files.sh

set -euo pipefail

REPO="${REPO:-https://raw.githubusercontent.com/reidyroo/ha-portfolio-collector/main}"
HA_IP="${HA_IP:-}"

echo "→ Pulling latest packages/portfolio.yaml ..."
curl -fsSL "${REPO}/packages/portfolio.yaml" -o /config/packages/portfolio.yaml

echo "→ Pulling latest lovelace/dashboard.yaml ..."
curl -fsSL "${REPO}/lovelace/dashboard.yaml" -o /config/lovelace/dashboard.yaml

if [ -n "$HA_IP" ] && [ "$HA_IP" != "homeassistant.local" ]; then
    echo "→ Rewriting any homeassistant.local:8000 references to use ${HA_IP} ..."
    sed -i "s|http://homeassistant.local:8000|http://${HA_IP}:8000|g" \
        /config/packages/portfolio.yaml \
        /config/lovelace/dashboard.yaml
fi

echo "→ Running ha core check ..."
ha core check

echo "→ Restarting HA Core to reload sensors, REST commands, and dashboard ..."
ha core restart

echo "✓ Sync complete."
