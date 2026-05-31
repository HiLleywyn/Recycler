"""services/crafting.py -- service layer for the Forge minigame on the Forge
Network. Mirrors services/farming.py (HRV/SEED) and services/fishing.py
(REEL/LURE) for the INGOT / FORGE / FGD economy.

INGOT is the earn-only token minted by ,craft make. FORGE is the network coin
(swappable, oracle-priced). FGD is the network's stablecoin and is debited
from the user's wallet_holdings as the per-recipe crafting fee. The token
firewall is identical to the Lure / Crypt / Buddy / Harvest networks:

- INGOT -> FORGE  via burn_ingot_for_forge (1:1 USD value at oracle, slippage)
- FORGE -> USD    via cashout_forge        (one-way burn, slippage on oracle)
- FORGE <-> {REEL, RUNE, BUD, HRV, INGOT} via the FORGE_SWAPPABLE carve-out
- INGOT has no AMM pool (earn-only-out)

Public API expected by cogs/crafting.py:
- ensure_state, get_state
- list_recipes, recipe_info
- craft_item             -- consume inputs from each game's inventory, mint INGOT
- apply_item             -- route crafted output back into source game's inventory
- inventory_summary      -- crafted_inventory rendered for ,craft bag
- burn_ingot_for_forge   -- INGOT -> FORGE with slippage
- cashout_forge          -- FORGE -> USD with slippage
- accrued_stake_yield, stake_ingot, claim_stake_yield, unstake_ingot
- get_top_crafters, get_user_crafts
"""
from __future__ import annotations

import json as _json_mod
import logging
import random
from dataclasses import dataclass
from typing import Any

import configs.crafting_config as cc
import configs.dungeon_config as dc
from core.config import Config
from core.framework.scale import to_human, to_raw
from services.fishing import (
    _distribute_burn_lp_reward,
    _price_impact,
    _write_burn_candle,
    _oracle_price,
)

log = logging.getLogger(__name__)


# ── JSON / state helpers (mirror services/farming.py) ──────────────────────

def _as_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = _json_mod.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _normalize_state(row: dict) -> dict:
    """Return ``row`` with JSONB columns parsed and missing keys defaulted.

    The row is mutated in place AND returned so callers can chain.
    """
    if not row:
        return {}
    row["crafted_inventory"] = _as_dict(row.get("crafted_inventory"))
    return row


# ── Result dataclasses ──────────────────────────────────────────────────────

@dataclass(slots=True)
class CraftResult:
    craft_key:        str
    qty:              int
    rarity:           str
    ingot_minted_raw: int
    fgd_spent_raw:    int
    xp_gained:        int
    new_level:        int
    leveled_up:       bool
    # Per-specialty progression. ``specialty`` is "" when the recipe didn't
    # declare one (legacy / catch-all). ``specialty_leveled_up`` lets the
    # cog publish a ``specialty_level_up`` bus event without re-querying.
    specialty:                str  = ""
    specialty_old_level:      int  = 1
    specialty_new_level:      int  = 1
    specialty_leveled_up:     bool = False


@dataclass(slots=True)
class BurnResult:
    burned_ingot_raw: int
    minted_forge_raw: int
    impact_pct:       float


@dataclass(slots=True)
class CashoutResult:
    burned_forge_raw: int
    usd_credit_raw:   int
    impact_pct:       float


# ── State load / ensure ─────────────────────────────────────────────────────

async def ensure_state(db: Any, guild_id: int, user_id: int) -> dict:
    """Insert a fresh row into ``user_crafting`` if one does not exist; return
    the row (with JSONB columns normalized).
    """
    await db.execute(
        """
        INSERT INTO user_crafting (guild_id, user_id)
        VALUES ($1, $2)
        ON CONFLICT (guild_id, user_id) DO NOTHING
        """,
        int(guild_id), int(user_id),
    )
    return await get_state(db, guild_id, user_id)


async def get_state(db: Any, guild_id: int, user_id: int) -> dict:
    row = await db.fetch_one(
        "SELECT * FROM user_crafting WHERE guild_id = $1 AND user_id = $2",
        int(guild_id), int(user_id),
    )
    return _normalize_state(dict(row)) if row else {}


def list_recipes(level: int) -> list[tuple[str, dict]]:
    """Wrapper around :func:`crafting_config.recipes_at_level` so the cog
    can stay decoupled from the config module.
    """
    return cc.recipes_at_level(int(level))


def recipe_info(craft_key: str) -> dict | None:
    return cc.craft_meta(craft_key)


# ── Specialty selection (pick-2) ────────────────────────────────────────────


async def add_specialty(
    db: Any, guild_id: int, user_id: int, specialty: str,
) -> tuple[list[str], str]:
    """Add ``specialty`` to the user's active set.

    Returns ``(new_active_list, message)``. Raises ``ValueError`` when the
    specialty key is unknown, the user is already at the
    ``ACTIVE_SPECIALTY_CAP`` limit, or the specialty is already active.
    """
    spec = str(specialty or "").strip().lower()
    if spec not in cc.SPECIALTIES:
        raise ValueError(
            f"Unknown specialty `{spec}`. Pick one of: "
            f"{', '.join(cc.SPECIALTIES)}."
        )
    state = await ensure_state(db, guild_id, user_id)
    cur = list(state.get("active_specialties") or [])
    if spec in cur:
        raise ValueError(
            f"You already have **{spec.title()}** active."
        )
    extra = int(state.get("extra_specialty_slots") or 0)
    cap = cc.ACTIVE_SPECIALTY_CAP + extra
    if len(cur) >= cap:
        bonus = (
            f" (+{extra} purchased)"
            if extra > 0 else ""
        )
        raise ValueError(
            f"You're at the cap of **{cap}**{bonus} active "
            f"specialties ({', '.join(s.title() for s in cur)}). Drop "
            f"one with `,craft despecialize <key>` first, or buy an "
            f"extra slot with `,shop buy specialty_slot`."
        )
    new_set = cur + [spec]
    await db.execute(
        "UPDATE user_crafting SET active_specialties = $3::text[] "
        "WHERE guild_id = $1 AND user_id = $2",
        int(guild_id), int(user_id), new_set,
    )
    meta = cc.specialty_meta(spec) or {}
    return new_set, (
        f"{meta.get('emoji', '')} **{meta.get('name', spec.title())}** "
        f"added. You can now craft its locked recipes."
    )


async def remove_specialty(
    db: Any, guild_id: int, user_id: int, specialty: str,
) -> tuple[list[str], str]:
    """Drop ``specialty`` from the user's active set.

    Returns ``(new_active_list, message)``. Raises ``ValueError`` when
    the specialty isn't currently active.
    """
    spec = str(specialty or "").strip().lower()
    state = await ensure_state(db, guild_id, user_id)
    cur = list(state.get("active_specialties") or [])
    if spec not in cur:
        raise ValueError(
            f"**{spec.title()}** isn't one of your active specialties."
        )
    new_set = [s for s in cur if s != spec]
    await db.execute(
        "UPDATE user_crafting SET active_specialties = $3::text[] "
        "WHERE guild_id = $1 AND user_id = $2",
        int(guild_id), int(user_id), new_set,
    )
    meta = cc.specialty_meta(spec) or {}
    return new_set, (
        f"{meta.get('emoji', '')} **{meta.get('name', spec.title())}** "
        f"dropped. Locked recipes from this branch are no longer "
        f"craftable until you re-specialise."
    )


def all_recipes() -> list[tuple[str, dict]]:
    """Every recipe in the catalog, sorted by min_level then key. Used
    by the recipe book browser; bypasses the level gate so the player
    can see what they're working toward.
    """
    items = list(cc.CRAFT_ITEMS.items())
    items.sort(
        key=lambda kv: (
            int(kv[1].get("min_level", 1)),
            str(kv[0]),
        ),
    )
    return items


# ── Ingredient resolution ───────────────────────────────────────────────────
#
# Each recipe input has a kind/sub pair (e.g. ``fish/bass``, ``crop/wheat``,
# ``ore/COPPER``). The check helpers READ the source-of-truth column for that
# kind and assert availability; the consume helpers MUTATE it. Both are pure
# DB calls; cogs/crafting.py wraps the whole thing in a single transaction
# via ``async with db.atomic()`` so a partial failure rolls back every game.

async def _check_fish(db, gid: int, uid: int, fish_key: str, qty: int) -> bool:
    row = await db.fetch_one(
        "SELECT fish_inventory FROM user_fishing "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    if not row:
        return False
    inv = _as_dict(row.get("fish_inventory"))
    catches = inv.get(fish_key) or []
    return len(catches) >= int(qty)


async def _consume_fish(db, gid: int, uid: int, fish_key: str, qty: int) -> None:
    """Pop ``qty`` smallest catches of ``fish_key`` from the user's
    fish_inventory. Smallest-first so the player keeps their trophies.
    """
    row = await db.fetch_one(
        "SELECT fish_inventory FROM user_fishing "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    if not row:
        raise ValueError(f"You don't have any {fish_key}.")
    inv = _as_dict(row.get("fish_inventory"))
    catches = list(inv.get(fish_key) or [])
    if len(catches) < int(qty):
        raise ValueError(f"You only have {len(catches)} {fish_key}.")
    catches.sort(key=lambda c: float((c or {}).get("lbs") or 0.0))
    catches = catches[int(qty):]
    if catches:
        inv[fish_key] = catches
    else:
        inv.pop(fish_key, None)
    await db.execute(
        "UPDATE user_fishing SET fish_inventory = $3::jsonb "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid), _json_mod.dumps(inv),
    )


async def _check_crop(db, gid: int, uid: int, crop_key: str, qty: int) -> bool:
    row = await db.fetch_one(
        "SELECT crop_inventory FROM user_farming "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    if not row:
        return False
    inv = _as_dict(row.get("crop_inventory"))
    return int(inv.get(crop_key) or 0) >= int(qty)


async def _consume_crop(db, gid: int, uid: int, crop_key: str, qty: int) -> None:
    row = await db.fetch_one(
        "SELECT crop_inventory FROM user_farming "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    if not row:
        raise ValueError(f"You don't have any {crop_key}.")
    inv = _as_dict(row.get("crop_inventory"))
    have = int(inv.get(crop_key) or 0)
    if have < int(qty):
        raise ValueError(f"You only have {have} {crop_key}.")
    new = have - int(qty)
    if new > 0:
        inv[crop_key] = new
    else:
        inv.pop(crop_key, None)
    await db.execute(
        "UPDATE user_farming SET crop_inventory = $3::jsonb "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid), _json_mod.dumps(inv),
    )


async def _check_ore(db, gid: int, uid: int, sym: str, qty_human: int) -> bool:
    """Ore lives in user_dungeon as raw NUMERIC columns (copper_staked_raw,
    silver_staked_raw, gold_staked_raw). The dungeon convention is that the
    "_staked" suffix names the ACTIVE balance, not a stake position -- it's
    the user's redeemable ore. Match the column to the symbol.
    """
    sym = sym.upper()
    col = {
        "COPPER": "copper_staked_raw",
        "SILVER": "silver_staked_raw",
        "GOLD":   "gold_staked_raw",
    }.get(sym)
    if not col:
        return False
    row = await db.fetch_one(
        f"SELECT {col} AS amt FROM user_dungeon "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    if not row:
        return False
    held = to_human(int(row.get("amt") or 0))
    return held >= float(qty_human)


async def _consume_ore(db, gid: int, uid: int, sym: str, qty_human: int) -> None:
    sym = sym.upper()
    col = {
        "COPPER": "copper_staked_raw",
        "SILVER": "silver_staked_raw",
        "GOLD":   "gold_staked_raw",
    }.get(sym)
    if not col:
        raise ValueError(f"Unknown ore symbol: {sym}")
    burn_raw = to_raw(float(qty_human))
    res = await db.execute(
        f"UPDATE user_dungeon "
        f"SET {col} = {col} - $3::numeric "
        f"WHERE guild_id = $1 AND user_id = $2 "
        f"AND {col} >= $3::numeric",
        int(gid), int(uid), int(burn_raw),
    )
    if not res or "UPDATE 0" in str(res):
        raise ValueError(f"You don't have enough {sym}.")


async def _check_token(db, gid: int, uid: int, sym: str, qty_human: float) -> bool:
    """Generic wallet_holdings check for tokens like FREN / BUD that live
    on their home network. We resolve the network from Config.TOKENS.
    """
    sym = sym.upper()
    net_full = Config.TOKENS.get(sym, {}).get("network") or ""
    from core.framework.network import normalize_short
    net_short = normalize_short(net_full)
    if not net_short:
        return False
    held = await db.get_wallet_holding(int(uid), int(gid), net_short, sym)
    held_raw = int((held or {}).get("amount") or 0)
    return to_human(held_raw) >= float(qty_human)


async def _consume_token(db, gid: int, uid: int, sym: str, qty_human: float) -> None:
    sym = sym.upper()
    net_full = Config.TOKENS.get(sym, {}).get("network") or ""
    from core.framework.network import normalize_short
    net_short = normalize_short(net_full)
    if not net_short:
        raise ValueError(f"Unknown token network for {sym}.")
    burn_raw = to_raw(float(qty_human))
    held = await db.get_wallet_holding(int(uid), int(gid), net_short, sym)
    held_raw = int((held or {}).get("amount") or 0)
    if held_raw < int(burn_raw):
        raise ValueError(f"You only have {to_human(held_raw):,.4f} {sym}.")
    await db.update_wallet_holding(int(uid), int(gid), net_short, sym, -int(burn_raw))


async def _spend_fgd(db, gid: int, uid: int, qty_human: float) -> int:
    """Burn FGD from the user's Forge-Network wallet_holdings as the
    crafting fee. FGD is a stablecoin pegged to $1 so the fee is, in effect,
    a flat USD price tag on the recipe. Returns the raw FGD spent.
    """
    if qty_human <= 0:
        return 0
    burn_raw = to_raw(float(qty_human))
    held = await db.get_wallet_holding(
        int(uid), int(gid), cc.FORGE_NETWORK_SHORT, cc.FGD_SYMBOL,
    )
    held_raw = int((held or {}).get("amount") or 0)
    if held_raw < int(burn_raw):
        raise ValueError(
            f"Crafting fee is {qty_human:,.2f} FGD but you only have "
            f"{to_human(held_raw):,.2f}. Buy FGD with `,buy FGD <amount>`."
        )
    await db.update_wallet_holding(
        int(uid), int(gid), cc.FORGE_NETWORK_SHORT, cc.FGD_SYMBOL, -int(burn_raw),
    )
    return int(burn_raw)


async def _resolve_inputs(
    db, gid: int, uid: int, inputs: dict[str, int | float],
) -> None:
    """Pre-flight check that the user has every ingredient. Raises
    ValueError with a human-readable message on the first missing input.
    """
    for raw_key, count in (inputs or {}).items():
        kind, sub = cc.parse_input_key(raw_key)
        if not kind or not sub:
            raise ValueError(f"Bad recipe input: {raw_key}")
        if kind == "fish":
            ok = await _check_fish(db, gid, uid, sub, int(count))
        elif kind == "crop":
            ok = await _check_crop(db, gid, uid, sub, int(count))
        elif kind == "ore":
            ok = await _check_ore(db, gid, uid, sub, int(count))
        elif kind == "token":
            ok = await _check_token(db, gid, uid, sub, float(count))
        else:
            raise ValueError(f"Unsupported input kind: {kind}")
        if not ok:
            raise ValueError(f"Missing ingredient: {raw_key} x{count}.")


async def _consume_inputs(
    db, gid: int, uid: int, inputs: dict[str, int | float],
) -> None:
    """Mutate every source-game inventory to remove the recipe inputs.
    Caller MUST have already wrapped the call in a transaction so a partial
    failure rolls back across all four games.
    """
    for raw_key, count in (inputs or {}).items():
        kind, sub = cc.parse_input_key(raw_key)
        if kind == "fish":
            await _consume_fish(db, gid, uid, sub, int(count))
        elif kind == "crop":
            await _consume_crop(db, gid, uid, sub, int(count))
        elif kind == "ore":
            await _consume_ore(db, gid, uid, sub, int(count))
        elif kind == "token":
            await _consume_token(db, gid, uid, sub, float(count))
        else:
            raise ValueError(f"Unsupported input kind: {kind}")


# ── Core: craft_item ────────────────────────────────────────────────────────

async def craft_item(
    db: Any, guild_id: int, user_id: int, craft_key: str, qty: int = 1,
) -> CraftResult:
    """Consume the recipe's inputs from each source game, burn FGD as the
    fee, mint INGOT, increment crafted_inventory, write the audit log, and
    return a CraftResult.

    Cooldown (CRAFT_COOLDOWN_SECONDS) is enforced via a DB-side clock on
    user_crafting.last_craft_at (EXTRACT(EPOCH FROM (NOW() - ...))) per the
    project rule about avoiding Python-clock comparisons against TIMESTAMPTZ.

    qty > 1 multiplies inputs and outputs linearly; the cooldown applies
    once per call so bulk crafting is the recommended path.
    """
    if int(qty) <= 0:
        raise ValueError("Quantity must be positive.")

    meta = cc.craft_meta(craft_key)
    if not meta:
        raise ValueError(f"No recipe with key `{craft_key}`.")

    state = await ensure_state(db, guild_id, user_id)
    level = int(state.get("crafting_level") or 1)
    if level < int(meta.get("min_level", 1)):
        raise ValueError(
            f"`{craft_key}` requires crafting level {meta['min_level']} "
            f"(you are {level})."
        )

    # Specialty lock: ``requires_specialty: True`` recipes only craft when
    # that recipe's specialty is in the player's active set. Pick-2 means
    # players make a real choice -- a Smith can't craft Enchanting locked
    # recipes without ditching one of their slots first.
    recipe_specialty = str(meta.get("specialty") or "").lower()
    active_specs = list(state.get("active_specialties") or [])
    if bool(meta.get("requires_specialty")) and not cc.in_specialty(
        recipe_specialty, active_specs,
    ):
        spec_label = recipe_specialty.title() or "a specialty"
        extra_slots = int(state.get("extra_specialty_slots") or 0)
        eff_cap = cc.ACTIVE_SPECIALTY_CAP + extra_slots
        cap_line = (
            f"you can hold up to **{eff_cap}** at once "
            f"({cc.ACTIVE_SPECIALTY_CAP} base + {extra_slots} purchased)"
            if extra_slots > 0 else
            f"you can hold up to {cc.ACTIVE_SPECIALTY_CAP} at a time, "
            f"or buy more via `,shop buy specialty_slot`"
        )
        raise ValueError(
            f"`{craft_key}` is a **{spec_label}** specialty recipe. "
            f"Specialise into {spec_label} first with "
            f"`,craft specialize {recipe_specialty}` ({cap_line})."
        )

    cd = await db.fetch_one(
        """
        SELECT EXTRACT(EPOCH FROM (NOW() - last_craft_at))::INTEGER AS dt,
               is_acting
          FROM user_crafting
         WHERE guild_id = $1 AND user_id = $2
        """,
        int(guild_id), int(user_id),
    )
    if cd and cd.get("is_acting"):
        raise ValueError("Already crafting -- finish that one first.")
    if cd and cd.get("dt") is not None and int(cd["dt"]) < cc.CRAFT_COOLDOWN_SECONDS:
        wait = cc.CRAFT_COOLDOWN_SECONDS - int(cd["dt"])
        raise ValueError(f"Forge is hot -- wait {wait}s before the next craft.")

    inputs = {k: int(v) * int(qty) if not isinstance(v, float) else float(v) * int(qty)
              for k, v in (meta.get("inputs") or {}).items()}
    fgd_total = float(meta.get("fgd_cost", 0.0)) * int(qty)

    await _resolve_inputs(db, guild_id, user_id, inputs)

    rarity = str(meta.get("rarity") or "common").lower()
    lo, hi = cc.RARITY_INGOT_PAYOUT.get(rarity, (1.0, 5.0))
    ingot_human = random.uniform(lo, hi) * int(qty)

    # Specialty bonuses:
    #   - In-specialty crafts get +1% INGOT mint per specialty level
    #     (recipe.specialty matches one of the player's active set).
    #   - Off-specialty crafts pay a 50% XP penalty (the OFF mult).
    in_spec = cc.in_specialty(recipe_specialty, active_specs)
    if in_spec:
        spec_lvl = int(
            state.get(f"{recipe_specialty}_level") or 1
        )
        ingot_human *= (
            1.0 + cc.SPECIALTY_INGOT_BONUS_PER_LEVEL * float(spec_lvl)
        )
    ingot_raw = to_raw(ingot_human)
    base_xp = int(cc.RARITY_XP.get(rarity, 0)) * int(qty)
    if recipe_specialty and not in_spec:
        xp_gained = int(round(base_xp * cc.OFF_SPECIALTY_XP_MULT))
    else:
        xp_gained = base_xp

    # ── Anvilstone (crafting meta gem) bonuses ───────────────────────────
    # Yield bonus: extra crafted units land in the inventory for free
    # (no extra input cost, no extra INGOT). XP bonus: per-recipe craft
    # skill XP scales up. Both are derived from level so a fresh stone
    # is a no-op. Best-effort -- a Anvilstone read failure must not
    # block the craft.
    anvil_bonus_qty = 0
    try:
        from services import themed_stones as _ts
        yield_pct = await _ts.anvilstone_yield_bonus(
            db, int(user_id), int(guild_id),
        )
        if yield_pct > 0:
            extra = float(qty) * float(yield_pct)
            anvil_bonus_qty = int(extra)
            # Carry the fractional remainder as a probabilistic bump
            # so a 0.4 fractional doesn't always round to zero.
            if random.random() < (extra - anvil_bonus_qty):
                anvil_bonus_qty += 1
        xp_pct = await _ts.anvilstone_xp_bonus(
            db, int(user_id), int(guild_id),
        )
        if xp_pct > 0:
            xp_gained = int(round(xp_gained * (1.0 + xp_pct)))
    except Exception:
        log.debug(
            "anvilstone bonus lookup failed gid=%s uid=%s",
            guild_id, user_id, exc_info=True,
        )
    out_qty = int(qty) + max(0, int(anvil_bonus_qty))

    async with db.atomic():
        await db.execute(
            "UPDATE user_crafting SET is_acting = TRUE "
            "WHERE guild_id = $1 AND user_id = $2",
            int(guild_id), int(user_id),
        )
        try:
            await _consume_inputs(db, guild_id, user_id, inputs)
            fgd_spent_raw = await _spend_fgd(db, guild_id, user_id, fgd_total)

            new_xp = int(state.get("crafting_xp") or 0) + int(xp_gained)
            new_level = cc.level_from_xp(new_xp)
            leveled_up = new_level > level

            # Per-specialty XP. Recipes carry a ``specialty`` field; the
            # column to bump is ``<specialty>_xp`` and the matching level
            # column ``<specialty>_level`` is recomputed off the new XP.
            # Specialty unknown / missing => skip the per-spec update,
            # aggregate XP / level still tick.
            specialty = str(meta.get("specialty") or "").lower()
            spec_old_lvl = 1
            spec_new_lvl = 1
            spec_leveled_up = False
            if specialty in cc.SPECIALTIES:
                spec_xp_col = f"{specialty}_xp"
                spec_lvl_col = f"{specialty}_level"
                spec_old_xp = int(state.get(spec_xp_col) or 0)
                spec_old_lvl = int(state.get(spec_lvl_col) or 1)
                spec_new_xp = spec_old_xp + int(xp_gained)
                spec_new_lvl = cc.level_from_xp(spec_new_xp)
                spec_leveled_up = spec_new_lvl > spec_old_lvl
                await db.execute(
                    f"""
                    UPDATE user_crafting
                       SET {spec_xp_col}  = $3::bigint,
                           {spec_lvl_col} = $4::int
                     WHERE guild_id = $1 AND user_id = $2
                    """,
                    int(guild_id), int(user_id),
                    int(spec_new_xp), int(spec_new_lvl),
                )

            await db.execute(
                """
                UPDATE user_crafting
                   SET crafted_inventory = COALESCE(crafted_inventory, '{}'::jsonb)
                                          || jsonb_build_object(
                                                $3::text,
                                                COALESCE(
                                                    (crafted_inventory ->> $3)::int, 0
                                                ) + $4::int
                                             ),
                       crafting_xp            = $5::bigint,
                       crafting_level         = $6::int,
                       total_crafts           = total_crafts + $4::int,
                       total_ingot_earned_raw = total_ingot_earned_raw + $7::numeric,
                       biggest_craft_key      = $3::text,
                       biggest_craft_at       = NOW(),
                       last_craft_at          = NOW(),
                       is_acting              = FALSE,
                       updated_at             = NOW()
                 WHERE guild_id = $1 AND user_id = $2
                """,
                int(guild_id), int(user_id), str(craft_key),
                int(out_qty), int(new_xp), int(new_level), int(ingot_raw),
            )

            # V3 enforced 10/90 USD split: a craft success used to mint
            # 100% INGOT (the coin). Now the same USD value is split
            # 10% INGOT + 90% FORGE (the yield token) using current
            # oracle prices.
            try:
                from core.framework.payout_split import rebalance_to_split
                from core.framework.scale import to_human as _h, to_raw as _r
                _ingot_h, _forge_h = await rebalance_to_split(
                    db, int(guild_id), cc.INGOT_SYMBOL, cc.FORGE_SYMBOL,
                    float(_h(int(ingot_raw))), 0.0,
                )
                _ingot_raw_split = int(_r(_ingot_h))
                _forge_raw_split = int(_r(_forge_h))
            except Exception:
                _ingot_raw_split = int(ingot_raw)
                _forge_raw_split = 0
            await db.update_wallet_holding(
                int(user_id), int(guild_id),
                cc.FORGE_NETWORK_SHORT, cc.INGOT_SYMBOL,
                _ingot_raw_split,
            )
            if _forge_raw_split > 0:
                try:
                    await db.update_wallet_holding(
                        int(user_id), int(guild_id),
                        cc.FORGE_NETWORK_SHORT, cc.FORGE_SYMBOL,
                        _forge_raw_split,
                    )
                except Exception:
                    log.debug(
                        "craft_success: FORGE split credit failed",
                        exc_info=True,
                    )

            await db.execute(
                """
                INSERT INTO crafting_logs
                    (guild_id, user_id, craft_key, qty, rarity,
                     ingot_earned_raw, fgd_spent_raw)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                int(guild_id), int(user_id), str(craft_key),
                int(out_qty), rarity, int(ingot_raw), int(fgd_spent_raw),
            )

            # NFT layer: mint one crafted token per produced unit
            # (including Anvilstone bonus units, so the NFT supply
            # matches the JSONB counter). Best-effort.
            try:
                from services import items as _items
                addr = _items.contract_address("crafted", str(craft_key))
                for unit_n in range(int(out_qty)):
                    await _items.mint_unit(
                        db,
                        guild_id=int(guild_id),
                        contract_address=addr,
                        owner_user_id=int(user_id),
                        metadata={
                            "craft_key": str(craft_key),
                            "rarity":    str(rarity or ""),
                            "specialty": str(specialty or ""),
                        },
                        mint_source="crafting.craft",
                        source_table="user_crafting.crafted_inventory",
                        source_id=(
                            f"{user_id}:{craft_key}:"
                            f"{int(__import__('time').time())}:{unit_n}"
                        ),
                    )
            except Exception:
                log.debug(
                    "nft crafted mint sync failed gid=%s uid=%s key=%s",
                    guild_id, user_id, craft_key, exc_info=True,
                )
        except Exception:
            await db.execute(
                "UPDATE user_crafting SET is_acting = FALSE "
                "WHERE guild_id = $1 AND user_id = $2",
                int(guild_id), int(user_id),
            )
            raise

    # ── Anvilstone XP grant (one per ,craft action, regardless of qty) ──
    # Done outside the atomic block so a stone-tracking hiccup never
    # rolls back the craft. _grant is itself best-effort; bot/guild
    # are not available here so the auto-levelup notify path is skipped
    # and the background poller catches up.
    try:
        from services import themed_stones as _ts
        await _ts.grant_anvilstone_xp(
            db, int(user_id), int(guild_id), crafted=True,
        )
    except Exception:
        log.debug(
            "anvilstone xp grant failed gid=%s uid=%s",
            guild_id, user_id, exc_info=True,
        )

    return CraftResult(
        craft_key=str(craft_key),
        qty=int(out_qty),
        rarity=rarity,
        ingot_minted_raw=int(ingot_raw),
        fgd_spent_raw=int(fgd_spent_raw),
        xp_gained=int(xp_gained),
        new_level=int(new_level),
        leveled_up=bool(leveled_up),
        specialty=str(specialty or ""),
        specialty_old_level=int(spec_old_lvl),
        specialty_new_level=int(spec_new_lvl),
        specialty_leveled_up=bool(spec_leveled_up),
    )


# ── Apply: route crafted output back into source-game inventory ────────────

@dataclass(slots=True)
class ApplyResult:
    craft_key:    str
    apply_kind:   str   # 'bait' | 'fert' | 'consum' | 'buddy' | 'battle' | 'cosmetic' | 'weapon' | 'armor'
    apply_target: str   # bait_key / fert_key / cons_key / buddy effect / battle item / etc.
    qty:          int   # how many were applied (1 for buddy effects)
    note:         str   # human-readable summary of the effect


async def _decrement_crafted(
    db, gid: int, uid: int, craft_key: str, qty: int,
) -> None:
    """Subtract ``qty`` of ``craft_key`` from user_crafting.crafted_inventory.
    Raises if the user doesn't have enough.
    """
    state = await get_state(db, gid, uid)
    inv = state.get("crafted_inventory") or {}
    have = int(inv.get(craft_key) or 0)
    if have < int(qty):
        raise ValueError(f"You only have {have} of `{craft_key}`.")
    new = have - int(qty)
    if new > 0:
        inv[craft_key] = new
    else:
        inv.pop(craft_key, None)
    await db.execute(
        "UPDATE user_crafting SET crafted_inventory = $3::jsonb, updated_at = NOW() "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid), _json_mod.dumps(inv),
    )
    # NFT layer: burn N crafted tokens. Best-effort.
    try:
        from services import items as _items
        for _ in range(int(qty)):
            await _items.consume_one(
                db,
                guild_id=int(gid), user_id=int(uid),
                contract_address=_items.contract_address("crafted", str(craft_key)),
                reason="crafting.apply",
            )
    except Exception:
        log.debug(
            "nft crafted burn sync failed gid=%s uid=%s key=%s",
            gid, uid, craft_key, exc_info=True,
        )


async def _apply_to_fishing_bait(
    db, gid: int, uid: int, bait_key: str, qty: int,
) -> str:
    """Top up user_fishing.bait_inventory[bait_key] by qty. The fishing cog
    uses the same bait keys for its shop, so equip / cast logic just works.
    """
    row = await db.fetch_one(
        "SELECT bait_inventory FROM user_fishing "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    if not row:
        raise ValueError("Run `,fish` once first to create your fishing profile.")
    inv = _as_dict(row.get("bait_inventory"))
    inv[bait_key] = int(inv.get(bait_key) or 0) + int(qty)
    await db.execute(
        "UPDATE user_fishing SET bait_inventory = $3::jsonb "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid), _json_mod.dumps(inv),
    )
    # NFT layer: mint N bait tokens. Best-effort.
    try:
        from services import items as _items
        addr = _items.contract_address("bait", str(bait_key))
        for unit_n in range(int(qty)):
            await _items.mint_unit(
                db,
                guild_id=int(gid),
                contract_address=addr,
                owner_user_id=int(uid),
                metadata={"bait_key": str(bait_key)},
                mint_source="crafting.apply.bait",
                source_table="user_fishing.bait_inventory",
                source_id=(
                    f"{uid}:{bait_key}:craft:"
                    f"{int(__import__('time').time())}:{unit_n}"
                ),
            )
    except Exception:
        log.debug(
            "nft bait mint (crafting.apply) sync failed gid=%s uid=%s key=%s",
            gid, uid, bait_key, exc_info=True,
        )
    return f"Added {qty}x `{bait_key}` to your bait inventory."


async def _apply_to_farming_fert(
    db, gid: int, uid: int, fert_key: str, qty: int,
) -> str:
    row = await db.fetch_one(
        "SELECT fertilizer_inventory FROM user_farming "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    if not row:
        raise ValueError("Run `,farm` once first to create your farm profile.")
    inv = _as_dict(row.get("fertilizer_inventory"))
    inv[fert_key] = int(inv.get(fert_key) or 0) + int(qty)
    await db.execute(
        "UPDATE user_farming SET fertilizer_inventory = $3::jsonb "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid), _json_mod.dumps(inv),
    )
    return f"Added {qty}x `{fert_key}` to your fertilizer inventory."


async def _apply_to_dungeon_consum(
    db, gid: int, uid: int, cons_key: str, qty: int,
) -> str:
    row = await db.fetch_one(
        "SELECT consumables FROM user_dungeon "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    if not row:
        raise ValueError("Run `,delve class <warrior|mage|rogue>` first.")
    inv = _as_dict(row.get("consumables"))
    inv[cons_key] = int(inv.get(cons_key) or 0) + int(qty)
    await db.execute(
        "UPDATE user_dungeon SET consumables = $3::jsonb "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid), _json_mod.dumps(inv),
    )
    cons_name = (dc.CONSUMABLES.get(cons_key) or {}).get("name", cons_key)
    return f"Added {qty}x **{cons_name}** to your dungeon pack."


async def _apply_to_buddy_battle_inventory(
    db, gid: int, uid: int, item_key: str, qty: int,
) -> str:
    """Top up ``user_buddy_economy.battle_inventory[item_key]`` by qty.

    The in-battle dropdown reads from this JSONB to populate options.
    Catalogue entry is verified against
    ``buddies_config.BATTLE_CONSUMABLES`` so a typo in a recipe surfaces
    here instead of silently producing dead inventory.
    """
    from configs.buddies_config import BATTLE_CONSUMABLES as _BC
    if item_key not in _BC:
        raise ValueError(
            f"`{item_key}` is not a known battle consumable."
        )
    # Ensure the user_buddy_economy row exists (matches the helper used
    # by services/buddy_economy.py for first-touch creation).
    await db.execute(
        "INSERT INTO user_buddy_economy (guild_id, user_id) "
        "VALUES ($1, $2) ON CONFLICT DO NOTHING",
        int(gid), int(uid),
    )
    row = await db.fetch_one(
        "SELECT battle_inventory FROM user_buddy_economy "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    inv = _as_dict(row.get("battle_inventory")) if row else {}
    inv[item_key] = int(inv.get(item_key) or 0) + int(qty)
    await db.execute(
        "UPDATE user_buddy_economy SET battle_inventory = $3::jsonb "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid), _json_mod.dumps(inv),
    )
    item_name = _BC[item_key].get("name") or item_key
    return f"Added {qty}x **{item_name}** to your buddy battle bag."


async def _apply_to_cosmetic_inventory(
    db, gid: int, uid: int, cos_key: str, qty: int,
) -> str:
    """Mint ``qty`` of cosmetic ``cos_key`` into ``users.cosmetics``.

    Cosmetics are craft-only -- the recipe's ``apply: cosmetic/<key>``
    routes here, the JSONB counter ticks up, and the player can then
    ,inventory use <key> to grant the linked role for the cosmetic's
    declared duration_seconds (default 1h).
    """
    from configs.items_config import SHOP_ITEMS as _ITEMS
    cfg = _ITEMS.get(cos_key) or {}
    if str(cfg.get("category") or "") != "cosmetic":
        raise ValueError(f"`{cos_key}` is not a cosmetic.")
    new_count = await db.add_cosmetic(int(uid), int(gid), cos_key, int(qty))
    cos_name = cfg.get("name") or cos_key.title()
    return (
        f"Added {qty}x {cfg.get('emoji', '')} **{cos_name}** to your "
        f"cosmetic inventory. Use `,inventory use {cos_key}` to wear it "
        f"({int(cfg.get('duration_seconds', 3600)) // 60} min)."
    )


async def _apply_to_dungeon_weapon(
    db, gid: int, uid: int, weapon_key: str, qty: int,
) -> str:
    """Mint a weapon directly into ``user_dungeon.weapons_owned``.

    Used by Smithing's legendary forge recipes (Soul Reaver, etc.) so
    the player gets the actual delve weapon as if they'd bought it
    from the shop. Equip with ``,delve equip weapon <key>``.
    """
    if weapon_key not in dc.WEAPONS:
        raise ValueError(f"Unknown weapon `{weapon_key}`.")
    row = await db.fetch_one(
        "SELECT weapons_owned FROM user_dungeon "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    if not row:
        raise ValueError("Run `,delve class <warrior|mage|rogue>` first.")
    owned = _as_dict(row.get("weapons_owned"))
    owned[weapon_key] = int(owned.get(weapon_key) or 0) + int(qty)
    await db.execute(
        "UPDATE user_dungeon SET weapons_owned = $3::jsonb "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid), _json_mod.dumps(owned),
    )
    name = dc.WEAPONS[weapon_key].get("name", weapon_key)
    return (
        f"Forged **{name}** straight into your delve pack. "
        f"Equip with `,delve equip weapon {weapon_key}`."
    )


async def _apply_to_dungeon_armor(
    db, gid: int, uid: int, armor_key: str, qty: int,
) -> str:
    """Mint an armor piece into ``user_dungeon.armor_owned``."""
    if armor_key not in dc.ARMOR:
        raise ValueError(f"Unknown armor `{armor_key}`.")
    row = await db.fetch_one(
        "SELECT armor_owned FROM user_dungeon "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid),
    )
    if not row:
        raise ValueError("Run `,delve class <warrior|mage|rogue>` first.")
    owned = _as_dict(row.get("armor_owned"))
    owned[armor_key] = int(owned.get(armor_key) or 0) + int(qty)
    await db.execute(
        "UPDATE user_dungeon SET armor_owned = $3::jsonb "
        "WHERE guild_id = $1 AND user_id = $2",
        int(gid), int(uid), _json_mod.dumps(owned),
    )
    name = dc.ARMOR[armor_key].get("name", armor_key)
    return (
        f"Forged **{name}** straight into your delve pack. "
        f"Equip with `,delve equip armor {armor_key}`."
    )


# Buddy effects are one-shot (no inventory column on cc_buddies). They're
# applied per-call directly to the active buddy. Each effect is a tiny
# DB write -- we deliberately keep them inline so services/crafting.py
# doesn't grow a hard dependency on services/buddy_*.py.

async def _active_buddy(db, gid: int, uid: int) -> dict | None:
    return await db.fetch_one(
        """
        SELECT id, level, xp, hunger, happiness, energy, rarity_tier
          FROM cc_buddies
         WHERE guild_id = $1 AND owner_user_id = $2
           AND status = 'owned'
           AND is_active = TRUE
         LIMIT 1
        """,
        int(gid), int(uid),
    )


# Crafted-food cooldown (seconds). All buddy/<effect> applies share one
# timer on the buddy so a player can't sequence Treat -> Toy -> Tonic
# -> Training Brew in five seconds to dump 500+ XP plus a full mood
# reset. ``reroll_rarity`` and the permanent-buff effects skip the
# gate -- those are limited by their own ``max_stack`` of 1-5.
_BUDDY_CRAFT_COOLDOWN_S: int = 30 * 60   # 30 minutes


async def _apply_buddy_effect(
    db, gid: int, uid: int, effect: str,
) -> str:
    bud = await _active_buddy(db, gid, uid)
    if not bud:
        raise ValueError("No active buddy. Activate one with `,buddy panel`.")
    bid = int(bud["id"])

    # Block crafted-food / treat / consumable applies while the active
    # buddy is on an expedition. Mirrors the panel-side feed/pet/talk
    # guard so a player can't bypass the lock by routing through the
    # crafting cog. Treats apply to the active buddy specifically, so
    # checking that one row is sufficient.
    busy = await db.fetch_val(
        "SELECT 1 FROM buddy_expeditions "
        "WHERE buddy_id = $1 AND status = 'running' LIMIT 1",
        bid,
    )
    if busy:
        raise ValueError(
            "Your active buddy is on an expedition -- can't feed treats "
            "or apply consumables until they're back. "
            "`,expedition` to track."
        )

    # Cooldown check on the rate-limited effects. Permanent-buff effects
    # (one-shot, max_stack=1) and rarity reroll bypass; everything else
    # shares the timer. DB-side EXTRACT keeps the comparison on the
    # database clock per CLAUDE.md.
    rate_limited = effect in {
        "feed", "play", "restore", "xp", "xp_big", "feast", "full_revive",
    }
    if rate_limited:
        elapsed = await db.fetch_val(
            "SELECT EXTRACT(EPOCH FROM "
            "       (NOW() - last_buddy_craft_apply_at))::bigint "
            "  FROM cc_buddies WHERE id = $1",
            bid,
        )
        if elapsed is not None and int(elapsed) < _BUDDY_CRAFT_COOLDOWN_S:
            wait = _BUDDY_CRAFT_COOLDOWN_S - int(elapsed)
            raise ValueError(
                f"This buddy was just fed -- wait "
                f"**{wait // 60}m {wait % 60}s** before applying another "
                f"crafted food."
            )

    if effect == "feed":
        await db.execute(
            """UPDATE cc_buddies
                  SET hunger    = LEAST(100, hunger + 35),
                      happiness = LEAST(100, happiness + 5),
                      energy    = LEAST(100, energy + 15),
                      last_interacted_at = NOW(),
                      last_buddy_craft_apply_at = NOW()
                WHERE id = $1""",
            bid,
        )
        return "Buddy fed: +35 hunger, +5 happiness, +15 energy."
    if effect == "play":
        await db.execute(
            """UPDATE cc_buddies
                  SET happiness = LEAST(100, happiness + 25),
                      last_interacted_at = NOW(),
                      last_buddy_craft_apply_at = NOW()
                WHERE id = $1""",
            bid,
        )
        return "Buddy played: +25 happiness."
    if effect == "restore":
        await db.execute(
            """UPDATE cc_buddies
                  SET hunger = 100, happiness = 100, energy = 100,
                      last_interacted_at = NOW(),
                      last_buddy_craft_apply_at = NOW()
                WHERE id = $1""",
            bid,
        )
        return "Buddy fully restored."
    if effect == "xp":
        # Rebalanced from 500 -> 100. Crafted XP food was outpacing
        # every other XP source by an order of magnitude; the new value
        # plus the 30-minute cooldown puts it on par with passive chat
        # / battle / expedition gain.
        await db.execute(
            "UPDATE cc_buddies SET "
            "  xp = xp + 100, "
            "  level = GREATEST(level, LEAST(50, GREATEST(1, "
            "      FLOOR((1.0 + SQRT(1.0 + 8.0 * (xp + 100)::double precision / 120.0)) / 2.0)::int"
            "  ))), "
            "  last_buddy_craft_apply_at = NOW() WHERE id = $1",
            bid,
        )
        return "Buddy gained +100 XP."
    if effect == "xp_big":
        # Rebalanced from 1500 -> 300.
        await db.execute(
            "UPDATE cc_buddies SET "
            "  xp = xp + 300, "
            "  level = GREATEST(level, LEAST(50, GREATEST(1, "
            "      FLOOR((1.0 + SQRT(1.0 + 8.0 * (xp + 300)::double precision / 120.0)) / 2.0)::int"
            "  ))), "
            "  last_buddy_craft_apply_at = NOW() WHERE id = $1",
            bid,
        )
        return "Buddy gained +300 XP."
    if effect == "reroll_rarity":
        new_tier = random.choices(
            [1, 2, 3, 4, 5], weights=[58, 18, 11, 9, 4], k=1,
        )[0]
        await db.execute(
            "UPDATE cc_buddies SET rarity_tier = $2 WHERE id = $1",
            bid, int(new_tier),
        )
        return f"Buddy rarity rerolled to tier {new_tier}."
    raise ValueError(f"Unknown buddy effect: {effect}")


async def apply_item(
    db: Any, guild_id: int, user_id: int, craft_key: str, qty: int = 1,
) -> ApplyResult:
    """Spend a crafted item and route its effect back into the source game.

    For inventory-bound items (bait / fert / consum) ``qty`` controls how
    many are deposited. For buddy effects, only one application happens per
    call regardless of qty (multi-apply on a single buddy is meaningless).
    """
    meta = cc.craft_meta(craft_key)
    if not meta:
        raise ValueError(f"No recipe with key `{craft_key}`.")
    if int(qty) <= 0:
        raise ValueError("Quantity must be positive.")

    apply_str = str(meta.get("apply") or "")
    kind, target = cc.parse_apply_target(apply_str)
    if not kind or not target:
        raise ValueError(f"Recipe `{craft_key}` has no valid apply target.")

    if kind == "buddy":
        await _decrement_crafted(db, guild_id, user_id, craft_key, 1)
        note = await _apply_buddy_effect(db, guild_id, user_id, target)
        return ApplyResult(craft_key, kind, target, 1, note)

    await _decrement_crafted(db, guild_id, user_id, craft_key, int(qty))
    if kind == "bait":
        note = await _apply_to_fishing_bait(db, guild_id, user_id, target, int(qty))
    elif kind == "fert":
        note = await _apply_to_farming_fert(db, guild_id, user_id, target, int(qty))
    elif kind == "consum":
        note = await _apply_to_dungeon_consum(db, guild_id, user_id, target, int(qty))
    elif kind == "weapon":
        note = await _apply_to_dungeon_weapon(db, guild_id, user_id, target, int(qty))
    elif kind == "armor":
        note = await _apply_to_dungeon_armor(db, guild_id, user_id, target, int(qty))
    elif kind == "cosmetic":
        note = await _apply_to_cosmetic_inventory(db, guild_id, user_id, target, int(qty))
    elif kind == "battle":
        note = await _apply_to_buddy_battle_inventory(db, guild_id, user_id, target, int(qty))
    else:
        raise ValueError(f"Unsupported apply kind: {kind}")
    return ApplyResult(craft_key, kind, target, int(qty), note)


def inventory_summary(state: dict) -> list[tuple[str, int, dict]]:
    """Return ``[(craft_key, count, meta), ...]`` sorted by min_level then
    alphabetically. Missing keys (recipe removed but inventory still has it)
    are skipped silently.
    """
    inv = (state or {}).get("crafted_inventory") or {}
    rows: list[tuple[str, int, dict]] = []
    for k, v in inv.items():
        meta = cc.craft_meta(k)
        if not meta:
            continue
        rows.append((k, int(v), meta))
    rows.sort(key=lambda r: (int(r[2].get("min_level", 1)), r[0]))
    return rows


# ── Token economy: INGOT -> FORGE burn, FORGE -> USD cashout ───────────────
#
# Both paths reuse services.fishing._price_impact / _write_burn_candle /
# _distribute_burn_lp_reward / _oracle_price so the slippage shape matches
# every other earn-only network exactly. The slippage IS the fee.

async def burn_ingot_for_forge(
    db: Any, guild_id: int, user_id: int, amt_raw: int,
) -> BurnResult:
    """Burn INGOT, mint FORGE, push both oracles by the standard impact
    formula. Mirrors services/farming.burn_seed_for_hrv exactly."""
    if amt_raw <= 0:
        raise ValueError("Amount must be positive.")

    held = await db.get_wallet_holding(
        user_id, guild_id, cc.FORGE_NETWORK_SHORT, cc.INGOT_SYMBOL,
    )
    held_raw = int((held or {}).get("amount") or 0)
    if held_raw < int(amt_raw):
        raise ValueError(f"You only have {to_human(held_raw):,.4f} INGOT.")

    ingot_oracle_before = await _oracle_price(db, guild_id, cc.INGOT_SYMBOL)
    forge_oracle_before = await _oracle_price(db, guild_id, cc.FORGE_SYMBOL)
    if ingot_oracle_before <= 0 or forge_oracle_before <= 0:
        raise ValueError("Oracle price is currently zero -- try again in a moment.")

    ingot_human = to_human(int(amt_raw))
    usd_value = ingot_human * ingot_oracle_before

    rows = await db.fetch_all(
        "SELECT symbol, circulating_supply FROM crypto_prices "
        "WHERE guild_id = $1 AND symbol = ANY($2::text[])",
        int(guild_id), [cc.INGOT_SYMBOL, cc.FORGE_SYMBOL],
    )
    supply: dict[str, float] = {}
    for r in (rows or []):
        supply[str(r["symbol"]).upper()] = to_human(int(r.get("circulating_supply") or 0))

    ingot_impact = _price_impact(usd_value, ingot_oracle_before, supply.get(cc.INGOT_SYMBOL, 0.0))
    forge_impact = _price_impact(usd_value, forge_oracle_before, supply.get(cc.FORGE_SYMBOL, 0.0))

    eff_forge_price = forge_oracle_before * (1.0 + forge_impact / 2.0)
    forge_minted_human = usd_value / max(1e-12, eff_forge_price)
    forge_minted_raw = to_raw(forge_minted_human)
    if forge_minted_raw <= 0:
        raise ValueError("Burn produces zero FORGE -- raise the INGOT amount.")

    await db.update_wallet_holding(
        user_id, guild_id, cc.FORGE_NETWORK_SHORT, cc.INGOT_SYMBOL, -int(amt_raw),
    )
    try:
        await db.update_wallet_holding(
            user_id, guild_id, cc.FORGE_NETWORK_SHORT, cc.FORGE_SYMBOL,
            int(forge_minted_raw),
        )
    except Exception:
        try:
            await db.update_wallet_holding(
                user_id, guild_id, cc.FORGE_NETWORK_SHORT, cc.INGOT_SYMBOL,
                int(amt_raw),
            )
        except Exception:
            log.exception("burn_ingot_for_forge: refund failed uid=%s gid=%s amt=%s",
                          user_id, guild_id, amt_raw)
        raise

    ingot_oracle_after = max(1e-9, ingot_oracle_before * (1.0 - ingot_impact))
    forge_oracle_after = max(1e-9, forge_oracle_before * (1.0 + forge_impact))
    try:
        await db.update_price(cc.INGOT_SYMBOL, guild_id, ingot_oracle_after)
        await db.update_price(cc.FORGE_SYMBOL, guild_id, forge_oracle_after)
    except Exception:
        log.exception("burn_ingot_for_forge: oracle update failed gid=%s", guild_id)

    await _write_burn_candle(
        db, guild_id, cc.INGOT_SYMBOL,
        ingot_oracle_before, ingot_oracle_after, usd_value,
    )
    await _write_burn_candle(
        db, guild_id, cc.FORGE_SYMBOL,
        forge_oracle_before, forge_oracle_after, usd_value,
    )

    fee_usd = usd_value * (int(cc.GEAR_BURN_LP_REWARD_BPS) / 10_000.0)
    if fee_usd > 0:
        await _distribute_burn_lp_reward(db, guild_id, cc.INGOT_SYMBOL, fee_usd / 2.0)
        await _distribute_burn_lp_reward(db, guild_id, cc.FORGE_SYMBOL, fee_usd / 2.0)

    await db.execute(
        """
        UPDATE user_crafting
           SET total_forge_earned_raw = total_forge_earned_raw + $3::numeric,
               updated_at             = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(forge_minted_raw),
    )

    return BurnResult(
        burned_ingot_raw=int(amt_raw),
        minted_forge_raw=int(forge_minted_raw),
        impact_pct=float(max(ingot_impact, forge_impact)),
    )


async def cashout_forge(
    db: Any, guild_id: int, user_id: int, amt_raw: int,
) -> CashoutResult:
    """Burn FORGE, push the FORGE oracle DOWN, credit users.wallet with USD.
    Mirrors services/farming.cashout_hrv exactly."""
    if amt_raw <= 0:
        raise ValueError("Amount must be positive.")

    held = await db.get_wallet_holding(
        user_id, guild_id, cc.FORGE_NETWORK_SHORT, cc.FORGE_SYMBOL,
    )
    held_raw = int((held or {}).get("amount") or 0)
    if held_raw < int(amt_raw):
        raise ValueError(f"You only have {to_human(held_raw):,.4f} FORGE.")

    forge_oracle_before = await _oracle_price(db, guild_id, cc.FORGE_SYMBOL)
    if forge_oracle_before <= 0:
        raise ValueError("FORGE oracle price is currently zero -- try again later.")

    forge_human = to_human(int(amt_raw))
    revenue_usd = forge_human * forge_oracle_before

    row = await db.fetch_one(
        "SELECT circulating_supply FROM crypto_prices "
        "WHERE guild_id = $1 AND symbol = $2",
        int(guild_id), cc.FORGE_SYMBOL,
    )
    supply_human = to_human(int((row or {}).get("circulating_supply") or 0))
    impact = _price_impact(revenue_usd, forge_oracle_before, supply_human)

    eff_price = forge_oracle_before * (1.0 - impact / 2.0)
    usd_credit_human = forge_human * eff_price

    # Group Industry bonus: members of a group with a crafting-bonus
    # upgrade (Forge Workshop / Guild Market / Master Industries) earn
    # the bonus on every cashout, anywhere.
    try:
        from services.group_reserve import member_activity_bonus
        _crafting_bonus = await member_activity_bonus(db, guild_id, user_id, "crafting")
    except Exception:
        log.debug("group crafting bonus probe failed", exc_info=True)
        _crafting_bonus = 0.0
    if _crafting_bonus > 0:
        usd_credit_human *= (1.0 + _crafting_bonus)

    usd_credit_raw = to_raw(usd_credit_human)

    await db.update_wallet_holding(
        user_id, guild_id, cc.FORGE_NETWORK_SHORT, cc.FORGE_SYMBOL, -int(amt_raw),
    )
    if usd_credit_raw > 0:
        try:
            await db.update_wallet(user_id, guild_id, int(usd_credit_raw))
        except Exception:
            try:
                await db.update_wallet_holding(
                    user_id, guild_id, cc.FORGE_NETWORK_SHORT, cc.FORGE_SYMBOL,
                    int(amt_raw),
                )
            except Exception:
                log.exception("cashout_forge: FORGE refund failed uid=%s gid=%s amt=%s",
                              user_id, guild_id, amt_raw)
            raise

    forge_oracle_after = max(1e-9, forge_oracle_before * (1.0 - impact))
    try:
        await db.update_price(cc.FORGE_SYMBOL, guild_id, forge_oracle_after)
    except Exception:
        log.exception("cashout_forge: oracle update failed gid=%s", guild_id)

    await _write_burn_candle(
        db, guild_id, cc.FORGE_SYMBOL,
        forge_oracle_before, forge_oracle_after, revenue_usd,
    )

    fee_usd = revenue_usd * (int(cc.GEAR_BURN_LP_REWARD_BPS) / 10_000.0)
    if fee_usd > 0:
        await _distribute_burn_lp_reward(db, guild_id, cc.FORGE_SYMBOL, fee_usd)

    # Group reserve tribute: system-funded grant on the gross USD value
    # of the cashout. The user's payout is unaffected.
    try:
        from services.group_reserve import tribute_from_activity
        await tribute_from_activity(
            db, guild_id, user_id, float(revenue_usd), "crafting",
        )
    except Exception:
        log.debug("group crafting tribute failed", exc_info=True)

    await db.execute(
        """
        UPDATE user_crafting
           SET total_usd_cashout_raw = total_usd_cashout_raw + $3::numeric,
               updated_at            = NOW()
         WHERE guild_id = $1 AND user_id = $2
        """,
        guild_id, user_id, int(usd_credit_raw),
    )

    return CashoutResult(
        burned_forge_raw=int(amt_raw),
        usd_credit_raw=int(usd_credit_raw),
        impact_pct=float(impact),
    )


# ── INGOT staking ──────────────────────────────────────────────────────────

async def stake_ingot(
    db: Any, guild_id: int, user_id: int, amt_raw: int,
) -> int:
    """Move INGOT from wallet_holdings into user_crafting.ingot_staked_raw.
    Returns the new total staked (raw).
    """
    if amt_raw <= 0:
        raise ValueError("Amount must be positive.")
    held = await db.get_wallet_holding(
        user_id, guild_id, cc.FORGE_NETWORK_SHORT, cc.INGOT_SYMBOL,
    )
    held_raw = int((held or {}).get("amount") or 0)
    if held_raw < int(amt_raw):
        raise ValueError(f"You only have {to_human(held_raw):,.4f} INGOT.")
    async with db.atomic():
        await db.update_wallet_holding(
            user_id, guild_id, cc.FORGE_NETWORK_SHORT, cc.INGOT_SYMBOL,
            -int(amt_raw),
        )
        row = await db.fetch_one(
            """
            UPDATE user_crafting
               SET ingot_staked_raw = ingot_staked_raw + $3::numeric,
                   last_stake_yield_at = COALESCE(last_stake_yield_at, NOW()),
                   updated_at = NOW()
             WHERE guild_id = $1 AND user_id = $2
            RETURNING ingot_staked_raw
            """,
            int(guild_id), int(user_id), int(amt_raw),
        )
    return int((row or {}).get("ingot_staked_raw") or 0)


async def unstake_ingot(
    db: Any, guild_id: int, user_id: int, amt_raw: int,
) -> int:
    """Move INGOT from ingot_staked_raw back to wallet_holdings."""
    if amt_raw <= 0:
        raise ValueError("Amount must be positive.")
    state = await get_state(db, guild_id, user_id)
    staked = int(state.get("ingot_staked_raw") or 0)
    if staked < int(amt_raw):
        raise ValueError(f"You only have {to_human(staked):,.4f} INGOT staked.")
    async with db.atomic():
        await db.execute(
            """
            UPDATE user_crafting
               SET ingot_staked_raw = ingot_staked_raw - $3::numeric,
                   updated_at = NOW()
             WHERE guild_id = $1 AND user_id = $2
            """,
            int(guild_id), int(user_id), int(amt_raw),
        )
        await db.update_wallet_holding(
            user_id, guild_id, cc.FORGE_NETWORK_SHORT, cc.INGOT_SYMBOL,
            int(amt_raw),
        )
    return int(staked - int(amt_raw))


async def accrued_stake_yield(
    db: Any, guild_id: int, user_id: int,
) -> int:
    """Compute INGOT-staking yield owed since the last claim, using a
    DB-side clock. Returns FORGE raw owed (not yet credited).
    """
    row = await db.fetch_one(
        """
        SELECT ingot_staked_raw,
               forge_yield_pending_raw,
               EXTRACT(EPOCH FROM (NOW() - COALESCE(last_stake_yield_at, NOW())))::FLOAT8 AS dt
          FROM user_crafting
         WHERE guild_id = $1 AND user_id = $2
        """,
        int(guild_id), int(user_id),
    )
    if not row:
        return 0
    staked_human = to_human(int(row.get("ingot_staked_raw") or 0))
    dt_seconds = float(row.get("dt") or 0.0)
    days = max(0.0, dt_seconds / 86_400.0)
    accrued_human = staked_human * float(cc.INGOT_STAKE_FORGE_PER_DAY) * days
    pending_raw = int(row.get("forge_yield_pending_raw") or 0)
    return int(pending_raw + to_raw(accrued_human))


async def claim_stake_yield(
    db: Any, guild_id: int, user_id: int,
) -> int:
    """Sweep accrued INGOT-stake yield into FORGE wallet_holdings and reset
    the yield clock. Returns FORGE raw credited.
    """
    owed = await accrued_stake_yield(db, guild_id, user_id)
    if owed <= 0:
        return 0
    async with db.atomic():
        await db.update_wallet_holding(
            user_id, guild_id, cc.FORGE_NETWORK_SHORT, cc.FORGE_SYMBOL,
            int(owed),
        )
        await db.execute(
            """
            UPDATE user_crafting
               SET forge_yield_pending_raw = 0,
                   last_stake_yield_at     = NOW(),
                   total_forge_earned_raw  = total_forge_earned_raw + $3::numeric,
                   updated_at              = NOW()
             WHERE guild_id = $1 AND user_id = $2
            """,
            int(guild_id), int(user_id), int(owed),
        )
    return int(owed)


# ── Leaderboards / history ──────────────────────────────────────────────────

async def get_top_crafters(
    db: Any, guild_id: int, limit: int = 10,
) -> list[dict]:
    return await db.fetch_all(
        """
        SELECT user_id, total_crafts, total_forge_earned_raw,
               total_ingot_earned_raw, crafting_level
          FROM user_crafting
         WHERE guild_id = $1
         ORDER BY total_forge_earned_raw DESC
         LIMIT $2
        """,
        int(guild_id), int(limit),
    )


async def get_user_crafts(
    db: Any, guild_id: int, user_id: int, limit: int = 10,
) -> list[dict]:
    return await db.fetch_all(
        """
        SELECT craft_key, qty, rarity, ingot_earned_raw, fgd_spent_raw, crafted_at
          FROM crafting_logs
         WHERE guild_id = $1 AND user_id = $2
         ORDER BY crafted_at DESC
         LIMIT $3
        """,
        int(guild_id), int(user_id), int(limit),
    )
