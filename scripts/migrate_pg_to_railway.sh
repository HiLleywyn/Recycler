#!/bin/sh
# scripts/migrate_pg_to_railway.sh
#
# Migrates Discoin's PostgreSQL and Redis data from the embedded single-container
# instance to Railway managed services (postgres-ssl + Railway Redis).
#
# Defaults pull directly from Railway env vars so you can run this as a
# Railway one-off job without setting any extra variables  -  just make sure
# the service has DATABASE_URL and REDIS_URL linked to the target services.
#
# Required env vars (all have sensible defaults):
#   SOURCE_DATABASE_URL  - source Postgres (default: embedded localhost)
#   TARGET_DATABASE_URL  - target Postgres (default: $DATABASE_URL)
#   SOURCE_REDIS_URL     - source Redis    (default: embedded localhost)
#   TARGET_REDIS_URL     - target Redis    (default: $REDIS_URL)
#
# Optional:
#   DUMP_FILE            - pg dump file path (default: /tmp/discoin_migration.dump)
#   SKIP_DUMP            - 1 = skip pg_dump, use existing DUMP_FILE
#   SKIP_RESTORE         - 1 = dump only, skip pg_restore
#   SKIP_REDIS           - 1 = skip Redis migration entirely
#   CLEAN_RESTORE        - 1 = drop all tables in target before pg_restore
#   PGSSLROOTCERT        - path to CA cert for target (unset = skip cert verify)
# ──────────────────────────────────────────────────────────────────────────────

set -e

# ── Connection defaults ───────────────────────────────────────────────────────
# Postgres: source = embedded, target = Railway DATABASE_URL
SOURCE="${SOURCE_DATABASE_URL:-postgresql://discoin:discoin@localhost:5432/discoin}"
TARGET="${TARGET_DATABASE_URL:-${DATABASE_URL:-}}"

# Redis: source = embedded, target = Railway REDIS_URL
SOURCE_REDIS="${SOURCE_REDIS_URL:-redis://localhost:6379}"
TARGET_REDIS="${TARGET_REDIS_URL:-${REDIS_URL:-}}"

DUMP_FILE="${DUMP_FILE:-/tmp/discoin_migration.dump}"

# ── Validate ──────────────────────────────────────────────────────────────────

if [ -z "$TARGET" ]; then
    echo "[migrate] ERROR: No target Postgres URL."
    echo "[migrate] Set TARGET_DATABASE_URL or link a Postgres service (DATABASE_URL) and re-run."
    exit 1
fi

command -v pg_dump    >/dev/null 2>&1 || { echo "[migrate] ERROR: pg_dump not found";    exit 1; }
command -v pg_restore >/dev/null 2>&1 || { echo "[migrate] ERROR: pg_restore not found"; exit 1; }
command -v psql       >/dev/null 2>&1 || { echo "[migrate] ERROR: psql not found";       exit 1; }

echo "[migrate] ════════════════════════════════════════"
echo "[migrate] Discoin data migration"
echo "[migrate] ════════════════════════════════════════"
echo "[migrate] Postgres source : $SOURCE"
echo "[migrate] Postgres target : $TARGET"
echo "[migrate] Dump file       : $DUMP_FILE"
if [ "${SKIP_REDIS:-0}" != "1" ] && [ -n "$TARGET_REDIS" ]; then
    echo "[migrate] Redis source    : $SOURCE_REDIS"
    echo "[migrate] Redis target    : $TARGET_REDIS"
else
    echo "[migrate] Redis           : skipped (SKIP_REDIS=1 or TARGET_REDIS_URL unset)"
fi
echo ""

# ── SSL for target Postgres ───────────────────────────────────────────────────
if [ -z "${PGSSLROOTCERT:-}" ]; then
    export PGSSLMODE=require
    export PGSSLCERT=""
    export PGSSLKEY=""
else
    export PGSSLMODE=verify-ca
fi

# ── Step 1: Dump Postgres ─────────────────────────────────────────────────────

if [ "${SKIP_DUMP:-0}" != "1" ]; then
    echo "[migrate] Dumping source Postgres..."
    PGPASSWORD="" \
    pg_dump \
        "$SOURCE" \
        --format=custom \
        --no-owner \
        --no-acl \
        --no-privileges \
        --compress=6 \
        --file="$DUMP_FILE"

    DUMP_SIZE=$(du -sh "$DUMP_FILE" 2>/dev/null | cut -f1)
    echo "[migrate] Dump complete: $DUMP_FILE ($DUMP_SIZE)"
else
    echo "[migrate] SKIP_DUMP=1  -  using existing dump at $DUMP_FILE"
    if [ ! -f "$DUMP_FILE" ]; then
        echo "[migrate] ERROR: $DUMP_FILE not found."
        exit 1
    fi
fi

if [ "${SKIP_RESTORE:-0}" = "1" ]; then
    echo "[migrate] SKIP_RESTORE=1  -  stopping after dump."
    exit 0
fi

# ── Step 2: Prepare target Postgres ──────────────────────────────────────────

echo "[migrate] Checking target Postgres..."
psql "$TARGET" -c "SELECT version();" -t 2>&1 | head -2 || {
    echo "[migrate] ERROR: Cannot connect to target Postgres."
    exit 1
}

EXISTING_TABLES=$(psql "$TARGET" -t -c \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" \
    2>/dev/null | tr -d ' \n')

if [ "${EXISTING_TABLES:-0}" -gt "0" ] 2>/dev/null; then
    echo ""
    echo "[migrate] WARNING: Target already has $EXISTING_TABLES table(s)."
    if [ "${CLEAN_RESTORE:-0}" = "1" ]; then
        echo "[migrate] CLEAN_RESTORE=1  -  dropping all public tables on target..."
        psql "$TARGET" -c "
            DO \$\$
            DECLARE r RECORD;
            BEGIN
                FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
                    EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE';
                END LOOP;
            END \$\$;
        " 2>&1
        echo "[migrate] Target tables dropped."
    else
        echo "[migrate] Set CLEAN_RESTORE=1 to drop existing tables before restore."
    fi
fi

# ── Step 3: Restore Postgres ──────────────────────────────────────────────────

echo "[migrate] Restoring Postgres dump to target..."
pg_restore \
    --dbname="$TARGET" \
    --no-owner \
    --no-acl \
    --no-privileges \
    --single-transaction \
    --exit-on-error \
    "$DUMP_FILE"

echo "[migrate] Postgres restore complete."

# ── Step 4: Verify Postgres ───────────────────────────────────────────────────

echo ""
echo "[migrate] Postgres row counts:"
psql "$TARGET" -t -c "
    SELECT 'users'           AS tbl, count(*) FROM users
    UNION ALL SELECT 'transactions',      count(*) FROM transactions
    UNION ALL SELECT 'guild_settings',    count(*) FROM guild_settings
    UNION ALL SELECT 'crypto_prices',     count(*) FROM crypto_prices
    UNION ALL SELECT 'schema_migrations', count(*) FROM schema_migrations
    ORDER BY 1;
" 2>/dev/null || echo "[migrate] (verification query failed)"

# ── Step 5: Redis migration ───────────────────────────────────────────────────

if [ "${SKIP_REDIS:-0}" = "1" ]; then
    echo ""
    echo "[migrate] SKIP_REDIS=1  -  skipping Redis migration."
elif [ -z "$TARGET_REDIS" ]; then
    echo ""
    echo "[migrate] No TARGET_REDIS_URL / REDIS_URL set  -  skipping Redis migration."
    echo "[migrate] Set TARGET_REDIS_URL=<railway-redis-url> to include Redis."
else
    echo ""
    echo "[migrate] Migrating Redis keys: $SOURCE_REDIS -> $TARGET_REDIS"
    python3 - "$SOURCE_REDIS" "$TARGET_REDIS" <<'PYEOF'
import sys, redis as _r

src_url, tgt_url = sys.argv[1], sys.argv[2]

# Source: embedded local Redis (no SSL, no auth by default)
src = _r.from_url(src_url, decode_responses=False, socket_timeout=10)

# Target: Railway Redis (may use rediss:// scheme, requires ssl_cert_reqs=none for self-signed)
tgt_kwargs = {}
if tgt_url.startswith("rediss://"):
    tgt_kwargs["ssl_cert_reqs"] = "none"
tgt = _r.from_url(tgt_url, decode_responses=False, socket_timeout=10, **tgt_kwargs)

try:
    src.ping()
except Exception as e:
    print(f"[redis-migrate] WARN: Cannot reach source Redis ({e})  -  skipping.")
    sys.exit(0)

try:
    tgt.ping()
except Exception as e:
    print(f"[redis-migrate] ERROR: Cannot reach target Redis ({e})")
    sys.exit(1)

keys = src.keys("*")
print(f"[redis-migrate] {len(keys)} keys found in source")

ok = skip = fail = 0
for key in keys:
    try:
        dump = src.dump(key)
        if dump is None:
            skip += 1
            continue
        ttl_ms = src.pttl(key)
        ttl_ms = max(0, ttl_ms)   # -1 (no expire) or -2 (missing) → 0 = no expire
        tgt.restore(key, ttl_ms, dump, replace=True)
        ok += 1
    except Exception as e:
        fail += 1
        print(f"[redis-migrate] WARN: key={key}: {e}")

print(f"[redis-migrate] Done: {ok} migrated, {skip} skipped (nil dump), {fail} failed")
PYEOF
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "[migrate] ════ Migration complete ════"
echo "[migrate] Next steps:"
echo "  1. Verify row counts above look correct."
echo "  2. Make sure DATABASE_URL in your Railway bot service points to:"
echo "     $TARGET"
if [ -n "$TARGET_REDIS" ]; then
    echo "  3. Make sure REDIS_URL in your Railway bot service points to:"
    echo "     $TARGET_REDIS"
fi
echo "  4. Remove RESTORE_FROM_EMBEDDED=1 / CLEAN_RESTORE=1 from Railway env vars."
echo "  5. Redeploy the bot."
