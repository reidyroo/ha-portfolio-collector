#!/bin/sh
set -e

OPTIONS="/data/options.json"

if [ -f "$OPTIONS" ]; then
    export T212_TOKEN=$(python3 -c "import json; d=json.load(open('$OPTIONS')); print(d.get('t212_token',''))")
    export T212_BASE=$(python3 -c "import json; d=json.load(open('$OPTIONS')); print(d.get('t212_base','https://demo.trading212.com'))")
else
    echo "WARNING: $OPTIONS not found - credentials must be set via environment"
fi

export PORT_DB="/data/portfolio.db"
export PORT="8000"

echo "Starting Portfolio Collector on port ${PORT}"
echo "T212 endpoint: ${T212_BASE:-not set}"

exec python3 /app/collector.py
