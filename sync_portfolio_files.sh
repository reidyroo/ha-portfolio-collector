#!/bin/bash
# sync_portfolio_files.sh
# Pull the latest packages/portfolio.yaml and lovelace/dashboard.yaml from
# the GitHub repo, optionally rewrite the iframe / button URL to a static IP,
# run a config-check, and restart HA Core.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/reidyroo/ha-portfolio-collector/main/sync_portfolio_files.sh \
#     -o /config/sync_portfolio_files.sh
#   chmod +x /config/sync_portfolio_files.sh
#   HA_IP=192.168.1.6 /config/sync_portfolio_files.sh
#
# Or set HA_IP=homeassistant.local to keep the upstream default.

set -euo pipefail

REPO="${REPO:-https://raw.githubusercontent.com/reidyroo/ha-portfolio-collector/main}"
HA_IP="${HA_IP:-homeassistant.local}"

echo "→ Pulling latest packages/portfolio.yaml ..."
curl -fsSL "${REPO}/packages/portfolio.yaml" -o /config/packages/portfolio.yaml

echo "→ Pulling latest lovelace/dashboard.yaml ..."
curl -fsSL "${REPO}/lovelace/dashboard.yaml" -o /config/lovelace/dashboard.yaml

if [ "$HA_IP" != "homeassistant.local" ]; then
    echo "→ Rewriting iframe / button URLs to use ${HA_IP} ..."
    sed -i "s|http://homeassistant.local:8000|http://${HA_IP}:8000|g" \
        /config/packages/portfolio.yaml \
        /config/lovelace/dashboard.yaml
fi

echo "→ Running ha core check ..."
ha core check

echo "→ Restarting HA Core to reload sensors, REST commands, and dashboard ..."
ha core restart

echo "✓ Sync complete."
