"""Recycler tests -- manifest contract + the slim, clanker-only data layer.

These are stdlib-only (no framework / DB needed) so they run in a lightweight
CI job: they assert the deployable contract (sojourns.json) and that Recycler
ships its own slim schema with the clanker migrations and NO economy tables.
"""
from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The verbatim clanker feature migrations Recycler carries (and nothing else).
EXPECTED_MIGRATIONS = [
    "0285_clanktank.sql",
    "0286_clanktank_evidence.sql",
    "0287_clanktank_leave_evade.sql",
    "0288_clanktank_clusters.sql",
    "0289_clamp_settings.sql",
    "0290_clamp_channel_ids.sql",
    "0291_automod_scam_hunter.sql",
    "0292_clank_escape.sql",
    "0293_clank_escape_message_id.sql",
    "0294_clank_escape_thread_setting.sql",
    "0295_clank_case_numbers.sql",
]

# Economy tables that must NOT appear in Recycler's slim schema.
ECONOMY_TABLES = (
    "crypto_prices", "wallet_holdings", "crypto_holdings", "stakes",
    "validators", "users", "hashstones", "user_crafting", "fishing",
)


def test_manifest_is_well_formed():
    m = json.loads((ROOT / "sojourns.json").read_text())
    assert m["manifest_version"] == "1"
    assert m["bot"]["slug"] == "recycler"
    assert m["bot"]["version"]
    assert m["features"] == ["cogs.clanktank"]
    assert m["channels"] == ["discord"]
    assert m["provision"]["database"] == "postgres"
    cred_keys = {c["key"] for c in m["credentials"]}
    assert "DISCORD_TOKEN" in cred_keys


def test_manifest_settings_cover_clanker_columns():
    m = json.loads((ROOT / "sojourns.json").read_text())
    keys = {f["key"] for g in m["settings"]["groups"] for f in g["fields"]}
    # The containment wiring the clanker cog reads.
    assert {"CLANKTANK_CHANNEL_ID", "CLANKER_ROLE_ID"} <= keys


def test_slim_db_package_present():
    db = ROOT / "database"
    assert (db / "__init__.py").exists()
    assert (db / "database.py").exists()
    assert (db / "schema.sql").exists()


def test_clanker_migrations_are_the_expected_set():
    migs = sorted(p.name for p in (ROOT / "database" / "migrations").glob("*.sql"))
    assert migs == EXPECTED_MIGRATIONS


def test_slim_schema_has_runtime_tables_and_no_economy():
    schema = (ROOT / "database" / "schema.sql").read_text().lower()
    # framework-runtime substrate Recycler needs
    for table in ("guild_settings", "guild_command_roles", "command_usage"):
        assert f"create table if not exists {table}" in schema
    # and nothing from the economy
    for table in ECONOMY_TABLES:
        assert f"create table if not exists {table} " not in schema
        assert f"create table {table} " not in schema
