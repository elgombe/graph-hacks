#!/bin/sh
# Start FalkorDB from the baked-in graph snapshot, wait for it to load, then start the app.
# Writes made while running (chat memory, triage tasks) live in memory until the container restarts.
set -e

DATA="${DATA_DIR:-/tmp/falkordb}"
mkdir -p "$DATA"
[ -f "$DATA/dump.rdb" ] || cp /app/snapshot/dump.rdb "$DATA/dump.rdb"

# shellcheck disable=SC2086
redis-server --port 6379 --bind 127.0.0.1 --protected-mode no \
    --dir "$DATA" --dbfilename dump.rdb --save "" --appendonly no \
    --daemonize yes --logfile "$DATA/falkordb.log" \
    --loadmodule "${FALKORDB_BIN_PATH:-/var/lib/falkordb/bin}/falkordb.so" ${FALKORDB_ARGS}

echo "loading graph snapshot..."
until redis-cli -p 6379 ping 2>/dev/null | grep -q PONG \
    && redis-cli -p 6379 info persistence 2>/dev/null | grep -q "^loading:0"; do
    sleep 1
done
echo "graph loaded: $(redis-cli -p 6379 GRAPH.LIST | tr '\n' ' ')"

exec python -m repobrain serve --host 0.0.0.0 --port "${PORT:-7860}"
