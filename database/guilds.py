"""Guilds repository  -  settings, tokens, networks, personas, webhooks (PostgreSQL)."""
from __future__ import annotations

from datetime import datetime, timezone
from core.framework.scale import to_human

from core.config import Config
from .base import PgBaseRepo


class PgGuildsRepo(PgBaseRepo):
    _BOOL_SETTINGS = {
        "ai_mm_enabled", "ai_chat_enabled", "ai_chat_threaded",
        "ai_commentary_enabled",
        "ai_flavor_enabled", "ai_events_enabled",
        "module_gambling", "module_lending", "module_staking",
        "module_mining", "module_drops", "module_faucet", "module_savings",
        "module_validators", "module_pools", "module_contracts",
        "module_groups", "module_chart", "module_crypto",
        "module_daily", "module_work", "module_economy", "module_chain",
        "module_shop", "module_games",
        "module_gambling_coinflip", "module_gambling_dice",
        "module_gambling_roulette", "module_gambling_blackjack",
        "module_gambling_slots",
        "module_ape", "module_nft", "module_predictions", "module_events",
        "module_rugpull", "module_security", "module_fishing", "module_farming",
        "module_crafting",
        "scam_detection",
        "clamp_clear_urls", "clamp_clear_addresses", "clamp_clear_scams",
        "clasp_auto_mute", "clasp_auto_delete",
        "automod_auto_clank",
    }

    # ── Guild Settings ─────────────────────────────────────────────────────

    async def get_guild_settings(self, guild_id: int) -> dict:
        await self.execute(
            "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
            guild_id,
        )
        return await self.fetch_one(
            "SELECT * FROM guild_settings WHERE guild_id=$1", guild_id
        )

    async def set_trade_channel(self, guild_id: int, channel_id: int | None) -> None:
        await self.execute(
            "INSERT INTO guild_settings (guild_id, trade_channel) VALUES ($1, $2) "
            "ON CONFLICT(guild_id) DO UPDATE SET trade_channel=EXCLUDED.trade_channel",
            guild_id, channel_id,
        )

    async def set_mine_channel(self, guild_id: int, channel_id: int | None) -> None:
        await self.execute(
            "INSERT INTO guild_settings (guild_id, mine_channel) VALUES ($1, $2) "
            "ON CONFLICT(guild_id) DO UPDATE SET mine_channel=EXCLUDED.mine_channel",
            guild_id, channel_id,
        )

    async def set_channel(self, guild_id: int, column: str, channel_id: int | None) -> None:
        """Generic channel setter. Column must be in the allowlist."""
        _ALLOWED = {
            "trade_channel", "mine_channel", "staking_channel",
            "crypto_channel", "gambling_channel", "pools_channel",
            "drops_channel", "job_channel", "drops_spawn_channel",
            "faucet_channel",
            "validators_channel", "contracts_channel",
            "wallet_channel", "error_channel",
            "whale_alerts_channel",
            "reports_feed_channel",
            "security_log_channel",
            "nft_channel", "predictions_channel",
            "events_channel", "ape_channel",
            "vault_feed_channel",
            "grouphall_channel",
            "income_channel",
            "fishing_channel",
            "changelog_channel",
        }
        if column not in _ALLOWED:
            raise ValueError(f"Unknown channel column: {column}")
        await self.execute(
            f"INSERT INTO guild_settings (guild_id, {column}) VALUES ($1, $2) "
            f"ON CONFLICT(guild_id) DO UPDATE SET {column}=EXCLUDED.{column}",
            guild_id, channel_id,
        )

    async def update_guild_setting(self, guild_id: int, column: str, value) -> None:
        _ALLOWED = {
            "prefix", "embed_color", "server_name", "currency_name",
            "trade_channel", "mine_channel", "staking_channel",
            "crypto_channel", "gambling_channel", "pools_channel", "drops_channel",
            "validators_channel", "contracts_channel",
            "wallet_channel", "error_channel",
            "ai_mm_enabled", "ai_chat_enabled", "ai_chat_threaded",
            "ai_commentary_enabled",
            "ai_flavor_enabled", "ai_events_enabled",
            "ai_prompt_chat", "ai_prompt_commentary", "ai_prompt_events",
            "ai_prompt_flavor", "ai_persona_name",
            "module_gambling", "module_lending", "module_staking",
            "module_mining", "module_drops", "module_faucet", "module_savings",
            "module_validators", "module_pools",
            "module_contracts", "module_groups", "module_chart", "module_crypto",
            "module_daily", "module_work", "module_economy", "module_chain",
            "module_shop", "module_games",
            "module_gambling_coinflip", "module_gambling_dice",
            "module_gambling_roulette", "module_gambling_blackjack", "module_gambling_slots",
            "module_ape", "module_nft", "module_predictions", "module_events",
            "module_rugpull", "module_fishing", "module_farming",
            "module_crafting",
            "reports_auto_diagnose",
            "reports_auto_fix",
            "reports_auto_close",
            "fishing_channel",
            "nft_channel", "predictions_channel", "events_channel", "ape_channel",
            "vault_feed_channel",
            "cmd_delete_after", "reply_delete_after",
            "ai_cmd_delete_after", "ai_reply_delete_after",
            "scam_detection", "scam_channel", "scam_timeout_minutes",
            "drop_interval", "drop_min", "drop_max",
            "faucet_multiplier", "faucet_tokens",
            "work_multiplier", "daily_multiplier", "gambling_multiplier",
            "mining_multiplier", "staking_multiplier", "validator_multiplier",
            "drops_multiplier", "beg_multiplier", "ape_multiplier", "savings_multiplier",
            "grouphall_channel",
            "platform_fee_pct", "platform_fee_min", "platform_fee_max", "treasury_cut_pct",
            "whale_alerts_channel", "whale_alert_threshold",
            "reports_feed_channel", "reports_feed_categories",
            "security_log_channel", "security_audit_roles",
            "heal_ai_backend", "heal_ai_model", "heal_ai_base_url",
            "search_backend", "tools_backend",
            "error_feed_levels",
            "changelog_channel",
            "changelog_last_posted",
            "clamp_clear_urls", "clamp_clear_addresses", "clamp_clear_scams",
            "clasp_auto_mute", "clasp_auto_delete",
            "clamp_channel_ids",
            "automod_auto_clank",
            "scam_report_channel", "scam_hunter_ids",
            "clank_escape_thread",
        }
        if column not in _ALLOWED:
            raise ValueError(f"Unknown setting: {column}")
        if column in self._BOOL_SETTINGS and value is not None and not isinstance(value, bool):
            if isinstance(value, int):
                if value in (0, 1):
                    value = bool(value)
                else:
                    raise ValueError(f"Invalid boolean value for {column}: {value!r}")
            elif isinstance(value, float):
                if value in (0.0, 1.0):
                    value = bool(value)
                else:
                    raise ValueError(f"Invalid boolean value for {column}: {value!r}")
            elif isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"1", "true", "yes", "on", "enabled"}:
                    value = True
                elif lowered in {"0", "false", "no", "off", "disabled"}:
                    value = False
                else:
                    raise ValueError(f"Invalid boolean value for {column}: {value!r}")
        await self.execute(
            f"INSERT INTO guild_settings (guild_id, {column}) VALUES ($1, $2) "
            f"ON CONFLICT(guild_id) DO UPDATE SET {column}=EXCLUDED.{column}",
            guild_id, value,
        )

    # ── Market Events ──────────────────────────────────────────────────────────

    async def get_guild_event(self, guild_id: int) -> dict | None:
        """Return the active market event for a guild, or None if no event / expired."""
        row = await self.fetch_one(
            "SELECT current_event, event_vol_mult, event_bias, event_expires_at "
            "FROM guild_settings WHERE guild_id=$1",
            guild_id,
        )
        if not row or not row.get("current_event"):
            return None
        # Check expiry (event_expires_at is epoch float from _coerce)
        expires = row.get("event_expires_at")
        if expires is not None:
            import time as _t
            exp_ts = expires.timestamp() if hasattr(expires, "timestamp") else float(expires)
            if _t.time() >= exp_ts:
                # Auto-clear expired event
                await self.clear_guild_event(guild_id)
                return None
        return dict(row)

    async def set_guild_event(
        self, guild_id: int, event_key: str, vol_mult: float, bias: float, expires_at,
    ) -> None:
        """Set the active market event for a guild."""
        await self.execute(
            "INSERT INTO guild_settings (guild_id, current_event, event_vol_mult, event_bias, event_expires_at) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "current_event=EXCLUDED.current_event, event_vol_mult=EXCLUDED.event_vol_mult, "
            "event_bias=EXCLUDED.event_bias, event_expires_at=EXCLUDED.event_expires_at",
            guild_id, event_key, vol_mult, bias, expires_at,
        )

    async def clear_guild_event(self, guild_id: int) -> None:
        """Clear the active market event for a guild."""
        await self.execute(
            "UPDATE guild_settings SET current_event=NULL, event_vol_mult=1.0, "
            "event_bias=0.0, event_expires_at=NULL WHERE guild_id=$1",
            guild_id,
        )

    async def get_disabled_events(self, guild_id: int) -> set[str]:
        """Return set of disabled event keys for a guild."""
        row = await self.fetch_one(
            "SELECT disabled_events FROM guild_settings WHERE guild_id=$1", guild_id,
        )
        if not row or not row.get("disabled_events"):
            return set()
        return set(filter(None, row["disabled_events"].split(",")))

    async def set_disabled_events(self, guild_id: int, disabled: set[str]) -> None:
        """Set the disabled events list for a guild."""
        value = ",".join(sorted(disabled)) if disabled else ""
        await self.execute(
            "INSERT INTO guild_settings (guild_id, disabled_events) VALUES ($1, $2) "
            "ON CONFLICT(guild_id) DO UPDATE SET disabled_events=EXCLUDED.disabled_events",
            guild_id, value,
        )

    async def get_event_frequency(self, guild_id: int) -> float:
        """Return the random event trigger probability per tick (default 0.0005)."""
        row = await self.fetch_one(
            "SELECT event_frequency FROM guild_settings WHERE guild_id=$1", guild_id,
        )
        if not row or row.get("event_frequency") is None:
            return 0.0005
        return float(row["event_frequency"])

    async def set_event_frequency(self, guild_id: int, freq: float) -> None:
        """Set the random event trigger probability per tick."""
        await self.execute(
            "INSERT INTO guild_settings (guild_id, event_frequency) VALUES ($1, $2) "
            "ON CONFLICT(guild_id) DO UPDATE SET event_frequency=EXCLUDED.event_frequency",
            guild_id, freq,
        )

    async def get_command_allowed_roles(self, guild_id: int, command_name: str) -> list[int]:
        """Return list of role IDs allowed to use command_name in this guild.
        Empty list means no restriction (everyone can use it)."""
        rows = await self.fetch_all(
            "SELECT role_id FROM guild_command_roles WHERE guild_id=$1 AND command_name=$2",
            guild_id, command_name,
        )
        return [r["role_id"] for r in rows]

    async def get_all_command_roles(self, guild_id: int) -> dict[str, list[int]]:
        """Return all command->role_id mappings for this guild."""
        rows = await self.fetch_all(
            "SELECT command_name, role_id FROM guild_command_roles WHERE guild_id=$1 ORDER BY command_name",
            guild_id,
        )
        result: dict[str, list[int]] = {}
        for r in rows:
            result.setdefault(r["command_name"], []).append(r["role_id"])
        return result

    async def add_command_role(self, guild_id: int, command_name: str, role_id: int) -> None:
        """Allow role_id to use command_name. No-op if already present."""
        await self.execute(
            "INSERT INTO guild_command_roles (guild_id, command_name, role_id) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING",
            guild_id, command_name, role_id,
        )

    async def remove_command_role(self, guild_id: int, command_name: str, role_id: int) -> None:
        """Remove role restriction. If last role removed, command becomes unrestricted."""
        await self.execute(
            "DELETE FROM guild_command_roles WHERE guild_id=$1 AND command_name=$2 AND role_id=$3",
            guild_id, command_name, role_id,
        )

    async def clear_command_roles(self, guild_id: int, command_name: str) -> None:
        """Remove all role restrictions for a command (makes it unrestricted again)."""
        await self.execute(
            "DELETE FROM guild_command_roles WHERE guild_id=$1 AND command_name=$2",
            guild_id, command_name,
        )

    # ── Beta Feature Access ──────────────────────────────────────────────────

    async def get_beta_grants(self, guild_id: int, feature_name: str | None = None) -> list[dict]:
        """Return all beta grants for a guild, optionally filtered by feature."""
        if feature_name:
            return await self.fetch_all(
                "SELECT * FROM beta_features WHERE guild_id=$1 AND feature_name=$2 ORDER BY feature_name, grant_type",
                guild_id, feature_name,
            )
        return await self.fetch_all(
            "SELECT * FROM beta_features WHERE guild_id=$1 ORDER BY feature_name, grant_type",
            guild_id,
        )

    async def has_beta_access(self, guild_id: int, feature_name: str, user_id: int, role_ids: list[int]) -> bool:
        """Check if a user has beta access to a feature (via direct user grant or any role grant)."""
        # Check user grant
        row = await self.fetch_one(
            "SELECT 1 FROM beta_features WHERE guild_id=$1 AND feature_name=$2 AND grant_type='user' AND grant_id=$3",
            guild_id, feature_name, user_id,
        )
        if row:
            return True
        # Check role grants
        if role_ids:
            row = await self.fetch_one(
                "SELECT 1 FROM beta_features WHERE guild_id=$1 AND feature_name=$2 AND grant_type='role' AND grant_id = ANY($3::bigint[])",
                guild_id, feature_name, role_ids,
            )
            if row:
                return True
        return False

    async def grant_beta(self, guild_id: int, feature_name: str, grant_type: str, grant_id: int, granted_by: int) -> None:
        """Grant beta access to a user or role."""
        await self.execute(
            "INSERT INTO beta_features (guild_id, feature_name, grant_type, grant_id, granted_by) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
            guild_id, feature_name, grant_type, grant_id, granted_by,
        )

    async def revoke_beta(self, guild_id: int, feature_name: str, grant_type: str, grant_id: int) -> None:
        """Revoke beta access from a user or role."""
        await self.execute(
            "DELETE FROM beta_features WHERE guild_id=$1 AND feature_name=$2 AND grant_type=$3 AND grant_id=$4",
            guild_id, feature_name, grant_type, grant_id,
        )

    async def clear_beta_feature(self, guild_id: int, feature_name: str) -> None:
        """Remove all beta grants for a feature."""
        await self.execute(
            "DELETE FROM beta_features WHERE guild_id=$1 AND feature_name=$2",
            guild_id, feature_name,
        )

    async def get_fee_config(self, guild_id: int) -> dict:
        """Return effective fee config for this guild, falling back to Config defaults.

        platform_fee_min and platform_fee_max are stored as NUMERIC(36,0) raw scaled
        integers in guild_configs.  Always return them as human-scale floats so callers
        can compute fees as ``max(fee_min, min(fee_max, gross * fee_pct))`` without
        any unit mismatch.

        Values that were written without raw-scaling (a historical bug in the
        admin API) show up here as absurdly tiny numbers like 2e-17.  Anything
        below 1e-6 USD (1_000_000_000_000 raw) is treated as corrupted and the
        env default is used instead; otherwise a buggy write silently clamps
        every fee to $0.
        """
        settings = await self.get_guild_settings(guild_id)
        _raw_min = settings.get("platform_fee_min")
        _raw_max = settings.get("platform_fee_max")
        _pct = settings.get("platform_fee_pct")

        _MIN_SANE_RAW = 1_000_000_000_000  # 1e-6 USD in raw scale
        fee_min = to_human(int(_raw_min)) if _raw_min and int(_raw_min) >= _MIN_SANE_RAW else Config.WALLET_PLATFORM_FEE_MIN
        fee_max = to_human(int(_raw_max)) if _raw_max and int(_raw_max) >= _MIN_SANE_RAW else Config.WALLET_PLATFORM_FEE_MAX
        # If min > max, the cap would swallow the floor -- use defaults for both.
        if fee_min > fee_max:
            fee_min = Config.WALLET_PLATFORM_FEE_MIN
            fee_max = Config.WALLET_PLATFORM_FEE_MAX
        return {
            "platform_fee_pct": float(_pct) if _pct is not None else Config.WALLET_PLATFORM_FEE_PCT,
            "platform_fee_min": fee_min,
            "platform_fee_max": fee_max,
            "treasury_cut_pct": settings.get("treasury_cut_pct") if settings.get("treasury_cut_pct") is not None else 0.10,
        }

    # ── Scam log ───────────────────────────────────────────────────────────

    async def log_scam(
        self,
        guild_id: int,
        user_id: int,
        username: str,
        channel_id: int,
        content: str,
        actions: str,
    ) -> None:
        await self.execute(
            "INSERT INTO scam_log (guild_id, user_id, username, channel_id, content, actions, ts) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            guild_id, user_id, username, channel_id, content[:600], actions,
            datetime.now(timezone.utc),
        )

    async def get_recent_scam_log(self, guild_id: int, limit: int = 8) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM scam_log WHERE guild_id=$1 ORDER BY ts DESC LIMIT $2",
            guild_id, limit,
        )

    async def get_user_scam_log(self, guild_id: int, user_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM scam_log WHERE guild_id=$1 AND user_id=$2 ORDER BY ts DESC LIMIT 5",
            guild_id, user_id,
        )

    # ── Scam notify users ──────────────────────────────────────────────────

    async def add_scam_notify_user(self, guild_id: int, user_id: int) -> None:
        await self.execute(
            "INSERT INTO scam_notify_users (guild_id, user_id) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING",
            guild_id, user_id,
        )

    async def remove_scam_notify_user(self, guild_id: int, user_id: int) -> None:
        await self.execute(
            "DELETE FROM scam_notify_users WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )

    async def get_scam_notify_users(self, guild_id: int) -> list[int]:
        rows = await self.fetch_all(
            "SELECT user_id FROM scam_notify_users WHERE guild_id=$1", guild_id
        )
        return [r["user_id"] for r in rows]

    async def get_ai_prompts(self, guild_id: int) -> dict[str, str | None]:
        """Return per-guild custom AI system prompts (None = use default)."""
        s = await self.get_guild_settings(guild_id)
        return {
            "chat":        s.get("ai_prompt_chat"),
            "commentary":  s.get("ai_prompt_commentary"),
            "events":      s.get("ai_prompt_events"),
            "flavor":      s.get("ai_prompt_flavor"),
            "persona_name": s.get("ai_persona_name"),
        }

    async def get_ai_flags(self, guild_id: int) -> dict[str, bool]:
        """Return per-guild AI feature flags, falling back to global Config defaults."""
        from core.config import Config as _Config
        s = await self.get_guild_settings(guild_id)

        def _flag(col: str, global_val: bool) -> bool:
            v = s.get(col)
            return bool(v) if v is not None else global_val

        return {
            "mm":          _flag("ai_mm_enabled",         _Config.AI_MM_ENABLED),
            "chat":        _flag("ai_chat_enabled",        _Config.AI_CHAT_ENABLED),
            "threaded":    _flag("ai_chat_threaded",       True),
            "commentary":  _flag("ai_commentary_enabled",  _Config.AI_COMMENTARY_ENABLED),
            "flavor":      _flag("ai_flavor_enabled",      _Config.AI_FLAVOR_ENABLED),
            "events":      _flag("ai_events_enabled",      _Config.AI_EVENTS_ENABLED),
        }

    async def get_heal_ai_config(self, guild_id: int) -> dict:
        """Return per-guild heal AI provider config, falling back to global defaults."""
        from core.config import Config as _Config
        s = await self.get_guild_settings(guild_id)
        return {
            "backend":  s.get("heal_ai_backend")  or _Config.TOOLS_BACKEND or "openrouter",
            "model":    s.get("heal_ai_model")    or _Config.TOOLS_MODEL   or "",
            "base_url": s.get("heal_ai_base_url") or "",
        }

    async def module_enabled(self, guild_id: int, module: str) -> bool:
        """Returns True if a module is enabled for the guild (defaults to True).
        NULL in the database is treated as enabled (legacy rows before column was added)."""
        settings = await self.get_guild_settings(guild_id)
        val = settings.get(f"module_{module}", True)
        # NULL / None means the column wasn't in the row yet -> treat as enabled
        if val is None:
            return True
        return bool(val)

    # ── Network / Token Halts ──────────────────────────────────────────────

    def _parse_set(self, raw: str) -> set[str]:
        return set(filter(None, raw.split(",")))

    async def halt_network(self, guild_id: int, network: str) -> None:
        s = await self.get_guild_settings(guild_id)
        nets = self._parse_set(s.get("halted_networks", ""))
        nets.add(network.lower())
        await self.execute(
            "UPDATE guild_settings SET halted_networks=$1 WHERE guild_id=$2",
            ",".join(sorted(nets)), guild_id,
        )

    async def unhalt_network(self, guild_id: int, network: str) -> None:
        s = await self.get_guild_settings(guild_id)
        nets = self._parse_set(s.get("halted_networks", ""))
        nets.discard(network.lower())
        await self.execute(
            "UPDATE guild_settings SET halted_networks=$1 WHERE guild_id=$2",
            ",".join(sorted(nets)), guild_id,
        )

    async def is_network_halted(self, guild_id: int, network: str) -> bool:
        s = await self.get_guild_settings(guild_id)
        return network.lower() in self._parse_set(s.get("halted_networks", ""))

    async def get_halted_networks(self, guild_id: int) -> list[str]:
        s = await self.get_guild_settings(guild_id)
        return sorted(self._parse_set(s.get("halted_networks", "")))

    async def disable_token(self, guild_id: int, symbol: str) -> None:
        s = await self.get_guild_settings(guild_id)
        toks = self._parse_set(s.get("disabled_tokens", ""))
        toks.add(symbol.upper())
        await self.execute(
            "UPDATE guild_settings SET disabled_tokens=$1 WHERE guild_id=$2",
            ",".join(sorted(toks)), guild_id,
        )

    async def enable_token(self, guild_id: int, symbol: str) -> None:
        s = await self.get_guild_settings(guild_id)
        toks = self._parse_set(s.get("disabled_tokens", ""))
        toks.discard(symbol.upper())
        await self.execute(
            "UPDATE guild_settings SET disabled_tokens=$1 WHERE guild_id=$2",
            ",".join(sorted(toks)), guild_id,
        )

    async def is_token_disabled(self, guild_id: int, symbol: str) -> bool:
        s = await self.get_guild_settings(guild_id)
        return symbol.upper() in self._parse_set(s.get("disabled_tokens", ""))

    async def get_disabled_tokens(self, guild_id: int) -> list[str]:
        s = await self.get_guild_settings(guild_id)
        return sorted(self._parse_set(s.get("disabled_tokens", "")))

    # ── Bot Channels (no-prefix mode) ───────────────────────────────────

    async def add_bot_channel(self, guild_id: int, channel_id: int) -> None:
        s = await self.get_guild_settings(guild_id)
        chs = self._parse_set(s.get("bot_channels", ""))
        chs.add(str(channel_id))
        await self.execute(
            "UPDATE guild_settings SET bot_channels=$1 WHERE guild_id=$2",
            ",".join(sorted(chs)), guild_id,
        )

    async def remove_bot_channel(self, guild_id: int, channel_id: int) -> None:
        s = await self.get_guild_settings(guild_id)
        chs = self._parse_set(s.get("bot_channels", ""))
        chs.discard(str(channel_id))
        await self.execute(
            "UPDATE guild_settings SET bot_channels=$1 WHERE guild_id=$2",
            ",".join(sorted(chs)), guild_id,
        )

    async def is_bot_channel(self, guild_id: int, channel_id: int) -> bool:
        s = await self.get_guild_settings(guild_id)
        return str(channel_id) in self._parse_set(s.get("bot_channels", ""))

    async def get_bot_channels(self, guild_id: int) -> list[int]:
        s = await self.get_guild_settings(guild_id)
        return [int(c) for c in self._parse_set(s.get("bot_channels", "")) if c.isdigit()]

    async def clear_bot_channels(self, guild_id: int) -> int:
        """Wipe the bot-channel allowlist. Returns the count of entries
        removed so the admin command can confirm what was cleared.
        """
        before = await self.get_bot_channels(guild_id)
        await self.execute(
            "UPDATE guild_settings SET bot_channels='' WHERE guild_id=$1",
            guild_id,
        )
        return len(before)

    # ── Real-market Channels ($-prefix allowlist) ──────────────────────
    #
    # Independent from bot_channels so admins can enable the $chart /
    # $info commands in a chat channel without also enabling the full
    # game-command surface there. cogs/realmarket.py treats the
    # effective allowlist as (bot_channels ∪ realmarket_channels).

    async def add_realmarket_channel(self, guild_id: int, channel_id: int) -> None:
        s = await self.get_guild_settings(guild_id)
        chs = self._parse_set(s.get("realmarket_channels", ""))
        chs.add(str(channel_id))
        await self.execute(
            "UPDATE guild_settings SET realmarket_channels=$1 WHERE guild_id=$2",
            ",".join(sorted(chs)), guild_id,
        )

    async def remove_realmarket_channel(self, guild_id: int, channel_id: int) -> None:
        s = await self.get_guild_settings(guild_id)
        chs = self._parse_set(s.get("realmarket_channels", ""))
        chs.discard(str(channel_id))
        await self.execute(
            "UPDATE guild_settings SET realmarket_channels=$1 WHERE guild_id=$2",
            ",".join(sorted(chs)), guild_id,
        )

    async def get_realmarket_channels(self, guild_id: int) -> list[int]:
        s = await self.get_guild_settings(guild_id)
        return [int(c) for c in self._parse_set(s.get("realmarket_channels", "")) if c.isdigit()]

    async def clear_realmarket_channels(self, guild_id: int) -> int:
        before = await self.get_realmarket_channels(guild_id)
        await self.execute(
            "UPDATE guild_settings SET realmarket_channels='' WHERE guild_id=$1",
            guild_id,
        )
        return len(before)

    # ── AI chat channels (ambient chatter allowlist) ────────────────────
    #
    # Allowlist for unsolicited AI chatter. Empty = allow everywhere.
    # Reactive paths (,ask / @mention / reply-to-bot) ignore this list.

    async def add_ai_chat_channel(self, guild_id: int, channel_id: int) -> None:
        s = await self.get_guild_settings(guild_id)
        chs = self._parse_set(s.get("ai_chat_channels", ""))
        chs.add(str(channel_id))
        await self.execute(
            "UPDATE guild_settings SET ai_chat_channels=$1 WHERE guild_id=$2",
            ",".join(sorted(chs)), guild_id,
        )

    async def remove_ai_chat_channel(self, guild_id: int, channel_id: int) -> None:
        s = await self.get_guild_settings(guild_id)
        chs = self._parse_set(s.get("ai_chat_channels", ""))
        chs.discard(str(channel_id))
        await self.execute(
            "UPDATE guild_settings SET ai_chat_channels=$1 WHERE guild_id=$2",
            ",".join(sorted(chs)), guild_id,
        )

    async def is_ai_chat_channel(self, guild_id: int, channel_id: int) -> bool:
        s = await self.get_guild_settings(guild_id)
        return str(channel_id) in self._parse_set(s.get("ai_chat_channels", ""))

    async def get_ai_chat_channels(self, guild_id: int) -> list[int]:
        s = await self.get_guild_settings(guild_id)
        return [int(c) for c in self._parse_set(s.get("ai_chat_channels", "")) if c.isdigit()]

    async def clear_ai_chat_channels(self, guild_id: int) -> int:
        """Wipe the AI ambient-chat allowlist. Returns the count of
        entries removed so the admin command can confirm. Empty list
        falls back to "AI may chime in anywhere the bot can post".
        """
        before = await self.get_ai_chat_channels(guild_id)
        await self.execute(
            "UPDATE guild_settings SET ai_chat_channels='' WHERE guild_id=$1",
            guild_id,
        )
        return len(before)

    async def reset_guild(self, guild_id: int) -> dict[str, int]:
        """Wipe all economy data for an entire guild (except guild_settings).
        Returns a dict of {table: rows_deleted} for each table that had data."""
        tables = [
            "users", "crypto_holdings", "stakes", "loans",
            "mining_rigs", "mining_pool_members", "lp_positions", "lp_snapshots",
            "user_jobs", "pools", "crypto_prices", "mining_network",
            "mining_blocks", "transactions", "chain_blocks",
            "mining_groups", "mining_group_members",
            "wallet_addresses", "token_contracts",
            # PoS / validator system
            "mempool", "validator_blocks", "pos_validators",
            "guild_treasury", "network_base_fees",
            # DeFi wallet
            "wallet_holdings",
            # Contracts
            "smart_contracts", "contract_events",
            # Candle history
            "price_candles",
            # Custom tokens and networks
            "guild_tokens", "guild_networks",
            # User prefs and mining config
            "user_prefs", "user_mining_config", "mining_group_weights",
            # Group features
            "group_invites", "group_upgrades",
            # AI conversation history
            "ai_conversations",
            # Shop items
            "hashstones", "lockstones", "vaultstones",
            # Savings
            "savings_deposits",
            # PoS delegations
            "pos_delegations",
        ]
        totals: dict[str, int] = {}
        async with self.transaction() as conn:
            for table in tables:
                try:
                    count = await conn.fetchval(
                        f"SELECT COUNT(*) FROM {table} WHERE guild_id=$1", guild_id
                    )
                    await conn.execute(f"DELETE FROM {table} WHERE guild_id=$1", guild_id)
                    if count and count > 0:
                        totals[table] = count
                except Exception:
                    pass  # table may not exist in older DBs
        return totals

    # ── Dynamic Guild Tokens ───────────────────────────────────────────────

    async def get_guild_tokens(self, guild_id: int) -> list[dict]:
        """Custom tokens registered for this guild."""
        return await self.fetch_all(
            "SELECT * FROM guild_tokens WHERE guild_id=$1", guild_id
        )

    async def add_guild_token(
        self, guild_id: int, symbol: str, name: str, emoji: str,
        consensus: str, network: str | None, start_price: float, daily_vol: float,
        max_supply: int | None = None,
    ) -> None:
        # Built-in symbols share the global crypto_prices/pools/tx-history
        # namespace with their guild_tokens row, so a guild_tokens entry with
        # the same symbol silently shadows the native token and produces
        # un-swappable Moon Network wallet rows. Reject the insert at the DB
        # boundary so callers (admin, groups, NFT issuance) all get the same
        # guarantee instead of each having to remember the check.
        from core.config import Config  # local import to avoid circular dependency at module import time
        if symbol in Config.TOKENS:
            raise ValueError(
                f"Cannot register guild token '{symbol}': symbol collides with built-in token. "
                f"Pick a different symbol."
            )
        # Default to a 100M cap when caller passed nothing -- mirrors the
        # 0271 migration so legacy deploys and group tokens get the same
        # protection the mint chokepoint enforces for built-ins.
        _DEFAULT_MAX_SUPPLY_RAW = 100_000_000 * 10**18
        if max_supply is None or max_supply <= 0:
            max_supply = _DEFAULT_MAX_SUPPLY_RAW
        await self.execute(
            "INSERT INTO guild_tokens "
            "(guild_id, symbol, name, emoji, consensus, network, start_price, daily_vol, max_supply) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
            "ON CONFLICT(guild_id, symbol) DO UPDATE SET "
            "name=EXCLUDED.name, emoji=EXCLUDED.emoji, consensus=EXCLUDED.consensus, "
            "network=EXCLUDED.network, start_price=EXCLUDED.start_price, daily_vol=EXCLUDED.daily_vol, "
            "max_supply=COALESCE(guild_tokens.max_supply, EXCLUDED.max_supply)",
            guild_id, symbol, name, emoji, consensus, network, start_price, daily_vol, max_supply,
        )

    async def remove_guild_token(self, guild_id: int, symbol: str) -> None:
        await self.execute(
            "DELETE FROM guild_tokens WHERE guild_id=$1 AND symbol=$2", guild_id, symbol
        )

    async def get_group_tokens(self, guild_id: int) -> list[dict]:
        """Return all group-type tokens with their associated group name."""
        return await self.fetch_all(
            "SELECT gt.*, mg.name AS group_name, mg.group_id "
            "FROM guild_tokens gt "
            "LEFT JOIN mining_groups mg ON mg.guild_id = gt.guild_id AND mg.token_symbol = gt.symbol "
            "WHERE gt.guild_id=$1 AND gt.token_type = 'group' "
            "ORDER BY gt.created_at DESC",
            guild_id,
        )

    async def enable_group_token_trading(self, guild_id: int, symbol: str) -> None:
        """Unlock a group token for player trading: clears disable flag and vault locks."""
        await self.enable_token(guild_id, symbol)
        await self.execute(
            "UPDATE guild_tokens SET vault_locked=FALSE, trading_enabled=TRUE "
            "WHERE guild_id=$1 AND symbol=$2",
            guild_id, symbol,
        )
        await self.execute(
            "UPDATE pools SET vault_locked=FALSE "
            "WHERE guild_id=$1 AND (token_a=$2 OR token_b=$2)",
            guild_id, symbol,
        )

    async def disable_group_token_trading(self, guild_id: int, symbol: str) -> None:
        """Lock a group token: adds disable flag and vault locks on token + pool."""
        await self.disable_token(guild_id, symbol)
        await self.execute(
            "UPDATE guild_tokens SET vault_locked=TRUE, trading_enabled=FALSE "
            "WHERE guild_id=$1 AND symbol=$2",
            guild_id, symbol,
        )
        await self.execute(
            "UPDATE pools SET vault_locked=TRUE "
            "WHERE guild_id=$1 AND (token_a=$2 OR token_b=$2)",
            guild_id, symbol,
        )

    # ── Dynamic Guild Networks ─────────────────────────────────────────────

    async def get_guild_networks(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM guild_networks WHERE guild_id=$1", guild_id
        )

    async def get_network_stake_token(self, guild_id: int, network_name: str) -> str | None:
        """Check Config.NETWORK_STAKE_TOKEN first, then guild_networks table."""
        if network_name in Config.NETWORK_STAKE_TOKEN:
            return Config.NETWORK_STAKE_TOKEN[network_name]
        row = await self.fetch_one(
            "SELECT stake_token FROM guild_networks WHERE guild_id=$1 AND network_name=$2",
            guild_id, network_name,
        )
        return row["stake_token"] if row else None

    async def add_guild_network(
        self, guild_id: int, network_name: str, stake_token: str, emoji: str = "🌐"
    ) -> None:
        await self.execute(
            "INSERT INTO guild_networks (guild_id, network_name, stake_token, emoji) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT(guild_id, network_name) DO UPDATE SET "
            "stake_token=EXCLUDED.stake_token, emoji=EXCLUDED.emoji",
            guild_id, network_name, stake_token, emoji,
        )

    async def remove_guild_network(self, guild_id: int, network_name: str) -> None:
        await self.execute(
            "DELETE FROM guild_networks WHERE guild_id=$1 AND network_name=$2",
            guild_id, network_name,
        )

    # ── MM Webhooks ─────────────────────────────────────────────────────────

    async def save_mm_webhook(
        self, guild_id: int, webhook_id: str, webhook_token: str, channel_id: int
    ) -> None:
        await self.execute(
            "INSERT INTO mm_webhooks (guild_id, webhook_id, webhook_token, channel_id) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "webhook_id=EXCLUDED.webhook_id, "
            "webhook_token=EXCLUDED.webhook_token, "
            "channel_id=EXCLUDED.channel_id",
            guild_id, webhook_id, webhook_token, channel_id,
        )

    async def get_mm_webhook(self, guild_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM mm_webhooks WHERE guild_id=$1", guild_id
        )

    async def delete_mm_webhook(self, guild_id: int) -> None:
        await self.execute(
            "DELETE FROM mm_webhooks WHERE guild_id=$1", guild_id
        )

    # ── MM Personas ──────────────────────────────────────────────────────────

    _DEFAULT_PERSONAS = [
        {
            "name": "MarketBot",
            "system_prompt": "You are MarketBot, a cold algorithmic quant. You are precise, data-driven, and clinical. You make decisions based purely on numbers.",
            "avatar_url": "https://robohash.org/MarketBot?set=set3&size=80x80",
            "trade_bias": "neutral",
            "emoji": "📊",
        },
        {
            "name": "AlgoTrader",
            "system_prompt": "You are AlgoTrader, a momentum-chasing trendfollower. You are degen AF and always riding the wave. You love buying pumps and selling dumps.",
            "avatar_url": "https://robohash.org/AlgoTrader?set=set3&size=80x80",
            "trade_bias": "bull",
            "emoji": "🚀",
        },
        {
            "name": "Sentinel-7",
            "system_prompt": "You are Sentinel-7, a cautious risk manager. You are conservative, hedge every position, and protect capital above all else.",
            "avatar_url": "https://robohash.org/Sentinel7?set=set3&size=80x80",
            "trade_bias": "bear",
            "emoji": "🛡️",
        },
        {
            "name": "ArbEngine",
            "system_prompt": "You are ArbEngine, a pure arbitrageur. You exploit every spread and inefficiency ruthlessly. You are emotionless and systematic.",
            "avatar_url": "https://robohash.org/ArbEngine?set=set3&size=80x80",
            "trade_bias": "random",
            "emoji": "⚡",
        },
        {
            "name": "DeepLiquid",
            "system_prompt": "You are DeepLiquid, a philosophical liquidity provider. You are market-neutral and muse about the nature of value while making trades.",
            "avatar_url": "https://robohash.org/DeepLiquid?set=set3&size=80x80",
            "trade_bias": "neutral",
            "emoji": "🌊",
        },
    ]

    async def seed_default_mm_personas(self, guild_id: int) -> None:
        """Insert built-in personas for a guild if none exist yet."""
        count = await self.fetch_val(
            "SELECT COUNT(*) FROM mm_personas WHERE guild_id=$1", guild_id
        )
        if count and count > 0:
            return  # already seeded
        for p in self._DEFAULT_PERSONAS:
            await self.execute(
                "INSERT INTO mm_personas "
                "(guild_id, name, system_prompt, avatar_url, trade_bias, emoji, active) "
                "VALUES ($1, $2, $3, $4, $5, $6, TRUE) "
                "ON CONFLICT DO NOTHING",
                guild_id, p["name"], p["system_prompt"], p["avatar_url"],
                p["trade_bias"], p["emoji"],
            )

    async def get_mm_personas(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM mm_personas WHERE guild_id=$1 ORDER BY id", guild_id
        )

    async def get_active_mm_personas(self, guild_id: int) -> list[dict]:
        """Return active personas, falling back to built-in defaults if none configured."""
        rows = await self.fetch_all(
            "SELECT * FROM mm_personas WHERE guild_id=$1 AND active = TRUE ORDER BY id",
            guild_id,
        )
        return rows  # empty list if none -- caller should fall back to hardcoded

    async def create_mm_persona(
        self, guild_id: int, name: str, system_prompt: str,
        avatar_url: str, trade_bias: str, emoji: str,
    ) -> None:
        await self.execute(
            "INSERT INTO mm_personas (guild_id, name, system_prompt, avatar_url, trade_bias, emoji, active) "
            "VALUES ($1, $2, $3, $4, $5, $6, TRUE) "
            "ON CONFLICT(guild_id, name) DO UPDATE SET "
            "system_prompt=EXCLUDED.system_prompt, "
            "avatar_url=EXCLUDED.avatar_url, "
            "trade_bias=EXCLUDED.trade_bias, "
            "emoji=EXCLUDED.emoji",
            guild_id, name, system_prompt, avatar_url, trade_bias, emoji,
        )

    async def update_mm_persona_field(
        self, guild_id: int, name: str, field: str, value
    ) -> None:
        _ALLOWED = {"system_prompt", "avatar_url", "trade_bias", "emoji", "active", "name"}
        if field not in _ALLOWED:
            raise ValueError(f"Unknown persona field: {field}")
        await self.execute(
            f"UPDATE mm_personas SET {field}=$1 WHERE guild_id=$2 AND name=$3",
            value, guild_id, name,
        )

    async def delete_mm_persona(self, guild_id: int, name: str) -> None:
        await self.execute(
            "DELETE FROM mm_personas WHERE guild_id=$1 AND name=$2", guild_id, name
        )

    # ── Guild custom emoji meanings + usage ────────────────────────────────
    #
    # See migration 0107_guild_emoji_meanings.sql. The indexer in
    # core/framework/emoji_index.py combines vision + recent usage snippets into a
    # single nuanced description per emoji and upserts it here; the chat
    # system prompt in core/framework/emoji_context.py reads it back out.

    async def upsert_emoji_meaning(
        self,
        guild_id: int,
        emoji_id: int,
        name: str,
        description: str,
        *,
        animated: bool = False,
        category: str | None = None,
        source: str = "vision",
    ) -> None:
        await self.execute(
            """
            INSERT INTO guild_emoji_meanings
                (guild_id, emoji_id, name, animated, description, category, source, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            ON CONFLICT (guild_id, emoji_id) DO UPDATE SET
                name        = EXCLUDED.name,
                animated    = EXCLUDED.animated,
                description = EXCLUDED.description,
                category    = EXCLUDED.category,
                source      = EXCLUDED.source,
                updated_at  = NOW()
            """,
            guild_id, emoji_id, name, animated, description, category, source,
        )

    async def get_emoji_meaning(self, guild_id: int, emoji_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM guild_emoji_meanings WHERE guild_id=$1 AND emoji_id=$2",
            guild_id, emoji_id,
        )

    async def get_all_emoji_meanings(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM guild_emoji_meanings WHERE guild_id=$1 ORDER BY name",
            guild_id,
        )

    async def get_stale_emoji_meaning_ids(
        self, guild_id: int, max_age_days: int = 14,
    ) -> list[int]:
        rows = await self.fetch_all(
            """
            SELECT emoji_id FROM guild_emoji_meanings
             WHERE guild_id=$1
               AND updated_at < NOW() - ($2 || ' days')::interval
            """,
            guild_id, str(int(max_age_days)),
        )
        return [int(r["emoji_id"]) for r in rows]

    async def delete_emoji_meaning(self, guild_id: int, emoji_id: int) -> None:
        await self.execute(
            "DELETE FROM guild_emoji_meanings WHERE guild_id=$1 AND emoji_id=$2",
            guild_id, emoji_id,
        )

    async def log_emoji_usage(
        self,
        guild_id: int,
        emoji_id: int,
        user_id: int,
        snippet: str,
    ) -> None:
        """Record one usage of a custom emoji. Snippet is capped to 200 chars."""
        snippet = (snippet or "")[:200]
        await self.execute(
            """
            INSERT INTO guild_emoji_usage (guild_id, emoji_id, user_id, snippet)
            VALUES ($1, $2, $3, $4)
            """,
            guild_id, emoji_id, user_id, snippet,
        )

    async def get_recent_emoji_usage(
        self, guild_id: int, emoji_id: int, *, limit: int = 20, days: int = 30,
    ) -> list[dict]:
        return await self.fetch_all(
            """
            SELECT user_id, snippet, ts FROM guild_emoji_usage
             WHERE guild_id=$1 AND emoji_id=$2
               AND ts > NOW() - ($3 || ' days')::interval
             ORDER BY ts DESC
             LIMIT $4
            """,
            guild_id, emoji_id, str(int(days)), int(limit),
        )

    async def count_recent_emoji_usage(
        self, guild_id: int, emoji_id: int, *, days: int = 30,
    ) -> int:
        val = await self.fetch_val(
            """
            SELECT COUNT(*) FROM guild_emoji_usage
             WHERE guild_id=$1 AND emoji_id=$2
               AND ts > NOW() - ($3 || ' days')::interval
            """,
            guild_id, emoji_id, str(int(days)),
        )
        return int(val or 0)

    async def prune_old_emoji_usage(self, days: int = 30) -> int:
        status = await self.execute(
            "DELETE FROM guild_emoji_usage WHERE ts < NOW() - ($1 || ' days')::interval",
            str(int(days)),
        )
        return self._row_count(status)

    # ── Cosmetic role overrides ─────────────────────────────────────────────

    async def get_cosmetic_role_overrides(self, guild_id: int) -> dict:
        """Return the guild's cosmetic_role_overrides JSONB as a plain dict.

        asyncpg hands back JSONB columns as ``str`` (raw JSON text) unless
        a per-connection codec is registered; the bare ``dict(value)``
        we used to call would then iterate the string char-by-char and
        blow up with "dictionary update sequence element #0 has length
        1; 2 is required" on the first non-empty override. Parse the
        string explicitly so the admin / shop callers always see a real
        dict regardless of how asyncpg returned the column.
        """
        row = await self.fetch_one(
            "SELECT cosmetic_role_overrides FROM guild_settings WHERE guild_id=$1",
            guild_id,
        )
        if not row:
            return {}
        raw = row.get("cosmetic_role_overrides")
        if raw is None:
            return {}
        if isinstance(raw, str):
            import json as _json
            try:
                parsed = _json.loads(raw) if raw else {}
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        if isinstance(raw, dict):
            return dict(raw)
        return {}

    async def set_cosmetic_role_override(
        self, guild_id: int, item_key: str, role_name: str | None
    ) -> None:
        """Set or clear the role name override for one cosmetic item."""
        await self.execute(
            "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
            guild_id,
        )
        if role_name is None:
            await self.execute(
                "UPDATE guild_settings"
                " SET cosmetic_role_overrides = cosmetic_role_overrides - $2"
                " WHERE guild_id=$1",
                guild_id, item_key,
            )
        else:
            await self.execute(
                "UPDATE guild_settings"
                " SET cosmetic_role_overrides = jsonb_set("
                "   COALESCE(cosmetic_role_overrides, '{}'), ARRAY[$2], to_jsonb($3::text)"
                " ) WHERE guild_id=$1",
                guild_id, item_key, role_name,
            )

    # ── Cosmetic role grants (time-limited 1h roles from craft path) ────────

    async def upsert_cosmetic_role_grant(
        self, user_id: int, guild_id: int, item_key: str,
        role_id: int, duration_seconds: int,
    ) -> None:
        """Stamp / refresh a (user, guild, item) cosmetic role grant.

        Unique on (user_id, guild_id, item_key) so re-applying the same
        cosmetic before the previous grant expires just bumps the
        deadline forward by ``duration_seconds`` from NOW(). The
        background sweeper in cogs/shop.py reads ``expires_at`` to know
        when to revoke the role.
        """
        await self.execute(
            """
            INSERT INTO cosmetic_role_grants
                (user_id, guild_id, item_key, role_id, granted_at, expires_at)
            VALUES ($1, $2, $3, $4, NOW(), NOW() + ($5 || ' seconds')::interval)
            ON CONFLICT (user_id, guild_id, item_key) DO UPDATE SET
                role_id    = EXCLUDED.role_id,
                granted_at = NOW(),
                expires_at = NOW() + ($5 || ' seconds')::interval
            """,
            int(user_id), int(guild_id), str(item_key),
            int(role_id), str(int(duration_seconds)),
        )

    async def get_active_cosmetic_role_grant(
        self, user_id: int, guild_id: int, item_key: str,
    ) -> dict | None:
        """Return the active grant row (or None) for one (user, guild, item)."""
        return await self.fetch_one(
            "SELECT * FROM cosmetic_role_grants "
            " WHERE user_id=$1 AND guild_id=$2 AND item_key=$3 "
            "   AND expires_at > NOW()",
            int(user_id), int(guild_id), str(item_key),
        )

    async def list_expired_cosmetic_role_grants(self, limit: int = 100) -> list[dict]:
        """Return every grant whose ``expires_at`` is in the past, capped.

        The sweeper iterates these, removes the role from the member,
        and deletes the row. ``limit`` keeps the per-tick batch bounded.
        """
        return await self.fetch_all(
            "SELECT * FROM cosmetic_role_grants "
            " WHERE expires_at <= NOW() "
            " ORDER BY expires_at ASC LIMIT $1",
            int(limit),
        ) or []

    async def delete_cosmetic_role_grant(self, grant_id: int) -> None:
        await self.execute(
            "DELETE FROM cosmetic_role_grants WHERE id = $1", int(grant_id),
        )
