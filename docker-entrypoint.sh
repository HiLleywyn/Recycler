#!/usr/bin/env bash
# Recycler entrypoint. The framework opens the DB pool and applies
# schema.sql + migrations on first connect, so there is no migration step
# here — just wait for the database to accept connections, then exec the bot.
set -euo pipefail

DB_URL="${DATABASE_URL:-}"

if [ -n "$DB_URL" ]; then
  echo "[recycler] waiting for PostgreSQL to accept connections…"
  for i in $(seq 1 30); do
    if python - "$DB_URL" <<'PY'
import sys, asyncio, asyncpg
async def main(dsn):
    try:
        conn = await asyncpg.connect(dsn=dsn, timeout=3)
        await conn.execute("SELECT 1")
        await conn.close()
    except Exception:
        sys.exit(1)
asyncio.run(main(sys.argv[1]))
PY
    then
      echo "[recycler] database is ready."
      break
    fi
    echo "[recycler]   …not ready yet (attempt $i/30)"
    sleep 2
  done
fi

exec "$@"
