"""Regression tests for the auto-status DM and `,status` / `.dev status` reports.

The status surfaces are easy to break and hard to notice -- a stale heartbeat
key just shows a permanent red light, a missing 10**18 descale prints
quintillion-dollar totals, and a hardcoded prefix sends users the wrong
command. These tests lock in the invariants the report code relies on so
the next refactor can't silently regress them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
COGS_DIR = REPO_ROOT / "cogs"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _all_pulse_keys() -> set[str]:
    """Every heartbeat key actually emitted by a ``pulse(...)`` call.

    The status surfaces should never reference a key that no task pulses --
    that's how the legacy ``price_drift`` key ended up flagged red on every
    server for months.
    """
    keys: set[str] = set()
    pulse_re = re.compile(r"""pulse\(\s*["']([a-zA-Z_][a-zA-Z0-9_]*)["']""")
    for path in COGS_DIR.rglob("*.py"):
        for m in pulse_re.finditer(_read(path)):
            keys.add(m.group(1))
    # Allow the test heartbeat used by the self-heal harness.
    keys.add("_test_heal_loop")
    return keys


def _hb_keys_referenced_in(path: Path) -> set[str]:
    """Heartbeat keys looked up via ``heartbeats[...]`` or string-tuple refs."""
    text = _read(path)
    keys: set[str] = set()
    # heartbeats["foo"]  /  heartbeats.get("foo")
    for m in re.finditer(
        r"""heartbeats(?:\.get)?\(?\s*\[?\s*["']([a-zA-Z_][a-zA-Z0-9_]*)["']""",
        text,
    ):
        keys.add(m.group(1))
    # ("price_drift_trade", "Price Drift") tuples in the _SERVICES list / .dev check
    for m in re.finditer(
        r"""\(\s*["']([a-z_]+)["']\s*,\s*["'][A-Z][^"']+["']""",
        text,
    ):
        # Only count entries that look like a heartbeat key (lowercase + underscore).
        candidate = m.group(1)
        if "_" in candidate and candidate.islower():
            keys.add(candidate)
    return keys


class TestHeartbeatKeyHygiene:
    """Status reports must only reference heartbeat keys that something pulses."""

    def test_status_cog_only_uses_live_keys(self):
        live = _all_pulse_keys()
        referenced = _hb_keys_referenced_in(COGS_DIR / "status.py")
        # Filter to keys that look like heartbeat names (snake_case lowercase).
        referenced = {k for k in referenced if k.islower() and "_" in k}
        stale = referenced - live
        assert not stale, (
            f"cogs/status.py references heartbeat key(s) that nothing pulses: "
            f"{sorted(stale)}. Either update the pulse() call site or remove "
            f"the stale entry from _SERVICES."
        )

    def test_dev_cog_only_uses_live_keys(self):
        live = _all_pulse_keys()
        referenced = _hb_keys_referenced_in(COGS_DIR / "dev.py")
        referenced = {k for k in referenced if k.islower() and "_" in k}
        stale = referenced - live
        assert not stale, (
            f"cogs/dev.py references heartbeat key(s) that nothing pulses: "
            f"{sorted(stale)}."
        )

    def test_legacy_price_drift_key_is_gone(self):
        """The pre-1.7 ``price_drift`` key was renamed to ``price_drift_trade``."""
        for fname in ("status.py", "dev.py", "diagnose.py"):
            text = _read(COGS_DIR / fname)
            # The exact string "price_drift" only -- ``price_drift_trade``
            # is the live key and must keep working. We test for the bare
            # form by requiring it not to be followed by ``_trade``.
            for m in re.finditer(r'"price_drift"', text):
                pytest.fail(
                    f"cogs/{fname} still references the legacy 'price_drift' "
                    f"heartbeat key. Use 'price_drift_trade'."
                )


class TestScaledMonetaryDisplay:
    """Raw NUMERIC(36,0) columns must always be descaled before display.

    Forgetting ``to_human(...)`` / ``row.h(...)`` on a stake_amount or pool
    reserve prints quintillion-dollar totals. We pin the display-time call
    sites to the descale path.
    """

    def test_status_pool_tvl_descales_reserves(self):
        text = _read(COGS_DIR / "status.py")
        # The TVL block must call _h() (or to_human) on reserve_a / reserve_b.
        # Match the loop body: ra = _h(pool.get("reserve_a"...
        assert re.search(
            r"ra\s*=\s*_h\(\s*pool\.get\(\s*[\"']reserve_a[\"']",
            text,
        ), "cogs/status.py pool TVL must descale reserve_a via _h(...)"
        assert re.search(
            r"rb\s*=\s*_h\(\s*pool\.get\(\s*[\"']reserve_b[\"']",
            text,
        ), "cogs/status.py pool TVL must descale reserve_b via _h(...)"

    def test_status_total_staked_uses_descale(self):
        text = _read(COGS_DIR / "status.py")
        # The PoS validator total must use _h on stake_amount
        assert re.search(
            r"_h\(\s*v\.get\(\s*[\"']stake_amount[\"']",
            text,
        ), (
            "cogs/status.py must descale pos_validator.stake_amount with _h(...) "
            "before summing -- raw values are 10**18-scaled and would print "
            "quintillions."
        )

    def test_dev_pools_check_descales_reserves(self):
        text = _read(COGS_DIR / "dev.py")
        # The .dev check pools subcommand must descale before TVL math.
        assert re.search(
            r"ra\s*=\s*_h\(\s*pool\.get\(\s*[\"']reserve_a[\"']",
            text,
        ), "cogs/dev.py .dev check pools must descale reserve_a"


class TestPrefixHygiene:
    """Status report user-facing strings must not hardcode the wrong prefix."""

    def test_auto_dm_footer_uses_config_prefix(self):
        text = _read(COGS_DIR / "dev.py")
        # The auto-status DM footer must compose its prefix from Config.PREFIX
        # (or ctx.prefix) -- never a hardcoded "." or "$" before "dev config".
        # We assert that the Config.PREFIX-based composition is present.
        assert re.search(
            r'\{prefix\}dev config interval',
            text,
        ), (
            "cogs/dev.py auto_status_dm footer must build the prefix from "
            "Config.PREFIX; do not hardcode it (e.g. '.dev config ...')."
        )


class TestModuleCoverage:
    """The diag block walking guild_settings.module_* must keep up with new flags."""

    def test_modules_diag_includes_modern_flags(self):
        """Modern systems (crafting/farming/fishing/rugpull) must be in the list."""
        text = _read(COGS_DIR / "diagnose.py")
        for flag in (
            "module_crafting", "module_farming", "module_fishing",
            "module_rugpull", "module_events", "module_faucet",
        ):
            assert f'"{flag}"' in text, (
                f"cogs/diagnose.py module_checks list is missing '{flag}'. "
                f"Auto-status reports won't surface its on/off state."
            )

    def test_auto_dm_module_health_lists_modern_systems(self):
        """The per-module roll-up should cover the major non-economy systems."""
        text = _read(COGS_DIR / "dev.py")
        for label in ("Crafting", "Dungeon", "Expeditions", "Farming",
                      "Fishing", "Quests", "Achievements", "Buddies"):
            assert f'"{label}"' in text, (
                f"cogs/dev.py auto-DM module health missing '{label}' entry."
            )


class TestNullModuleFlagIsEnabled:
    """Module flags with NULL value mean enabled-by-default; never report disabled.

    `module_crafting`, `module_farming`, `module_fishing` and several other
    admin-toggleable modules were added with `BOOLEAN` (no DEFAULT TRUE), so
    `dict.get(col, True)` returns `None` for guilds that have never run the
    admin toggle. The pre-fix diag code then classified these as 'disabled'
    and the auto-status DM said "Crafting/Fishing/Farming disabled" on every
    guild that had never explicitly enabled them via admin command.
    """

    def test_diag_uses_is_not_false_pattern(self):
        text = _read(COGS_DIR / "diagnose.py")
        # The new safe pattern: `settings.get(col) is not False`
        assert "settings.get(col) is not False" in text, (
            "cogs/diagnose.py _check_modules must use the "
            "`settings.get(col) is not False` pattern so a NULL module flag "
            "is treated as enabled-by-default. The legacy "
            "`settings.get(col, True)` returns None when the column exists "
            "with NULL, which is falsy and incorrectly reports the module "
            "as disabled."
        )

    def test_diag_does_not_use_legacy_truthy_get(self):
        """Legacy `settings.get(col, True)` is the bug we just fixed."""
        text = _read(COGS_DIR / "diagnose.py")
        # The module_checks loop body must not fall back to the broken
        # truthy `settings.get(col, True)` form.
        bad = re.search(
            r"for col, name in module_checks:[^\n]*\n\s*if settings\.get\(col,\s*True\):",
            text,
        )
        assert bad is None, (
            "cogs/diagnose.py _check_modules loop body still uses "
            "`settings.get(col, True)` which fails on NULL module columns."
        )

    def test_dev_module_state_distinguishes_disabled_from_unloaded(self):
        """The module health rollup must distinguish 'admin-disabled' from 'cog not loaded'."""
        text = _read(COGS_DIR / "dev.py")
        # The helper must be present and key on `is False` (not just falsy).
        assert "def _module_state(" in text, (
            "cogs/dev.py must define a _module_state helper that decides "
            "module status (cog-loaded vs admin-disabled vs enabled)."
        )
        assert "if val is False:" in text, (
            "cogs/dev.py _module_state must use `val is False` so a NULL "
            "module flag falls through to enabled-by-default."
        )


class TestModuleCatalogIntegrity:
    """Module catalog must reference real cog class names and real DB tables."""

    def _catalog_lines(self) -> list[tuple[str, str, str | None]]:
        """Pull (label, cog_class, table_or_None) tuples out of the catalog."""
        text = _read(COGS_DIR / "dev.py")
        m = re.search(
            r"_MODULE_CATALOG.*?=\s*\[(.*?)\n    \]",
            text, re.DOTALL,
        )
        assert m, "cogs/dev.py must declare _MODULE_CATALOG"
        body = m.group(1)
        rows: list[tuple[str, str, str | None]] = []
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("("):
                continue
            tokens = re.findall(r"\"([^\"]*)\"|(None)", line)
            vals = [a if a else None for a, _ in tokens]
            if len(vals) >= 3:
                rows.append((vals[0], vals[1], vals[2]))
        return rows

    def test_cog_classes_match_real_classes(self):
        """Every cog_class in the catalog must match a real registered cog name.

        Cogs declared as ``class Foo(commands.Cog, name="Bar"):`` register
        under "Bar" (the explicit name), not "Foo". The catalog must use
        whichever name actually shows up in ``bot.cogs.keys()``.
        """
        registered: set[str] = set()
        for path in COGS_DIR.glob("*.py"):
            text = _read(path)
            for m in re.finditer(
                r"^class (\w+)\(commands\.Cog(?:[^)]*?name\s*=\s*\"([^\"]+)\")?[^)]*\):",
                text, re.M,
            ):
                cls, explicit = m.group(1), m.group(2)
                registered.add(explicit if explicit else cls)
        for label, cog_class, _ in self._catalog_lines():
            assert cog_class in registered, (
                f"_MODULE_CATALOG row '{label}' points at cog name "
                f"`{cog_class}` but no cog registers under that name. "
                f"Cogs declared as `class X(commands.Cog, name=\"Y\"):` "
                f"register as Y, not X. Check bot.cogs.keys() for the "
                f"actual key."
            )

    def test_tables_exist_in_schema_or_migrations(self):
        """Every table referenced by the catalog must be defined somewhere."""
        sql_text = ""
        sql_paths = list(REPO_ROOT.glob("database/schema.sql"))
        sql_paths += list((REPO_ROOT / "database" / "migrations").glob("*.sql"))
        for p in sql_paths:
            sql_text += "\n" + _read(p)
        defined = set(re.findall(
            r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", sql_text, re.I,
        ))
        for label, _, table in self._catalog_lines():
            if table is None:
                continue
            assert table in defined, (
                f"_MODULE_CATALOG row '{label}' references table `{table}` "
                f"which is not defined in schema.sql or any migration. "
                f"The catalog used to point at non-existent names like "
                f"`farm_plots` and `fishing_inventory`, producing perpetual "
                f"red rows for healthy systems."
            )


class TestEmbedBuilderReturn:
    """Helpers that claim to return discord.Embed must call .build().

    `core.framework.embed.card(...)` returns a CardBuilder, not an Embed. Sending
    a bare builder to Discord fails with "'CardBuilder' object has no
    attribute 'to_dict'" the moment discord.py tries to serialise it. The
    regression hit `,dev status` because `_build_module_health_embed` was
    declared `-> discord.Embed | None` but actually returned the builder.
    """

    def test_build_module_health_embed_returns_built_embed(self):
        text = _read(COGS_DIR / "dev.py")
        # Locate the helper body and assert it ends with `.build()`.
        m = re.search(
            r"async def _build_module_health_embed\([^)]*\)[^:]*:.*?(?=\n    (?:async )?def |\nasync def setup\b)",
            text, re.DOTALL,
        )
        assert m, "Could not locate _build_module_health_embed source."
        body = m.group(0)
        # The function returns either None (early exits) or a CardBuilder
        # chain that MUST end in .build(). Guard against the bare-builder
        # form that previously returned `embed = card(...); return embed`.
        bad = re.search(r"return\s+embed\s*$", body, re.M)
        assert bad is None, (
            "_build_module_health_embed returns a bare CardBuilder. "
            "Embed builders must end with `.build()` before being "
            "returned -- otherwise discord.py crashes with 'CardBuilder' "
            "object has no attribute 'to_dict' when the embed is sent."
        )
        # The new form chains to .build() at the end of the return.
        assert re.search(r"\.build\(\)\s*\n\s*\)", body), (
            "_build_module_health_embed must finish its chain with "
            "`.build()` so the caller gets a real discord.Embed."
        )


class TestSeverityShadowing:
    """The doctor scan must not shadow the module-level error_tracker.Severity.

    `cogs/dev.py` already does ``from core.framework.error_tracker import ... Severity``
    at module level. A naked ``from cogs.health import ... Severity`` inside a
    function makes Python compile that name as a function-local, shadowing the
    module-level binding -- and any earlier reference to ``Severity`` in the
    same function then raises ``UnboundLocalError`` even though the name is
    visibly defined at the module level. The fix is to import the doctor
    Severity under a different name (``DoctorSeverity``).
    """

    def test_dev_does_not_shadow_module_severity(self):
        text = _read(COGS_DIR / "dev.py")
        # The bare local-import form is the bug. Allow only the renamed form.
        bad = re.search(
            r"from cogs\.health import [^\n]*\bSeverity\b(?!\s+as\b)",
            text,
        )
        assert bad is None, (
            "cogs/dev.py imports `Severity` from cogs.health without "
            "an `as` rename. That shadows the module-level "
            "`core.framework.error_tracker.Severity` and triggers "
            "UnboundLocalError on any earlier reference. Use "
            "`from cogs.health import Severity as DoctorSeverity`."
        )

    def test_dev_uses_doctor_severity_alias(self):
        text = _read(COGS_DIR / "dev.py")
        # Both surfaces (.dev status and auto_status_dm) must consume the
        # renamed symbol so a search for the doctor severity is unambiguous.
        assert "from cogs.health import Severity as DoctorSeverity" in text, (
            "cogs/dev.py must import the doctor severity as DoctorSeverity."
        )
        assert "DoctorSeverity.CRITICAL" in text, (
            "cogs/dev.py doctor snapshot must reference DoctorSeverity.* "
            "not the shadowed Severity."
        )


class TestSecurityAlertDescale:
    """Economy security alerts must descale transaction amounts before display.

    `transactions.amount_in` / `amount_out` are NUMERIC(36,0) scaled by 10**18.
    Summing them raw produces alert text like "$1,015,941,903,741,947,871,232.00
    earned in 5 minutes" which looks like an exploit but is just the wrong unit.
    """

    def test_economy_security_imports_descale(self):
        text = _read(COGS_DIR / "economy_security.py")
        # The cog must pull `to_human` (or its `_h` alias) into scope.
        has_h = bool(re.search(
            r"from core\.framework\.scale import (to_human|.*\bto_human\b)",
            text,
        ))
        assert has_h, (
            "cogs/economy_security.py must import `to_human` from "
            "core.framework.scale so transaction amounts can be descaled "
            "before being summed into alert text."
        )

    def test_income_velocity_descales_amount_out(self):
        text = _read(COGS_DIR / "economy_security.py")
        # Match the actual loop body: total_earned = sum(float(_h(t.get("amount_out") ...
        assert re.search(
            r"total_earned\s*=\s*sum\(\s*float\(_h\(t\.get\(\s*[\"']amount_out[\"']",
            text,
        ), (
            "INCOME_VELOCITY total_earned must descale amount_out via "
            "_h(...) -- summing the raw 10**18-scaled value produced "
            "the legendary '$1 sextillion earned' alert."
        )

    def test_gambling_velocity_descales_amount_in(self):
        text = _read(COGS_DIR / "economy_security.py")
        assert re.search(
            r"total_wagered\s*=\s*sum\(\s*float\(_h\(t\.get\(\s*[\"']amount_in[\"']",
            text,
        ), (
            "GAMBLING_VELOCITY total_wagered must descale amount_in via "
            "_h(...)."
        )

    def test_transfer_velocity_descales_amount_out(self):
        text = _read(COGS_DIR / "economy_security.py")
        assert re.search(
            r"total_moved\s*=\s*sum\(\s*float\(_h\(t\.get\(\s*[\"']amount_out[\"']",
            text,
        ), (
            "TRANSFER_VELOCITY total_moved must descale amount_out via "
            "_h(...)."
        )

    def test_no_raw_amount_sum_in_economy_security(self):
        """Catch any future regression that re-introduces a raw amount sum."""
        text = _read(COGS_DIR / "economy_security.py")
        # Allow `float(_h(...))` and forbid the bare `float(t.get("amount_..."))`.
        for m in re.finditer(
            r"sum\(\s*float\(t\.get\(\s*[\"']amount_(in|out)[\"']",
            text,
        ):
            pytest.fail(
                "cogs/economy_security.py contains a sum of raw "
                "`amount_in/_out` -- those columns are 10**18-scaled, so "
                "the resulting alert prints quintillion-dollar totals. "
                "Wrap each value with _h(...) before sum()."
            )


class TestDoctorScanIntegration:
    """The auto-DM and .dev status must surface the read-only doctor scan."""

    def test_doctor_quick_scan_is_exposed(self):
        text = _read(COGS_DIR / "health.py")
        assert "async def doctor_quick_scan(" in text, (
            "cogs/health.py must expose a module-level `doctor_quick_scan` "
            "helper so .dev status and auto_status_dm can reuse the same "
            "issue-collection logic without running the live repair flow."
        )

    def test_dev_status_calls_doctor_scan(self):
        text = _read(COGS_DIR / "dev.py")
        assert "doctor_quick_scan(self.bot, ctx.guild)" in text, (
            "cogs/dev.py dev_status must call doctor_quick_scan so the "
            "Doctor Snapshot page is populated with real data."
        )

    def test_auto_dm_calls_doctor_scan(self):
        text = _read(COGS_DIR / "dev.py")
        # Auto-DM uses the primary guild
        assert "doctor_quick_scan(self.bot, primary_guild)" in text, (
            "cogs/dev.py auto_status_dm must call doctor_quick_scan against "
            "the primary guild so the auto-DM includes a Doctor Snapshot."
        )

    def test_status_pages_show_health_score_bar(self):
        """Both surfaces must render the 12-segment score bar."""
        for fname in ("dev.py",):
            text = _read(COGS_DIR / fname)
            assert "doctor_score" in text, (
                f"cogs/{fname} must expose `doctor_score` to the embed."
            )
            assert "12 - len(_bar_full)" in text, (
                f"cogs/{fname} must render a 12-segment health bar so the "
                "header always reads `▓▓▓░░░░ 30/100` style."
            )
