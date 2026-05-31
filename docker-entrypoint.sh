#!/bin/sh
set -e

# Ensure /tmp is world-writable (Railway containers may restrict it)
chmod 1777 /tmp 2>/dev/null || true

PG_USER="${PG_USER:-discoin}"
PG_PASSWORD="${PG_PASSWORD:-discoin}"
PG_DB="${PG_DB:-discoin}"
PGDATA="/data/pgdata"

# ── Decide whether to start embedded PostgreSQL ────────────────────────────
# If DATABASE_URL points to localhost we're in single-container mode (Railway).
# If it points to a remote host (e.g. "postgres" in docker-compose) skip.
NEED_EMBEDDED_PG=false
case "${DATABASE_URL:-}" in
    *@localhost:*|*@127.0.0.1:*|"") NEED_EMBEDDED_PG=true ;;
esac

if [ "$NEED_EMBEDDED_PG" = true ]; then
    # Locate PG binaries (Debian puts them under /usr/lib/postgresql/<ver>/bin)
    PG_BIN=$(find /usr/lib/postgresql -name "pg_ctl" -type f 2>/dev/null | head -1 | xargs dirname 2>/dev/null || true)
    if [ -n "$PG_BIN" ]; then
        export PATH="$PG_BIN:$PATH"
    fi

    # Ensure pgdata directory exists (volumes may mount an empty /data)
    mkdir -p "$PGDATA"
    chown -R postgres:postgres "$PGDATA"

    # Initialise the cluster if this is the first run (empty volume)
    # SAFETY: only init if PG_VERSION is truly missing  -  never wipe existing data
    if [ ! -f "$PGDATA/PG_VERSION" ]; then
        if [ "$(ls -A "$PGDATA" 2>/dev/null)" ]; then
            echo "[entrypoint] WARNING: $PGDATA has files but no PG_VERSION  -  may be corrupted."
            echo "[entrypoint] Refusing to re-init over existing data. Check /data/pgdata manually."
            echo "[entrypoint] If this is intentional, remove all files in $PGDATA first."
            exit 1
        fi
        echo "[entrypoint] Initialising PostgreSQL data directory..."
        gosu postgres initdb -D "$PGDATA" --auth=trust --username="$PG_USER"

        # Allow local trusted connections (single-container, no external access)
        cat > "$PGDATA/pg_hba.conf" <<CONF
local   all   all                 trust
host    all   all   127.0.0.1/32  trust
host    all   all   ::1/128       trust
CONF

        # Listen on localhost only + container-friendly tuning
        cat >> "$PGDATA/postgresql.conf" <<PGCONF
listen_addresses = '127.0.0.1'
# Container-friendly shared memory settings
dynamic_shared_memory_type = posix
shared_buffers = 128MB
work_mem = 4MB
max_connections = 50
# Reduce log noise in Railway
log_checkpoints = off
log_min_messages = warning
PGCONF
    fi

    # Ensure container-friendly settings exist even on existing volumes
    if ! grep -q 'dynamic_shared_memory_type' "$PGDATA/postgresql.conf" 2>/dev/null; then
        cat >> "$PGDATA/postgresql.conf" <<PGCONF
# Container-friendly shared memory settings (added by entrypoint)
dynamic_shared_memory_type = posix
shared_buffers = 128MB
work_mem = 4MB
max_connections = 50
PGCONF
    fi

    # Reduce log noise on existing volumes
    if ! grep -q 'log_checkpoints' "$PGDATA/postgresql.conf" 2>/dev/null; then
        cat >> "$PGDATA/postgresql.conf" <<PGCONF
# Reduce log noise in Railway (added by entrypoint)
log_checkpoints = off
log_min_messages = warning
PGCONF
    fi

    # Disable SSL  -  embedded PG listens on localhost only, no certs needed.
    # The persistent volume may have ssl=on from a prior config, causing a
    # FATAL error when the cert files don't exist.
    if grep -q '^ssl\s*=' "$PGDATA/postgresql.conf" 2>/dev/null; then
        sed -i 's/^ssl\s*=.*/ssl = off/' "$PGDATA/postgresql.conf"
    elif ! grep -q '^ssl' "$PGDATA/postgresql.conf" 2>/dev/null; then
        echo "ssl = off" >> "$PGDATA/postgresql.conf"
    fi

    # Ensure pgdata ownership (Railway volumes may mount as root)
    chown -R postgres:postgres "$PGDATA"

    # Remove stale pid file if it exists (unclean shutdown on previous deploy)
    if [ -f "$PGDATA/postmaster.pid" ]; then
        echo "[entrypoint] Removing stale postmaster.pid from previous run..."
        rm -f "$PGDATA/postmaster.pid"
    fi

    # Truncate old pg.log to prevent Railway from replaying thousands of
    # historical log lines on every deploy (the file lives on persistent volume)
    : > "$PGDATA/pg.log" 2>/dev/null || true
    chown postgres:postgres "$PGDATA/pg.log" 2>/dev/null || true

    echo "[entrypoint] Starting embedded PostgreSQL..."
    if ! gosu postgres pg_ctl -D "$PGDATA" -l "$PGDATA/pg.log" -w -t 30 start -o "-k /tmp"; then
        echo "[entrypoint] ERROR: PostgreSQL failed to start. Last 50 lines of log:"
        tail -50 "$PGDATA/pg.log" 2>/dev/null || echo "(no log file found)"
        exit 1
    fi

    # Set password for psql/createdb (needed for md5 auth on existing volumes)
    export PGPASSWORD="$PG_PASSWORD"

    # Create the database if it doesn't exist yet
    if ! gosu postgres psql -h /tmp -U "$PG_USER" -lqt | cut -d \| -f 1 | grep -qw "$PG_DB"; then
        echo "[entrypoint] Creating database '$PG_DB'..."
        gosu postgres createdb -h /tmp -U "$PG_USER" "$PG_DB"
    fi

    # Apply critical schema fixes before the bot starts to avoid error spam.
    # These are idempotent and only run if the tables already exist (i.e.
    # NOT on a fresh install  -  the bot's schema.sql handles that).
    echo "[entrypoint] Applying schema hotfixes..."
    gosu postgres psql -h /tmp -U "$PG_USER" -d "$PG_DB" -q <<'SQL'
DO $$
BEGIN
    -- Only apply hotfixes if the schema already exists (not a fresh install).
    -- On fresh installs, schema.sql creates everything correctly.
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'users') THEN
        ALTER TABLE users ADD COLUMN IF NOT EXISTS game_lockout_until TIMESTAMPTZ;
        ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS vault_feed_channel BIGINT;
    END IF;
    IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'network_base_fees') THEN
        UPDATE network_base_fees
        SET base_fee = 10000000000
        WHERE network = 'Discoin Network' AND base_fee > 1000000000000;
    END IF;
END $$;

-- 0292/0293: Clank Tank escape room
CREATE TABLE IF NOT EXISTS clank_escape (
    user_id         BIGINT      NOT NULL,
    guild_id        BIGINT      NOT NULL,
    case_num        INT         NOT NULL DEFAULT (FLOOR(RANDOM() * 899999) + 100001)::INT,
    thread_id       BIGINT,
    message_id      BIGINT,
    step            SMALLINT    NOT NULL DEFAULT 0,
    step_data       JSONB       NOT NULL DEFAULT '{}',
    fail_count      SMALLINT    NOT NULL DEFAULT 0,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    step_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    PRIMARY KEY (user_id, guild_id)
);
ALTER TABLE clank_escape ADD COLUMN IF NOT EXISTS message_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS clank_escape_thread BIGINT;
CREATE TABLE IF NOT EXISTS clank_case_counter (
    guild_id  BIGINT  PRIMARY KEY,
    last_num  BIGINT  NOT NULL DEFAULT 0
);
ALTER TABLE clanker_records ADD COLUMN IF NOT EXISTS case_num BIGINT;
SQL

    unset PGPASSWORD
else
    echo "[entrypoint] DATABASE_URL points to external host  -  skipping embedded PostgreSQL."
fi

# ── Apply schema hotfixes to external DB if needed ─────────────────────────
# The Python migration runner (database/database.py _run_migrations) handles
# full migration on startup, but the two columns added as entrypoint hotfixes
# for the embedded DB also need to reach external databases on Railway.
#
# We only run this if DATABASE_URL is set AND points to an external host.
# pg_isready / psql are available in the image (postgresql-client).
#
_EXTERNAL_DB=false
case "${DATABASE_URL:-}" in
    *@localhost:*|*@127.0.0.1:*|"") _EXTERNAL_DB=false ;;
    *) _EXTERNAL_DB=true ;;
esac

if [ "$_EXTERNAL_DB" = true ] && command -v psql >/dev/null 2>&1; then
    echo "[entrypoint] Applying schema hotfixes to external database..."
    # PGSSLMODE=require so psql uses SSL (Railway postgres-ssl needs it)
    # PGSSLROOTCERT=system trusts Railway's self-signed cert without a CA file
    PGSSLMODE=require PGSSLROOTCERT="" PSQL_PGSSLMODE=require \
    psql "${DATABASE_URL}" --no-password -q <<'SQL' 2>/dev/null || true
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'users') THEN
        ALTER TABLE users ADD COLUMN IF NOT EXISTS game_lockout_until TIMESTAMPTZ;
        ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS vault_feed_channel BIGINT;
        ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS heal_ai_backend  TEXT DEFAULT NULL;
        ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS heal_ai_model    TEXT DEFAULT NULL;
        ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS heal_ai_base_url TEXT DEFAULT NULL;
        ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS realmarket_channels TEXT NOT NULL DEFAULT '';
        ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS dm_itemlevelup           BOOLEAN NOT NULL DEFAULT TRUE;
        ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS dm_whale_alerts          BOOLEAN NOT NULL DEFAULT TRUE;
        ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS muted_networks_mining    TEXT    NOT NULL DEFAULT '';
        ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS muted_networks_staking   TEXT    NOT NULL DEFAULT '';
        ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS muted_networks_validator TEXT    NOT NULL DEFAULT '';
        ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS muted_networks_whale     TEXT    NOT NULL DEFAULT '';
    END IF;
END $$;

-- 0050: Fix wallet_holdings network key "discoin" -> "dsc" (merge duplicates first)
DO $$ BEGIN
    IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'wallet_holdings') THEN
        -- Merge amounts where both 'discoin' and 'dsc' rows exist
        UPDATE wallet_holdings AS dst
        SET    amount = dst.amount + src.amount
        FROM   wallet_holdings AS src
        WHERE  src.network = 'discoin' AND dst.network = 'dsc'
          AND  src.user_id = dst.user_id AND src.guild_id = dst.guild_id AND src.symbol = dst.symbol;
        -- Delete the now-merged 'discoin' rows
        DELETE FROM wallet_holdings AS old
        WHERE  old.network = 'discoin'
          AND  EXISTS (SELECT 1 FROM wallet_holdings AS dup
                       WHERE dup.network='dsc' AND dup.user_id=old.user_id
                         AND dup.guild_id=old.guild_id AND dup.symbol=old.symbol);
        -- Rename remaining 'discoin' rows with no 'dsc' counterpart
        UPDATE wallet_holdings SET network = 'dsc' WHERE network = 'discoin';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'transactions' AND column_name = 'network') THEN
        UPDATE transactions SET network = 'dsc' WHERE LOWER(network) = 'discoin';
    END IF;
END $$;

-- 0051: Rename staked_sun -> staked_amount on all stone tables
DO $$ BEGIN
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='hashstones'  AND column_name='staked_sun') THEN ALTER TABLE hashstones  RENAME COLUMN staked_sun TO staked_amount; END IF;
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='lockstones'  AND column_name='staked_sun') THEN ALTER TABLE lockstones  RENAME COLUMN staked_sun TO staked_amount; END IF;
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='vaultstones' AND column_name='staked_sun') THEN ALTER TABLE vaultstones RENAME COLUMN staked_sun TO staked_amount; END IF;
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='liqstones'   AND column_name='staked_sun') THEN ALTER TABLE liqstones   RENAME COLUMN staked_sun TO staked_amount; END IF;
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='gambastones' AND column_name='staked_sun') THEN ALTER TABLE gambastones RENAME COLUMN staked_sun TO staked_amount; END IF;
END $$;

-- Reset drifted Discoin Network base fees to the new initial value.
-- New initial: 1e-8 DSC/gas-unit = 10000000000 raw (10^10).
-- New max (GAS_MAX_MULT=100): 1e-6 = 1000000000000 raw (10^12).
-- Old initial was 1e-6; at 100x drift that reached 1e-4, making send gas ~2.52 DSC
-- per transaction which could exceed the amount being sent entirely.
-- Safe to run repeatedly: only clamps rows above the new max down to the new initial.
DO $$ BEGIN
    IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'network_base_fees') THEN
        UPDATE network_base_fees
        SET base_fee = 10000000000
        WHERE network = 'Discoin Network'
          AND base_fee > 1000000000000;
    END IF;
END $$;

-- 0272: Cycle Phase game expansion (best_cycle_streak column + sage_runs game check).
DO $$ BEGIN
    IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'user_sage') THEN
        ALTER TABLE user_sage ADD COLUMN IF NOT EXISTS best_cycle_streak INTEGER NOT NULL DEFAULT 0;
    END IF;
    IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'sage_runs') THEN
        IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sage_runs_game_check') THEN
            ALTER TABLE sage_runs DROP CONSTRAINT sage_runs_game_check;
        END IF;
        ALTER TABLE sage_runs ADD CONSTRAINT sage_runs_game_check
            CHECK (game IN ('pattern', 'gauge', 'tknom', 'cycle'));
    END IF;
END $$;

-- 0273: Sage Shop inventory table.
CREATE TABLE IF NOT EXISTS sage_items (
    user_id     BIGINT       NOT NULL,
    guild_id    BIGINT       NOT NULL,
    item_key    TEXT         NOT NULL,
    qty         INTEGER      NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id, item_key),
    CONSTRAINT chk_sage_items_qty CHECK (qty >= 0)
);
CREATE INDEX IF NOT EXISTS idx_sage_items_owned
    ON sage_items (guild_id, user_id) WHERE qty > 0;

-- 0292/0293: Clank Tank escape room
CREATE TABLE IF NOT EXISTS clank_escape (
    user_id         BIGINT      NOT NULL,
    guild_id        BIGINT      NOT NULL,
    case_num        INT         NOT NULL DEFAULT (FLOOR(RANDOM() * 899999) + 100001)::INT,
    thread_id       BIGINT,
    message_id      BIGINT,
    step            SMALLINT    NOT NULL DEFAULT 0,
    step_data       JSONB       NOT NULL DEFAULT '{}',
    fail_count      SMALLINT    NOT NULL DEFAULT 0,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    step_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    PRIMARY KEY (user_id, guild_id)
);
ALTER TABLE clank_escape ADD COLUMN IF NOT EXISTS message_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS clank_escape_thread BIGINT;
CREATE TABLE IF NOT EXISTS clank_case_counter (
    guild_id  BIGINT  PRIMARY KEY,
    last_num  BIGINT  NOT NULL DEFAULT 0
);
ALTER TABLE clanker_records ADD COLUMN IF NOT EXISTS case_num BIGINT;
SQL
    echo "[entrypoint] External DB hotfixes applied."
fi

# ── One-shot restore: embedded PG → external Railway DB ──────────────────
# Trigger by setting RESTORE_FROM_EMBEDDED=1 in Railway env vars.
# Combine with CLEAN_RESTORE=1 to drop all tables in the external DB first
# (required when the new DB already has a fresh empty schema applied).
# A sentinel file /data/.restore_complete is created on success so this
# block never re-runs on subsequent deploys.  Delete that file to re-run.
#
# After restore succeeds: REMOVE RESTORE_FROM_EMBEDDED=1 from Railway env
# to avoid re-triggering on the next deploy.
if [ "${RESTORE_FROM_EMBEDDED:-0}" = "1" ] && [ "$_EXTERNAL_DB" = true ]; then
    if [ "${FORCE_RESTORE:-0}" = "1" ]; then
        echo "[entrypoint] FORCE_RESTORE=1  -  removing sentinel and re-running restore..."
        rm -f /data/.restore_complete /data/.redis_restore_complete
    fi
    if [ -f "/data/.restore_complete" ]; then
        echo "[entrypoint] Restore already completed  -  skipping. Set FORCE_RESTORE=1 to re-run."
    elif [ ! -f "$PGDATA/PG_VERSION" ]; then
        echo "[entrypoint] ERROR: No embedded PG data found at $PGDATA  -  nothing to restore."
        echo "[entrypoint] Continuing without restore."
    else
        echo "[entrypoint] RESTORE_FROM_EMBEDDED=1  -  restoring embedded PG -> external DB..."

        # Locate PG binaries matching the data directory's version
        PG_DATA_VER=$(cat "$PGDATA/PG_VERSION" 2>/dev/null | tr -d '[:space:]' || echo "")
        if [ -n "$PG_DATA_VER" ]; then
            PG_BIN_R="/usr/lib/postgresql/${PG_DATA_VER}/bin"
        fi
        if [ ! -x "${PG_BIN_R}/pg_ctl" ]; then
            PG_BIN_R=$(find /usr/lib/postgresql -name "pg_ctl" -type f 2>/dev/null | sort | head -1 | xargs dirname 2>/dev/null || true)
        fi
        # PG18 bin for talking to the external Railway DB (pg_restore, psql target)
        PG18_BIN="/usr/lib/postgresql/18/bin"
        # Versioned bin for starting/dumping the OLD embedded cluster
        if [ -n "$PG_BIN_R" ]; then export PATH="$PG_BIN_R:$PATH"; fi
        echo "[entrypoint] Using PG binaries from: $PG_BIN_R (data dir version: ${PG_DATA_VER:-unknown})"

        # Remove stale pid if previous container crashed without clean shutdown
        rm -f "$PGDATA/postmaster.pid"
        # Disable SSL in embedded PG (it listens localhost only, certs not present)
        if grep -q '^ssl\s*=' "$PGDATA/postgresql.conf" 2>/dev/null; then
            sed -i 's/^ssl\s*=.*/ssl = off/' "$PGDATA/postgresql.conf"
        else
            echo "ssl = off" >> "$PGDATA/postgresql.conf"
        fi
        chown -R postgres:postgres "$PGDATA"

        echo "[entrypoint] Starting embedded PG for dump..."
        if ! gosu postgres pg_ctl -D "$PGDATA" -l "$PGDATA/pg.log" -w -t 60 start -o "-k /tmp"; then
            echo "[entrypoint] ERROR: embedded PG failed to start:"
            tail -20 "$PGDATA/pg.log" 2>/dev/null || true
            echo "[entrypoint] Continuing without restore."
        else
            DUMP_FILE="/tmp/embedded_restore.dump"
            echo "[entrypoint] Dumping embedded DB (format=custom)..."
            if gosu postgres pg_dump -h /tmp -U "$PG_USER" -Fc --no-owner --no-acl "$PG_DB" > "$DUMP_FILE"; then
                DUMP_SIZE=$(du -sh "$DUMP_FILE" 2>/dev/null | cut -f1 || echo "?")
                echo "[entrypoint] Dump complete ($DUMP_SIZE). Stopping embedded PG..."
                gosu postgres pg_ctl -D "$PGDATA" stop -m fast || true

                if [ "${CLEAN_RESTORE:-0}" = "1" ]; then
                    echo "[entrypoint] CLEAN_RESTORE=1  -  nuking public schema..."
                    PGSSLMODE=require PGSSLROOTCERT="" \
                    "${PG18_BIN}/psql" "${DATABASE_URL}" --no-password -q <<'DROPSQL' 2>/dev/null || true
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO PUBLIC;
DROPSQL
                    echo "[entrypoint] Public schema recreated (clean slate)."
                fi

                echo "[entrypoint] Restoring dump to external DB..."
                PGSSLMODE=require PGSSLROOTCERT="" \
                "${PG18_BIN}/pg_restore" --no-owner --no-acl --clean --if-exists \
                    -d "${DATABASE_URL}" "$DUMP_FILE" 2>&1 | grep -Ev "^$|pg_restore: warning" || true

                rm -f "$DUMP_FILE"

                # Verify restore by checking row count in users table
                ROW_COUNT=$(PGSSLMODE=require PGSSLROOTCERT="" \
                    "${PG18_BIN}/psql" "${DATABASE_URL}" --no-password -tAq \
                    -c "SELECT COUNT(*) FROM users;" 2>/dev/null || echo "0")
                echo "[entrypoint] Restore complete. users table has $ROW_COUNT rows."

                touch /data/.restore_complete
                echo "[entrypoint] Sentinel /data/.restore_complete written."
                echo "[entrypoint] IMPORTANT: Remove RESTORE_FROM_EMBEDDED=1 from Railway env to prevent re-trigger."
            else
                echo "[entrypoint] ERROR: pg_dump failed. Stopping embedded PG and continuing without restore."
                gosu postgres pg_ctl -D "$PGDATA" stop -m fast || true
            fi
        fi
    fi
fi

# Always ensure PG18 bin is first in PATH so the bot's pg_dump/pg_restore
# targets Railway Postgres 18 (PG17 may have been prepended for the restore)
PG18_BIN="/usr/lib/postgresql/18/bin"
if [ -d "$PG18_BIN" ]; then export PATH="$PG18_BIN:$PATH"; fi

# ── One-shot Redis migration: embedded → external Railway Redis ───────────────
# Runs automatically alongside RESTORE_FROM_EMBEDDED=1 when REDIS_URL points
# to an external host.  Can also be triggered standalone with MIGRATE_REDIS=1.
# Skipped if /data/.redis_restore_complete exists or SKIP_REDIS=1 is set.
_EXTERNAL_REDIS=false
case "${REDIS_URL:-}" in
    *localhost*|*127.0.0.1*|"") _EXTERNAL_REDIS=false ;;
    *) _EXTERNAL_REDIS=true ;;
esac

_SHOULD_MIGRATE_REDIS=false
if [ "${RESTORE_FROM_EMBEDDED:-0}" = "1" ] && [ "$_EXTERNAL_REDIS" = true ]; then
    _SHOULD_MIGRATE_REDIS=true
fi
if [ "${MIGRATE_REDIS:-0}" = "1" ] && [ "$_EXTERNAL_REDIS" = true ]; then
    _SHOULD_MIGRATE_REDIS=true
fi

if [ "$_SHOULD_MIGRATE_REDIS" = true ] && [ "${SKIP_REDIS:-0}" != "1" ]; then
    if [ -f "/data/.redis_restore_complete" ]; then
        echo "[entrypoint] Redis restore already completed  -  skipping. Delete /data/.redis_restore_complete to re-run."
    else
        REDIS_DATA_DIR="/data/redis"
        SOURCE_REDIS="${SOURCE_REDIS_URL:-redis://localhost:6379}"
        TARGET_REDIS="${TARGET_REDIS_URL:-${REDIS_URL:-}}"

        echo "[entrypoint] Migrating Redis: $SOURCE_REDIS -> $TARGET_REDIS"

        # Start embedded Redis temporarily if data dir exists
        if [ -d "$REDIS_DATA_DIR" ] || [ -f "$REDIS_DATA_DIR/dump.rdb" ]; then
            echo "[entrypoint] Starting embedded Redis for migration..."
            redis-server --daemonize yes \
                --dir "$REDIS_DATA_DIR" \
                --dbfilename dump.rdb \
                --port 6379 \
                --bind 127.0.0.1 \
                --loglevel warning \
                --save "" \
                --maxmemory 256mb \
                --stop-writes-on-bgsave-error no
            sleep 2
        fi

        # Migrate keys using Python (redis-py is already installed)
        python3 - "$SOURCE_REDIS" "$TARGET_REDIS" <<'PYEOF' 2>&1 || true
import sys, redis as _r

src_url, tgt_url = sys.argv[1], sys.argv[2]
src = _r.from_url(src_url, decode_responses=False, socket_timeout=5)
tgt_kwargs = {}
if tgt_url.startswith("rediss://"):
    tgt_kwargs["ssl_cert_reqs"] = "none"
tgt = _r.from_url(tgt_url, decode_responses=False, socket_timeout=10, **tgt_kwargs)

try:
    src.ping()
except Exception as e:
    print(f"[redis-migrate] WARN: Cannot reach source Redis ({e})  -  skipping.")
    sys.exit(0)

keys = src.keys("*")
print(f"[redis-migrate] {len(keys)} keys found in source Redis")
ok = skip = fail = 0
for key in keys:
    try:
        dump = src.dump(key)
        if dump is None:
            skip += 1
            continue
        ttl_ms = max(0, src.pttl(key))
        tgt.restore(key, ttl_ms, dump, replace=True)
        ok += 1
    except Exception as e:
        fail += 1
        print(f"[redis-migrate] WARN: {key}: {e}")
print(f"[redis-migrate] Done: {ok} migrated, {skip} skipped, {fail} failed")
PYEOF

        touch /data/.redis_restore_complete
        echo "[entrypoint] Redis migration complete. Sentinel /data/.redis_restore_complete written."
    fi
fi

# ── Decide whether to start embedded Redis ───────────────────────────────
NEED_EMBEDDED_REDIS=false
case "${REDIS_URL:-}" in
    *localhost*|*127.0.0.1*|"") NEED_EMBEDDED_REDIS=true ;;
esac

if [ "$NEED_EMBEDDED_REDIS" = true ]; then
    # Silence the "Memory overcommit must be enabled!" warning.
    # In containers this sysctl may be read-only, so ignore failures.
    sysctl vm.overcommit_memory=1 >/dev/null 2>&1 || true
    echo "[entrypoint] Starting embedded Redis..."
    redis-server --daemonize yes --maxmemory 256mb --maxmemory-policy allkeys-lru \
        --stop-writes-on-bgsave-error no --save "" --loglevel warning
fi

# ── Ensure /data dirs ──────────────────────────────────────────────────────
# Railway mounts the persistent volume at /data as root.  Every subdir the
# bot needs to write into has to be created + chown'd to the discoin user
# BEFORE we drop privileges with gosu, otherwise mkdir() raises EACCES.
mkdir -p /data/backups
chown -R discoin:discoin /data/backups

# DiscoAI memory sidecar state.  Honour DISCOAI_DATA_DIR (defaults to
# /data/discoai) so operators can relocate it.  Today the sidecar is
# Postgres-backed so nothing actually has to live on disk, but keeping
# the dir around means future features (log exports, fact backups, etc.)
# already have a writable spot on the persistent volume.
DISCOAI_DATA_DIR_DEFAULT="/data/discoai"
DISCOAI_DATA_DIR_RESOLVED="${DISCOAI_DATA_DIR:-$DISCOAI_DATA_DIR_DEFAULT}"
mkdir -p "$DISCOAI_DATA_DIR_RESOLVED"
chown -R discoin:discoin "$DISCOAI_DATA_DIR_RESOLVED"

# ── Start the bot ──────────────────────────────────────────────────────────
echo "[entrypoint] Starting Discoin bot..."
exec gosu discoin "$@"
