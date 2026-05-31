"""
services/fishing.py  -  Fishing-game state management.

The cog (cogs/fishing.py) drives the user-facing flow (animations,
buttons, embeds). Everything that touches the DB or the loot model
lives here so the cog stays presentation-only.

Public API (chunked across this module):
    ensure_state(db, gid, uid)                   -> dict
    list_state(db, gid, uid)                     -> dict
    get_top_fishers(db, gid, limit)              -> list[dict]
    get_biggest_catches(db, gid, limit)          -> list[dict]
    cast_resolve(...)                            -> CastResult
    sell_inventory(db, gid, uid, fish_key=None)  -> tuple[int, float]
    buy_rod(db, gid, uid, target_tier)           -> (dict, GearSpendImpact|None)
    buy_bait(db, gid, uid, bait_key, qty)        -> (dict, GearSpendImpact|None, int)
    set_zone(db, gid, uid, zone)                 -> dict
    set_bait(db, gid, uid, bait_key)             -> dict
    apply_combo_decay(db, gid, uid)              -> int
    hatch_fishing_buddy(db, gid, uid)            -> dict | None
    record_catch(...)                            -> int
    fire_catch_events(bot, gid, uid, payload)    -> None

Conventions:
    -- Monetary deltas are passed to the DB as raw-scaled NUMERIC(36,0)
       via core.framework.scale.to_raw / to_human.
    -- Every public function returns plain dicts so the cog never
       imports asyncpg.Record.
    -- Time comparisons happen DB-side via EXTRACT(EPOCH ...).
"""
from __future__ import annotations

import datetime as _dt
import logging
import random
from dataclasses import dataclass, field
from typing import Any

from core.framework.scale import to_human, to_raw

import configs.fishing_config as fc

log = logging.getLogger(__name__)


_JSONB_COLUMNS: tuple[str, ...] = (
    "bait_inventory", "fish_inventory", "junk_inventory",
    "crab_trap_inventory",
)
# JSONB columns shaped as JSON arrays (list[dict] in Python). Decoded
# separately from _JSONB_COLUMNS because _as_dict() throws away non-dict
# payloads. Keep dict and list columns split so the normaliser doesn't
# silently coerce one to the other.
_JSONB_LIST_COLUMNS: tuple[str, ...] = ("placed_crab_traps", "held_eggs")


def _as_dict(value: Any) -> dict:
    """Normalise a JSONB column value to a Python dict.

    asyncpg returns JSONB as a raw JSON string by default in this
    project (no codec is registered), so callers that did
    ``dict(row.get("bait_inventory") or {})`` were blowing up with
    "dictionary update sequence element #0 has length 1; 2 is required"
    once the column had real data. This helper accepts the string,
    dict, list, or None and always returns a dict.
    """
    import json
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _as_list(value: Any) -> list:
    """Normalise a JSONB column value to a Python list.

    Sibling of ``_as_dict`` for columns shaped as JSON arrays (e.g.
    ``placed_crab_traps``). Accepts the string, list, dict, or None
    and always returns a list -- a stray dict gets wrapped, a missing
    value yields an empty list.
    """
    import json
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return []
        if isinstance(decoded, list):
            return decoded
        return []
    return []


def _normalize_state(row: Any) -> dict:
    """Convert an asyncpg row to a plain dict with decoded JSONB columns.

    ``dict(row)`` works for asyncpg.Record but leaves JSONB values as
    strings -- this wrapper json-decodes those columns so downstream
    code can treat them as native dicts (or lists) everywhere.
    """
    if row is None:
        return {}
    out = dict(row)
    for col in _JSONB_COLUMNS:
        if col in out:
            out[col] = _as_dict(out.get(col))
    for col in _JSONB_LIST_COLUMNS:
        if col in out:
            out[col] = _as_list(out.get(col))
    return out


@dataclass
class CastResult:
    """Outcome of a single ``cast_resolve()`` call.

    The cog turns this into the final result embed. Everything the
    embed needs is on this object so the cog never makes a follow-up
    DB call to render.
    """
    outcome:        str               # 'fish' | 'junk' | 'money_bag' | 'mystery_box' | 'buddy_egg' | 'wild_battle' | 'miss'
    fish_key:       str | None = None
    fish_meta:      dict | None = None
    junk_key:       str | None = None
    junk_meta:      dict | None = None
    weight_lbs:     float = 0.0
    payout_lure:    float = 0.0       # LURE credited to wallet_holdings right away (money_bag, mystery_box, junk salvage)
    quality_mult:   float = 1.0
    combo_mult:     float = 1.0
    new_combo:      int = 0
    xp_gained:      int = 0
    new_level:      int = 1
    leveled_up:     bool = False
    rarity:         str | None = None
    splash:         bool = False      # whether to fan out a public splash embed
    bonus_subtype:  str | None = None # money_bag / mystery_box / buddy_egg
    buddy_row:      dict | None = None
    # Set when a buddy_egg rolled into a player whose shelter was full
    # but who had room in their held-eggs inventory. The cog renders a
    # different "egg saved for later" branch in this case. Shape matches
    # ``EggHatchResult.stored_egg``.
    stored_egg:     dict | None = None
    catch_id:       int | None = None
    # Set when ``outcome == 'wild_battle'``: the synthesised aquatic
    # opponent. Cog uses this to render the Challenge prompt and feed
    # services.buddy_battle.run_battle. None on every other outcome.
    wild_buddy:     dict | None = None
    # True when the wild-battle spawn fired thanks to an active battle
    # attractor (boosted spawn chance). Cog renders a magnet badge on
    # the encounter prompt so players see their attractor working.
    attractor_pulled: bool = False

    def is_success(self) -> bool:
        return self.outcome != "miss"


# === STATE_START ===
# ==========================================================================
# State helpers
# ==========================================================================

async def ensure_state(db: Any, guild_id: int, user_id: int) -> dict:
    """Insert a default user_fishing row on first touch and return it.

    Runs the buddy-egg daily-cap reset DB-side too: if the stored
    ``last_buddy_egg_at`` is older than 24 h, the counter zeroes out so
    a player who claims an egg yesterday isn't blocked today. The
    write is a single UPSERT round-trip.
    """
    await db.execute(
        """
        INSERT INTO user_fishing (guild_id, user_id, current_zone)
        VALUES ($1, $2, $3)
        ON CONFLICT (guild_id, user_id) DO NOTHING
        """,
        guild_id, user_id, fc.DEFAULT_ZONE,
    )
    # Daily-cap reset is purely date-based on the DB clock so timezones
    # never drift. We compare the day-of-year of the last egg against
    # the current day-of-year, both in UTC.
    await db.execute(
        """
        UPDATE user_fishing SET
            buddy_eggs_today = 0
        WHERE guild_id = $1 AND user_id = $2
          AND last_buddy_egg_at IS NOT NULL
          AND DATE(last_buddy_egg_at AT TIME ZONE 'UTC')
              < DATE(NOW() AT TIME ZONE 'UTC')
        """,
        guild_id, user_id,
    )
    row = await db.fetch_one(
        "SELECT * FROM user_fishing WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id,
    )
    return _normalize_state(row)


async def list_state(db: Any, guild_id: int, user_id: int) -> dict:
    """Return the current user_fishing row (caller is responsible for
    calling ``ensure_state`` first when the row may not exist yet)."""
    row = await db.fetch_one(
        "SELECT * FROM user_fishing WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id,
    )
    return _normalize_state(row) if row else {}


async def apply_combo_decay(db: Any, guild_id: int, user_id: int) -> int:
    """Reset current_combo to 0 if last_cast_at is past the idle window.

    Returns the new ``current_combo`` value (0 if reset, unchanged if
    not). Run every cast as a cheap UPDATE; the WHERE clause skips the
    write when the combo doesn't need decaying.
    """
    row = await db.fetch_one(
        """
        UPDATE user_fishing SET current_combo = 0, updated_at = NOW()
        WHERE guild_id = $1 AND user_id = $2
          AND current_combo > 0
          AND last_cast_at IS NOT NULL
          AND EXTRACT(EPOCH FROM (NOW() - last_cast_at)) > $3
        RETURNING current_combo
        """,
        guild_id, user_id, fc.COMBO_IDLE_RESET_S,
    )
    if row is not None:
        return int(row["current_combo"])
    cur = await db.fetch_val(
        "SELECT current_combo FROM user_fishing WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id,
    )
    return int(cur or 0)


async def set_zone(db: Any, guild_id: int, user_id: int, zone: str) -> dict:
    """Switch the player's active fishing zone.

    Validates the zone exists, the player's rod meets the entry tier,
    and the rod's max_zone_tier covers the zone. Raises ValueError on
    any of those so the cog can surface a friendly message.
    """
    if zone not in fc.ZONES:
        raise ValueError(f"Unknown zone `{zone}`. Try `,fish zones`.")
    state = await ensure_state(db, guild_id, user_id)
    rod_tier = int(state.get("rod_tier") or 0)
    rod = fc.rod_meta(rod_tier)
    z = fc.zone_meta(zone)
    if rod_tier < int(z.get("min_rod_tier") or 0):
        raise ValueError(
            f"**{z['name']}** needs at least the "
            f"**{fc.rod_meta(int(z['min_rod_tier']))['name']}**."
        )
    if int(rod.get("max_zone_tier") or 0) < int(z.get("tier") or 0):
        raise ValueError(
            f"Your **{rod['name']}** can't reach the **{z['name']}** "
            f"(rod tier {rod_tier} caps at zone tier {rod['max_zone_tier']})."
        )
    row = await db.fetch_one(
        """
        UPDATE user_fishing
           SET current_zone = $3, updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        RETURNING *
        """,
        guild_id, user_id, zone,
    )
    return _normalize_state(row) if row else {}


async def set_bait(db: Any, guild_id: int, user_id: int, bait_key: str | None) -> dict:
    """Equip / unequip bait. ``bait_key='none' or None`` clears the slot."""
    state = await ensure_state(db, guild_id, user_id)
    if bait_key in (None, "", "none", "off"):
        bait_key = None
    if bait_key is not None and bait_key not in fc.BAIT:
        raise ValueError(f"Unknown bait `{bait_key}`. Try `,fish shop`.")
    if bait_key is not None:
        # Refuse to equip bait the user owns 0 of -- avoids the "cast
        # with empty stack" footgun where the cog silently swaps to a
        # no-bait roll mid-session.
        inv = _as_dict(state.get("bait_inventory"))
        if int(inv.get(bait_key, 0)) <= 0:
            raise ValueError(
                f"You have no **{fc.BAIT[bait_key]['name']}** in your tackle box."
            )
    row = await db.fetch_one(
        """
        UPDATE user_fishing
           SET equipped_bait = $3, updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        RETURNING *
        """,
        guild_id, user_id, bait_key,
    )
    return _normalize_state(row) if row else {}


async def _set_casting(db: Any, guild_id: int, user_id: int, value: bool) -> bool:
    """Flip the soft is_casting flag. Returns True when the flip won
    the race so the cog can refuse a second concurrent cast cleanly.

    The acquire path also reclaims a STALE lock: if a previous cast
    crashed mid-flow (e.g. the JSONB-decode bug we just fixed) and
    left ``is_casting = TRUE`` with a ``last_cast_at`` older than
    twice the session timeout, the new cast is allowed to take it
    over. Without the reclaim, a single failed cast pinned the user
    forever.
    """
    if value:
        # Stale-aware UPSERT-ish: take the lock when it's free OR when
        # the previous holder has been idle for > 2 * SESSION_TIMEOUT_S.
        # NULL last_cast_at + is_casting=TRUE is also stale (never had
        # a cast complete). Both branches are expressed in one WHERE.
        stale_after = fc.SESSION_TIMEOUT_S * 2
        row = await db.fetch_one(
            """
            UPDATE user_fishing SET is_casting = TRUE, updated_at = NOW()
             WHERE guild_id = $1 AND user_id = $2
               AND (
                    is_casting = FALSE
                 OR last_cast_at IS NULL
                 OR EXTRACT(EPOCH FROM (NOW() - last_cast_at)) > $3
               )
            RETURNING user_id
            """,
            guild_id, user_id, stale_after,
        )
        return row is not None
    await db.execute(
        """
        UPDATE user_fishing SET is_casting = FALSE, updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id,
    )
    return True


async def force_unstuck(db: Any, guild_id: int, user_id: int) -> bool:
    """Hard-clear a user's casting lock and return True if anything was
    actually unstuck.

    Used by ``,fish unstuck`` (self-service) and ``,admin fishing reset``
    (admin override) when a player's row is wedged. Safe to call when
    the row doesn't exist yet -- the WHERE clause just no-ops.
    """
    row = await db.fetch_one(
        """
        UPDATE user_fishing SET is_casting = FALSE, updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2 AND is_casting = TRUE
        RETURNING user_id
        """,
        guild_id, user_id,
    )
    return row is not None


# === STATE_END ===

# === ROLL_START ===
# ==========================================================================
# Roll helpers (pure -- no DB)
# ==========================================================================

def _combo_multiplier(combo: int) -> float:
    """Convert a streak counter to a payout multiplier."""
    return min(fc.COMBO_MAX, 1.0 + max(0, combo) * fc.COMBO_STEP)


def _roll_money_bag() -> float:
    """LURE rolled per money-bag pull. Returns float; caller converts to raw."""
    return round(random.uniform(fc.MONEY_BAG_MIN_LURE, fc.MONEY_BAG_MAX_LURE), 2)


def _roll_mystery_box() -> float:
    """LURE rolled per mystery-box pull. Returns float; caller converts to raw."""
    return round(random.uniform(fc.MYSTERY_BOX_MIN_LURE, fc.MYSTERY_BOX_MAX_LURE), 2)


def _consume_bait(inv: dict, equipped: str | None) -> tuple[dict, str | None]:
    """Decrement the equipped bait stack by one and clear the slot when
    the stack hits zero. Returns the updated inventory dict and the
    bait_key that was actually consumed (None when the user had nothing
    equipped or their stack was empty).
    """
    if not equipped:
        return inv, None
    cur = int(inv.get(equipped, 0))
    if cur <= 0:
        return inv, None
    new = dict(inv)
    new[equipped] = cur - 1
    if new[equipped] <= 0:
        new.pop(equipped, None)
    return new, equipped


# === ROLL_END ===

# === RESOLVE_START ===
# ==========================================================================
# Cast resolution
# ==========================================================================

async def begin_cast(db: Any, guild_id: int, user_id: int) -> dict | None:
    """Reserve the player's casting slot and consume one unit of bait.

    Returns the post-write user_fishing row, or ``None`` if the soft
    lock was already taken AND fresh (the stale-lock branch in
    ``_set_casting`` already covers crashed-mid-cast cases). Any
    exception raised after the lock is acquired is rolled back so
    a partial failure can never wedge the row.
    """
    state = await ensure_state(db, guild_id, user_id)
    won = await _set_casting(db, guild_id, user_id, True)
    if not won:
        return None

    try:
        # Bait consumption is a separate UPDATE so we can cleanly skip it
        # when the user has nothing equipped or has run out.
        bait_inv = _as_dict(state.get("bait_inventory"))
        new_inv, _consumed = _consume_bait(bait_inv, state.get("equipped_bait"))
        if new_inv != bait_inv:
            await db.execute(
                """
                UPDATE user_fishing
                   SET bait_inventory = $3::jsonb, updated_at = NOW()
                 WHERE guild_id = $1 AND user_id = $2
                """,
                guild_id, user_id, _json(new_inv),
            )
            # NFT layer: burn one bait token from the user's holdings so
            # the on-chain count tracks the JSONB counter. Best-effort --
            # the JSONB counter is still source of truth this phase.
            if _consumed:
                try:
                    from services import items as _items
                    await _items.consume_one(
                        db,
                        guild_id=guild_id, user_id=user_id,
                        contract_address=_items.contract_address("bait", _consumed),
                        reason="fishing.cast",
                    )
                except Exception:
                    log.debug(
                        "nft bait consume sync failed gid=%s uid=%s key=%s",
                        guild_id, user_id, _consumed, exc_info=True,
                    )
            # If we just emptied the equipped stack, clear the slot too so
            # the next cast doesn't show a phantom equipped item.
            if _consumed and new_inv.get(_consumed, 0) <= 0:
                await db.execute(
                    """
                    UPDATE user_fishing
                       SET equipped_bait = NULL, updated_at = NOW()
                     WHERE guild_id = $1 AND user_id = $2 AND equipped_bait = $3
                    """,
                    guild_id, user_id, _consumed,
                )
        # Re-fetch so the caller has the post-deduction row.
        row = await db.fetch_one(
            "SELECT * FROM user_fishing WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )
        return _normalize_state(row) if row else None
    except Exception:
        # Anything blew up between lock-acquire and return -- release
        # the soft lock so the next cast attempt isn't permanently
        # blocked. Re-raise so the cog still surfaces the error.
        try:
            await _set_casting(db, guild_id, user_id, False)
        except Exception:
            log.exception("begin_cast: rollback unlock also failed gid=%s uid=%s",
                          guild_id, user_id)
        raise


async def end_cast(db: Any, guild_id: int, user_id: int) -> None:
    """Release the soft casting lock. Always safe to call (idempotent)."""
    await _set_casting(db, guild_id, user_id, False)


async def cast_resolve(
    db: Any,
    guild_id: int,
    user_id: int,
    *,
    reaction_seconds: float | None,
    hook_window_s: float | None = None,
    secondary_required: bool = False,
    secondary_hit: bool = False,
) -> CastResult:
    """Roll the outcome and persist all side effects atomically.

    ``reaction_seconds=None`` represents a missed bite (the user did
    not click HOOK in time). The roll still runs so the player gets
    consistent feedback, but quality is set to the late penalty and
    only the salvage / bonus paths can pay anything.

    ``hook_window_s`` is the per-cast window the cog showed the
    player (computed via :func:`fishing_config.compute_hook_window`).
    Falls back to the legacy flat ``fc.HOOK_WINDOW_S`` when None so
    older callers still work. Used to gate sweet vs late vs miss
    against the actual presented window rather than the constant.

    ``secondary_required`` / ``secondary_hit`` carry the optional
    mid-cast action result. When the prompt fires (cog-side roll on
    ``fc.SECONDARY_TRIGGER_CHANCE``) and the player MISSES it, the
    catch is forced to a miss outcome regardless of hook timing.
    When they HIT it, quality gets boosted -- 2.0x with sweet hook,
    1.5x without (mirrors the legacy sweet bonus when the secondary
    alone fires).
    """
    state = await ensure_state(db, guild_id, user_id)
    rod_tier = int(state.get("rod_tier") or 0)
    bait_key = state.get("equipped_bait")
    zone = str(state.get("current_zone") or fc.DEFAULT_ZONE)

    # Apply combo decay first so a stale streak doesn't inflate this
    # roll. The DB-side check is what the trade-off is built around.
    cur_combo = await apply_combo_decay(db, guild_id, user_id)

    # Effective hook window: per-cast scaling overrides the constant
    # when supplied so the quality gating matches the deadline the
    # player actually saw.
    eff_window = float(
        hook_window_s if hook_window_s is not None else fc.HOOK_WINDOW_S
    )

    # Secondary action override: a triggered-but-missed secondary
    # forces a miss outcome (the fish slipped because the player
    # didn't reel / pull when prompted). This branch runs BEFORE the
    # quality calc so the late-penalty path takes over correctly.
    if secondary_required and not secondary_hit:
        reaction_seconds = None

    # Quality multiplier from reaction time. Missed bites still resolve
    # but at the late penalty so even a miss can salvage trash.
    if reaction_seconds is None:
        quality = fc.HOOK_LATE_PENALTY * 0.6   # extra penalty on misses
        missed = True
    else:
        # Rod's sweet_window extends the sweet zone before we evaluate.
        rod = fc.rod_meta(rod_tier)
        eff_sweet = fc.HOOK_SWEET_S + float(rod.get("sweet_window") or 0.0)
        if reaction_seconds <= eff_sweet:
            quality = fc.HOOK_SWEET_BONUS
        elif reaction_seconds <= eff_window:
            quality = 1.0
        else:
            quality = fc.HOOK_LATE_PENALTY
        # Secondary-action bonus stacking. When the prompt fired and
        # the player nailed it, bonus stacks on top of the timing
        # quality: sweet + secondary -> 2.0x (legendary catch); just
        # secondary -> 1.5x equivalent (matches the legacy sweet
        # value, so missing the sweet but nailing the secondary keeps
        # the cast meaningful).
        if secondary_required and secondary_hit:
            if quality >= fc.HOOK_SWEET_BONUS:
                quality = fc.SECONDARY_DOUBLE_BONUS
            else:
                quality = max(quality, fc.SECONDARY_SOLO_BONUS)
        missed = False

    # Wild-buddy battle pre-roll: if the player landed the hook AND a
    # wild battle rolls in this zone, short-circuit the entire normal
    # outcome path. The cog renders a Challenge prompt and runs the
    # PvE fight via services.buddy_battle.run_battle; rewards / capture
    # are credited inside the cog's resolution callback (NOT here) so
    # this function stays the cast-bucket router and the battle stays
    # the cog's responsibility, mirroring the escaped-buddy pattern.
    zone_md = fc.zone_meta(zone) or {}
    zone_tier = int(zone_md.get("tier") or 1)
    # Attractor doubles the per-cast wild-battle chance. Active flag is
    # also returned in the result so the cog receipt can render a "pulled
    # by attractor" badge when this branch fires off the boost.
    base_chance = fc.wild_battle_chance(zone_tier)
    spawn_chance = base_chance
    attractor_on = False
    try:
        from services.buddy_economy import attractor_active as _att
        if await _att(db, guild_id, user_id):
            attractor_on = True
            spawn_chance = min(1.0, base_chance * 2.0)
    except Exception:
        log.debug("fishing attractor probe failed", exc_info=True)
    if not missed and random.random() < spawn_chance:
        wild_buddy = fc.roll_wild_battle(
            zone_tier=zone_tier, rod_tier=rod_tier, zone=zone,
        )
        # Reset combo: a hooked fight breaks any catching streak.
        await db.execute(
            """
            UPDATE user_fishing SET
                current_combo    = 0,
                last_cast_at     = NOW(),
                is_casting       = FALSE,
                updated_at       = NOW()
             WHERE guild_id = $1 AND user_id = $2
            """,
            guild_id, user_id,
        )
        # Append a row to fishing_catches so the history command shows
        # the wild encounter even before the cog resolves the fight.
        # Reward is zero at this stage; the cog persists the win/loss
        # delta separately when the battle ends.
        catch_id = await record_catch(
            db,
            guild_id=guild_id, user_id=user_id,
            outcome="wild_battle",
            fish_key=None, junk_key=None,
            rarity=None,
            weight_lbs=None, payout_raw=0,
            quality_mult=quality, combo_mult=1.0,
            zone=zone, rod_tier=rod_tier, bait_key=bait_key,
        )
        return CastResult(
            outcome="wild_battle",
            quality_mult=float(quality),
            combo_mult=1.0,
            new_combo=0,
            xp_gained=0,
            new_level=int(state.get("fish_level") or 1),
            leveled_up=False,
            rarity=None,
            splash=False,
            bonus_subtype=None,
            buddy_row=None,
            catch_id=catch_id,
            wild_buddy=wild_buddy,
            attractor_pulled=bool(attractor_on),
        )

    # Pick the top-level bucket. On a miss we force the bucket to
    # 'junk' with ~50% probability so the player still walks away with
    # something half the time.
    if missed and random.random() < 0.5:
        bucket = "junk"
    else:
        bucket = fc.roll_outcome(rod_tier, bait_key, zone)

    # Immediate-LURE payout for this cast: junk salvage, money bag, or
    # mystery box. Caught fish are persisted in inventory and converted
    # to LURE only when the player runs ,fish sell.
    fish_payout_lure = 0.0
    fish_key = junk_key = bonus_subtype = rarity = None
    fish_meta = junk_meta = None
    weight_lbs = 0.0
    splash = False

    if bucket == "fish":
        fish_key = fc.roll_fish(zone, rod_tier)
        if fish_key is None:
            # Ultra-defensive fallback: the catalog should always have
            # a common fish in every zone, but if a future config
            # change ever leaves a zone empty we want salvage, not
            # crash.
            bucket = "junk"
        else:
            fish_meta = fc.fish_meta(fish_key)
            rarity = str(fish_meta["rarity"]) if fish_meta else None
            weight_lbs = round(fc.roll_weight(fish_key, rod_tier, quality), 2)
            # Active-buddy fishing-lane bonus inflates the catch weight.
            # Same multiplier shape as the chat / work / trade buffs --
            # signature-lane buddies grow this into a real edge by
            # mid-level. Failure falls through to baseline weight.
            try:
                from services.buddy_bonus import buddy_bonus as _bb
                weight_lbs = round(
                    weight_lbs * await _bb(db, guild_id, user_id, lane="fishing"),
                    2,
                )
            except Exception:
                log.debug("fishing buddy_bonus failed", exc_info=True)
            splash = bool(fc.rarity_meta(rarity or "common").get("splash"))

    if bucket == "junk":
        junk_key = fc.roll_junk()
        junk_meta = fc.junk_meta(junk_key)
        # Junk gets paid out immediately as LURE salvage; fish need to be sold.
        fish_payout_lure = float((junk_meta or {}).get("salvage_lure") or 0.0)

    if bucket == "bonus":
        bonus_subtype = fc.roll_bonus_subtype()
        if bonus_subtype == "money_bag":
            fish_payout_lure = _roll_money_bag()
        elif bonus_subtype == "mystery_box":
            fish_payout_lure = _roll_mystery_box()
        elif bonus_subtype == "buddy_egg":
            # Egg payout is intentionally zero -- the value comes from
            # the buddy spawn handled below. If the daily cap is hit
            # we fall through to a generous mystery-box payout so the
            # roll never feels like a sucker punch.
            cap_hit = int(state.get("buddy_eggs_today") or 0) >= fc.BUDDY_EGG_DAILY_CAP
            if cap_hit:
                bonus_subtype = "mystery_box"
                fish_payout_lure = _roll_mystery_box()

    # Combo update: success bumps, miss / junk reset.
    if bucket == "fish":
        new_combo = cur_combo + 1
    elif bucket == "bonus":
        new_combo = cur_combo + 1
    else:
        new_combo = 0

    combo_mult = _combo_multiplier(new_combo)
    fish_level = int(state.get("fish_level") or 1)
    level_mult = fc.level_payout_mult(fish_level)

    # Apply combo + level boost to direct LURE payouts (junk salvage,
    # money bag, mystery box). Caught fish are persisted in inventory
    # at base value; the sell command applies its own combo at sell
    # time so spamming sell after a streak doesn't double-stack.
    if fish_payout_lure > 0:
        fish_payout_lure = round(fish_payout_lure * combo_mult * level_mult, 2)

    # XP gain: only fish reward XP. Bonuses get a small flat XP grant
    # so the bonus path still feels like progress.
    if bucket == "fish" and fish_key:
        xp_gained = fc.fish_xp(fish_key)
    elif bucket == "bonus":
        xp_gained = 5
    else:
        xp_gained = 0

    # Persist everything in a single round-trip.
    new_xp = int(state.get("fish_xp") or 0) + xp_gained
    new_level = fc.level_from_xp(new_xp)
    leveled_up = new_level > fish_level

    fish_inv = _as_dict(state.get("fish_inventory"))
    if bucket == "fish" and fish_key:
        entry = fish_inv.get(fish_key) or []
        entry.append({"lbs": float(weight_lbs), "ts": int(_now_ts())})
        fish_inv[fish_key] = entry

    junk_inv = _as_dict(state.get("junk_inventory"))
    if bucket == "junk" and junk_key:
        junk_inv[junk_key] = int(junk_inv.get(junk_key, 0)) + 1

    biggest = state.get("biggest_lbs") or 0.0
    biggest_fish = state.get("biggest_fish")
    if bucket == "fish" and weight_lbs > float(biggest):
        biggest_fish = fish_key
        biggest_at = "NOW()"  # interpolated via SQL, see below
    else:
        biggest_at = None

    longest_combo = max(int(state.get("longest_combo") or 0), new_combo)

    egg_today = int(state.get("buddy_eggs_today") or 0)
    if bonus_subtype == "buddy_egg":
        egg_today += 1

    # Single UPDATE keeps the row consistent. We use COALESCE on
    # biggest_fish / biggest_lbs so we don't accidentally overwrite a
    # bigger record with a smaller one when biggest_fish is None this
    # cast.
    new_biggest_lbs = max(float(biggest), float(weight_lbs))
    payout_raw_delta = to_raw(fish_payout_lure) if fish_payout_lure > 0 else 0
    await db.execute(
        """
        UPDATE user_fishing SET
            fish_inventory   = $3::jsonb,
            junk_inventory   = $4::jsonb,
            current_combo    = $5,
            longest_combo    = $6,
            total_caught     = total_caught + $7,
            total_junk       = total_junk + $8,
            total_weight_lbs = total_weight_lbs + $9,
            total_lure_earned_raw = total_lure_earned_raw + $10::numeric,
            biggest_fish     = COALESCE($11, biggest_fish),
            biggest_lbs      = GREATEST(biggest_lbs, $12),
            biggest_caught_at = CASE WHEN $11 IS NOT NULL THEN NOW() ELSE biggest_caught_at END,
            fish_xp          = fish_xp + $13,
            fish_level       = GREATEST(fish_level, $14),
            last_cast_at     = NOW(),
            last_buddy_egg_at = CASE WHEN $15 THEN NOW() ELSE last_buddy_egg_at END,
            buddy_eggs_today = $16,
            is_casting       = FALSE,
            updated_at       = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id,
        _json(fish_inv), _json(junk_inv),
        new_combo, longest_combo,
        1 if bucket == "fish" else 0,
        1 if bucket == "junk" else 0,
        float(weight_lbs),
        payout_raw_delta,
        biggest_fish if (bucket == "fish" and weight_lbs > float(state.get("biggest_lbs") or 0.0)) else None,
        new_biggest_lbs,
        xp_gained, new_level,
        bonus_subtype == "buddy_egg",
        egg_today,
    )

    # Append to the catch log (append-only history; powers leaderboards).
    catch_id = await record_catch(
        db,
        guild_id=guild_id, user_id=user_id,
        outcome=("fish" if bucket == "fish"
                 else "junk" if bucket == "junk"
                 else (bonus_subtype or "mystery_box")),
        fish_key=fish_key, junk_key=junk_key, rarity=rarity,
        weight_lbs=weight_lbs, payout_raw=payout_raw_delta,
        quality_mult=quality, combo_mult=combo_mult,
        zone=zone, rod_tier=rod_tier, bait_key=bait_key,
    )

    # NFT layer sync: mint a per-unit token for the fish or junk caught.
    # Best-effort so a registry hiccup never costs a player their catch.
    if bucket == "fish" and fish_key:
        try:
            from services import items as _items
            await _items.mint_unit(
                db,
                guild_id=guild_id,
                contract_address=_items.contract_address("fish", str(fish_key)),
                owner_user_id=user_id,
                metadata={
                    "fish_key": str(fish_key),
                    "lbs":      float(weight_lbs),
                    "ts":       int(_now_ts()),
                    "rarity":   str(rarity or ""),
                    "zone":     str(zone or ""),
                },
                mint_source="fishing.catch",
                source_table="user_fishing.fish_inventory",
                source_id=f"{user_id}:{fish_key}:{int(_now_ts())}:{int(catch_id or 0)}",
            )
        except Exception:
            log.debug(
                "nft fish mint sync failed gid=%s uid=%s key=%s",
                guild_id, user_id, fish_key, exc_info=True,
            )
    elif bucket == "junk" and junk_key:
        try:
            from services import items as _items
            await _items.mint_unit(
                db,
                guild_id=guild_id,
                contract_address=_items.contract_address("junk", str(junk_key)),
                owner_user_id=user_id,
                metadata={"junk_key": str(junk_key), "ts": int(_now_ts())},
                mint_source="fishing.junk",
                source_table="user_fishing.junk_inventory",
                source_id=f"{user_id}:{junk_key}:{int(_now_ts())}:{int(catch_id or 0)}",
            )
        except Exception:
            log.debug(
                "nft junk mint sync failed gid=%s uid=%s key=%s",
                guild_id, user_id, junk_key, exc_info=True,
            )

    # Hatch a buddy if the bonus rolled a buddy_egg AND the cap wasn't hit.
    # Three possible outcomes from the helper:
    #   * buddy_row populated -- shelter had room, hatched fresh.
    #   * stored_egg populated -- shelter was full, egg sits in held_eggs.
    #   * both None -- shelter full + held-egg cap reached; cog falls
    #                  back to a mystery-box payout (handled in caller).
    buddy_row: dict | None = None
    stored_egg: dict | None = None
    if bonus_subtype == "buddy_egg":
        try:
            outcome = await hatch_fishing_buddy(
                db, guild_id, user_id, source="fishing",
            )
            buddy_row = outcome.buddy_row
            stored_egg = outcome.stored_egg
        except Exception:
            log.exception("fishing: hatch_fishing_buddy failed gid=%s uid=%s",
                          guild_id, user_id)

    # Credit the player's LURE balance for the immediate-LURE outcomes
    # (money_bag / mystery_box / junk salvage). LURE lives on the Lure
    # Network in wallet_holdings; never on users.wallet (which is USD).
    # Errors are logged and swallowed so a holdings-write hiccup never
    # rolls back the catch row -- the player keeps the catch in their
    # inventory and the cog message still reflects what they pulled.
    if payout_raw_delta > 0:
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                fc.LURE_NETWORK_SHORT, fc.LURE_SYMBOL,
                payout_raw_delta,
            )
        except Exception:
            log.exception("fishing: LURE credit failed uid=%s gid=%s amt=%s",
                          user_id, guild_id, payout_raw_delta)

    return CastResult(
        outcome=("fish" if bucket == "fish"
                 else "junk" if bucket == "junk"
                 else (bonus_subtype or "mystery_box")),
        fish_key=fish_key, fish_meta=dict(fish_meta) if fish_meta else None,
        junk_key=junk_key, junk_meta=dict(junk_meta) if junk_meta else None,
        weight_lbs=float(weight_lbs),
        payout_lure=float(fish_payout_lure),
        quality_mult=float(quality),
        combo_mult=float(combo_mult),
        new_combo=int(new_combo),
        xp_gained=int(xp_gained),
        new_level=int(new_level),
        leveled_up=bool(leveled_up),
        rarity=rarity,
        splash=bool(splash),
        bonus_subtype=bonus_subtype,
        buddy_row=buddy_row,
        stored_egg=stored_egg,
        catch_id=catch_id,
    )


async def record_catch(
    db: Any,
    *,
    guild_id: int, user_id: int,
    outcome: str,
    fish_key: str | None = None,
    junk_key: str | None = None,
    rarity: str | None = None,
    weight_lbs: float | None = None,
    payout_raw: int = 0,
    quality_mult: float = 1.0,
    combo_mult: float = 1.0,
    zone: str | None = None,
    rod_tier: int = 0,
    bait_key: str | None = None,
) -> int:
    """Append one row to fishing_catches and return its catch_id.

    ``payout_raw`` is the LURE amount credited at cast time (junk salvage,
    money_bag, mystery_box). The catch_log column is named
    ``payout_lure_raw``; the parameter keeps the legacy name to minimise
    call-site churn. New rows are tagged 'LURE' explicitly so the
    migration's pre-cutover 'USD' tag stays meaningful.
    """
    row = await db.fetch_one(
        """
        INSERT INTO fishing_catches
            (guild_id, user_id, outcome, fish_key, junk_key, rarity,
             weight_lbs, payout_lure_raw, payout_symbol,
             quality_mult, combo_mult, zone, rod_tier, bait_key)
        VALUES
            ($1, $2, $3, $4, $5, $6, $7, $8::numeric, 'LURE',
             $9, $10, $11, $12, $13)
        RETURNING catch_id
        """,
        guild_id, user_id, outcome, fish_key, junk_key, rarity,
        float(weight_lbs) if weight_lbs is not None else None,
        int(payout_raw or 0), float(quality_mult), float(combo_mult),
        zone, int(rod_tier), bait_key,
    )
    return int(row["catch_id"]) if row else 0


@dataclass
class EggHatchResult:
    """Outcome of trying to hatch a fishing-borne buddy egg.

    Exactly one of ``buddy_row`` / ``stored_egg`` is set on success.
    Both ``None`` means the egg is being converted upstream (held-egg
    cap reached) and the cog should fall back to the mystery-box
    payout -- the player isn't punished for repeatedly adopting.

    ``buddy_row``  -- inserted into ``cc_buddies``; the player's
                      shelter had room and the buddy hatched fresh.
    ``stored_egg`` -- appended to ``user_fishing.held_eggs``; the
                      shelter was full but the held-egg cap wasn't.
                      The player can hatch later or sell / gift it
                      via ``,fish egg``. Shape:
                          {"species": str, "rarity_tier": int,
                           "rolled_at": iso8601_utc,
                           "from": "fishing"|"wild_battle"}
    """
    buddy_row: dict | None = None
    stored_egg: dict | None = None

    @property
    def hatched(self) -> bool:
        return self.buddy_row is not None

    @property
    def stored(self) -> bool:
        return self.stored_egg is not None


async def hatch_fishing_buddy(
    db: Any, guild_id: int, user_id: int,
    *, source: str = "fishing",
) -> EggHatchResult:
    """Spawn a water-themed buddy from a fishing egg, or store it as a
    held egg when the shelter is full.

    Mirrors the cogs/buddy.py hatch path so the buddy slots into the
    existing system (panel, decay, battles all work). The new buddy is
    set as INACTIVE so it never silently displaces the player's
    current active buddy -- they have to promote it from the panel.

    Returns an ``EggHatchResult``:
      * ``buddy_row``  set: hatched into cc_buddies (shelter had room).
      * ``stored_egg`` set: shelter was full, egg landed in
        ``user_fishing.held_eggs`` for later hatch / sell / gift.
      * both ``None``:  shelter full AND the player is at MAX_HELD_EGGS,
        so the caller should fall back to a mystery-box LURE payout
        (existing behaviour). Also returned on import failure so a
        broken buddies_config never wedges a cast.

    ``source`` is recorded on the stored egg so a future analytics
    query can split fishing-egg vs wild-battle-capture provenance
    without joining the catch log.
    """
    try:
        from configs.buddies_config import (
            roll_gender,
            roll_rarity,
        )
        from services.buddy_names import generate_name
    except Exception:
        log.exception("fishing: buddies_config import failed")
        return EggHatchResult()

    species = random.choice(fc.FISHING_BUDDY_SPECIES)
    tier = roll_rarity()  # Pin rarity at LAY time so a stored egg can't
                           # be re-rolled into a worse tier later, and a
                           # gifted egg keeps its quality across players.
    count = await db.fetch_val(
        """
        SELECT COUNT(*) FROM cc_buddies
         WHERE guild_id = $1 AND owner_user_id = $2 AND status = 'owned'
        """,
        guild_id, user_id,
    )
    from services.buddy_economy import user_max_battle_slots as _max_battle
    cap = await _max_battle(db, guild_id, user_id)
    if int(count or 0) >= cap:
        # Battle slots full (per the player's BASE + purchased slot
        # cap). Try held inventory first, then buddy egg storage,
        # then fall back to a mystery-box LURE payout. The cap-counter
        # rollback only fires on the final fallback so a player who
        # successfully banks an overflow egg still spends their daily
        # attempt.
        state = await ensure_state(db, guild_id, user_id)
        held = list(_as_list(state.get("held_eggs")))
        rolled_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        # Eggs are genderless until they hatch -- no gender field on
        # the JSONB row.
        egg = {
            "species":     species,
            "rarity_tier": int(tier),
            "rolled_at":   rolled_at,
            "from":        str(source),
        }
        if len(held) >= int(fc.MAX_HELD_EGGS):
            # Held cap reached. Try the buddy egg-storage container
            # before giving up; storage is upgradable in the buddy
            # shop and tends to have far more headroom than held.
            from services import buddy_storage_eggs as bse
            accepted = await bse.deposit(
                db, guild_id, user_id, [egg], from_=str(source),
            )
            if accepted == 1:
                # Egg banked. Fall through to NFT mint + result, but
                # skip the user_fishing held_eggs UPDATE since the
                # storage column already absorbed the row.
                await db.execute(
                    """
                    UPDATE user_fishing
                       SET total_eggs_laid = total_eggs_laid + 1,
                           updated_at      = NOW()
                     WHERE guild_id = $1 AND user_id = $2
                    """,
                    guild_id, user_id,
                )
                # NFT mint at the bottom of the helper still wants the
                # egg payload, so jump straight to the post-write tail.
                try:
                    from services import items as _items
                    await _items.mint_unit(
                        db,
                        guild_id=guild_id,
                        contract_address=_items.contract_address("egg", str(species)),
                        owner_user_id=user_id,
                        metadata={
                            "species":     str(species),
                            "rarity_tier": int(tier),
                            "rolled_at":   rolled_at,
                            "from":        str(source),
                        },
                        mint_source=f"fishing.egg.{source}",
                        source_table="user_buddy_economy.egg_storage",
                        source_id=f"{user_id}:{species}:{rolled_at}",
                    )
                except Exception:
                    log.debug(
                        "nft egg mint (banked) sync failed gid=%s uid=%s",
                        guild_id, user_id, exc_info=True,
                    )
                return EggHatchResult(stored_egg=egg)
            # Both held and storage full -- fall back to the LURE
            # mystery-box payout (legacy behaviour).
            await db.execute(
                """
                UPDATE user_fishing
                   SET buddy_eggs_today = GREATEST(0, buddy_eggs_today - 1),
                       updated_at = NOW()
                 WHERE guild_id = $1 AND user_id = $2
                """,
                guild_id, user_id,
            )
            return EggHatchResult()
        held.append(egg)
        await db.execute(
            """
            UPDATE user_fishing
               SET held_eggs       = $3::jsonb,
                   total_eggs_laid = total_eggs_laid + 1,
                   updated_at      = NOW()
             WHERE guild_id = $1 AND user_id = $2
            """,
            guild_id, user_id, _json(held),
        )
        # NFT layer sync: mint one egg token. Best-effort.
        try:
            from services import items as _items
            await _items.mint_unit(
                db,
                guild_id=guild_id,
                contract_address=_items.contract_address("egg", str(species)),
                owner_user_id=user_id,
                metadata={
                    "species":     str(species),
                    "rarity_tier": int(tier),
                    "rolled_at":   rolled_at,
                    "from":        str(source),
                },
                mint_source=f"fishing.egg.{source}",
                source_table="user_fishing.held_eggs",
                source_id=f"{user_id}:{species}:{rolled_at}",
            )
        except Exception:
            log.debug(
                "nft egg mint sync failed gid=%s uid=%s",
                guild_id, user_id, exc_info=True,
            )
        return EggHatchResult(stored_egg=egg)

    name = await generate_name(species, db, guild_id)

    # Hatch log: idempotent ON CONFLICT DO NOTHING. Players who hatch
    # via fishing AFTER already hatching once (the normal route) keep
    # their original first_species record -- this is just bookkeeping.
    try:
        await db.execute(
            """
            INSERT INTO cc_buddy_hatches (guild_id, user_id, first_species)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, user_id) DO NOTHING
            """,
            guild_id, user_id, species,
        )
    except Exception:
        log.exception("fishing: cc_buddy_hatches insert failed")

    # Gender is rolled here, at hatch time -- the egg never carried one.
    rolled_gender = roll_gender()
    row = await db.fetch_one(
        """
        INSERT INTO cc_buddies
            (guild_id, owner_user_id, species, name, status,
             is_active, rarity_tier, gender)
        VALUES ($1, $2, $3, $4, 'owned', FALSE, $5, $6)
        RETURNING *
        """,
        guild_id, user_id, species, name, tier, rolled_gender,
    )
    # NFT layer sync: mint a buddy token for the freshly-hatched buddy.
    if row:
        try:
            from services import items as _items
            await _items.mint_unit(
                db,
                guild_id=guild_id,
                contract_address=_items.contract_address("buddy", str(species)),
                owner_user_id=user_id,
                metadata={
                    "species":     str(species),
                    "rarity_tier": int(tier),
                    "gender":      str(rolled_gender),
                    "buddy_id":    int(row["id"]),
                    "name":        str(name),
                },
                mint_source=f"fishing.hatch.{source}",
                source_table="cc_buddies",
                source_id=int(row["id"]),
            )
        except Exception:
            log.debug(
                "nft buddy mint sync failed gid=%s uid=%s buddy=%s",
                guild_id, user_id, row.get("id"), exc_info=True,
            )
    return EggHatchResult(buddy_row=_normalize_state(row) if row else None)


# --------------------------------------------------------------------------
# Held eggs: list / sell / gift / hatch
# --------------------------------------------------------------------------
# Held eggs are JSONB-list entries on user_fishing.held_eggs. They land
# there when a fishing or wild-battle buddy egg rolls but the player's
# shelter is already at MAX_OWNED_BUDDIES. Eggs carry their species +
# rarity_tier from the original roll so transferring or hatching later
# never re-rolls the prize.
#
# Each operation is one round-trip. Sell credits LURE via the same
# wallet_holding mint path fish sales use (no oracle move). Gift moves
# the egg between two users in a single transaction. Hatch consults the
# shelter cap, rolls a name, and inserts into cc_buddies.


def _summarise_eggs(eggs: list) -> dict:
    """Compress a held_eggs list into render-ready buckets.

    Returns ``{"total", "by_species_tier", "rows"}`` where ``rows`` is
    the same list with stable indices. Eggs are genderless until they
    hatch, so the rendered rows never carry a gender.
    """
    rows: list[dict] = []
    by_st: dict[tuple[str, int], int] = {}
    for i, e in enumerate(eggs):
        species = str(e.get("species") or "")
        tier = int(e.get("rarity_tier") or 1)
        rows.append({
            "idx":         i,
            "species":     species,
            "rarity_tier": tier,
            "rolled_at":   e.get("rolled_at"),
            "from":        str(e.get("from") or ""),
        })
        by_st[(species, tier)] = by_st.get((species, tier), 0) + 1
    return {
        "total":           len(eggs),
        "by_species_tier": by_st,
        "rows":            rows,
    }


async def list_held_eggs(db: Any, guild_id: int, user_id: int) -> dict:
    """Return the player's held-eggs panel summary.

    Read-only -- safe to call from any command. The cog hands the result
    to ``_egg_status_embed`` for display.
    """
    state = await ensure_state(db, guild_id, user_id)
    return _summarise_eggs(_as_list(state.get("held_eggs")))


async def pop_held_eggs(
    db: Any, guild_id: int, user_id: int,
    *, n: int = 1, species: str | None = None,
) -> list[dict]:
    """Pop up to ``n`` eggs off held_eggs (FIFO) and return them.

    Used by the buddy egg-storage deposit path: the caller pops eggs
    out of held inventory, hands them to ``buddy_storage_eggs.deposit``,
    and pushes any leftovers back via ``push_held_eggs`` if the deposit
    can't accept the full batch. Optional ``species`` narrows the
    selection to a single species key.
    """
    n = int(n)
    if n <= 0:
        return []
    state = await ensure_state(db, guild_id, user_id)
    eggs = list(_as_list(state.get("held_eggs")))
    if not eggs:
        return []
    sp = (species or "").strip().lower() or None

    pulled: list[dict] = []
    keep: list[dict] = []
    for egg in eggs:
        if len(pulled) < n and (
            sp is None or str(egg.get("species") or "") == sp
        ):
            pulled.append(egg)
            continue
        keep.append(egg)
    if not pulled:
        return []
    await db.execute(
        """
        UPDATE user_fishing
           SET held_eggs  = $3::jsonb,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        int(guild_id), int(user_id), _json(keep),
    )
    return pulled


async def push_held_eggs(
    db: Any, guild_id: int, user_id: int,
    eggs: list[dict],
) -> int:
    """Append ``eggs`` to held_eggs, capped at ``fc.MAX_HELD_EGGS``.

    Returns the count actually accepted. Used by the buddy egg-storage
    withdraw path (banked -> held) and by the deposit path's rollback
    when the storage container can't accept the full pop. Held cap is
    fixed (10 by config) and not upgradable; a partial accept is the
    explicit signal that the surplus must stay banked.
    """
    if not eggs:
        return 0
    state = await ensure_state(db, guild_id, user_id)
    held = list(_as_list(state.get("held_eggs")))
    free = max(0, int(fc.MAX_HELD_EGGS) - len(held))
    if free <= 0:
        return 0
    accepted = list(eggs)[:free]
    held.extend(accepted)
    await db.execute(
        """
        UPDATE user_fishing
           SET held_eggs  = $3::jsonb,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        int(guild_id), int(user_id), _json(held),
    )
    return len(accepted)


@dataclass
class EggSellResult:
    """Receipt for a ``,buddy egg sell`` call."""
    sold_count:    int                       # eggs removed from inventory
    lure_paid:     float                     # FREN credited (legacy field name)
    leftover:      int                       # eggs still in inventory after sell
    by_tier:       dict[int, int]            # {tier: count_sold} for receipt copy
    payout_symbol: str = "FREN"              # Display symbol for the receipt


async def sell_held_eggs(
    db: Any, guild_id: int, user_id: int,
    *, species: str | None = None, count: int | None = None,
) -> EggSellResult:
    """Sell ``count`` held eggs for FREN; ``count=None`` sells all matching.

    Eggs are buddy items, so they pay out in FREN on the Buddy Network
    instead of the legacy LURE/Lure Network mint that fish sales use.
    The numeric payout (per-tier) carries over from the original LURE
    table -- both tokens float around the same starting oracle, and
    we'd rather keep the values stable for players than re-tune them.

    ``species`` filter: pass a species key to only sell that species
    (e.g. only sell the wecco eggs); pass ``None`` to sell across the
    inventory. Sold from the OLDEST eggs first so a player who held an
    epic at the top of the list doesn't accidentally sell it before
    they realised they wanted to hatch it.

    The payout is a mint -- no oracle move. Mirrors mint_bbt_reward's
    shape so the buddy-network credit stays consistent with battle
    rewards.
    """
    state = await ensure_state(db, guild_id, user_id)
    eggs = list(_as_list(state.get("held_eggs")))
    if not eggs:
        raise ValueError("You have no held eggs to sell.")

    sp = species.strip().lower() if species else None
    if sp is not None:
        # Validate against the global SPECIES catalog -- eggs land in
        # held inventory from delve, farm, fishing, and breeding now,
        # so the old fishing-only whitelist would reject perfectly
        # legal species (wolf, ember, etc).
        try:
            from configs.buddies_config import SPECIES as _ALL_SPECIES
            valid = set(_ALL_SPECIES.keys())
        except Exception:
            valid = set(fc.FISHING_BUDDY_SPECIES)
        if sp not in valid:
            held_species = sorted({
                str(e.get("species") or "")
                for e in eggs
                if str(e.get("species") or "")
            })
            hint = (
                ", ".join(held_species)
                if held_species
                else ", ".join(fc.FISHING_BUDDY_SPECIES)
            )
            raise ValueError(
                f"Unknown species `{species}`. You hold: {hint}."
            )

    # Walk from the FRONT (oldest first). Build a kept list and a
    # consumed list so the JSONB write is a single replacement.
    kept: list = []
    consumed: list = []
    cap = int(count) if count is not None else None
    for e in eggs:
        if cap is not None and len(consumed) >= cap:
            kept.append(e); continue
        if sp is not None and str(e.get("species") or "") != sp:
            kept.append(e); continue
        consumed.append(e)

    if not consumed:
        if sp is not None:
            raise ValueError(f"You hold no **{sp}** eggs.")
        raise ValueError("Nothing to sell.")

    by_tier: dict[int, int] = {}
    total_fren = 0.0
    for e in consumed:
        t = int(e.get("rarity_tier") or 1)
        total_fren += fc.egg_sell_lure(t)
        by_tier[t] = by_tier.get(t, 0) + 1
    total_fren = round(total_fren, 2)
    raw = to_raw(total_fren) if total_fren > 0 else 0

    await db.execute(
        """
        UPDATE user_fishing SET
            held_eggs             = $3::jsonb,
            total_eggs_sold       = total_eggs_sold + $4,
            total_lure_earned_raw = total_lure_earned_raw + $5::numeric,
            updated_at            = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, _json(kept), int(len(consumed)), int(raw),
    )
    if raw > 0:
        # FREN lives on the Buddy Network ('bud' short). Mirrors
        # mint_bbt_reward's wallet_holding write so the credit shows
        # up wherever BBT does.
        await db.update_wallet_holding(
            user_id, guild_id, "bud", "FREN", int(raw),
        )
    # NFT layer: burn one egg token per consumed egg. Best-effort.
    try:
        from services import items as _items
        for e in consumed:
            sp_consumed = str(e.get("species") or "").lower()
            if not sp_consumed:
                continue
            await _items.consume_one(
                db,
                guild_id=guild_id, user_id=user_id,
                contract_address=_items.contract_address("egg", sp_consumed),
                reason="fishing.egg.sell",
            )
    except Exception:
        log.debug(
            "nft egg burn sync failed gid=%s uid=%s",
            guild_id, user_id, exc_info=True,
        )
    return EggSellResult(
        sold_count=int(len(consumed)),
        lure_paid=float(total_fren),
        leftover=int(len(kept)),
        by_tier=by_tier,
        payout_symbol="FREN",
    )


@dataclass
class EggGiftResult:
    """Receipt for ``,fish egg gift``."""
    gifted_count:    int                # eggs moved from sender to recipient
    sender_leftover: int                # eggs still on sender after gift
    recipient_total: int                # eggs on recipient after gift
    by_tier:         dict[int, int]     # {tier: count_gifted} for receipt copy


async def gift_held_eggs(
    db: Any, guild_id: int, sender_id: int, recipient_id: int,
    *, species: str | None = None, count: int = 1,
) -> EggGiftResult:
    """Move ``count`` eggs from sender to recipient.

    Same selection rules as ``sell_held_eggs``: oldest-first, optional
    species filter. Recipient's held-eggs list is capped at
    ``fc.MAX_HELD_EGGS`` -- if the gift would overflow, fewer eggs move
    and the receipt reports the actual count. Two UPDATEs are issued in
    sequence; the sender side rolls back automatically if the recipient
    side fails (the sender hadn't been touched yet).
    """
    if sender_id == recipient_id:
        raise ValueError("Can't gift eggs to yourself.")
    if count <= 0:
        raise ValueError("Count must be positive.")

    sender_state = await ensure_state(db, guild_id, sender_id)
    sender_eggs = list(_as_list(sender_state.get("held_eggs")))
    if not sender_eggs:
        raise ValueError("You have no held eggs to gift.")

    sp = species.strip().lower() if species else None
    if sp is not None:
        try:
            from configs.buddies_config import SPECIES as _ALL_SPECIES
            valid = set(_ALL_SPECIES.keys())
        except Exception:
            valid = set(fc.FISHING_BUDDY_SPECIES)
        if sp not in valid:
            held_species = sorted({
                str(e.get("species") or "")
                for e in sender_eggs
                if str(e.get("species") or "")
            })
            hint = (
                ", ".join(held_species)
                if held_species
                else ", ".join(fc.FISHING_BUDDY_SPECIES)
            )
            raise ValueError(
                f"Unknown species `{species}`. You hold: {hint}."
            )

    recipient_state = await ensure_state(db, guild_id, recipient_id)
    recipient_eggs = list(_as_list(recipient_state.get("held_eggs")))
    room = max(0, int(fc.MAX_HELD_EGGS) - len(recipient_eggs))
    if room <= 0:
        raise ValueError(
            "Recipient is at the held-egg cap. They need to hatch or "
            "sell some before you can gift them more."
        )

    kept: list = []
    consumed: list = []
    cap = min(int(count), room)
    for e in sender_eggs:
        if len(consumed) >= cap:
            kept.append(e); continue
        if sp is not None and str(e.get("species") or "") != sp:
            kept.append(e); continue
        consumed.append(e)

    if not consumed:
        if sp is not None:
            raise ValueError(f"You hold no **{sp}** eggs to gift.")
        raise ValueError("Nothing to gift.")

    new_recipient_eggs = recipient_eggs + consumed
    by_tier: dict[int, int] = {}
    for e in consumed:
        t = int(e.get("rarity_tier") or 1)
        by_tier[t] = by_tier.get(t, 0) + 1

    # Sender side first.  If the recipient UPDATE fails, the sender's
    # held_eggs and counter never get touched, so retrying is safe and
    # nobody loses an egg to a dropped write.
    await db.execute(
        """
        UPDATE user_fishing SET
            held_eggs         = $3::jsonb,
            total_eggs_gifted = total_eggs_gifted + $4,
            updated_at        = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, sender_id, _json(kept), int(len(consumed)),
    )
    try:
        await db.execute(
            """
            UPDATE user_fishing SET
                held_eggs       = $3::jsonb,
                total_eggs_laid = total_eggs_laid + $4,
                updated_at      = NOW()
             WHERE guild_id = $1 AND user_id = $2
            """,
            guild_id, recipient_id, _json(new_recipient_eggs),
            int(len(consumed)),
        )
    except Exception:
        # Roll back the sender side so the eggs never disappear into a
        # void if the recipient write fails. This is best-effort -- the
        # user is told to retry.
        await db.execute(
            """
            UPDATE user_fishing SET
                held_eggs         = $3::jsonb,
                total_eggs_gifted = GREATEST(0, total_eggs_gifted - $4),
                updated_at        = NOW()
             WHERE guild_id = $1 AND user_id = $2
            """,
            guild_id, sender_id, _json(sender_eggs), int(len(consumed)),
        )
        raise ValueError(
            "Couldn't deliver the gift -- recipient row write failed. "
            "Your eggs are safe; try again."
        )

    # NFT layer: transfer one egg token per gifted egg from sender to
    # recipient. We don't have a stable token id per held-egg row in the
    # JSONB so we walk the sender's owned egg tokens (oldest first) per
    # contract and reassign them. Best-effort.
    try:
        from services import items as _items
        per_species_count: dict[str, int] = {}
        for e in consumed:
            sp_g = str(e.get("species") or "").lower()
            if not sp_g:
                continue
            per_species_count[sp_g] = per_species_count.get(sp_g, 0) + 1
        for sp_g, n in per_species_count.items():
            owned = await _items.list_owned(
                db,
                guild_id=guild_id, user_id=sender_id,
                contract_address=_items.contract_address("egg", sp_g),
                limit=n,
            )
            for tok in owned[:n]:
                await _items.transfer(
                    db, str(tok["token_id"]), int(recipient_id),
                )
    except Exception:
        log.debug(
            "nft egg gift transfer sync failed gid=%s sender=%s recip=%s",
            guild_id, sender_id, recipient_id, exc_info=True,
        )

    return EggGiftResult(
        gifted_count=int(len(consumed)),
        sender_leftover=int(len(kept)),
        recipient_total=int(len(new_recipient_eggs)),
        by_tier=by_tier,
    )


async def hatch_held_egg(
    db: Any, guild_id: int, user_id: int,
    *, species: str | None = None,
) -> dict:
    """Hatch ONE held egg into a buddy in cc_buddies.

    Pops the OLDEST matching egg (or ``species``-filtered) from
    held_eggs and hatches it through the same ``cc_buddies`` insert
    path ``hatch_fishing_buddy`` uses for fresh hatches. Refuses when
    the shelter is already at ``MAX_OWNED_BUDDIES`` -- the player must
    surrender a buddy first.

    Returns the new buddy row dict.
    """
    try:
        from services.buddy_names import generate_name
    except Exception:
        log.exception("hatch_held_egg: buddies_config import failed")
        raise ValueError("Buddy system unavailable -- try again shortly.")

    state = await ensure_state(db, guild_id, user_id)
    eggs = list(_as_list(state.get("held_eggs")))
    if not eggs:
        raise ValueError("You have no held eggs.")

    sp = species.strip().lower() if species else None
    if sp is not None:
        # Validate against the global SPECIES catalog -- eggs land in
        # held inventory from delve, farm, fishing, and breeding now,
        # so the old fishing-only whitelist would reject perfectly
        # legal species (wolf, ember, etc).
        try:
            from configs.buddies_config import SPECIES as _ALL_SPECIES
            valid = set(_ALL_SPECIES.keys())
        except Exception:
            valid = set(fc.FISHING_BUDDY_SPECIES)
        if sp not in valid:
            held_species = sorted({
                str(e.get("species") or "")
                for e in eggs
                if str(e.get("species") or "")
            })
            hint = (
                ", ".join(held_species)
                if held_species
                else ", ".join(fc.FISHING_BUDDY_SPECIES)
            )
            raise ValueError(
                f"Unknown species `{species}`. You hold: {hint}."
            )

    pick_idx: int | None = None
    for i, e in enumerate(eggs):
        if sp is None or str(e.get("species") or "") == sp:
            pick_idx = i
            break
    if pick_idx is None:
        raise ValueError(f"You hold no **{sp}** eggs.")

    from services.buddy_economy import (
        capture_destination as _capture_destination,
    )
    hatch_dest = await _capture_destination(db, guild_id, user_id)
    if hatch_dest is None:
        raise ValueError(
            "Battle and storage are both full. Free a slot via "
            "`,buddy store`, `,buddy surrender`, or buy more from "
            "`,buddy shop` before hatching."
        )
    hatch_status = "owned" if hatch_dest == "battle" else "stored"

    egg = eggs.pop(pick_idx)
    species_key = str(egg.get("species") or "")
    tier = int(egg.get("rarity_tier") or 1)
    # Eggs are genderless until they hatch -- gender is rolled fresh
    # here, never carried from the held egg.
    from configs.buddies_config import roll_gender as _roll_gender
    egg_gender = _roll_gender()
    name = await generate_name(species_key, db, guild_id)

    # First-hatch log: ON CONFLICT DO NOTHING so a held-egg hatch never
    # overwrites the player's original first_species record.
    try:
        await db.execute(
            """
            INSERT INTO cc_buddy_hatches (guild_id, user_id, first_species)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, user_id) DO NOTHING
            """,
            guild_id, user_id, species_key,
        )
    except Exception:
        log.exception("hatch_held_egg: cc_buddy_hatches insert failed")

    row = await db.fetch_one(
        """
        INSERT INTO cc_buddies
            (guild_id, owner_user_id, species, name, status,
             is_active, rarity_tier, gender)
        VALUES ($1, $2, $3, $4, $5, FALSE, $6, $7)
        RETURNING *
        """,
        guild_id, user_id, species_key, name,
        str(hatch_status), tier, egg_gender,
    )
    # Persist the popped egg list + bump the lifetime hatch counter.
    await db.execute(
        """
        UPDATE user_fishing SET
            held_eggs          = $3::jsonb,
            total_eggs_hatched = total_eggs_hatched + 1,
            updated_at         = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, _json(eggs),
    )
    # NFT layer: burn one egg token, mint one buddy token. Best-effort.
    try:
        from services import items as _items
        await _items.consume_one(
            db,
            guild_id=guild_id, user_id=user_id,
            contract_address=_items.contract_address("egg", species_key.lower()),
            reason="fishing.egg.hatch",
        )
        if row:
            await _items.mint_unit(
                db,
                guild_id=guild_id,
                contract_address=_items.contract_address("buddy", species_key.lower()),
                owner_user_id=user_id,
                metadata={
                    "species":     species_key,
                    "rarity_tier": int(tier),
                    "gender":      str(egg_gender),
                    "buddy_id":    int(row["id"]),
                    "name":        str(name),
                },
                mint_source="fishing.egg.hatch",
                source_table="cc_buddies",
                source_id=int(row["id"]),
            )
    except Exception:
        log.debug(
            "nft hatch_held_egg sync failed gid=%s uid=%s",
            guild_id, user_id, exc_info=True,
        )
    out = _normalize_state(row) if row else {}
    if out:
        out["_hatch_destination"] = hatch_status  # 'owned' | 'stored'
    return out


def _now_ts() -> int:
    return int(_dt.datetime.utcnow().timestamp())


def _json(payload: Any) -> str:
    """Serialise ``payload`` for asyncpg jsonb binds."""
    import json
    return json.dumps(payload, separators=(",", ":"))


# --------------------------------------------------------------------------
# Treasure maps -- ,fish dig
# --------------------------------------------------------------------------
# The "Soggy Treasure Map" junk item (junk key "map") is consumable via
# dig_treasure_map. Cooldown + map-decrement + loot-credit happen in a
# single round-trip. All loot is MINT (no oracle move) on the same
# wallet_holding code path stake yield uses, so the cog renders the
# standard _MINT_FOOTER. The map is consumed BEFORE the loot resolves
# so a partial failure can never duplicate maps; if the loot credit
# itself fails, the map is gone (logged) and the player gets the
# "muddy hole" outcome instead of a free re-roll.


@dataclass
class DigResult:
    """Receipt for ``,fish dig``.

    Exactly one of the payout fields is set per dig (or none at all on
    the empty / muddy-hole branch). The cog renders different copy for
    each outcome key but the dataclass keeps everything flat so a
    single render function handles them all without if-storms.

    ``lure_impact`` / ``reel_impact`` carry the inflation-style oracle
    move generated by minting tokens into the wallet. Either is
    ``None`` when no tokens of that side were credited (or the oracle
    write hit a transient failure). The cog renders both alongside the
    payout lines so users see what the chart did the moment they dug.
    """
    outcome_key:    str                       # bucket from TREASURE_LOOT_WEIGHTS
    label:          str                       # human-readable headline
    lure_credited:  float                     # LURE minted to wallet (0 if none)
    reel_credited:  float                     # REEL minted to wallet (0 if none)
    bait_added:     tuple[str, int] | None    # (bait_key, qty) if bait dropped
    trap_added:     tuple[str, int] | None    # (trap_key, qty) if trap dropped
    egg_added:      dict | None               # held_egg dict if egg dropped
    fish_added:     tuple[str, float] | None  # (fish_key, lbs) if jackpot
    leftover_maps:  int                       # maps remaining after this dig
    lure_impact:    "MintImpact | None" = None  # LURE oracle move on credit
    reel_impact:    "MintImpact | None" = None  # REEL oracle move on credit


_TREASURE_LABELS: dict[str, str] = {
    "lure_small":     "Small Cache",
    "lure_medium":    "Buried Stash",
    "lure_large":     "Pirate's Hoard",
    "reel_kicker":    "Sealed Coin Pouch",
    "rare_bait":      "Smuggler's Bait Box",
    "trap_cache":     "Forgotten Crab Pot Crate",
    "wild_egg":       "Petrified Egg",
    "ancient_relic":  "Ancient Relic",
}


async def dig_treasure_map(
    db: Any, guild_id: int, user_id: int,
) -> DigResult:
    """Consume one Soggy Treasure Map and roll a weighted loot outcome.

    Cooldown enforced via ``fc.TREASURE_DIG_COOLDOWN_S`` on the DB
    clock. Refuses to run when the player has no maps in junk inventory.
    The map is decremented BEFORE the loot resolves (single UPDATE later
    in the function) so a crash partway through never duplicates a map.
    """
    state = await ensure_state(db, guild_id, user_id)

    # DB-side cooldown clock: returns elapsed seconds since the last
    # dig (0 if never dug). Mirrors the trap-collect cooldown pattern.
    cd_row = await db.fetch_one(
        """
        SELECT
            CASE
                WHEN last_treasure_dig_at IS NULL THEN 0
                ELSE EXTRACT(EPOCH FROM (NOW() - last_treasure_dig_at))::INTEGER
            END AS elapsed_s
          FROM user_fishing
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id,
    )
    elapsed_s = int((cd_row or {}).get("elapsed_s") or 0)
    if elapsed_s > 0 and elapsed_s < int(fc.TREASURE_DIG_COOLDOWN_S):
        wait = int(fc.TREASURE_DIG_COOLDOWN_S - elapsed_s)
        raise ValueError(
            f"You're still catching your breath -- dig again in **{wait}s**."
        )

    junk_inv = _as_dict(state.get("junk_inventory"))
    have_maps = int(junk_inv.get("map", 0))
    if have_maps <= 0:
        raise ValueError(
            "You have no **Soggy Treasure Map** to dig with. "
            "Pull more by fishing -- they show up as junk catches."
        )
    junk_inv["map"] = have_maps - 1
    if junk_inv["map"] <= 0:
        junk_inv.pop("map", None)
    leftover_maps = int(junk_inv.get("map", 0))

    # Roll the loot bucket. The fan-out below populates exactly one
    # payout field on the result, except lure/reel which can both be
    # populated by the wild_egg fallback path.
    outcome = fc.roll_treasure_loot()
    label = _TREASURE_LABELS.get(outcome, outcome.replace("_", " ").title())
    lure_credited = 0.0
    reel_credited = 0.0
    bait_added: tuple[str, int] | None = None
    trap_added: tuple[str, int] | None = None
    egg_added: dict | None = None
    fish_added: tuple[str, float] | None = None

    bait_inv = _as_dict(state.get("bait_inventory"))
    trap_inv = _as_dict(state.get("crab_trap_inventory"))
    fish_inv = _as_dict(state.get("fish_inventory"))
    held_eggs = list(_as_list(state.get("held_eggs")))

    if outcome in ("lure_small", "lure_medium", "lure_large"):
        lo, hi = fc.TREASURE_PAYOUT[outcome]
        lure_credited = round(random.uniform(lo, hi), 2)

    elif outcome == "reel_kicker":
        lo, hi = fc.TREASURE_PAYOUT["reel_kicker"]
        reel_credited = round(random.uniform(lo, hi), 2)

    elif outcome == "rare_bait":
        bait_key = random.choice(fc.TREASURE_RARE_BAIT_POOL)
        lo, hi = fc.TREASURE_PAYOUT["rare_bait"]
        qty = int(round(random.uniform(lo, hi)))
        cfg = fc.BAIT.get(bait_key) or {}
        cap = int(cfg.get("max_stack") or 1_000_000)
        cur = int(bait_inv.get(bait_key, 0))
        actual = max(0, min(qty, cap - cur))
        if actual > 0:
            bait_inv[bait_key] = cur + actual
            bait_added = (bait_key, actual)
        else:
            # Bait stack capped -- convert the would-be drop into a
            # small LURE consolation so the dig still feels rewarding.
            lure_credited = float(qty) * 100.0
            label = f"{label} (stack full -- LURE consolation)"

    elif outcome == "trap_cache":
        trap_key = random.choice(fc.TREASURE_TRAP_POOL)
        lo, hi = fc.TREASURE_PAYOUT["trap_cache"]
        qty = int(round(random.uniform(lo, hi)))
        cfg = fc.CRAB_TRAPS.get(trap_key) or {}
        cap = int(cfg.get("max_stack") or 1_000_000)
        cur = int(trap_inv.get(trap_key, 0))
        actual = max(0, min(qty, cap - cur))
        if actual > 0:
            trap_inv[trap_key] = cur + actual
            trap_added = (trap_key, actual)
        else:
            lure_credited = float(qty) * 250.0
            label = f"{label} (stack full -- LURE consolation)"

    elif outcome == "wild_egg":
        # Roll species + tier the same way fishing-borne eggs do, so a
        # treasure egg looks indistinguishable from a fishing egg in the
        # held-eggs panel. Falls back to LURE when the held-egg cap is
        # already reached.
        try:
            from configs.buddies_config import roll_rarity
            species = random.choice(fc.FISHING_BUDDY_SPECIES)
            tier = int(roll_rarity())
            if len(held_eggs) >= int(fc.MAX_HELD_EGGS):
                # Egg inventory full -- drop the egg as a LURE chunk
                # equivalent to its sell value. Player isn't punished
                # for being a successful collector.
                lure_credited = float(fc.egg_sell_lure(tier))
                label = f"{label} (held-egg cap -- LURE consolation)"
            else:
                rolled_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
                egg_added = {
                    "species":     species,
                    "rarity_tier": tier,
                    "rolled_at":   rolled_at,
                    "from":        "treasure_dig",
                }
                held_eggs.append(egg_added)
        except Exception:
            log.exception("dig_treasure_map: egg roll fallback")
            # If the buddies config blew up entirely, treat as lure_medium.
            lo, hi = fc.TREASURE_PAYOUT["lure_medium"]
            lure_credited = round(random.uniform(lo, hi), 2)
            label = "Buried Stash (egg roll failed)"

    elif outcome == "ancient_relic":
        # JACKPOT: a legendary fish drops at MAX rolled weight straight
        # into fish_inventory so the player can sell it normally for a
        # huge LURE payout. The fish can also be hauled out with
        # ,fish sell <fish_key> like any other catch.
        fish_key = fc.roll_treasure_jackpot_fish()
        if fish_key:
            spec = fc.FISH.get(fish_key) or {}
            lbs = round(float(spec.get("max_lbs") or 1.0), 2)
            entries = list(fish_inv.get(fish_key) or [])
            entries.append({"lbs": lbs, "ts": int(_now_ts())})
            fish_inv[fish_key] = entries
            fish_added = (fish_key, lbs)
        else:
            # No legendary candidates -- fall back to lure_large.
            lo, hi = fc.TREASURE_PAYOUT["lure_large"]
            lure_credited = round(random.uniform(lo, hi), 2)
            label = "Pirate's Hoard (relic decayed -- LURE consolation)"

    # V3 enforced 10/90 USD split (LURE:REEL).
    try:
        from core.framework.payout_split import rebalance_to_split
        lure_credited, reel_credited = await rebalance_to_split(
            db, guild_id, fc.LURE_SYMBOL, fc.REEL_SYMBOL,
            float(lure_credited), float(reel_credited),
        )
    except Exception:
        log.debug("dig_treasure_map: 10/90 rebalance skipped", exc_info=True)
    raw_lure = to_raw(lure_credited) if lure_credited > 0 else 0
    raw_reel = to_raw(reel_credited) if reel_credited > 0 else 0

    await db.execute(
        """
        UPDATE user_fishing SET
            junk_inventory        = $3::jsonb,
            bait_inventory        = $4::jsonb,
            crab_trap_inventory   = $5::jsonb,
            fish_inventory        = $6::jsonb,
            held_eggs             = $7::jsonb,
            total_lure_earned_raw = total_lure_earned_raw + $8::numeric,
            total_reel_earned_raw = total_reel_earned_raw + $9::numeric,
            total_eggs_laid       = total_eggs_laid + $10,
            total_treasures_dug   = total_treasures_dug + 1,
            last_treasure_dig_at  = NOW(),
            updated_at            = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id,
        _json(junk_inv), _json(bait_inv), _json(trap_inv),
        _json(fish_inv), _json(held_eggs),
        int(raw_lure), int(raw_reel),
        1 if egg_added is not None else 0,
    )
    lure_impact: MintImpact | None = None
    reel_impact: MintImpact | None = None
    if raw_lure > 0:
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                fc.LURE_NETWORK_SHORT, fc.LURE_SYMBOL,
                int(raw_lure),
            )
        except Exception:
            log.exception("dig_treasure_map: LURE credit failed "
                          "uid=%s gid=%s amt=%s",
                          user_id, guild_id, raw_lure)
            lure_credited = 0.0
        else:
            # Inflation-style oracle drop: LURE supply just expanded
            # without a corresponding buy, so the chart should reflect
            # that. Best-effort -- if the oracle write fails, the
            # player still has their LURE; only the chart-impact line
            # disappears from the receipt.
            lure_impact = await _apply_mint_inflation_effect(
                db, guild_id, fc.LURE_SYMBOL, lure_credited,
            )
    if raw_reel > 0:
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                fc.LURE_NETWORK_SHORT, fc.REEL_SYMBOL,
                int(raw_reel),
            )
        except Exception:
            log.exception("dig_treasure_map: REEL credit failed "
                          "uid=%s gid=%s amt=%s",
                          user_id, guild_id, raw_reel)
            reel_credited = 0.0
        else:
            reel_impact = await _apply_mint_inflation_effect(
                db, guild_id, fc.REEL_SYMBOL, reel_credited,
            )

    return DigResult(
        outcome_key=outcome,
        label=label,
        lure_credited=float(lure_credited),
        reel_credited=float(reel_credited),
        bait_added=bait_added,
        trap_added=trap_added,
        egg_added=egg_added,
        fish_added=fish_added,
        leftover_maps=leftover_maps,
        lure_impact=lure_impact,
        reel_impact=reel_impact,
    )


# === RESOLVE_END ===

# ==========================================================================
# Beachcomb (free 10-min wander; mirrors farm forage)
# ==========================================================================
# Free roll on the shore: small LURE / REEL purses, a stash of bait, an
# occasional Soggy Treasure Map (which feeds back into ,fish dig), and on
# a rare jackpot drops a max-weight legendary fish straight to the user's
# fish_inventory. No inputs consumed, 10-minute DB-clock cooldown.

@dataclass
class BeachcombResult:
    outcome_key: str
    label: str
    lure_credited: float = 0.0
    reel_credited: float = 0.0
    baits_added:   list[tuple[str, int]] = field(default_factory=list)
    maps_added:    int = 0
    fish_added:    tuple[str, float] | None = None
    lure_impact:   "MintImpact | None" = None
    reel_impact:   "MintImpact | None" = None


_BEACHCOMB_LABELS: dict[str, str] = {
    "lure_purse_small":   "Sandy Coin Purse",
    "lure_purse_big":     "Heavy LURE Purse",
    "reel_kicker_small":  "Snagged Tackle",
    "reel_kicker_big":    "Snapped Reel Cache",
    "bait_stash":         "Tin of Bait Packets",
    "treasure_map":       "Soggy Treasure Map",
    "ancient_relic":      "ANCIENT RELIC",
    "empty":              "Just Shells",
}


async def beachcomb(
    db: Any, guild_id: int, user_id: int,
) -> BeachcombResult:
    """Wander the shore once. Free roll, 10-minute cooldown.

    Cooldown enforced via DB-side clock on user_fishing.last_beachcomb_at
    so Python now() vs Postgres TIMESTAMPTZ never drift. Stamps the
    cooldown + bumps total_beachcombs in the same UPDATE that credits
    the loot, so a transient crash never lets a player double-roll.
    """
    state = await ensure_state(db, guild_id, user_id)

    cd_row = await db.fetch_one(
        """
        SELECT
            CASE
                WHEN last_beachcomb_at IS NULL THEN 0
                ELSE EXTRACT(EPOCH FROM (NOW() - last_beachcomb_at))::INTEGER
            END AS elapsed_s
          FROM user_fishing
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id,
    )
    elapsed_s = int((cd_row or {}).get("elapsed_s") or 0)
    if elapsed_s > 0 and elapsed_s < int(fc.BEACHCOMB_COOLDOWN_S):
        wait = int(fc.BEACHCOMB_COOLDOWN_S - elapsed_s)
        raise ValueError(
            f"You're still drying off  -  beachcomb again in **{wait}s**."
        )

    rng = random.Random()
    outcome = fc.roll_beachcomb_outcome(rng)
    label = _BEACHCOMB_LABELS.get(outcome, outcome.replace("_", " ").title())

    bait_inv = _as_dict(state.get("bait_inventory"))
    junk_inv = _as_dict(state.get("junk_inventory"))
    fish_inv = _as_dict(state.get("fish_inventory"))

    lure_credited = 0.0
    reel_credited = 0.0
    baits_added:  list[tuple[str, int]] = []
    maps_added = 0
    fish_added: tuple[str, float] | None = None

    # Fish-level payout multiplier: beachcomb gains scale with the
    # player's fishing level so a Lv 40 beachcomber doesn't pull the
    # same handful of LURE as a fresh Lv 1 (mirrors fish_cast's payout).
    _fish_lvl = int(state.get("fish_level") or 1)
    _lvl_mult = fc.level_payout_mult(_fish_lvl)
    if outcome in ("lure_purse_small", "lure_purse_big"):
        lo, hi = fc.BEACHCOMB_PAYOUTS[outcome]
        lure_credited = round(rng.uniform(lo, hi) * _lvl_mult, 2)
    elif outcome in ("reel_kicker_small", "reel_kicker_big"):
        lo, hi = fc.BEACHCOMB_PAYOUTS[outcome]
        reel_credited = round(rng.uniform(lo, hi) * _lvl_mult, 2)
    elif outcome == "bait_stash":
        # Two distinct bait keys, varied counts -- feels like a stash
        # rather than one repeated drop. Cap each by the bait's
        # max_stack so we never overflow the inventory.
        pool = list(fc.BEACHCOMB_BAIT_POOL)
        picks = rng.sample(pool, k=min(fc.BEACHCOMB_BAIT_PICKS, len(pool)))
        qty_lo, qty_hi = fc.BEACHCOMB_BAIT_QTY
        for bait_key in picks:
            cfg = fc.BAIT.get(bait_key) or {}
            cap = int(cfg.get("max_stack") or 1_000_000)
            cur = int(bait_inv.get(bait_key, 0))
            qty = rng.randint(qty_lo, qty_hi)
            actual = max(0, min(qty, cap - cur))
            if actual > 0:
                bait_inv[bait_key] = cur + actual
                baits_added.append((bait_key, actual))
    elif outcome == "treasure_map":
        qty_lo, qty_hi = fc.BEACHCOMB_MAP_QTY
        maps_added = rng.randint(qty_lo, qty_hi)
        junk_inv["map"] = int(junk_inv.get("map", 0)) + maps_added
    elif outcome == "ancient_relic":
        # Same legendary-fish jackpot the dig command uses. Drops at
        # max_lbs into fish_inventory so the player can sell it
        # normally for a huge LURE payout.
        fish_key = fc.roll_treasure_jackpot_fish()
        if fish_key:
            spec = fc.FISH.get(fish_key) or {}
            lbs = round(float(spec.get("max_lbs") or 1.0), 2)
            entries = list(fish_inv.get(fish_key) or [])
            entries.append({"lbs": lbs, "ts": int(_now_ts())})
            fish_inv[fish_key] = entries
            fish_added = (fish_key, lbs)
        else:
            # No legendary candidates -- consolation LURE drop, mirrors dig.
            lo, hi = fc.BEACHCOMB_PAYOUTS["lure_purse_big"]
            lure_credited = round(rng.uniform(lo, hi), 2)
            label = "Pirate's Hoard (relic decayed -- LURE consolation)"

    # V3 enforced 10/90 USD split (LURE:REEL).
    try:
        from core.framework.payout_split import rebalance_to_split
        lure_credited, reel_credited = await rebalance_to_split(
            db, guild_id, fc.LURE_SYMBOL, fc.REEL_SYMBOL,
            float(lure_credited), float(reel_credited),
        )
    except Exception:
        log.debug("beachcomb: 10/90 rebalance skipped", exc_info=True)
    raw_lure = to_raw(lure_credited) if lure_credited > 0 else 0
    raw_reel = to_raw(reel_credited) if reel_credited > 0 else 0

    await db.execute(
        """
        UPDATE user_fishing SET
            bait_inventory        = $3::jsonb,
            junk_inventory        = $4::jsonb,
            fish_inventory        = $5::jsonb,
            total_lure_earned_raw = total_lure_earned_raw + $6::numeric,
            total_reel_earned_raw = total_reel_earned_raw + $7::numeric,
            total_beachcombs      = total_beachcombs + 1,
            last_beachcomb_at     = NOW(),
            updated_at            = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id,
        _json(bait_inv), _json(junk_inv), _json(fish_inv),
        int(raw_lure), int(raw_reel),
    )

    lure_impact: MintImpact | None = None
    reel_impact: MintImpact | None = None
    if raw_lure > 0:
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                fc.LURE_NETWORK_SHORT, fc.LURE_SYMBOL,
                int(raw_lure),
            )
        except Exception:
            log.exception("beachcomb: LURE credit failed uid=%s gid=%s amt=%s",
                          user_id, guild_id, raw_lure)
            lure_credited = 0.0
        else:
            lure_impact = await _apply_mint_inflation_effect(
                db, guild_id, fc.LURE_SYMBOL, lure_credited,
            )
    if raw_reel > 0:
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                fc.LURE_NETWORK_SHORT, fc.REEL_SYMBOL,
                int(raw_reel),
            )
        except Exception:
            log.exception("beachcomb: REEL credit failed uid=%s gid=%s amt=%s",
                          user_id, guild_id, raw_reel)
            reel_credited = 0.0
        else:
            reel_impact = await _apply_mint_inflation_effect(
                db, guild_id, fc.REEL_SYMBOL, reel_credited,
            )

    return BeachcombResult(
        outcome_key=outcome,
        label=label,
        lure_credited=float(lure_credited),
        reel_credited=float(reel_credited),
        baits_added=baits_added,
        maps_added=maps_added,
        fish_added=fish_added,
        lure_impact=lure_impact,
        reel_impact=reel_impact,
    )


# === INVENTORY_START ===
# ==========================================================================
# Inventory: sell fish + junk
# ==========================================================================

async def sell_inventory(
    db: Any, guild_id: int, user_id: int,
    *, fish_key: str | None = None, junk_only: bool = False,
) -> tuple[int, float]:
    """Sell caught fish (and optionally junk) from the user's inventory.

    ``fish_key=None`` sells everything; passing a specific key only
    sells that species. ``junk_only=True`` sells just junk and leaves
    fish untouched.

    Pays out in LURE (fishing-only token, see Config.EARN_ONLY_TOKENS).
    Returns ``(items_sold_count, lure_paid_float)``.
    """
    state = await ensure_state(db, guild_id, user_id)
    fish_inv = _as_dict(state.get("fish_inventory"))
    junk_inv = _as_dict(state.get("junk_inventory"))

    sold_count = 0
    total_lure = 0.0
    fish_zone = str(state.get("current_zone") or fc.DEFAULT_ZONE)
    combo_mult = _combo_multiplier(int(state.get("current_combo") or 0))
    level_mult = fc.level_payout_mult(int(state.get("fish_level") or 1))
    sold_fish_keys: list[str] = []   # NFT-layer sync targets
    sold_junk: list[tuple[str, int]] = []

    # Fish branch
    if not junk_only:
        keys = list(fish_inv.keys()) if fish_key is None else [fish_key]
        for k in keys:
            entries = fish_inv.get(k) or []
            if not entries:
                continue
            for entry in entries:
                lbs = float(entry.get("lbs") or 0.0)
                payout = fc.fish_payout(
                    k, lbs,
                    combo_mult=combo_mult,
                    quality_mult=1.0,         # quality already baked into recorded weight
                    zone=fish_zone,
                )
                total_lure += payout * level_mult
                sold_count += 1
                sold_fish_keys.append(str(k))
            fish_inv.pop(k, None)

    # Junk branch (sells everything when called)
    if fish_key is None or junk_only:
        for k, count in list(junk_inv.items()):
            jm = fc.junk_meta(k) or {}
            salvage = float(jm.get("salvage_lure") or 0.0)
            total_lure += salvage * int(count) * level_mult
            sold_count += int(count)
            sold_junk.append((str(k), int(count)))
            junk_inv.pop(k, None)

    total_lure = round(total_lure, 2)
    raw_payout = to_raw(total_lure) if total_lure > 0 else 0

    if sold_count == 0:
        return (0, 0.0)

    await db.execute(
        """
        UPDATE user_fishing SET
            fish_inventory        = $3::jsonb,
            junk_inventory        = $4::jsonb,
            total_lure_earned_raw = total_lure_earned_raw + $5::numeric,
            updated_at            = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, _json(fish_inv), _json(junk_inv), int(raw_payout),
    )
    if raw_payout > 0:
        await db.update_wallet_holding(
            user_id, guild_id,
            fc.LURE_NETWORK_SHORT, fc.LURE_SYMBOL,
            int(raw_payout),
        )

    # NFT layer sync: burn one fish/junk token per unit sold. Best-effort.
    try:
        from services import items as _items
        for k in sold_fish_keys:
            await _items.consume_one(
                db,
                guild_id=guild_id, user_id=user_id,
                contract_address=_items.contract_address("fish", k),
                reason="fishing.sell",
            )
        for k, n in sold_junk:
            for _ in range(int(n)):
                await _items.consume_one(
                    db,
                    guild_id=guild_id, user_id=user_id,
                    contract_address=_items.contract_address("junk", k),
                    reason="fishing.sell",
                )
    except Exception:
        log.debug(
            "nft sell burn sync failed gid=%s uid=%s",
            guild_id, user_id, exc_info=True,
        )

    return (sold_count, total_lure)


def inventory_summary(state: dict) -> dict:
    """Compress the JSONB inventory blobs into display-ready data.

    Returns a dict with ``fish``: list of (key, count, total_lbs,
    biggest_lbs); ``junk``: list of (key, count, salvage_lure_each);
    ``bait``: list of (key, count); plus totals.
    """
    fish_inv = _as_dict(state.get("fish_inventory"))
    junk_inv = _as_dict(state.get("junk_inventory"))
    bait_inv = _as_dict(state.get("bait_inventory"))

    fish_rows: list[dict] = []
    fish_total = 0
    for k, entries in fish_inv.items():
        if not entries:
            continue
        meta = fc.fish_meta(k) or {}
        lbs_list = [float(e.get("lbs") or 0.0) for e in entries]
        fish_total += len(entries)
        fish_rows.append({
            "key":         k,
            "name":        str(meta.get("name") or k),
            "emoji":       str(meta.get("emoji") or ""),
            "rarity":      str(meta.get("rarity") or "common"),
            "count":       len(entries),
            "total_lbs":   round(sum(lbs_list), 2),
            "biggest_lbs": round(max(lbs_list), 2) if lbs_list else 0.0,
        })

    junk_rows: list[dict] = []
    junk_total = 0
    for k, count in junk_inv.items():
        meta = fc.junk_meta(k) or {}
        junk_total += int(count)
        junk_rows.append({
            "key":           k,
            "name":          str(meta.get("name") or k),
            "emoji":         str(meta.get("emoji") or ""),
            "count":         int(count),
            "salvage_each":  float(meta.get("salvage_lure") or 0.0),
        })

    bait_rows: list[dict] = []
    for k, count in bait_inv.items():
        meta = fc.bait_meta(k) or {}
        bait_rows.append({
            "key":   k,
            "name":  str(meta.get("name") or k),
            "emoji": str(meta.get("emoji") or ""),
            "count": int(count),
        })

    trap_inv = _as_dict(state.get("crab_trap_inventory"))
    trap_rows: list[dict] = []
    for k, count in trap_inv.items():
        meta = fc.crab_trap_meta(k) or {}
        trap_rows.append({
            "key":   k,
            "name":  str(meta.get("name") or k),
            "emoji": str(meta.get("emoji") or ""),
            "count": int(count),
        })

    return {
        "fish":       fish_rows,
        "junk":       junk_rows,
        "bait":       bait_rows,
        "traps":      trap_rows,
        "fish_total": fish_total,
        "junk_total": junk_total,
    }


# === INVENTORY_END ===

# === SHOP_START ===
# ==========================================================================
# Shop: rod upgrades + bait stocking
# ==========================================================================

async def buy_rod(
    db: Any, guild_id: int, user_id: int, target_tier: int,
) -> tuple[dict, "GearSpendImpact | None"]:
    """Upgrade the user's rod to ``target_tier``.

    Charges the catalog price out of wallet+bank combined. Refuses to
    downgrade and refuses to "buy" the same tier the user already
    has. Raises ValueError on any failure case so the cog gets a
    single error path. Returns ``(state, impact)`` so the cog can
    render slippage/USD/LP numbers on the receipt; impact is None
    for free tier 0 rods or when the helper short-circuited.
    """
    state = await ensure_state(db, guild_id, user_id)
    cur = int(state.get("rod_tier") or 0)
    if target_tier == cur:
        raise ValueError(f"You already own the **{fc.rod_meta(cur)['name']}**.")
    if target_tier < cur:
        raise ValueError("Rods are upgrade-only -- you can't downgrade.")
    if target_tier not in fc.RODS:
        raise ValueError(f"Rod tier `{target_tier}` does not exist.")
    if target_tier > cur + 1:
        # Force the player to climb the ladder one step at a time so
        # someone can't skip from the twig rod straight to the
        # Abyssal Rod with one fat wallet.
        next_rod = fc.rod_meta(cur + 1)
        raise ValueError(
            f"You can only upgrade one tier at a time. Buy the "
            f"**{next_rod['name']}** first."
        )

    rod = fc.rod_meta(target_tier)
    price_reel = float(rod.get("price_reel") or 0.0)
    if price_reel > 0:
        # Burn REEL from the user's Lure-Network wallet. update_wallet_holding
        # raises ValueError on insufficient balance with a generic message;
        # rewrap so the cog can show the price the user owes.
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                fc.LURE_NETWORK_SHORT, fc.REEL_SYMBOL,
                -to_raw(price_reel),
            )
        except ValueError:
            raise ValueError(
                f"You need **{price_reel:,.2f} REEL** to upgrade. Earn REEL by "
                f"swapping LURE (`,fish swap`) or staking LURE (`,fish stake`)."
            )
        # The REEL is gone from circulation -- replicate the cashout
        # mechanics so the chart, oracle, and LP holders all see it.
        impact = await _apply_gear_spend_burn_effect(db, guild_id, price_reel)
    else:
        impact = None

    row = await db.fetch_one(
        """
        UPDATE user_fishing
           SET rod_tier = $3, updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        RETURNING *
        """,
        guild_id, user_id, int(target_tier),
    )
    return (_normalize_state(row) if row else {}, impact)


async def buy_bait(
    db: Any, guild_id: int, user_id: int,
    bait_key: str, qty: int,
) -> tuple[dict, "GearSpendImpact | None", int]:
    """Add ``qty`` of ``bait_key`` to the player's tackle box.

    Charges price * qty out of wallet+bank. Caps the new stack at the
    catalog's max_stack so the player can't accidentally over-buy.
    Returns ``(state, impact, actual_qty)`` so the cog can render
    slippage/USD/LP numbers on the receipt and tell the user how many
    of ``qty`` actually fit (the leftover hit the stack cap).
    """
    if qty <= 0:
        raise ValueError("Quantity must be positive.")
    if bait_key not in fc.BAIT:
        raise ValueError(f"Unknown bait `{bait_key}`. Try `,fish shop`.")
    state = await ensure_state(db, guild_id, user_id)
    cfg = fc.BAIT[bait_key]
    cur_inv = _as_dict(state.get("bait_inventory"))
    cur = int(cur_inv.get(bait_key, 0))
    cap = int(cfg.get("max_stack") or 1_000_000)
    room = max(0, cap - cur)
    actual = min(qty, room)
    if actual <= 0:
        raise ValueError(
            f"You already hold the maximum **{cap}** {cfg['name']}."
        )

    cost = float(cfg.get("price_reel") or 0.0) * actual
    if cost > 0:
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                fc.LURE_NETWORK_SHORT, fc.REEL_SYMBOL,
                -to_raw(cost),
            )
        except ValueError:
            raise ValueError(
                f"You need **{cost:,.2f} REEL** for {actual}x {cfg['name']}."
            )
        # The REEL is gone from circulation -- replicate the cashout
        # mechanics so the chart, oracle, and LP holders all see it.
        impact = await _apply_gear_spend_burn_effect(db, guild_id, cost)
    else:
        impact = None

    cur_inv[bait_key] = cur + actual
    row = await db.fetch_one(
        """
        UPDATE user_fishing
           SET bait_inventory = $3::jsonb, updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        RETURNING *
        """,
        guild_id, user_id, _json(cur_inv),
    )
    # NFT layer sync: mint one bait token per actual unit purchased.
    # Best-effort -- the JSONB counter is still source of truth.
    try:
        from services import items as _items
        addr = _items.contract_address("bait", str(bait_key))
        for unit_n in range(int(actual)):
            await _items.mint_unit(
                db,
                guild_id=guild_id,
                contract_address=addr,
                owner_user_id=user_id,
                metadata={"bait_key": str(bait_key)},
                mint_source="fishing.buy_bait",
                source_table="user_fishing.bait_inventory",
                source_id=f"{user_id}:{bait_key}:{int(_now_ts())}:{unit_n}",
            )
    except Exception:
        log.debug(
            "nft bait mint sync failed gid=%s uid=%s key=%s",
            guild_id, user_id, bait_key, exc_info=True,
        )
    return (_normalize_state(row) if row else {}, impact, int(actual))


# --------------------------------------------------------------------------
# Crab traps -- buy / place / collect
# --------------------------------------------------------------------------
# Lifecycle and chart impact are described in fishing_config.CRAB_TRAPS.
# Every buy burns REEL through the same _apply_gear_spend_burn_effect
# helper rod and bait spends use, so the REEL oracle / chart / LP rewards
# all move uniformly.


@dataclass
class TrapCollectResult:
    """Receipt for a ``,fish trap collect`` call.

    The cog uses this to render the haul embed without a follow-up DB
    read. Empty hauls (nothing was ready) get returned with all zeros so
    the cog can show a "patience" message instead of erroring.
    """
    traps_collected:    int                      # how many traps were hauled in
    lure_paid:          float                    # LURE credited to wallet
    crabs_added:        dict[str, int]           # {fish_key: count} added to fish_inventory
    leftover_traps:     int                      # placed traps still soaking after this collect
    per_trap_haul:      list[dict]               # [{"key", "zone", "lure", "crabs": [keys]}]


async def buy_crab_trap(
    db: Any, guild_id: int, user_id: int,
    trap_key: str, qty: int,
) -> tuple[dict, "GearSpendImpact | None", int]:
    """Add ``qty`` of ``trap_key`` to the player's crab trap inventory.

    Mirrors ``buy_bait``: charges price * qty in REEL, caps the new
    stack at the catalog's ``max_stack``, runs the standard gear-spend
    burn effect so the REEL oracle / chart / LP reward all see the
    spend. Returns ``(state, impact, actual_qty)`` so the cog can show
    slippage and how many of ``qty`` actually fit.
    """
    if qty <= 0:
        raise ValueError("Quantity must be positive.")
    if trap_key not in fc.CRAB_TRAPS:
        raise ValueError(
            f"Unknown crab trap `{trap_key}`. Try `,fish trap shop`."
        )
    state = await ensure_state(db, guild_id, user_id)
    cfg = fc.CRAB_TRAPS[trap_key]
    cur_inv = _as_dict(state.get("crab_trap_inventory"))
    cur = int(cur_inv.get(trap_key, 0))
    cap = int(cfg.get("max_stack") or 1_000_000)
    room = max(0, cap - cur)
    actual = min(qty, room)
    if actual <= 0:
        raise ValueError(
            f"You already hold the maximum **{cap}** {cfg['name']}."
        )

    cost = float(cfg.get("price_reel") or 0.0) * actual
    if cost > 0:
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                fc.LURE_NETWORK_SHORT, fc.REEL_SYMBOL,
                -to_raw(cost),
            )
        except ValueError:
            raise ValueError(
                f"You need **{cost:,.2f} REEL** for {actual}x {cfg['name']}."
            )
        # Trap purchases burn REEL out of circulation -- run the same
        # oracle / chart / LP reward path as rod and bait purchases so
        # "everything affects the chart" stays a real invariant.
        impact = await _apply_gear_spend_burn_effect(db, guild_id, cost)
    else:
        impact = None

    cur_inv[trap_key] = cur + actual
    row = await db.fetch_one(
        """
        UPDATE user_fishing
           SET crab_trap_inventory = $3::jsonb, updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        RETURNING *
        """,
        guild_id, user_id, _json(cur_inv),
    )
    return (_normalize_state(row) if row else {}, impact, int(actual))


async def place_crab_traps(
    db: Any, guild_id: int, user_id: int,
    trap_key: str, qty: int = 1,
) -> tuple[dict, int]:
    """Move ``qty`` traps of ``trap_key`` from inventory to the current zone.

    Refuses to place traps the player doesn't own, refuses to place in a
    zone whose tier exceeds the trap's ``max_zone_tier``, and refuses to
    push the placed-trap count over ``CRAB_TRAP_PLACED_CAP``. Returns
    the post-write state and the number of traps actually placed (caller
    might've asked for more than fit under the cap).
    """
    if qty <= 0:
        raise ValueError("Quantity must be positive.")
    if trap_key not in fc.CRAB_TRAPS:
        raise ValueError(f"Unknown crab trap `{trap_key}`.")
    state = await ensure_state(db, guild_id, user_id)
    cfg = fc.CRAB_TRAPS[trap_key]
    zone_key = str(state.get("current_zone") or fc.DEFAULT_ZONE)
    zone = fc.zone_meta(zone_key)
    if int(zone.get("tier") or 0) > int(cfg.get("max_zone_tier") or 0):
        raise ValueError(
            f"The **{cfg['name']}** can't be placed in **{zone['name']}** "
            f"(zone tier {zone.get('tier')}, trap max tier "
            f"{cfg.get('max_zone_tier')})."
        )

    inv = _as_dict(state.get("crab_trap_inventory"))
    placed = list(_as_list(state.get("placed_crab_traps")))
    have = int(inv.get(trap_key, 0))
    if have <= 0:
        raise ValueError(
            f"You don't own any **{cfg['name']}**. Buy some with "
            f"`,fish trap buy {trap_key} <qty>`."
        )
    room = max(0, fc.CRAB_TRAP_PLACED_CAP - len(placed))
    if room <= 0:
        raise ValueError(
            f"You already have the maximum **{fc.CRAB_TRAP_PLACED_CAP}** "
            f"traps placed. Collect them first with `,fish trap collect`."
        )
    actual = min(qty, have, room)
    if actual <= 0:
        raise ValueError("Nothing to place -- check your inventory and the placed-trap cap.")

    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    for _ in range(actual):
        placed.append({
            "key":       trap_key,
            "zone":      zone_key,
            "placed_at": now_iso,
        })
    inv[trap_key] = have - actual
    if inv[trap_key] <= 0:
        inv.pop(trap_key, None)

    row = await db.fetch_one(
        """
        UPDATE user_fishing SET
            crab_trap_inventory = $3::jsonb,
            placed_crab_traps   = $4::jsonb,
            total_traps_placed  = total_traps_placed + $5,
            updated_at          = NOW()
         WHERE guild_id = $1 AND user_id = $2
        RETURNING *
        """,
        guild_id, user_id, _json(inv), _json(placed), int(actual),
    )
    return (_normalize_state(row) if row else {}, int(actual))


def _trap_ready_at(placed_at_iso: str, soak_seconds: int) -> _dt.datetime:
    """Return the UTC datetime at which a trap finishes soaking."""
    placed = _dt.datetime.fromisoformat(placed_at_iso)
    if placed.tzinfo is None:
        placed = placed.replace(tzinfo=_dt.timezone.utc)
    return placed + _dt.timedelta(seconds=int(soak_seconds))


def trap_status_summary(state: dict) -> dict:
    """Compress placed_crab_traps into a render-ready summary dict.

    Returns ``{"placed_total", "ready_total", "rows": [...]}`` where each
    row has ``key``, ``zone``, ``ready`` (bool), ``ready_in_s`` (int),
    and ``placed_at`` (datetime). The cog uses this directly in
    ``,fish trap`` and the stats panel.
    """
    placed = _as_list(state.get("placed_crab_traps"))
    now = _dt.datetime.now(_dt.timezone.utc)
    rows: list[dict] = []
    ready_total = 0
    for entry in placed:
        key = str(entry.get("key") or "")
        zone = str(entry.get("zone") or fc.DEFAULT_ZONE)
        cfg = fc.CRAB_TRAPS.get(key) or {}
        soak = int(cfg.get("soak_seconds") or 0)
        try:
            ready_at = _trap_ready_at(str(entry.get("placed_at") or ""), soak)
        except (ValueError, TypeError):
            # Corrupt placed_at -- mark as ready so the player can
            # collect (and effectively prune) the bad entry.
            ready_at = now
        ready_in = max(0, int((ready_at - now).total_seconds()))
        is_ready = ready_in <= 0
        if is_ready:
            ready_total += 1
        rows.append({
            "key":         key,
            "zone":        zone,
            "ready":       is_ready,
            "ready_in_s":  ready_in,
            "placed_at":   entry.get("placed_at"),
        })
    return {
        "placed_total": len(placed),
        "ready_total":  ready_total,
        "rows":         rows,
    }


async def collect_crab_traps(
    db: Any, guild_id: int, user_id: int,
) -> TrapCollectResult:
    """Pull every soaked trap, pay LURE, drop crabs into fish_inventory.

    Anti-spam cooldown: refuses to run again within
    ``CRAB_TRAP_COLLECT_COOLDOWN_S`` of the previous collect (DB-side
    clock). Empty hauls return all zeros so the cog can show a friendly
    message instead of erroring.

    The collect is idempotent at the JSONB level: every ready trap is
    consumed in a single UPDATE (no per-trap round-trips), the LURE
    credit goes through the same ``update_wallet_holding`` path as fish
    sales, and ``total_crabs_collected`` ticks for analytics.
    """
    state = await ensure_state(db, guild_id, user_id)

    # DB-side cooldown clock. Zero rows back means the cooldown is still
    # active; the row exists but the WHERE clause didn't match.
    cd_row = await db.fetch_one(
        """
        SELECT
            CASE
                WHEN last_trap_collect_at IS NULL THEN 0
                ELSE EXTRACT(EPOCH FROM (NOW() - last_trap_collect_at))::INTEGER
            END AS elapsed_s
          FROM user_fishing
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id,
    )
    elapsed_s = int((cd_row or {}).get("elapsed_s") or 0)
    if elapsed_s > 0 and elapsed_s < int(fc.CRAB_TRAP_COLLECT_COOLDOWN_S):
        wait = int(fc.CRAB_TRAP_COLLECT_COOLDOWN_S - elapsed_s)
        raise ValueError(
            f"You just hauled traps -- give them **{wait}s** before checking again."
        )

    placed = list(_as_list(state.get("placed_crab_traps")))
    if not placed:
        return TrapCollectResult(0, 0.0, {}, 0, [])

    summary = trap_status_summary(state)
    ready_indices = [i for i, r in enumerate(summary["rows"]) if r["ready"]]
    if not ready_indices:
        return TrapCollectResult(0, 0.0, {}, len(placed), [])

    fish_inv = _as_dict(state.get("fish_inventory"))
    # Crab traps are now permanent gear (rod-priced, never consumed).
    # On collect we (a) drop the soaked trap from ``placed_crab_traps``
    # and (b) RETURN it to ``crab_trap_inventory`` so the player can
    # re-place it. The previous behavior dropped the trap entirely,
    # which made high-tier traps disposable -- not what we want now
    # that they cost 5M+ REEL each.
    trap_inv = _as_dict(state.get("crab_trap_inventory"))
    leftover = [t for i, t in enumerate(placed) if i not in set(ready_indices)]
    haul: list[dict] = []
    crabs_added: dict[str, int] = {}
    total_lure = 0.0
    now_ts = _now_ts()

    for idx in ready_indices:
        entry = placed[idx]
        key = str(entry.get("key") or "")
        zone = str(entry.get("zone") or fc.DEFAULT_ZONE)
        cfg = fc.CRAB_TRAPS.get(key) or {}
        if key:
            trap_inv[key] = int(trap_inv.get(key, 0) or 0) + 1
        lure_haul = fc.trap_yield_lure(key, zone)
        total_lure += lure_haul
        per_trap_crabs: list[str] = []
        crab_count = int(fc.CRAB_TRAP_CRABS_PER_COLLECT.get(key, 1))
        for _ in range(crab_count):
            crab_key = fc.roll_crab(zone, key)
            if not crab_key:
                continue
            spec = fc.FISH.get(crab_key)
            if not spec:
                continue
            lbs = round(random.uniform(
                float(spec["min_lbs"]), float(spec["max_lbs"]),
            ), 2)
            entries = list(fish_inv.get(crab_key) or [])
            entries.append({"lbs": lbs, "ts": now_ts})
            fish_inv[crab_key] = entries
            crabs_added[crab_key] = crabs_added.get(crab_key, 0) + 1
            per_trap_crabs.append(crab_key)
        haul.append({
            "key":   key,
            "zone":  zone,
            "lure":  lure_haul,
            "crabs": per_trap_crabs,
        })

    total_lure = round(total_lure, 2)
    raw_lure = to_raw(total_lure) if total_lure > 0 else 0
    crabs_total = sum(crabs_added.values())

    await db.execute(
        """
        UPDATE user_fishing SET
            placed_crab_traps     = $3::jsonb,
            fish_inventory        = $4::jsonb,
            crab_trap_inventory   = $5::jsonb,
            total_crabs_collected = total_crabs_collected + $6,
            total_lure_earned_raw = total_lure_earned_raw + $7::numeric,
            last_trap_collect_at  = NOW(),
            updated_at            = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id,
        _json(leftover), _json(fish_inv), _json(trap_inv),
        int(crabs_total), int(raw_lure),
    )
    if raw_lure > 0:
        await db.update_wallet_holding(
            user_id, guild_id,
            fc.LURE_NETWORK_SHORT, fc.LURE_SYMBOL,
            int(raw_lure),
        )

    return TrapCollectResult(
        traps_collected=len(ready_indices),
        lure_paid=float(total_lure),
        crabs_added=crabs_added,
        leftover_traps=len(leftover),
        per_trap_haul=haul,
    )


# === SHOP_END ===

# === ECONOMY_START ===
# ==========================================================================
# Token economy: LURE -> REEL (burn-swap or stake) and REEL -> USD cashout
# ==========================================================================
# Two one-way conversions plus a passive stake yield. All three move real
# numbers through the SAME price-impact + supply-burn machinery the rest
# of the codebase uses for .buy / .sell -- no parallel mechanics:
#
#   burn_lure_for_reel   -- destroy LURE, mint REEL at the live oracle
#                           ratio; LURE oracle drops (sell pressure +
#                           supply contraction), REEL oracle rises (mint
#                           pressure). Slippage is the standard
#                           Config.PRICE_IMPACT_DIVISOR formula.
#   stake_lure           -- lock LURE; accrues REEL at LURE_STAKE_REEL_PER_DAY
#   accrued_stake_yield  -- pure read: how much pending REEL would unlock now
#   claim_stake_yield    -- pay out accrued REEL, reset the clock (LURE stays staked)
#   unstake_lure         -- return staked LURE to wallet (also claims yield)
#   cashout_reel         -- destroy REEL, credit USD at the post-impact
#                           oracle price. Identical mechanics to .sell:
#                           supply burn + downward price impact + chart
#                           updates via crypto_prices.update_price.
#
# Conversion rates are NOT hard-coded; they are read from crypto_prices
# at trade time. Big burns naturally produce large slippage; small
# burns produce tiny slippage. Same as every other token in the system.

import time as _time


# Soft cap on per-trade impact magnitude. Mirrors the 0.95 clamp in
# cogs/trade.py .sell so a single huge burn cannot wipe more than 95%
# of the oracle price in one shot. Keeps the chart legible.
_PRICE_IMPACT_MAX: float = 0.95


@dataclass
class BurnResult:
    """LURE -> REEL burn receipt."""
    lure_burned_raw: int            # LURE removed from supply
    reel_minted_raw: int            # REEL credited to the user (post-impact)
    lure_oracle_before: float       # LURE/USD before the burn
    lure_oracle_after: float        # LURE/USD after the impact applied
    reel_oracle_before: float       # REEL/USD before the burn
    reel_oracle_after: float        # REEL/USD after the impact applied
    price_impact_pct: float         # impact magnitude as a decimal (0.07 = 7%)
    lp_reward_usd: float = 0.0      # USD paid to LP holders of LURE/REEL pools


@dataclass
class StakeResult:
    lure_staked_raw: int            # NEW total staked after operation
    lure_delta_raw: int             # signed change applied to staked balance
    reel_yield_paid_raw: int        # REEL paid out as part of this op (claim/unstake)
    pending_reel_raw: int           # REEL still pending (post-op)


@dataclass
class CashoutResult:
    """REEL -> USD cashout receipt."""
    reel_burned_raw: int            # REEL removed from supply (== input)
    usd_credited_raw: int           # raw USD added to users.wallet
    reel_oracle_before: float       # REEL/USD before the cashout
    reel_oracle_after: float        # REEL/USD after the impact applied
    price_impact_pct: float         # impact magnitude as a decimal
    revenue_usd: float = 0.0        # gross USD value of the burn (pre-impact)
    lp_reward_usd: float = 0.0      # USD paid to LP holders of REEL pools


async def get_lure_wallet_raw(db: Any, guild_id: int, user_id: int) -> int:
    """Return the user's LURE wallet balance (raw-scaled). 0 if no row yet.

    Used by ``,fish swap all`` / ``,fish stake all`` to translate the
    "all" sentinel into the actual spendable balance before calling the
    burn / stake path.
    """
    row = await db.get_wallet_holding(
        user_id, guild_id, fc.LURE_NETWORK_SHORT, fc.LURE_SYMBOL,
    )
    return int((row or {}).get("amount") or 0)


async def get_reel_wallet_raw(db: Any, guild_id: int, user_id: int) -> int:
    """Return the user's REEL wallet balance (raw-scaled). 0 if no row yet.

    Used by ``,fish cashout all`` to cash out the full balance without the
    caller having to juggle raw-vs-human unit conversions.
    """
    row = await db.get_wallet_holding(
        user_id, guild_id, fc.LURE_NETWORK_SHORT, fc.REEL_SYMBOL,
    )
    return int((row or {}).get("amount") or 0)


async def _oracle_price(db: Any, guild_id: int, symbol: str) -> float:
    """Live <symbol>/USD oracle price. Falls back to start_price if not seeded.

    crypto_prices is the single source of truth for the chart, .buy / .sell
    impact, and these fishing burns. Reading the same row everyone else
    reads is what makes the burn impact actually show up on the chart on
    the next refresh.
    """
    row = await db.fetch_one(
        "SELECT price FROM crypto_prices WHERE guild_id = $1 AND symbol = $2",
        int(guild_id), symbol.upper(),
    )
    if row and row.get("price") is not None:
        return float(row["price"])
    from core.config import Config
    return float(Config.TOKENS.get(symbol.upper(), {}).get("start_price", 1.0))


def _minute_ts() -> int:
    """Round UNIX seconds down to the current 1-minute candle bucket.
    Matches the convention in cogs/trade.py so all chart writers agree
    on the same bucket key."""
    return int(_time.time()) // 60 * 60


async def _write_burn_candle(
    db: Any, guild_id: int, symbol: str,
    oracle_before: float, oracle_after: float, volume_usd: float,
) -> None:
    """Extend the current 1-minute candle with the burn impact.

    Mirrors the .buy / .sell pattern in cogs/trade.py: an out-of-band
    oracle move immediately gets a candle write so the user-visible
    chart reflects the impact without waiting up to PRICE_TICK_SECONDS
    for the drift task. ``upsert_candle`` takes GREATEST/LEAST on
    high/low and overwrites close, so this is safe to stack with
    subsequent drift ticks within the same minute.

    Best-effort: a candle-write failure must NOT take down a burn,
    since the oracle has already been moved by the caller.
    """
    try:
        await db.upsert_candle(
            int(guild_id), f"{symbol.upper()}USD", _minute_ts(),
            open_=float(oracle_before),
            high=max(float(oracle_before), float(oracle_after)),
            low=min(float(oracle_before), float(oracle_after)),
            close=float(oracle_after),
            volume_delta=float(max(0.0, volume_usd)),
        )
    except Exception:
        log.exception(
            "fishing burn candle update failed gid=%s sym=%s",
            guild_id, symbol,
        )


async def _distribute_burn_lp_reward(
    db: Any, guild_id: int, symbol: str, fee_usd: float,
) -> float:
    """Pay a USD reward to LP holders of pools containing ``symbol``.

    Every active user lp_position whose pool has ``symbol`` on either
    side (and is not vault-locked) is credited a pro-rata slice of
    ``fee_usd`` weighted by ``lp_shares / total_lp`` summed and
    normalised across qualifying positions. Group LP positions are
    intentionally excluded so this stays user-rewarding -- the hourly
    services/lp_yield.py tick already pays the group side.

    Logged as ``LP_BURN_REWARD`` per payout so it shows up alongside
    LP_YIELD in the user's income history. No-op when ``fee_usd`` is
    zero or no LP positions hold ``symbol`` (the common case for the
    EARN_ONLY tokens until someone seeds a manual pool), so it is safe
    to call on every burn unconditionally.

    Returns the USD actually paid out (sum of per-user payouts), which
    callers can stash on their result dataclass for the receipt embed.
    """
    if fee_usd <= 0:
        return 0.0
    sym = symbol.upper()
    rows = await db.fetch_all(
        """
        SELECT lp.user_id, lp.pool_id, lp.lp_shares, p.total_lp
          FROM lp_positions lp
          JOIN pools p
            ON p.pool_id = lp.pool_id
           AND p.guild_id = lp.guild_id
         WHERE lp.guild_id = $1
           AND lp.lp_shares > 0
           AND COALESCE(p.vault_locked, FALSE) = FALSE
           AND (p.token_a = $2 OR p.token_b = $2)
        """,
        int(guild_id), sym,
    )
    if not rows:
        return 0.0

    weights: list[tuple[int, float]] = []
    total_weight = 0.0
    for r in rows:
        total_lp = int(r.get("total_lp") or 0)
        shares = int(r.get("lp_shares") or 0)
        if total_lp <= 0 or shares <= 0:
            continue
        w = shares / total_lp
        weights.append((int(r["user_id"]), w))
        total_weight += w
    if total_weight <= 0:
        return 0.0

    paid_total = 0.0
    for uid, w in weights:
        payout_usd = fee_usd * (w / total_weight)
        payout_raw = to_raw(payout_usd)
        if payout_raw <= 0:
            continue
        try:
            async with db.atomic():
                await db.update_wallet(uid, int(guild_id), int(payout_raw))
                await db.log_tx(
                    int(guild_id), uid, "LP_BURN_REWARD",
                    symbol_in=sym,
                    symbol_out="USD", amount_out=int(payout_raw),
                    network="usd",
                )
            paid_total += payout_usd
        except Exception:
            log.exception(
                "lp burn reward credit failed gid=%s uid=%s sym=%s usd=%.6f",
                guild_id, uid, sym, payout_usd,
            )
    return paid_total


def _price_impact(usd_value: float, oracle: float, supply_human: float) -> float:
    """Same impact formula cogs/trade.py .buy / .sell uses, lifted into one
    helper so LURE/REEL burns are mathematically uniform with the rest of
    the trading surface.

        impact = usd_value / PRICE_IMPACT_DIVISOR
        if usd_value > 0.001 * market_cap:
            impact *= min(1 + (usd_value / market_cap) * 2.0, 5.0)
        impact = min(impact, _PRICE_IMPACT_MAX)
    """
    from core.config import Config
    impact = usd_value / float(Config.PRICE_IMPACT_DIVISOR)
    market_cap = max(0.0, oracle * supply_human)
    if market_cap > 0 and usd_value > 0.001 * market_cap:
        mc_ratio = usd_value / market_cap
        impact *= min(1.0 + mc_ratio * 2.0, 5.0)
    return min(impact, _PRICE_IMPACT_MAX)


async def _apply_gear_spend_burn_effect(
    db: Any, guild_id: int, reel_amount_human: float,
) -> "GearSpendImpact | None":
    """Apply burn-style oracle, chart, and LP reward to a REEL gear spend.

    Spending REEL on a rod or bait pulls supply out of circulation via
    update_wallet_holding (negative delta on an EARN_ONLY token), so the
    market footprint should match a cashout of the same size: REEL
    oracle drops by the standard impact formula, the live REEL/USD
    candle is extended in the same minute, and the standard LP-reward
    slice is paid to LP holders of any REEL pool.

    Returns a ``GearSpendImpact`` snapshot when the spend produced a
    measurable footprint so the cog can surface oracle/slippage/LP
    numbers on the receipt embed, or ``None`` when there was nothing
    to do (zero spend, zero oracle).

    Best-effort: failures are logged but never raised. The user has
    already been debited their REEL upstream, and a missing chart
    write or empty LP set should not abort a successful purchase.
    """
    if reel_amount_human <= 0:
        return None
    try:
        oracle_before = await _oracle_price(db, guild_id, fc.REEL_SYMBOL)
        if oracle_before <= 0:
            return None
        usd_value = float(reel_amount_human) * oracle_before
        row = await db.fetch_one(
            "SELECT circulating_supply FROM crypto_prices "
            "WHERE guild_id = $1 AND symbol = $2",
            int(guild_id), fc.REEL_SYMBOL,
        )
        supply_human = to_human(int((row or {}).get("circulating_supply") or 0))
        impact = _price_impact(usd_value, oracle_before, supply_human)
        oracle_after = max(1e-9, oracle_before * (1.0 - impact))
        await db.update_price(fc.REEL_SYMBOL, guild_id, oracle_after)
        await _write_burn_candle(
            db, guild_id, fc.REEL_SYMBOL,
            oracle_before, oracle_after, usd_value,
        )
        fee_usd_target = usd_value * (int(fc.GEAR_BURN_LP_REWARD_BPS) / 10_000.0)
        lp_paid_usd = 0.0
        if fee_usd_target > 0:
            lp_paid_usd = await _distribute_burn_lp_reward(
                db, guild_id, fc.REEL_SYMBOL, fee_usd_target,
            )
        return GearSpendImpact(
            reel_amount_human=float(reel_amount_human),
            usd_value=float(usd_value),
            oracle_before=float(oracle_before),
            oracle_after=float(oracle_after),
            price_impact_pct=float(impact),
            lp_reward_usd=float(lp_paid_usd),
        )
    except Exception:
        log.exception(
            "gear spend burn effect failed gid=%s reel=%s",
            guild_id, reel_amount_human,
        )
        return None


@dataclass
class MintImpact:
    """Receipt for an inflation-style mint that moved an oracle.

    Mirrors ``GearSpendImpact`` but for the symmetric direction: when
    tokens are minted from "thin air" (treasure dig, future fish-sale
    inflation), supply expands without corresponding buy pressure, and
    the oracle drops by the same ``_price_impact`` formula gear-spend
    uses. The cog renders this on the receipt embed so users see the
    chart-impact side of every payout.
    """
    symbol:           str        # "LURE" or "REEL"
    amount_human:     float      # tokens minted
    usd_value:        float      # USD value at the pre-mint oracle
    oracle_before:    float
    oracle_after:     float
    price_impact_pct: float      # decimal, e.g. 0.025 == 2.5%


async def _apply_mint_inflation_effect(
    db: Any, guild_id: int, symbol: str, mint_amount_human: float,
) -> "MintImpact | None":
    """Apply inflation-style oracle drop + candle write for a mint.

    When tokens are minted out of thin air (treasure dig payouts, future
    inflation surfaces), supply expands without buy pressure. The
    oracle drops by the same ``_price_impact`` formula gear-spend uses,
    so the chart honestly reflects what just happened to circulating
    supply -- "everything affects the chart of whatever it uses",
    extended to the mint side.

    Returns ``None`` for zero mints, missing oracle rows, or any internal
    failure (best-effort -- the user has already been credited the
    tokens upstream and a chart-write hiccup must not take that back).
    """
    if mint_amount_human <= 0:
        return None
    try:
        oracle_before = await _oracle_price(db, guild_id, symbol)
        if oracle_before <= 0:
            return None
        usd_value = float(mint_amount_human) * oracle_before
        row = await db.fetch_one(
            "SELECT circulating_supply FROM crypto_prices "
            "WHERE guild_id = $1 AND symbol = $2",
            int(guild_id), symbol,
        )
        supply_human = to_human(int((row or {}).get("circulating_supply") or 0))
        impact = _price_impact(usd_value, oracle_before, supply_human)
        oracle_after = max(1e-9, oracle_before * (1.0 - impact))
        await db.update_price(symbol, guild_id, oracle_after)
        # _write_burn_candle is symbol-direction-agnostic -- it just
        # extends the current 1-minute candle with the new high/low/
        # close. Reused here so a future renamed helper centralises
        # candle handling for both burns and mints.
        await _write_burn_candle(
            db, guild_id, symbol,
            oracle_before, oracle_after, usd_value,
        )
        return MintImpact(
            symbol=symbol,
            amount_human=float(mint_amount_human),
            usd_value=float(usd_value),
            oracle_before=float(oracle_before),
            oracle_after=float(oracle_after),
            price_impact_pct=float(impact),
        )
    except Exception:
        log.exception(
            "mint inflation effect failed gid=%s sym=%s amt=%s",
            guild_id, symbol, mint_amount_human,
        )
        return None


@dataclass
class GearSpendImpact:
    """Receipt for a REEL gear spend so the cog can show slippage + USD.

    ``lp_reward_usd`` is the actual amount distributed (zero if no LP
    positions hold REEL); the requested-vs-paid split lets the cog
    say "1% reserved, $0.04 paid out" once a pool exists.
    """
    reel_amount_human: float
    usd_value: float
    oracle_before: float
    oracle_after: float
    price_impact_pct: float
    lp_reward_usd: float


async def burn_lure_for_reel(
    db: Any, guild_id: int, user_id: int, lure_amount_raw: int,
) -> BurnResult:
    """Burn LURE, mint REEL, push both oracles by the standard impact formula.

    Conversion: USD value at the live LURE oracle is preserved into REEL
    at the live REEL oracle. So 1000 LURE @ $0.10 -> $100 USD-value ->
    100 REEL @ $1.00. After the trade the LURE oracle drops by ``impact``
    (sell-pressure + supply contraction) and the REEL oracle rises by
    ``impact`` (mint pressure). The chart picks both up via
    ``crypto_prices.update_price``.

    No fixed rate, no minimum, no fee that disappears into thin air --
    the slippage IS the fee.
    """
    if lure_amount_raw <= 0:
        raise ValueError("Amount must be positive.")

    # Resolve the LURE wallet up front so we can fail before any writes.
    held = await get_lure_wallet_raw(db, guild_id, user_id)
    if held < int(lure_amount_raw):
        raise ValueError(f"You only have {to_human(held):,.4f} LURE.")

    lure_oracle_before = await _oracle_price(db, guild_id, fc.LURE_SYMBOL)
    reel_oracle_before = await _oracle_price(db, guild_id, fc.REEL_SYMBOL)
    if lure_oracle_before <= 0 or reel_oracle_before <= 0:
        raise ValueError("Oracle price is currently zero -- try again in a moment.")

    lure_human = to_human(int(lure_amount_raw))
    usd_value = lure_human * lure_oracle_before

    # Pull supply for both sides so impact scales with market cap, just
    # like .sell. crypto_prices.circulating_supply is raw-scaled; convert.
    rows = await db.fetch_all(
        "SELECT symbol, circulating_supply FROM crypto_prices "
        "WHERE guild_id = $1 AND symbol = ANY($2::text[])",
        int(guild_id), [fc.LURE_SYMBOL, fc.REEL_SYMBOL],
    )
    supply: dict[str, float] = {}
    for r in (rows or []):
        supply[str(r["symbol"]).upper()] = to_human(int(r.get("circulating_supply") or 0))

    # Compute symmetric impact magnitude on each side.
    lure_impact = _price_impact(usd_value, lure_oracle_before, supply.get(fc.LURE_SYMBOL, 0.0))
    reel_impact = _price_impact(usd_value, reel_oracle_before, supply.get(fc.REEL_SYMBOL, 0.0))

    # Apply slippage to the user's REEL receipt the same way .sell does:
    # the user gets the AVERAGE of pre- and post-impact REEL price (using
    # a small linear approximation = effective price = oracle * (1 + impact/2)),
    # which means a big burn pays the user SLIGHTLY less REEL per LURE.
    # Mathematically: minted_reel = usd_value / (reel_oracle * (1 + reel_impact/2)).
    eff_reel_price = reel_oracle_before * (1.0 + reel_impact / 2.0)
    reel_minted_human = usd_value / max(1e-12, eff_reel_price)
    reel_minted_raw = to_raw(reel_minted_human)
    if reel_minted_raw <= 0:
        raise ValueError("Burn produces zero REEL -- raise the LURE amount.")

    # Burn LURE from the user's wallet (auto-decrements crypto_prices
    # circulating_supply; that is what makes this a real burn rather than
    # a transfer).
    await db.update_wallet_holding(
        user_id, guild_id,
        fc.LURE_NETWORK_SHORT, fc.LURE_SYMBOL,
        -int(lure_amount_raw),
    )
    try:
        await db.update_wallet_holding(
            user_id, guild_id,
            fc.LURE_NETWORK_SHORT, fc.REEL_SYMBOL,
            int(reel_minted_raw),
        )
    except Exception:
        # Refund LURE so a credit failure never silently eats the user.
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                fc.LURE_NETWORK_SHORT, fc.LURE_SYMBOL,
                int(lure_amount_raw),
            )
        except Exception:
            log.exception("burn_lure_for_reel: refund of LURE also failed "
                          "uid=%s gid=%s amt=%s", user_id, guild_id, lure_amount_raw)
        raise

    # Push both oracles AFTER the wallet writes so the price reflects the
    # new circulating supply. .sell uses the same ordering.
    lure_oracle_after = max(1e-9, lure_oracle_before * (1.0 - lure_impact))
    reel_oracle_after = max(1e-9, reel_oracle_before * (1.0 + reel_impact))
    try:
        await db.update_price(fc.LURE_SYMBOL, guild_id, lure_oracle_after)
        await db.update_price(fc.REEL_SYMBOL, guild_id, reel_oracle_after)
    except Exception:
        log.exception(
            "burn_lure_for_reel: oracle update failed gid=%s -- chart will "
            "lag until the next drift tick", guild_id,
        )

    # Extend the live LURE/USD and REEL/USD candles so the chart picks
    # up the impact in the same minute, matching how .buy / .sell write
    # candles after their oracle move.
    await _write_burn_candle(
        db, guild_id, fc.LURE_SYMBOL,
        lure_oracle_before, lure_oracle_after, usd_value,
    )
    await _write_burn_candle(
        db, guild_id, fc.REEL_SYMBOL,
        reel_oracle_before, reel_oracle_after, usd_value,
    )

    # Distribute the LP-reward slice of this burn to LP holders of any
    # pool that contains LURE or REEL. Fan-out is split evenly across
    # the two sides since the burn moved BOTH oracles.
    fee_usd = usd_value * (int(fc.GEAR_BURN_LP_REWARD_BPS) / 10_000.0)
    lp_paid_total = 0.0
    if fee_usd > 0:
        lp_paid_total += await _distribute_burn_lp_reward(
            db, guild_id, fc.LURE_SYMBOL, fee_usd / 2.0,
        )
        lp_paid_total += await _distribute_burn_lp_reward(
            db, guild_id, fc.REEL_SYMBOL, fee_usd / 2.0,
        )

    # Track lifetime REEL earned for analytics / achievements.
    await db.execute(
        """
        UPDATE user_fishing
           SET total_reel_earned_raw = total_reel_earned_raw + $3::numeric,
               updated_at = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(reel_minted_raw),
    )

    # Report the larger of the two impact percentages so the user sees the
    # most aggressive number (consistent with how .sell reports impact).
    return BurnResult(
        lure_burned_raw=int(lure_amount_raw),
        reel_minted_raw=int(reel_minted_raw),
        lure_oracle_before=float(lure_oracle_before),
        lure_oracle_after=float(lure_oracle_after),
        lp_reward_usd=float(lp_paid_total),
        reel_oracle_before=float(reel_oracle_before),
        reel_oracle_after=float(reel_oracle_after),
        price_impact_pct=float(max(lure_impact, reel_impact)),
    )


# Back-compat: callers still importing ``swap_lure_to_reel`` and
# ``SwapResult`` keep working. The cog already does the renaming, but
# this alias keeps the public name graveyard small.
swap_lure_to_reel = burn_lure_for_reel
SwapResult = BurnResult


def _accrue_pending(staked_raw: int, last_at: Any) -> tuple[int, int]:
    """Return ``(elapsed_seconds, accrued_reel_raw)`` for a stake position.

    ``last_at`` may be a datetime, an epoch float (per the project's _coerce
    convention), or None. Using ``time.time()`` for the diff keeps this pure
    Python and avoids datetime subtraction edge cases. The DB clock is the
    source of truth -- callers always re-read after writes -- so a small
    drift between Python and DB clocks is acceptable for a passive yield.
    """
    if staked_raw <= 0 or not last_at:
        return 0, 0
    if isinstance(last_at, _dt.datetime):
        last_ts = last_at.timestamp()
    else:
        last_ts = float(last_at)
    now_ts = float(_time.time())
    elapsed = max(0, int(now_ts - last_ts))
    if elapsed <= 0:
        return 0, 0
    # accrued = staked * rate_per_day * elapsed_days
    # Doing this in raw space: staked_raw is already scaled by 10**18, the
    # rate is dimensionless, divide by SECS_PER_DAY at the end.
    rate_raw = to_raw(fc.LURE_STAKE_REEL_PER_DAY)
    accrued_raw = (staked_raw * rate_raw * elapsed) // (to_raw(1.0) * 86400)
    return elapsed, int(accrued_raw)


async def accrued_stake_yield(
    db: Any, guild_id: int, user_id: int,
) -> int:
    """Read-only: how much REEL would be claimable right now (raw)."""
    state = await ensure_state(db, guild_id, user_id)
    staked = int(state.get("lure_staked_raw") or 0)
    pending = int(state.get("lure_yield_pending_raw") or 0)
    _, fresh = _accrue_pending(staked, state.get("last_stake_yield_at"))
    return pending + fresh


async def stake_lure(
    db: Any, guild_id: int, user_id: int, lure_amount_raw: int,
) -> StakeResult:
    """Move LURE from wallet -> stake. Crystallises any pending yield first.

    Crystallising on every write keeps the math simple: ``last_stake_yield_at``
    only ever measures uninterrupted accrual on the CURRENT staked balance.

    No minimum -- staking 1 LURE for a day is just as valid as staking
    a million. Yield scales linearly with the staked balance, so dust
    stakes earn dust REEL and that is fine.
    """
    if lure_amount_raw <= 0:
        raise ValueError("Amount must be positive.")

    state = await ensure_state(db, guild_id, user_id)
    cur_staked = int(state.get("lure_staked_raw") or 0)
    pending = int(state.get("lure_yield_pending_raw") or 0)
    _, fresh = _accrue_pending(cur_staked, state.get("last_stake_yield_at"))
    new_pending = pending + fresh

    # Burn LURE from wallet (raises ValueError on insufficient).
    await db.update_wallet_holding(
        user_id, guild_id,
        fc.LURE_NETWORK_SHORT, fc.LURE_SYMBOL,
        -int(lure_amount_raw),
    )
    new_staked = cur_staked + int(lure_amount_raw)
    await db.execute(
        """
        UPDATE user_fishing
           SET lure_staked_raw         = $3::numeric,
               lure_yield_pending_raw  = $4::numeric,
               last_stake_yield_at     = NOW(),
               updated_at              = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(new_staked), int(new_pending),
    )
    return StakeResult(
        lure_staked_raw=int(new_staked),
        lure_delta_raw=int(lure_amount_raw),
        reel_yield_paid_raw=0,
        pending_reel_raw=int(new_pending),
    )


async def claim_stake_yield(
    db: Any, guild_id: int, user_id: int,
) -> StakeResult:
    """Pay out accrued REEL to the user's wallet. Stake stays locked.

    Resets the accrual clock to NOW(). Returns the post-op stake balance
    plus the REEL paid out (so the cog can render a receipt).
    """
    state = await ensure_state(db, guild_id, user_id)
    cur_staked = int(state.get("lure_staked_raw") or 0)
    pending = int(state.get("lure_yield_pending_raw") or 0)
    _, fresh = _accrue_pending(cur_staked, state.get("last_stake_yield_at"))
    payout = pending + fresh
    if payout <= 0:
        raise ValueError(
            "No REEL has accrued yet. Try again after some time has passed."
        )

    await db.update_wallet_holding(
        user_id, guild_id,
        fc.LURE_NETWORK_SHORT, fc.REEL_SYMBOL,
        int(payout),
    )
    await db.execute(
        """
        UPDATE user_fishing
           SET lure_yield_pending_raw  = 0,
               last_stake_yield_at     = NOW(),
               total_reel_earned_raw   = total_reel_earned_raw + $3::numeric,
               updated_at              = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(payout),
    )
    return StakeResult(
        lure_staked_raw=int(cur_staked),
        lure_delta_raw=0,
        reel_yield_paid_raw=int(payout),
        pending_reel_raw=0,
    )


async def unstake_lure(
    db: Any, guild_id: int, user_id: int, lure_amount_raw: int,
) -> StakeResult:
    """Move LURE from stake -> wallet. Crystallises and pays accrued REEL.

    ``lure_amount_raw`` capped at the user's current staked balance so the
    cog can pass a sentinel like ``2**62`` to mean "all of it". Always pays
    out any accrued REEL alongside the unlocked LURE so the user never
    loses pending yield by unstaking.
    """
    state = await ensure_state(db, guild_id, user_id)
    cur_staked = int(state.get("lure_staked_raw") or 0)
    pending = int(state.get("lure_yield_pending_raw") or 0)
    _, fresh = _accrue_pending(cur_staked, state.get("last_stake_yield_at"))
    payout = pending + fresh

    requested = max(0, int(lure_amount_raw))
    if cur_staked <= 0 or requested <= 0:
        raise ValueError("You have no LURE staked.")
    actual = min(requested, cur_staked)
    new_staked = cur_staked - actual

    # Credit unlocked LURE first; if that fails, the row stays unchanged.
    await db.update_wallet_holding(
        user_id, guild_id,
        fc.LURE_NETWORK_SHORT, fc.LURE_SYMBOL,
        int(actual),
    )
    if payout > 0:
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                fc.LURE_NETWORK_SHORT, fc.REEL_SYMBOL,
                int(payout),
            )
        except Exception:
            log.exception("unstake_lure: REEL yield payout failed uid=%s gid=%s",
                          user_id, guild_id)
            payout = 0  # don't credit ledger if the wallet write failed
    await db.execute(
        """
        UPDATE user_fishing
           SET lure_staked_raw         = $3::numeric,
               lure_yield_pending_raw  = 0,
               last_stake_yield_at     = NOW(),
               total_reel_earned_raw   = total_reel_earned_raw + $4::numeric,
               updated_at              = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(new_staked), int(payout),
    )
    return StakeResult(
        lure_staked_raw=int(new_staked),
        lure_delta_raw=-int(actual),
        reel_yield_paid_raw=int(payout),
        pending_reel_raw=0,
    )


async def cashout_reel(
    db: Any, guild_id: int, user_id: int, reel_amount_raw: int,
) -> CashoutResult:
    """Burn REEL, push the REEL oracle DOWN, credit users.wallet with USD.

    Identical mechanics to ``cogs/trade.py .sell``: the full quantity
    leaves the user's wallet (decrementing crypto_prices.circulating_supply
    via update_wallet_holding, which IS the burn), the standard
    Config.PRICE_IMPACT_DIVISOR formula computes a downward price impact,
    and the user receives USD at the post-impact REEL oracle price. The
    chart picks up the new oracle on the next refresh because
    ``crypto_prices.update_price`` is the same row .sell writes.

    No fixed haircut, no minimum -- the slippage IS the fee, and dust
    cashouts get dust slippage.
    """
    if reel_amount_raw <= 0:
        raise ValueError("Amount must be positive.")

    held = await get_reel_wallet_raw(db, guild_id, user_id)
    if held < int(reel_amount_raw):
        raise ValueError(f"You only have {to_human(held):,.4f} REEL.")

    reel_oracle_before = await _oracle_price(db, guild_id, fc.REEL_SYMBOL)
    if reel_oracle_before <= 0:
        raise ValueError("REEL oracle price is currently zero -- try again later.")

    reel_human = to_human(int(reel_amount_raw))
    revenue_usd = reel_human * reel_oracle_before

    # Pull supply for the same impact formula .sell uses.
    row = await db.fetch_one(
        "SELECT circulating_supply FROM crypto_prices "
        "WHERE guild_id = $1 AND symbol = $2",
        int(guild_id), fc.REEL_SYMBOL,
    )
    supply_human = to_human(int((row or {}).get("circulating_supply") or 0))
    impact = _price_impact(revenue_usd, reel_oracle_before, supply_human)

    # Effective sell price (post-impact). User receives USD at the
    # average between pre-impact and post-impact oracle, identical to
    # how .sell computes net revenue.
    eff_price = reel_oracle_before * (1.0 - impact / 2.0)
    usd_credit_human = reel_human * eff_price

    # Group Industry bonus: members of a group that bought Angler's Dock
    # (or any upgrade that grants member_fishing_bonus) earn the bonus on
    # every cashout, anywhere. Applied BEFORE rounding to raw so the
    # bonus is preserved at low precision.
    try:
        from services.group_reserve import member_activity_bonus
        _fishing_bonus = await member_activity_bonus(db, guild_id, user_id, "fishing")
    except Exception:
        log.debug("group fishing bonus probe failed", exc_info=True)
        _fishing_bonus = 0.0
    if _fishing_bonus > 0:
        usd_credit_human *= (1.0 + _fishing_bonus)

    usd_credit_raw = to_raw(usd_credit_human)

    # Burn first; refund on credit failure.
    await db.update_wallet_holding(
        user_id, guild_id,
        fc.LURE_NETWORK_SHORT, fc.REEL_SYMBOL,
        -int(reel_amount_raw),
    )
    if usd_credit_raw > 0:
        try:
            await db.update_wallet(user_id, guild_id, int(usd_credit_raw))
        except Exception:
            try:
                await db.update_wallet_holding(
                    user_id, guild_id,
                    fc.LURE_NETWORK_SHORT, fc.REEL_SYMBOL,
                    int(reel_amount_raw),
                )
            except Exception:
                log.exception("cashout_reel: REEL refund failed uid=%s gid=%s amt=%s",
                              user_id, guild_id, reel_amount_raw)
            raise

    # Push the oracle DOWN. Same call .sell makes; the chart's drift loop
    # reads from this row.
    reel_oracle_after = max(1e-9, reel_oracle_before * (1.0 - impact))
    try:
        await db.update_price(fc.REEL_SYMBOL, guild_id, reel_oracle_after)
    except Exception:
        log.exception(
            "cashout_reel: oracle update failed gid=%s -- chart will lag "
            "until the next drift tick", guild_id,
        )

    # Extend the REEL/USD candle in the same minute so the chart shows
    # the cashout drop without waiting for the drift task. Volume = the
    # USD value sold (revenue_usd, NOT usd_credit -- the volume is the
    # gross size of the burn, not the post-slippage receipt).
    await _write_burn_candle(
        db, guild_id, fc.REEL_SYMBOL,
        reel_oracle_before, reel_oracle_after, revenue_usd,
    )

    # Distribute the LP-reward slice of this cashout to LP holders of
    # any REEL pool. Cashout is one-sided (only REEL moved), so the
    # entire fee goes to REEL pools.
    fee_usd = revenue_usd * (int(fc.GEAR_BURN_LP_REWARD_BPS) / 10_000.0)
    lp_paid = 0.0
    if fee_usd > 0:
        lp_paid = await _distribute_burn_lp_reward(
            db, guild_id, fc.REEL_SYMBOL, fee_usd,
        )

    # Group reserve tribute: system-funded grant on the gross USD value of
    # the cashout. No deduction from the user's payout. Failures are
    # logged inside the helper and never raise.
    try:
        from services.group_reserve import tribute_from_activity
        await tribute_from_activity(
            db, guild_id, user_id, float(revenue_usd), "fishing",
        )
    except Exception:
        log.debug("group fishing tribute failed", exc_info=True)

    await db.execute(
        """
        UPDATE user_fishing
           SET total_usd_cashout_raw = total_usd_cashout_raw + $3::numeric,
               updated_at            = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(usd_credit_raw),
    )
    return CashoutResult(
        reel_burned_raw=int(reel_amount_raw),
        usd_credited_raw=int(usd_credit_raw),
        reel_oracle_before=float(reel_oracle_before),
        reel_oracle_after=float(reel_oracle_after),
        price_impact_pct=float(impact),
        revenue_usd=float(revenue_usd),
        lp_reward_usd=float(lp_paid),
    )


# === ECONOMY_END ===

# === WILD_BATTLE_START ===
# ==========================================================================
# Wild-buddy battle resolution
# ==========================================================================
# The cast_resolve path returns ``outcome='wild_battle'`` and a payload
# describing the synthesised opponent. The cog renders a Challenge prompt,
# invokes ``services.buddy_battle.run_battle`` against the user's active
# buddy, and calls back into ``resolve_wild_battle`` with the engine
# result. Everything that touches the DB (LURE reward, lifetime counters,
# capture roll) lives here so the cog stays presentation-only and the
# state machinery is easy to test.

@dataclass
class WildBattleResolution:
    won: bool
    captured_species: str | None    # populated when capture roll hit
    lure_reward_raw: int            # LURE credited on a win (post-bonus)
    reel_reward_raw: int            # REEL kicker credited on a win (post-bonus)
    new_won_total: int              # lifetime wild_battles_won AFTER this row
    new_lost_total: int             # lifetime wild_battles_lost AFTER this row
    new_capture_total: int          # lifetime wild_buddies_captured AFTER this row
    buddy_row: dict | None          # set when a capture hatched a buddy in cc_buddies
    # Set when the capture rolled but the player's shelter was full, so
    # the wild buddy's egg landed in user_fishing.held_eggs instead.
    # Either ``buddy_row`` or ``stored_egg`` is set on a capture, never
    # both. Cog branches on which is populated to render the copy.
    stored_egg: dict | None = None
    # Decimal performance bonus applied to the LURE / REEL base rewards.
    # 0.30 means "+30%". Cog adds this to the receipt so the player sees
    # WHY the haul was bigger than the floor (clean fight bonuses).
    bonus_pct_applied: float = 0.0
    # Loot drop awarded on top of LURE/REEL when the random roll hit.
    # Shape: ``{"kind": "treasure_map"|"magic_bait"|"chum_bait"|"wild_egg",
    #            "qty": int, "label": str}``. None when no loot rolled
    # (the common case -- ~5% chance per win).
    loot_dropped: dict | None = None
    # XP credited to the active buddy on a win (mirrors PvP / chat-XP path).
    # 0 on a loss. ``fighter_buddy_id`` is the cc_buddies.id that took the
    # XP so the cog can render "Sparky gained +N XP" in the receipt.
    buddy_xp_awarded: int = 0
    fighter_buddy_id: int | None = None
    # True when the post-battle capture roll succeeded but the player
    # was already at their owned-buddy cap, so the wild buddy was
    # released. The cog renders an explicit "almost!" line so players
    # know to free a slot (store / surrender / buy slot) before the
    # next wild battle. Mutually exclusive with ``buddy_row`` set.
    capture_refused_full: bool = False
    # Owned-buddy cap at the time of the capture roll. Surfaced
    # alongside ``capture_refused_full`` so the receipt can say
    # "you're at 5/5".
    owned_cap: int = 0


async def resolve_wild_battle(
    db: Any, guild_id: int, user_id: int,
    *, won: bool, zone: str, opponent_species: str | None = None,
    opponent_level: int = 1,
    opponent_rarity_tier: int = 1,
    bonus_pct: float = 0.0,
    loot_drop: dict | None = None,
    capture_message_id: int | None = None,
    capture_channel_id: int | None = None,
    skip_capture_roll: bool = False,
) -> WildBattleResolution:
    """Persist the outcome of a wild-buddy battle.

    ``won=False`` just bumps the loss counter -- no penalty, no payout.
    ``won=True`` rolls a LURE reward via ``fc.wild_battle_lure_reward``
    plus a REEL kicker via ``fc.wild_battle_reel_reward`` (both scale
    with zone tier), credits each to the user's wallet_holdings, rolls
    a capture chance, and -- on capture -- routes through the existing
    ``hatch_fishing_buddy`` path so the buddy spawn respects
    BUDDY_EGG_DAILY_CAP and the standard hatch limits.

    Both currencies are MINTED for the win (no oracle move) on the same
    code path stake yield uses, so the chart accounting stays uniform
    with the rest of the cog and the receipt's ``no oracle impact``
    footer is accurate.

    ``bonus_pct`` is a decimal multiplier added to BOTH base rewards
    (0.30 == "+30%"). The cog computes this from the interactive battle
    performance (clean win, low rounds, action variety) and passes it
    in so the receipt can show why the haul was bigger than the floor.

    ``loot_drop`` is an optional bonus drop awarded on a win (rolled
    by the cog ~5% of the time). Shape: ``{"kind": ..., "qty": ...}``;
    see ``_apply_wild_loot_drop`` for the supported kinds. The actual
    inventory write happens here so the loot lands in the SAME txn as
    the LURE/REEL credit.

    All counters are read back AFTER the UPDATE so the receipt the cog
    prints reflects the post-write state.
    """
    state = await ensure_state(db, guild_id, user_id)
    zone_md = fc.zone_meta(zone) or {}
    zone_tier = int(zone_md.get("tier") or 1)

    captured = False
    captured_species: str | None = None
    buddy_row: dict | None = None
    stored_egg: dict | None = None
    lure_reward_raw = 0
    reel_reward_raw = 0
    loot_dropped_dict: dict | None = None
    buddy_xp_awarded = 0
    fighter_buddy_id: int | None = None
    capture_refused_full = False
    owned_cap_snapshot = 0
    bonus_mult = 1.0 + max(0.0, float(bonus_pct))

    if won:
        # Bonus multiplier scales BOTH currencies so a clean fight pays
        # more LURE AND more REEL. The base rolls themselves still vary
        # across calls, so two clean wins still differ -- the bonus just
        # raises the floor.
        reward_lure = fc.wild_battle_lure_reward(zone_tier) * bonus_mult
        lure_reward_raw = to_raw(reward_lure)
        # Credit LURE via the same wallet_holding path that fish sales
        # use so circulating_supply / chart accounting stays uniform.
        try:
            await db.update_wallet_holding(
                user_id, guild_id,
                fc.LURE_NETWORK_SHORT, fc.LURE_SYMBOL,
                int(lure_reward_raw),
            )
            await db.execute(
                """
                UPDATE user_fishing
                   SET total_lure_earned_raw = total_lure_earned_raw + $3::numeric,
                       updated_at = NOW()
                 WHERE guild_id = $1 AND user_id = $2
                """,
                guild_id, user_id, int(lure_reward_raw),
            )
        except Exception:
            log.exception("resolve_wild_battle: LURE reward credit failed "
                          "uid=%s gid=%s amt=%s", user_id, guild_id, lure_reward_raw)
            lure_reward_raw = 0  # don't show a phantom credit on the receipt

        # REEL kicker. Independent try/except so a REEL credit failure
        # doesn't roll back the LURE the user already saw, and vice
        # versa. Same wallet_holding mint path as stake yield -- no
        # oracle update, just supply expansion + balance credit.
        reward_reel = fc.wild_battle_reel_reward(zone_tier) * bonus_mult
        reel_reward_raw = to_raw(reward_reel) if reward_reel > 0 else 0
        if reel_reward_raw > 0:
            try:
                await db.update_wallet_holding(
                    user_id, guild_id,
                    fc.LURE_NETWORK_SHORT, fc.REEL_SYMBOL,
                    int(reel_reward_raw),
                )
                # total_reel_earned_raw is the lifetime REEL counter that
                # already tracks stake-yield mints; wild-battle REEL goes
                # in the same column so the analytics stay coherent.
                await db.execute(
                    """
                    UPDATE user_fishing
                       SET total_reel_earned_raw = total_reel_earned_raw + $3::numeric,
                           updated_at = NOW()
                     WHERE guild_id = $1 AND user_id = $2
                    """,
                    guild_id, user_id, int(reel_reward_raw),
                )
            except Exception:
                log.exception("resolve_wild_battle: REEL kicker credit failed "
                              "uid=%s gid=%s amt=%s", user_id, guild_id, reel_reward_raw)
                reel_reward_raw = 0

        # BBT (Buddy Battle Token) -- universal battle reward minted on
        # every wild win across the bot. Scales with zone_tier the same
        # way the LURE/REEL prizes do; bonus_mult applies for clean
        # fights. Failure here doesn't roll back the LURE / REEL
        # rewards the user already saw -- BBT is best-effort additive.
        try:
            from services import buddy_economy as _bes
            bbt_amount = (1.0 + 0.5 * max(0, int(zone_tier) - 1)) * bonus_mult
            await _bes.mint_bbt_reward(
                db, guild_id, user_id, float(bbt_amount), source="fish_wild",
            )
        except Exception:
            log.exception(
                "resolve_wild_battle: BBT mint failed uid=%s gid=%s",
                user_id, guild_id,
            )

        # Active-buddy XP. The player's buddy actually fought the wild
        # encounter; pre-fix the only reward to its row was the win
        # counter / mood. Now we credit XP via the canonical
        # award_battle_xp helper so level / panel / decay stays aligned
        # with PvP and the chat-XP path.
        try:
            from services.buddy_battle import award_battle_xp as _award_bxp
            active = await db.fetch_one(
                "SELECT id FROM cc_buddies "
                "WHERE guild_id = $1 AND owner_user_id = $2 "
                "  AND status = 'owned' AND is_active "
                "LIMIT 1",
                int(guild_id), int(user_id),
            )
            if active and int(active.get("id") or 0) > 0:
                # Battle-lane buddy multiplier on top of base XP.
                try:
                    from services.buddy_bonus import buddy_bonus as _bb
                    battle_mult = await _bb(
                        db, guild_id, user_id, lane="battle",
                    )
                except Exception:
                    battle_mult = 1.0
                xp_award = int(round(
                    fc.wild_battle_xp_reward(
                        int(zone_tier), int(opponent_rarity_tier or 1),
                    ) * bonus_mult * battle_mult
                ))
                if xp_award > 0:
                    await _award_bxp(
                        db, int(guild_id),
                        int(user_id), int(active["id"]),
                        int(xp_award),
                    )
                    buddy_xp_awarded = xp_award
                    fighter_buddy_id = int(active["id"])
        except Exception:
            log.exception(
                "resolve_wild_battle: buddy XP credit failed uid=%s gid=%s",
                user_id, guild_id,
            )

        # Capture roll. Wild captures use the OPPONENT'S actual species,
        # level, and rarity tier -- the wild buddy you just beat IS the
        # buddy you're capturing, not a fresh roll. If the player's
        # shelter is full the capture is just refused (the wild buddy
        # gets away); held-eggs are reserved for the buddy_egg roll on
        # a normal fishing cast, NOT for wild-battle captures.
        # ``skip_capture_roll`` short-circuits this when the cog already
        # inserted the cc_buddies row via the in-fight Capture button so
        # the manual + auto paths can never double-insert.
        if not skip_capture_roll and random.random() < fc.WILD_BATTLE_CAPTURE_CHANCE:
            try:
                # Capture-routing: battle slot first, else into
                # storage if there's room, else the capture is
                # silently refused (player keeps LURE/REEL win and
                # counters, just doesn't get the buddy). Both caps
                # respect the BUD-purchased upgrades.
                from services.buddy_economy import (
                    capture_destination as _dest,
                    user_max_battle_slots as _max_battle,
                )
                _cap = await _max_battle(db, guild_id, user_id)
                owned_cap_snapshot = int(_cap)
                _capture_dest = await _dest(db, guild_id, user_id)
                if _capture_dest is None:
                    capture_refused_full = True
                if _capture_dest is not None:
                    species_capture = str(opponent_species or "")
                    capture_status = (
                        "owned" if _capture_dest == "battle" else "stored"
                    )
                    if species_capture:
                        try:
                            from services.buddy_names import generate_name
                            new_name = await generate_name(
                                species_capture, db, guild_id,
                            )
                        except Exception:
                            new_name = species_capture.title()

                        # First-hatch log. Idempotent; a wild capture
                        # never overwrites the player's original
                        # first_species record.
                        try:
                            await db.execute(
                                """
                                INSERT INTO cc_buddy_hatches
                                  (guild_id, user_id, first_species)
                                VALUES ($1, $2, $3)
                                ON CONFLICT (guild_id, user_id) DO NOTHING
                                """,
                                guild_id, user_id, species_capture,
                            )
                        except Exception:
                            log.exception(
                                "resolve_wild_battle: cc_buddy_hatches "
                                "insert failed uid=%s gid=%s",
                                user_id, guild_id,
                            )

                        from configs.buddies_config import (
                            roll_gender as _roll_gender,
                            xp_for_level as _xp_for_level,
                        )
                        _cap_level = int(max(1, opponent_level))
                        new_row = await db.fetch_one(
                            """
                            INSERT INTO cc_buddies
                                (guild_id, owner_user_id, species, name,
                                 status, is_active, rarity_tier, level, xp,
                                 gender, capture_message_id,
                                 capture_channel_id)
                            VALUES ($1, $2, $3, $4, $5, FALSE, $6, $7, $8,
                                    $9, $10, $11)
                            RETURNING *
                            """,
                            guild_id, user_id, species_capture, new_name,
                            str(capture_status),
                            int(max(1, opponent_rarity_tier)),
                            _cap_level,
                            int(_xp_for_level(_cap_level)),
                            _roll_gender(),
                            int(capture_message_id) if capture_message_id else None,
                            int(capture_channel_id) if capture_channel_id else None,
                        )
                        if new_row:
                            captured = True
                            buddy_row = _normalize_state(new_row)
                            captured_species = species_capture
                            # NFT layer: mint a buddy token for the
                            # captured wild buddy. Best-effort.
                            try:
                                from services import items as _items
                                await _items.mint_unit(
                                    db,
                                    guild_id=guild_id,
                                    contract_address=_items.contract_address(
                                        "buddy", str(species_capture).lower(),
                                    ),
                                    owner_user_id=user_id,
                                    metadata={
                                        "species":     str(species_capture),
                                        "rarity_tier": int(
                                            max(1, opponent_rarity_tier),
                                        ),
                                        "level":       int(
                                            max(1, opponent_level),
                                        ),
                                        "gender":      str(
                                            new_row.get("gender") or "",
                                        ).upper(),
                                        "buddy_id":    int(new_row["id"]),
                                        "name":        str(new_name),
                                    },
                                    mint_source="fishing.wild_capture",
                                    source_table="cc_buddies",
                                    source_id=int(new_row["id"]),
                                )
                            except Exception:
                                log.debug(
                                    "nft wild_capture mint sync failed "
                                    "gid=%s uid=%s buddy=%s",
                                    guild_id, user_id, new_row.get("id"),
                                    exc_info=True,
                                )
            except Exception:
                log.exception(
                    "resolve_wild_battle: capture insert failed "
                    "uid=%s gid=%s", user_id, guild_id,
                )
                buddy_row = None

    # Counter bump. Single UPDATE returns the post-write totals so the
    # cog can print "Wild battle wins: N" without an extra read.
    row = await db.fetch_one(
        """
        UPDATE user_fishing
           SET wild_battles_won      = wild_battles_won
                                     + (CASE WHEN $3 THEN 1 ELSE 0 END),
               wild_battles_lost     = wild_battles_lost
                                     + (CASE WHEN $3 THEN 0 ELSE 1 END),
               wild_buddies_captured = wild_buddies_captured
                                     + (CASE WHEN $4 THEN 1 ELSE 0 END),
               updated_at            = NOW()
         WHERE guild_id = $1 AND user_id = $2
        RETURNING wild_battles_won, wild_battles_lost, wild_buddies_captured
        """,
        guild_id, user_id, bool(won), bool(captured),
    )
    # Bonus loot drop -- only fires on a win + when the cog passed a
    # loot_drop dict (random roll happens caller-side so the cog can
    # tune the rate per-fight without a service round-trip). Best-effort:
    # a loot-write failure is logged but doesn't roll back the LURE/REEL
    # the player already received.
    if won and loot_drop:
        try:
            loot_dropped_dict = await _apply_wild_loot_drop(
                db, guild_id, user_id, loot_drop,
            )
        except Exception:
            log.exception(
                "resolve_wild_battle: loot drop apply failed "
                "uid=%s gid=%s drop=%s", user_id, guild_id, loot_drop,
            )
            loot_dropped_dict = None

    return WildBattleResolution(
        won=bool(won),
        captured_species=captured_species,
        lure_reward_raw=int(lure_reward_raw),
        reel_reward_raw=int(reel_reward_raw),
        new_won_total=int((row or {}).get("wild_battles_won") or 0),
        new_lost_total=int((row or {}).get("wild_battles_lost") or 0),
        new_capture_total=int((row or {}).get("wild_buddies_captured") or 0),
        buddy_row=buddy_row,
        stored_egg=stored_egg,
        bonus_pct_applied=float(max(0.0, bonus_pct)),
        loot_dropped=loot_dropped_dict,
        buddy_xp_awarded=int(buddy_xp_awarded),
        fighter_buddy_id=fighter_buddy_id,
        capture_refused_full=bool(capture_refused_full),
        owned_cap=int(owned_cap_snapshot),
    )


async def _apply_wild_loot_drop(
    db: Any, guild_id: int, user_id: int, drop: dict,
) -> dict | None:
    """Apply a wild-battle bonus loot drop to the player's inventory.

    Supported drops (``drop["kind"]``):
      * "treasure_map"  -- adds 1 to junk_inventory["map"]; usable via
                           ,fish dig.
      * "magic_bait"    -- adds qty (default 5) to bait_inventory["magic"]
                           (capped by BAIT["magic"].max_stack).
      * "chum_bait"     -- same as magic_bait but for chum.
      * "wild_egg"      -- pops a fresh species/tier roll into
                           held_eggs (respects MAX_HELD_EGGS).

    Returns the populated drop dict (with ``label`` attached) on success
    so the cog can display "🎁 Bonus loot: ..." on the receipt, or
    ``None`` when the drop couldn't apply (e.g. stack full and no
    consolation logic). Inventory writes happen in a single UPDATE so
    a partial failure can never split the drop.
    """
    kind = str(drop.get("kind") or "")
    state = await ensure_state(db, guild_id, user_id)

    if kind == "treasure_map":
        junk_inv = _as_dict(state.get("junk_inventory"))
        junk_inv["map"] = int(junk_inv.get("map", 0)) + 1
        await db.execute(
            "UPDATE user_fishing SET junk_inventory = $3::jsonb, "
            "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(junk_inv),
        )
        return {"kind": kind, "qty": 1, "label": "Soggy Treasure Map"}

    if kind in ("magic_bait", "chum_bait"):
        bait_key = "magic" if kind == "magic_bait" else "chum"
        cfg = fc.BAIT.get(bait_key) or {}
        cap = int(cfg.get("max_stack") or 1_000_000)
        bait_inv = _as_dict(state.get("bait_inventory"))
        cur = int(bait_inv.get(bait_key, 0))
        want = int(drop.get("qty") or 5)
        actual = max(0, min(want, cap - cur))
        if actual <= 0:
            return None  # stack full, no consolation; cog skips the line
        bait_inv[bait_key] = cur + actual
        await db.execute(
            "UPDATE user_fishing SET bait_inventory = $3::jsonb, "
            "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(bait_inv),
        )
        return {
            "kind": kind, "qty": actual,
            "label": f"{actual}x {cfg.get('name', bait_key)}",
        }

    if kind == "wild_egg":
        # Skip if player already at the held-egg cap. Cog can fall back
        # to a different drop type next time.
        held = list(_as_list(state.get("held_eggs")))
        if len(held) >= int(fc.MAX_HELD_EGGS):
            return None
        try:
            from configs.buddies_config import roll_rarity, rarity_meta
            species = random.choice(fc.FISHING_BUDDY_SPECIES)
            tier = int(roll_rarity())
            rolled_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
            egg = {
                "species": species, "rarity_tier": tier,
                "rolled_at": rolled_at, "from": "wild_battle_loot",
            }
            held.append(egg)
            await db.execute(
                "UPDATE user_fishing SET held_eggs = $3::jsonb, "
                "total_eggs_laid = total_eggs_laid + 1, "
                "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id, _json(held),
            )
            # NFT layer: mint one egg token. Best-effort.
            try:
                from services import items as _items
                await _items.mint_unit(
                    db,
                    guild_id=guild_id,
                    contract_address=_items.contract_address("egg", species),
                    owner_user_id=user_id,
                    metadata={
                        "species":     species,
                        "rarity_tier": int(tier),
                        "rolled_at":   rolled_at,
                        "from":        "wild_battle_loot",
                    },
                    mint_source="fishing.wild_loot_egg",
                    source_table="user_fishing.held_eggs",
                    source_id=f"{user_id}:{species}:{rolled_at}",
                )
            except Exception:
                log.debug(
                    "nft wild_egg mint sync failed gid=%s uid=%s",
                    guild_id, user_id, exc_info=True,
                )
            tier_name = str(rarity_meta(tier).get("name") or "Common")
            return {
                "kind": kind, "qty": 1,
                "label": f"{tier_name} {species.title()} Egg",
                "egg":   egg,
            }
        except Exception:
            log.exception(
                "wild loot egg roll failed gid=%s uid=%s", guild_id, user_id,
            )
            return None

    return None


# === WILD_BATTLE_END ===


async def give_held_egg(
    db: Any,
    guild_id: int,
    user_id: int,
    *,
    species: str,
    rarity_tier: int,
    source: str = "external",
) -> tuple[bool, str | None]:
    """Push an egg into the player's held-egg slot.

    Centralised helper for any caller that wants to grant an egg through
    the standard hatch pipeline (e.g. daycare breeding, event rewards).
    Refuses when the player is at the ``MAX_HELD_EGGS`` cap so non-fishing
    callers don't have to reach into ``user_fishing`` directly.

    Eggs are genderless until they hatch, so this helper does not accept
    a gender argument; the gender is rolled at hatch time.

    Returns ``(ok, error_message)`` where ``ok=False`` means the slot was
    full (or persistence failed) and the caller should consolation-pay or
    queue a retry.
    """
    state = await ensure_state(db, guild_id, user_id)
    held = list(_as_list(state.get("held_eggs")))
    if len(held) >= int(fc.MAX_HELD_EGGS):
        return False, (
            f"Your held-egg slot is full ({fc.MAX_HELD_EGGS} eggs). "
            f"Hatch one with `,buddy hatch` first."
        )
    rolled_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    egg = {
        "species":     str(species or ""),
        "rarity_tier": int(rarity_tier or 1),
        "rolled_at":   rolled_at,
        "from":        str(source or "external"),
    }
    held.append(egg)
    try:
        await db.execute(
            "UPDATE user_fishing SET held_eggs = $3::jsonb, "
            "total_eggs_laid = total_eggs_laid + 1, "
            "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, _json(held),
        )
    except Exception:
        log.exception(
            "give_held_egg: persist failed gid=%s uid=%s source=%s",
            guild_id, user_id, source,
        )
        return False, "Egg grant failed -- please try again."
    # NFT layer: mint one egg token. Best-effort.
    try:
        from services import items as _items
        await _items.mint_unit(
            db,
            guild_id=guild_id,
            contract_address=_items.contract_address("egg", str(species).lower()),
            owner_user_id=user_id,
            metadata={
                "species":     str(species),
                "rarity_tier": int(rarity_tier or 1),
                "rolled_at":   rolled_at,
                "from":        str(source or "external"),
            },
            mint_source=f"give_held_egg.{source}",
            source_table="user_fishing.held_eggs",
            source_id=f"{user_id}:{species}:{rolled_at}",
        )
    except Exception:
        log.debug(
            "nft give_held_egg mint sync failed gid=%s uid=%s",
            guild_id, user_id, exc_info=True,
        )
    return True, None


# === LEADERBOARD_START ===
# ==========================================================================
# Leaderboards
# ==========================================================================

async def get_top_fishers(
    db: Any, guild_id: int, *, limit: int = 10,
) -> list[dict]:
    """Top players by lifetime LURE earned from fishing (sold + bonus payouts).

    Aliases ``total_lure_earned_raw`` to ``total_payout_raw`` so the cog and
    any external callers that still read the old field name keep working
    without breaking the source-of-truth column rename in migration 0135.
    """
    rows = await db.fetch_all(
        """
        SELECT user_id, total_caught, total_weight_lbs,
               total_lure_earned_raw,
               total_lure_earned_raw AS total_payout_raw,
               biggest_fish, biggest_lbs, longest_combo, fish_level
          FROM user_fishing
         WHERE guild_id = $1
           AND (total_caught > 0 OR total_lure_earned_raw > 0)
         ORDER BY total_lure_earned_raw DESC
         LIMIT $2
        """,
        guild_id, int(limit),
    )
    return [dict(r) for r in (rows or [])]


async def get_biggest_catches(
    db: Any, guild_id: int, *, limit: int = 10,
) -> list[dict]:
    """All-time biggest fish on this server (across all players)."""
    rows = await db.fetch_all(
        """
        SELECT catch_id, user_id, fish_key, rarity, weight_lbs,
               zone, rod_tier, caught_at
          FROM fishing_catches
         WHERE guild_id = $1 AND outcome = 'fish'
         ORDER BY weight_lbs DESC NULLS LAST
         LIMIT $2
        """,
        guild_id, int(limit),
    )
    return [dict(r) for r in (rows or [])]


async def get_user_recent(
    db: Any, guild_id: int, user_id: int, *, limit: int = 10,
) -> list[dict]:
    """Most recent catches for a single user (for ,fish history)."""
    rows = await db.fetch_all(
        """
        SELECT catch_id, outcome, fish_key, junk_key, rarity,
               weight_lbs, payout_raw, zone, caught_at
          FROM fishing_catches
         WHERE guild_id = $1 AND user_id = $2
         ORDER BY caught_at DESC
         LIMIT $3
        """,
        guild_id, user_id, int(limit),
    )
    return [dict(r) for r in (rows or [])]


async def get_recent_splashes(
    db: Any, guild_id: int, *, limit: int = 5,
) -> list[dict]:
    """Recent rare/epic/legendary catches for the AI lexicon block."""
    rows = await db.fetch_all(
        """
        SELECT catch_id, user_id, fish_key, rarity, weight_lbs, caught_at
          FROM fishing_catches
         WHERE guild_id = $1
           AND outcome = 'fish'
           AND rarity IN ('rare', 'epic', 'legendary')
         ORDER BY caught_at DESC
         LIMIT $2
        """,
        guild_id, int(limit),
    )
    return [dict(r) for r in (rows or [])]


# === LEADERBOARD_END ===

# === EVENTS_START ===
# ==========================================================================
# Bus events: fan-out into achievements / quests / challenges / seasons
# ==========================================================================
# Fishing publishes a small, well-typed set of events. The
# achievements / quests / challenges / seasons services subscribe
# explicitly so a config typo can't silently break a downstream.
#
# Event matrix:
#     fish_caught       -- any successful FISH outcome (junk + bonus excluded)
#     fish_legendary    -- legendary fish landed (subset of fish_caught)
#     fish_buddy_egg    -- a fishing buddy was hatched
#     fishing_outcome   -- low-level: every cast, including misses

EVENT_FISH_CAUGHT:        str = "fish_caught"
EVENT_FISH_LEGEND:        str = "fish_legendary"
EVENT_BUDDY_EGG:          str = "fish_buddy_egg"
EVENT_FISHING_ANY:        str = "fishing_outcome"
EVENT_LURE_SWAP:          str = "fish_lure_swap"
EVENT_LURE_STAKE:         str = "fish_lure_stake"
EVENT_REEL_CASHOUT:       str = "fish_reel_cashout"
# Wild-buddy battle bus events. Cog publishes the spawn on cast resolve;
# the win / loss / capture trio fire after run_battle returns. Achievements,
# quests, challenges, and the season pass all subscribe to these without
# any custom plumbing -- they share the same dispatcher as fish_caught.
EVENT_WILD_SPAWN:         str = "fish_wild_battle_spawn"
EVENT_WILD_WIN:           str = "fish_wild_battle_won"
EVENT_WILD_LOSS:          str = "fish_wild_battle_lost"
EVENT_WILD_CAPTURE:       str = "fish_wild_buddy_captured"


async def _publish_economy_event(
    bot: Any, *, event: str,
    guild_id: int, user_id: int, **payload: Any,
) -> None:
    """Helper: publish a Lure-economy bus event without blowing up callers.

    The cog drives swap / stake / cashout commands and must not 500 on a
    bus glitch -- the user already has the receipt of their action.
    """
    if not bot or not getattr(bot, "bus", None):
        return
    try:
        await bot.bus.publish(
            event,
            user_id=int(user_id), guild_id=int(guild_id),
            **payload,
        )
    except Exception:
        log.exception("fishing: bus publish %s failed", event)


async def fire_catch_events(bot: Any, guild: Any, user: Any, result: CastResult) -> None:
    """Publish all relevant bus events for a finished cast.

    Always publishes ``fishing_outcome``. Adds ``fish_caught`` for
    fish, ``fish_legendary`` for legendary pulls, and
    ``fish_buddy_egg`` when an egg actually spawned a buddy. The cog
    calls this AFTER ``cast_resolve`` so the DB writes are durable
    before any consumer ticks.
    """
    if not bot or not getattr(bot, "bus", None):
        return
    bus = bot.bus
    payload_base = {
        "user":      user,
        "guild":     guild,
        "user_id":   getattr(user, "id", user),
        "guild_id":  getattr(guild, "id", guild),
        "outcome":   result.outcome,
        "fish_key":  result.fish_key,
        "rarity":    result.rarity,
        "weight":    result.weight_lbs,
        # ``payout`` is the LURE amount credited at cast time (junk
        # salvage / money_bag / mystery_box). Subscribers in challenges /
        # quests / season-pass use it for "earn N LURE" style triggers.
        "payout":    result.payout_lure,
        "symbol":    fc.LURE_SYMBOL,
        "zone":      None,
    }
    try:
        await bus.publish(EVENT_FISHING_ANY, **payload_base)
    except Exception:
        log.exception("fishing: bus publish %s failed", EVENT_FISHING_ANY)
    if result.outcome == "fish":
        try:
            await bus.publish(EVENT_FISH_CAUGHT, **payload_base)
        except Exception:
            log.exception("fishing: bus publish %s failed", EVENT_FISH_CAUGHT)
        if result.rarity == "legendary":
            try:
                await bus.publish(EVENT_FISH_LEGEND, **payload_base)
            except Exception:
                log.exception("fishing: bus publish %s failed", EVENT_FISH_LEGEND)
    if result.outcome == "buddy_egg" and result.buddy_row:
        try:
            await bus.publish(EVENT_BUDDY_EGG, **payload_base)
        except Exception:
            log.exception("fishing: bus publish %s failed", EVENT_BUDDY_EGG)
    if result.outcome == "wild_battle" and result.wild_buddy:
        wb_payload = {
            **payload_base,
            "wild_species":     str(result.wild_buddy.get("species") or ""),
            "wild_level":       int(result.wild_buddy.get("level") or 1),
            "wild_rarity_tier": int(result.wild_buddy.get("rarity_tier") or 1),
        }
        try:
            await bus.publish(EVENT_WILD_SPAWN, **wb_payload)
        except Exception:
            log.exception("fishing: bus publish %s failed", EVENT_WILD_SPAWN)


_GEAR_SELL_RATE: float = 0.50


async def sell_rod(db: Any, guild_id: int, user_id: int) -> tuple[float, str, str]:
    """Sell the current rod (downgrade by one tier) for 50% of its REEL price.

    Returns ``(reel_refunded_human, sold_rod_name, new_rod_name)``.
    Raises ``ValueError`` if already on the starter rod.
    """
    state = await ensure_state(db, guild_id, user_id)
    cur_tier = int(state.get("rod_tier") or 0)
    if cur_tier <= 0:
        raise ValueError("You're already using the starter rod -- nothing to sell.")
    rod_meta = fc.RODS.get(cur_tier) or {}
    price = float(rod_meta.get("price_reel") or 0.0)
    refund = round(price * _GEAR_SELL_RATE, 4)
    new_tier = cur_tier - 1
    new_meta = fc.RODS.get(new_tier) or {}
    refund_raw = int(to_raw(refund))
    await db.execute(
        "UPDATE user_fishing SET rod_tier = $3, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, new_tier,
    )
    if refund_raw > 0:
        # REEL lives on the Lure Network. Two bugs were stacked here:
        # EARN_ONLY_TOKENS is a frozenset (no .get) so the lookup raised
        # "Frozenset object has no attribute get" the moment ,fish sell
        # <trap_key> ran, and the fallback "dsc" was the wrong network
        # short anyway -- wallet_holdings keys on "lur" (see
        # fishing_config.LURE_NETWORK_SHORT) and a "dsc" write would
        # silently bypass the player's REEL balance even if the call
        # had not crashed.
        await db.update_wallet_holding(
            user_id, guild_id, fc.LURE_NETWORK_SHORT, "REEL", refund_raw,
        )
    return refund, str(rod_meta.get("name") or f"Tier {cur_tier}"), str(new_meta.get("name") or "Twig Rod")


async def sell_trap(
    db: Any, guild_id: int, user_id: int, key: str, qty: int = 1,
) -> tuple[float, int]:
    """Sell crab traps from crab_trap_inventory for 50% of price_reel each.

    Returns ``(reel_refunded_human, qty_sold)``.
    Raises ``ValueError`` if the player doesn't own enough. ``qty`` is
    clamped to the owned count, so passing a very large number is the
    canonical "sell all of this trap type".
    """
    trap_meta = fc.crab_trap_meta(key)
    if not trap_meta:
        valid = ", ".join(f"`{k}`" for k in fc.CRAB_TRAPS)
        raise ValueError(f"Unknown trap `{key}`. Valid: {valid}")

    state = await ensure_state(db, guild_id, user_id)
    inv = _as_dict(state.get("crab_trap_inventory"))
    owned_qty = int(inv.get(key) or 0)
    if owned_qty <= 0:
        raise ValueError(f"You don't own any **{trap_meta['name']}**.")
    qty = min(qty, owned_qty)

    price = float(trap_meta.get("price_reel") or 0.0)
    refund = round(price * _GEAR_SELL_RATE * qty, 4)
    refund_raw = int(to_raw(refund))

    new_qty = owned_qty - qty
    if new_qty > 0:
        inv[key] = new_qty
    else:
        inv.pop(key, None)

    await db.execute(
        "UPDATE user_fishing SET crab_trap_inventory = $3::jsonb, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id, _json(inv),
    )
    if refund_raw > 0:
        # REEL lives on the Lure Network. Two bugs were stacked here:
        # EARN_ONLY_TOKENS is a frozenset (no .get) so the lookup raised
        # "Frozenset object has no attribute get" the moment ,fish sell
        # <trap_key> ran, and the fallback "dsc" was the wrong network
        # short anyway -- wallet_holdings keys on "lur" (see
        # fishing_config.LURE_NETWORK_SHORT) and a "dsc" write would
        # silently bypass the player's REEL balance even if the call
        # had not crashed.
        await db.update_wallet_holding(
            user_id, guild_id, fc.LURE_NETWORK_SHORT, "REEL", refund_raw,
        )
    return refund, qty


async def sell_all_traps(
    db: Any, guild_id: int, user_id: int,
) -> tuple[float, dict[str, int]]:
    """Sell every crab trap in the player's inventory at 50% REEL each.

    Returns ``(reel_refunded_total_human, sold_by_key)`` so the cog can
    render a per-trap breakdown in the receipt. Raises ``ValueError`` if
    the inventory is empty so the cog can surface a clean error.
    """
    state = await ensure_state(db, guild_id, user_id)
    inv = _as_dict(state.get("crab_trap_inventory"))
    sold: dict[str, int] = {}
    refund_total = 0.0
    for key, qty_v in list(inv.items()):
        try:
            qty_n = int(qty_v or 0)
        except (TypeError, ValueError):
            qty_n = 0
        if qty_n <= 0:
            continue
        meta = fc.crab_trap_meta(str(key)) or {}
        price = float(meta.get("price_reel") or 0.0)
        refund_total += price * _GEAR_SELL_RATE * qty_n
        sold[str(key)] = qty_n
    if not sold:
        raise ValueError("You don't own any crab traps to sell.")

    refund_total = round(refund_total, 4)
    refund_raw = int(to_raw(refund_total))
    await db.execute(
        "UPDATE user_fishing SET crab_trap_inventory = '{}'::jsonb, "
        "updated_at = NOW() WHERE guild_id = $1 AND user_id = $2",
        guild_id, user_id,
    )
    if refund_raw > 0:
        # REEL lives on the Lure Network. Two bugs were stacked here:
        # EARN_ONLY_TOKENS is a frozenset (no .get) so the lookup raised
        # "Frozenset object has no attribute get" the moment ,fish sell
        # <trap_key> ran, and the fallback "dsc" was the wrong network
        # short anyway -- wallet_holdings keys on "lur" (see
        # fishing_config.LURE_NETWORK_SHORT) and a "dsc" write would
        # silently bypass the player's REEL balance even if the call
        # had not crashed.
        await db.update_wallet_holding(
            user_id, guild_id, fc.LURE_NETWORK_SHORT, "REEL", refund_raw,
        )
    return refund_total, sold


def attach_listeners(bot: Any) -> None:
    """Wire fishing events into achievements / quests / challenges / seasons.

    Each downstream system already keeps a "trigger -> handler" map,
    so we just publish bus events with the same trigger labels the
    other services use ("fish_caught", "fish_legendary"). The
    downstream services have already attached their own listeners by
    the time this runs; we add fan-out helpers that turn the higher-
    level events into the correct ``progress_trigger`` / ``bump`` /
    ``grant_xp`` calls.
    """
    if not bot or not getattr(bot, "bus", None):
        return
    bus = bot.bus

    async def _on_fish_caught(**kw) -> None:
        uid = int(kw.get("user_id") or 0)
        gid = int(kw.get("guild_id") or 0)
        if not (uid and gid):
            return
        # Achievements: counter-style "catches" trigger.
        try:
            from services import achievements as _ach
            await _ach.bump(bot, uid, gid, "fish_caught")
        except Exception:
            log.debug("fishing: achievements.bump fish_caught failed", exc_info=True)
        # Quests: same trigger label, the quest catalog opts in.
        # NOTE: services.quests.progress_trigger takes (db, ...) NOT (bot, ...)
        # like the other two. Passing bot here used to silently fail (the
        # try/except below swallowed it) and the catch-fish daily quest
        # never advanced. Same fix applied across every fishing fan-out.
        try:
            from services import quests as _quests
            await _quests.progress_trigger(bot.db, uid, gid, "fish_caught")
        except Exception:
            log.debug("fishing: quests.progress_trigger fish_caught failed", exc_info=True)
        # Challenges: server-wide "catch N fish" challenge.
        try:
            from services import challenges as _ch
            await _ch.progress_trigger(bot, uid, gid, "fish_caught")
        except Exception:
            log.debug("fishing: challenges.progress_trigger fish_caught failed", exc_info=True)
        # Season pass XP is granted by seasons.py's own subscriber on
        # the same event; no direct grant_xp here or it double-counts.
        # Themed Tidestone XP: per landed catch + combo bonus. ``bot`` +
        # ``guild`` opt the grant into auto-levelup + ready-DM machinery.
        try:
            from services import themed_stones as _ts
            await _ts.grant_tidestone_xp(
                bot.db, uid, gid,
                landed=True,
                combo=int(kw.get("combo") or 0),
                bot=bot, guild=kw.get("guild"),
            )
        except Exception:
            log.debug(
                "fishing: themed_stones.grant_tidestone_xp failed",
                exc_info=True,
            )

    async def _on_fish_legendary(**kw) -> None:
        uid = int(kw.get("user_id") or 0)
        gid = int(kw.get("guild_id") or 0)
        if not (uid and gid):
            return
        try:
            from services import achievements as _ach
            await _ach.bump(bot, uid, gid, "fish_legendary")
        except Exception:
            log.debug("fishing: achievements.bump fish_legendary failed", exc_info=True)
        try:
            from services import quests as _quests
            await _quests.progress_trigger(bot.db, uid, gid, "fish_legendary")
        except Exception:
            log.debug("fishing: quests.progress_trigger fish_legendary failed", exc_info=True)
        try:
            from services import challenges as _ch
            await _ch.progress_trigger(bot, uid, gid, "fish_legendary")
        except Exception:
            log.debug("fishing: challenges.progress_trigger fish_legendary failed", exc_info=True)
        # Season pass XP is granted by seasons.py's own subscriber.
        # Themed Tidestone XP: legendary fish bonus on top of the regular
        # cast XP (which fired via _on_fish_caught for the same event).
        try:
            from services import themed_stones as _ts
            await _ts.grant_tidestone_xp(
                bot.db, uid, gid, legendary=True,
                bot=bot, guild=kw.get("guild"),
            )
        except Exception:
            log.debug(
                "fishing: themed_stones.grant_tidestone_xp legendary failed",
                exc_info=True,
            )

    async def _on_buddy_egg(**kw) -> None:
        uid = int(kw.get("user_id") or 0)
        gid = int(kw.get("guild_id") or 0)
        if not (uid and gid):
            return
        try:
            from services import achievements as _ach
            await _ach.bump(bot, uid, gid, "fish_buddy_egg")
        except Exception:
            log.debug("fishing: achievements.bump fish_buddy_egg failed", exc_info=True)
        try:
            from services import quests as _quests
            await _quests.progress_trigger(bot.db, uid, gid, "fish_buddy_egg")
        except Exception:
            log.debug("fishing: quests.progress_trigger fish_buddy_egg failed", exc_info=True)

    async def _fan_out(uid: int, gid: int, trigger: str) -> None:
        """Fan a single fishing trigger into achievements / quests / challenges.

        services.quests.progress_trigger takes ``db`` as its first arg
        while achievements.bump and challenges.progress_trigger take
        ``bot``. Passing the wrong one used to silently fail in the
        try/except below; fan-out now passes the right thing per call.
        """
        try:
            from services import achievements as _ach
            await _ach.bump(bot, uid, gid, trigger)
        except Exception:
            log.debug("fishing: achievements.bump %s failed", trigger, exc_info=True)
        try:
            from services import quests as _quests
            await _quests.progress_trigger(bot.db, uid, gid, trigger)
        except Exception:
            log.debug("fishing: quests.progress_trigger %s failed", trigger, exc_info=True)
        try:
            from services import challenges as _ch
            await _ch.progress_trigger(bot, uid, gid, trigger)
        except Exception:
            log.debug("fishing: challenges.progress_trigger %s failed", trigger, exc_info=True)

    async def _on_wild_spawn(**kw) -> None:
        uid = int(kw.get("user_id") or 0); gid = int(kw.get("guild_id") or 0)
        if uid and gid:
            await _fan_out(uid, gid, "fish_wild_battle_spawn")

    async def _on_wild_win(**kw) -> None:
        uid = int(kw.get("user_id") or 0); gid = int(kw.get("guild_id") or 0)
        if uid and gid:
            await _fan_out(uid, gid, "fish_wild_battle_won")

    async def _on_wild_loss(**kw) -> None:
        uid = int(kw.get("user_id") or 0); gid = int(kw.get("guild_id") or 0)
        if uid and gid:
            await _fan_out(uid, gid, "fish_wild_battle_lost")

    async def _on_wild_capture(**kw) -> None:
        uid = int(kw.get("user_id") or 0); gid = int(kw.get("guild_id") or 0)
        if uid and gid:
            await _fan_out(uid, gid, "fish_wild_buddy_captured")

    # Token-economy fan-out: every swap / stake / cashout was already
    # publishing its bus event, but the listener wiring for the three
    # economy events was missing -- so the quest "Cycle the Tackle"
    # (trigger fish_lure_swap), the "Banker" achievement (trigger
    # fish_lure_stake), and the "Cash Out" quest (fish_reel_cashout)
    # silently never credited. quests_config.py + achievements_config.py
    # + challenges.py all already opt into these triggers; we just
    # have to forward the events.
    async def _on_lure_swap(**kw) -> None:
        uid = int(kw.get("user_id") or 0); gid = int(kw.get("guild_id") or 0)
        if uid and gid:
            await _fan_out(uid, gid, "fish_lure_swap")

    async def _on_lure_stake(**kw) -> None:
        uid = int(kw.get("user_id") or 0); gid = int(kw.get("guild_id") or 0)
        if uid and gid:
            await _fan_out(uid, gid, "fish_lure_stake")

    async def _on_reel_cashout(**kw) -> None:
        uid = int(kw.get("user_id") or 0); gid = int(kw.get("guild_id") or 0)
        if uid and gid:
            await _fan_out(uid, gid, "fish_reel_cashout")

    bus.subscribe(EVENT_FISH_CAUGHT,  _on_fish_caught)
    bus.subscribe(EVENT_FISH_LEGEND,  _on_fish_legendary)
    bus.subscribe(EVENT_BUDDY_EGG,    _on_buddy_egg)
    bus.subscribe(EVENT_WILD_SPAWN,   _on_wild_spawn)
    bus.subscribe(EVENT_WILD_WIN,     _on_wild_win)
    bus.subscribe(EVENT_WILD_LOSS,    _on_wild_loss)
    bus.subscribe(EVENT_WILD_CAPTURE, _on_wild_capture)
    bus.subscribe(EVENT_LURE_SWAP,    _on_lure_swap)
    bus.subscribe(EVENT_LURE_STAKE,   _on_lure_stake)
    bus.subscribe(EVENT_REEL_CASHOUT, _on_reel_cashout)


# === EVENTS_END ===
