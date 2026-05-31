"""services/vault.py  -  Network vault deposits and level-up checks.

Called from fee collection points (validators, mining, shop) to route
a small fraction of fees into per-network vaults and check for level-ups.
"""
from __future__ import annotations

import logging

from constants.ui import C_GRAY
from constants.vaults import (
    VAULT_FEE_FRACTION,
    VAULT_VOLUME_FRACTION,
    VAULT_DISPLAY,
    ALL_VAULT_NETWORKS,
    level_for_balance,
    next_threshold,
)

log = logging.getLogger("discoin.vault")


async def deposit_to_vault(
    db,
    guild_id: int,
    network: str,
    fee_amount: float,
    bot=None,
) -> dict | None:
    """Deposit a fraction of fees into the network vault and check for level-up.

    Args:
        db: Database instance
        guild_id: Guild ID
        network: Network short name ('sun', 'mta', 'arc', 'dsc')
        fee_amount: The total treasury fee amount (vault gets VAULT_FEE_FRACTION of this)
        bot: Bot instance (for level-up announcements)

    Returns:
        Level-up info dict if a level was gained, else None.
    """
    network = network.lower()
    if network not in ALL_VAULT_NETWORKS:
        return None

    vault_deposit = fee_amount * VAULT_FEE_FRACTION
    if vault_deposit <= 0:
        return None

    # Convert native coin amount to USD so vault balance aligns with USD thresholds.
    _VAULT_COIN: dict[str, str] = {"sun": "SUN", "mta": "MTA", "arc": "ARC", "dsc": "DSC"}
    coin = _VAULT_COIN.get(network)
    if coin:
        price_row = await db.get_price(coin, guild_id)
        if price_row and float(price_row["price"]) > 0:
            vault_deposit = vault_deposit * float(price_row["price"])
        else:
            # No price available  -  skip deposit rather than store a meaningless amount.
            log.debug("deposit_to_vault: no price for %s in guild %s  -  skipping", coin, guild_id)
            return None

    # Moon Network: split LUNAR_VAULT_SHARE off into the Moon Pool stakers'
    # distributable bucket BEFORE crediting the vault balance, so server-level
    # progression only fires against the portion the server actually owns.
    # Stakers drain distributable via the Moon Pool tick; balance never
    # re-absorbs that share, so levels stay earned rather than double-counted.
    distributable_delta = 0.0
    balance_delta = vault_deposit
    if network == "moon":
        try:
            from constants.moons import LUNAR_VAULT_SHARE
            distributable_delta = vault_deposit * LUNAR_VAULT_SHARE
            balance_delta = vault_deposit - distributable_delta
        except Exception as exc:
            log.warning(
                "moon vault distributable split failed for guild %s: %s",
                guild_id, exc,
            )
            distributable_delta = 0.0
            balance_delta = vault_deposit

    vault = await db.add_to_vault(guild_id, network, balance_delta)
    new_balance = vault["balance"]
    stored_level = vault["level"]

    if distributable_delta > 0:
        try:
            await db.add_moon_vault_distributable(guild_id, distributable_delta)
        except Exception as exc:
            log.warning(
                "moon vault distributable deposit failed for guild %s: %s",
                guild_id, exc,
            )

    # Check if we crossed a level threshold
    computed_level = level_for_balance(network, new_balance)
    if computed_level > stored_level:
        await db.set_vault_level(guild_id, network, computed_level)
        log.info(
            "Guild %s leveled up %s vault: %d -> %d (balance: $%.2f)",
            guild_id, network, stored_level, computed_level, new_balance,
        )

        level_info = {
            "network": network,
            "old_level": stored_level,
            "new_level": computed_level,
            "balance": new_balance,
        }

        # Announce level-up
        if bot:
            await _announce_levelup(bot, guild_id, level_info)
            if hasattr(bot, "bus"):
                await bot.bus.publish(
                    "vault_level_up",
                    guild_id=guild_id,
                    network=network,
                    level=computed_level,
                )

        return level_info

    return None


async def credit_vault_volume(
    db,
    guild_id: int,
    network: str,
    volume_usd: float,
    bot=None,
) -> dict | None:
    """Credit a small fraction of trade volume (already in USD) to a network vault.

    Unlike deposit_to_vault (which takes a native-coin fee and converts to USD),
    this function accepts a pre-calculated USD volume and applies VAULT_VOLUME_FRACTION
    directly.  Call after every buy / sell / swap so all networks accumulate progress
    from trading activity rather than from fees alone.
    """
    network = network.lower()
    if network not in ALL_VAULT_NETWORKS:
        return None
    vault_deposit = volume_usd * VAULT_VOLUME_FRACTION
    if vault_deposit <= 0:
        return None

    vault = await db.add_to_vault(guild_id, network, vault_deposit)
    new_balance = vault["balance"]
    stored_level = vault["level"]

    # Moon Network: earmark LUNAR_VAULT_SHARE of this inflow for Moon Pool stakers.
    # Never block the trade path on a bookkeeping failure  -  log and swallow.
    if network == "moon":
        try:
            from constants.moons import LUNAR_VAULT_SHARE
            distributable_delta = vault_deposit * LUNAR_VAULT_SHARE
            if distributable_delta > 0:
                await db.add_moon_vault_distributable(guild_id, distributable_delta)
        except Exception as exc:
            log.warning(
                "moon vault distributable credit failed for guild %s: %s",
                guild_id, exc,
            )

    computed_level = level_for_balance(network, new_balance)
    if computed_level > stored_level:
        await db.set_vault_level(guild_id, network, computed_level)
        log.info(
            "Guild %s leveled up %s vault: %d -> %d (balance: $%.2f)",
            guild_id, network, stored_level, computed_level, new_balance,
        )
        level_info = {
            "network": network,
            "old_level": stored_level,
            "new_level": computed_level,
            "balance": new_balance,
        }
        if bot:
            await _announce_levelup(bot, guild_id, level_info)
            if hasattr(bot, "bus"):
                await bot.bus.publish(
                    "vault_level_up",
                    guild_id=guild_id,
                    network=network,
                    level=computed_level,
                )
        return level_info

    return None


async def _announce_levelup(bot, guild_id: int, info: dict) -> None:
    """Send a level-up embed to the vault feed channel, and trigger a bull run at levels 5 and 10."""
    from core.framework.embed import card

    guild = bot.get_guild(guild_id)
    if guild is None:
        _check_level = info.get("new_level", 0)
        if _check_level in (5, 10):
            log.warning(
                "Vault milestone skipped: level %d, network=%s, guild=%d not in cache  -  "
                "bull run was not triggered.",
                _check_level, info.get("network", ""), guild_id,
            )
        return

    new_level = info.get("new_level", 0)
    if new_level in (5, 10):
        net = info.get("network", "")
        display = VAULT_DISPLAY.get(net, {"name": net.title(), "emoji": "\U0001f4e6"})
        await _trigger_milestone_bull_run(bot, guild, net, new_level, display)

    settings = await bot.db.get_guild_settings(guild_id)
    ch_id = settings.get("vault_feed_channel") if settings else None
    if not ch_id:
        # Fall back to events channel, then crypto channel
        ch_id = (settings.get("events_channel") or settings.get("crypto_channel")) if settings else None
    if not ch_id:
        return

    ch = guild.get_channel(int(ch_id))
    if ch is None or not hasattr(ch, "send"):
        return

    net = info["network"]
    display = VAULT_DISPLAY.get(net, {"name": net.title(), "emoji": "\U0001f4e6", "color": C_GRAY})
    new_level = info["new_level"]
    balance = info["balance"]

    nxt = next_threshold(net, new_level)
    nxt_str = f"${nxt:,.0f}" if nxt else "MAX"

    _b = card(
        f"{display['emoji']} {display['name']} \u2014 Level {new_level}!",
        description=(
            f"The server's **{display['name']}** vault has reached **Level {new_level}**!\n\n"
            f"Vault balance: **${balance:,.2f}**\n"
            f"Next level at: **{nxt_str}**"
        ),
        color=display["color"],
    )
    _b.footer("Network vaults grow from trading volume \u2014 keep trading!")

    try:
        await ch.send(
            content=f"\U0001f3c6 **LEVEL UP! {display['name']} vault reached Level {new_level}!**",
            embed=_b.build(),
        )
    except Exception as exc:
        log.warning("Failed to announce vault level-up in guild %s: %s", guild_id, exc)


async def _trigger_milestone_bull_run(bot, guild, network: str, level: int, display: dict) -> None:
    """Trigger a 24-hour bull run market event as a milestone reward for hitting vault level 5 or 10.

    Uses the existing market event engine so the bull run shows up in event history,
    sends the standard announcement embed, and phases properly. A custom 'vault_bull_run'
    event is registered with a single 24-hour euphoria phase  -  never picked randomly.
    Skips if another event is already active.
    """
    try:
        from cogs.events import trigger_event
        from services.market_event_engine import get_active_event

        redis = getattr(bot, "bus", None) and getattr(bot.bus, "_redis", None)

        # Don't stack on top of an existing event  -  only trigger if the market is quiet
        existing = await get_active_event(redis, guild.id)
        if existing is not None:
            log.info(
                "Milestone bull run skipped for guild %s (event '%s' already active)",
                guild.id, existing.event_id,
            )
            return

        # Ensure the vault_bull_run event is registered (idempotent)
        from configs.market_events_config import EVENT_REGISTRY, MarketEvent, EventPhase
        from constants.ui import C_BULL

        bull_run_id = "vault_bull_run"
        if bull_run_id not in EVENT_REGISTRY:
            EVENT_REGISTRY[bull_run_id] = MarketEvent(
                event_id=bull_run_id,
                display_name="Network Bull Run",
                emoji="\U0001f7e2",  # green circle
                description="A network vault milestone has unlocked a bull run. The whole market rallies for 24 hours.",
                rarity_weight=0,  # never picked randomly
                cooldown_minutes=0,
                phases=(
                    EventPhase(
                        name="bull_run",
                        duration_minutes=1440,  # 24 hours
                        vol_multiplier=1.4,
                        price_bias_pct_per_day=1.5,
                        fee_multiplier=0.85,
                        staking_apy_mult=1.2,
                        embed_color=C_BULL,
                        flavor_text=(
                            f"The {display['name']} vault hit **Level {level}**! "
                            "Bulls are in control for the next 24 hours."
                        ),
                    ),
                ),
            )

        await trigger_event(bot.db, guild, bull_run_id, bot=bot)
        log.info(
            "Milestone bull run triggered for guild %s  -  %s vault Level %d",
            guild.id, network, level,
        )
    except Exception as exc:
        log.warning(
            "Failed to trigger milestone bull run for guild %s network %s level %d: %s",
            guild.id, network, level, exc,
        )
