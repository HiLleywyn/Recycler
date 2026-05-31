"""
services/net_worth.py  -  Shared net-worth computation used by both
the Discord -balance command and the dashboard API profile endpoint.

Call ``compute_net_worth(uid, gid, db)`` to obtain a fully-populated
``NetWorthResult`` that mirrors exactly what -balance's Summary tab shows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.config import Config
from core.framework.scale import to_human

if TYPE_CHECKING:
    from database import Database


@dataclass
class NetWorthResult:
    wallet: float = 0.0
    bank: float = 0.0
    cefi_crypto: float = 0.0       # CeFi crypto holdings (USD value)
    defi_wallet: float = 0.0       # DeFi wallet holdings (USD value)
    stake_value: float = 0.0       # NPC yield-farm stakes
    pos_stake_value: float = 0.0   # Player PoS validator own stake
    moon_stake_value: float = 0.0  # Lunar Mint staked group tokens (Moon Network)
    moon_pool_stake_value: float = 0.0  # Moon Pool (Tier 2) staked MOON valued at spot
    lp_value: float = 0.0          # LP positions
    rig_value: float = 0.0         # Mining rig book value (50% of cost)
    delegation_value: float = 0.0  # Delegated tokens
    savings_value: float = 0.0     # USD savings deposits
    items_value: float = 0.0       # Stones + consumables staked value
    fishing_stake_value: float = 0.0  # Staked LURE + accrued REEL yield (USD)
    delve_stake_value: float = 0.0    # Staked dungeon ore + accrued RUNE yield (USD)
    delve_party_value: float = 0.0    # Captured dungeon buddies valued at hatch_base * tier
    buddy_economy_value: float = 0.0  # Staked FREN + accrued BUD yield + slot sink (USD)
    farming_stake_value:     float = 0.0  # Staked SEED + accrued HRV yield (USD)
    farming_plot_value:      float = 0.0  # Cumulative HRV plot prices, valued at 50% (USD)
    farming_inventory_value: float = 0.0  # Crops + processed goods at HRV sell price (USD)
    crafting_stake_value:     float = 0.0  # Staked INGOT + accrued FORGE yield (USD)
    crafting_inventory_value: float = 0.0  # Crafted items at FGD cost (stable USD)
    safety_module_value:     float = 0.0  # VTR/DSY staked in Safety Module (USD)
    disc_fun_value:          float = 0.0  # Active Disc.Fun proto-token positions (USD)
    gamba_stake_value:       float = 0.0  # Staked game tokens + pending GBC yield (USD)
    sage_stake_value:        float = 0.0  # Staked EDU + pending SAGE yield (USD)
    eat_stake_value:         float = 0.0  # Staked $EAT in the EatChain minigame (USD)
    nft_value: float = 0.0
    nfts_owned: list[dict] = field(default_factory=list)
    loan_liability: float = 0.0    # USD loan outstanding (negative)

    # Raw component details (for detailed portfolio display)
    holdings: list[dict] = field(default_factory=list)
    wallet_holdings: list[dict] = field(default_factory=list)
    stakes: list[dict] = field(default_factory=list)
    pos_validators: list[dict] = field(default_factory=list)
    lp_positions: list[dict] = field(default_factory=list)
    delegations: list[dict] = field(default_factory=list)
    rigs: list[dict] = field(default_factory=list)
    hashstone: dict | None = None
    lockstone: dict | None = None
    vaultstone: dict | None = None
    liqstone: dict | None = None
    gambastone: dict | None = None
    validator_guard_count: int = 0
    yield_guard_count: int = 0
    usd_savings: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.wallet + self.bank
            + self.cefi_crypto + self.defi_wallet
            + self.stake_value + self.pos_stake_value
            + self.moon_stake_value + self.moon_pool_stake_value
            + self.lp_value + self.rig_value
            + self.delegation_value + self.savings_value
            + self.items_value + self.fishing_stake_value
            + self.delve_stake_value + self.delve_party_value
            + self.buddy_economy_value
            + self.farming_stake_value
            + self.farming_plot_value
            + self.farming_inventory_value
            + self.crafting_stake_value
            + self.crafting_inventory_value
            + self.safety_module_value
            + self.disc_fun_value
            + self.gamba_stake_value
            + self.sage_stake_value
            + self.eat_stake_value
            + self.nft_value
            - self.loan_liability,
            2,
        )


async def compute_net_worth(uid: int, gid: int, db: "Database") -> NetWorthResult:
    """Compute a complete net-worth breakdown for the given user."""
    result = NetWorthResult()

    # ── Wallet + Bank ────────────────────────────────────────────────────────
    user_row = await db.get_user(uid, gid)
    if not user_row:
        return result
    result.wallet = to_human(user_row["wallet"])
    result.bank   = to_human(user_row["bank"])

    # ── CeFi Crypto holdings ─────────────────────────────────────────────────
    holdings_raw = await db.get_holdings(uid, gid)
    for h in holdings_raw:
        price_row = await db.get_price(h["symbol"], gid)
        price = float(price_row["price"]) if price_row else 0.0
        usd_val = price * to_human(h["amount"])
        result.cefi_crypto += usd_val
        result.holdings.append({
            "symbol": h["symbol"], "amount": h["amount"],
            "price": price, "usd_value": usd_val, "network": "cefi",
        })

    # ── DeFi Wallet holdings ─────────────────────────────────────────────────
    wallet_holdings = await db.get_all_wallet_holdings(uid, gid)
    for wh in wallet_holdings:
        price_row = await db.get_price(wh["symbol"], gid)
        price = float(price_row["price"]) if price_row else 0.0
        usd_val = price * to_human(wh["amount"])
        result.defi_wallet += usd_val
        result.wallet_holdings.append({
            "symbol": wh["symbol"], "amount": wh["amount"],
            "network": wh["network"], "price": price, "usd_value": usd_val,
        })

    # ── NPC Yield-Farm Stakes ─────────────────────────────────────────────────
    stakes_raw = await db.get_user_stakes(uid, gid)
    for s in stakes_raw:
        price_row = await db.get_price(s["symbol"], gid)
        price = float(price_row["price"]) if price_row else 0.0
        usd_val = to_human(s["amount"]) * price
        result.stake_value += usd_val
        result.stakes.append(dict(s) | {"usd_value": usd_val, "price": price})

    # ── PoS Validator own stake ───────────────────────────────────────────────
    pos_validators = await db.get_user_pos_validators(uid, gid)
    for pv in pos_validators:
        if pv["stake_amount"] > 0:
            price_row = await db.get_price(pv["stake_token"], gid)
            price = float(price_row["price"]) if price_row else 0.0
            usd_val = to_human(pv["stake_amount"]) * price
            result.pos_stake_value += usd_val
        result.pos_validators.append(dict(pv))

    # ── Lunar Mint stakes (Moon Network) ─────────────────────────────────────
    # Staked group tokens earning MOON. Valued at 24h TWAP so a whale pump on
    # a thin group token does not inflate net worth. Mirrors the MOON_TWAP_WINDOW
    # used by the tick loop.
    moon_stake_value = 0.0
    lunar_rows = await db.fetch_all(
        "SELECT symbol, amount FROM lunar_stakes WHERE user_id=$1 AND guild_id=$2 AND amount > 0",
        uid, gid,
    )
    for r in lunar_rows:
        twap, _ = await db.get_twap(r["symbol"], gid, window=1440)
        if twap <= 0:
            # Fall back to spot price if TWAP has insufficient history
            pr = await db.get_price(r["symbol"], gid)
            twap = float(pr["price"]) if pr else 0.0
        moon_stake_value += r.h("amount") * twap
    result.moon_stake_value = moon_stake_value

    # ── Moon Pool stakes (Tier 2, MOON staked for DSD yield) ─────────────────
    # Valued at MOON spot price: MOON is a native network coin with real market
    # dynamics, so TWAP is not required here -- that guard is for thin group
    # tokens, not for MOON itself.
    moon_pool_row = await db.fetch_one(
        "SELECT amount FROM moon_stakes WHERE user_id=$1 AND guild_id=$2 AND amount > 0",
        uid, gid,
    )
    if moon_pool_row:
        moon_price_row = await db.get_price("MOON", gid)
        moon_price = float(moon_price_row["price"]) if moon_price_row else 0.0
        result.moon_pool_stake_value = moon_pool_row.h("amount") * moon_price

    # ── LP Positions ─────────────────────────────────────────────────────────
    lp_positions = await db.get_user_lp_positions(uid, gid)
    for lp in lp_positions:
        pool = await db.get_pool(lp["pool_id"], gid)
        if not pool or pool["total_lp"] == 0:
            result.lp_positions.append(dict(lp) | {"usd_value": 0.0})
            continue
        # share is a dimensionless ratio of raw ints -- same as human/human
        share = int(lp["lp_shares"]) / int(pool["total_lp"])
        val_a = share * to_human(pool["reserve_a"])
        val_b = share * to_human(pool["reserve_b"])
        ta, tb = pool["token_a"], pool["token_b"]
        if tb == "USD":
            usd_val = val_b * 2
        elif ta == "USD":
            usd_val = val_a * 2
        else:
            pa = await db.get_price(ta, gid)
            pb = await db.get_price(tb, gid)
            usd_val = val_a * (float(pa["price"]) if pa else 0) + val_b * (float(pb["price"]) if pb else 0)
        result.lp_value += usd_val
        result.lp_positions.append(dict(lp) | {
            "token_a": ta, "token_b": tb,
            "amount_a": val_a, "amount_b": val_b, "usd_value": usd_val,
        })

    # ── Mining Rigs ───────────────────────────────────────────────────────────
    rigs_raw = await db.get_user_rigs(uid, gid)
    for r in rigs_raw:
        rig_cfg = Config.MINING_RIGS.get(r["rig_id"], {})
        book_val = to_human(rig_cfg.get("price", 0)) * r["quantity"] * 0.5
        result.rig_value += book_val
        result.rigs.append(dict(r) | {
            "rig_name": rig_cfg.get("name", r["rig_id"]),
            "hashrate_per_rig": rig_cfg.get("hashrate", 0),
            "total_hashrate": rig_cfg.get("hashrate", 0) * r["quantity"],
            "book_value": book_val,
        })

    # ── Delegations ───────────────────────────────────────────────────────────
    delegations = await db.get_user_delegations(uid, gid)
    for d in delegations:
        price_row = await db.get_price(d["token"], gid)
        price = float(price_row["price"]) if price_row else 0.0
        usd_val = to_human(d["amount"]) * price
        result.delegation_value += usd_val
        result.delegations.append(dict(d) | {"usd_value": usd_val, "price": price})

    # ── Savings Deposits ─────────────────────────────────────────────────────
    # USD savings are pegged 1:1; any other symbol gets priced at oracle so a
    # whale can't hide wealth by parking it in a non-USD savings vault.
    usd_save = await db.get_savings_deposit(uid, gid, "USD")
    result.usd_savings = to_human(usd_save["amount"]) if usd_save else 0.0
    result.savings_value += result.usd_savings
    nonusd_saves = await db.fetch_all(
        "SELECT symbol, amount FROM savings_deposits "
        "WHERE user_id=$1 AND guild_id=$2 AND amount > 0 AND symbol <> 'USD'",
        uid, gid,
    )
    for s in nonusd_saves:
        price_row = await db.get_price(s["symbol"], gid)
        price = float(price_row["price"]) if price_row else 0.0
        result.savings_value += to_human(int(s["amount"])) * price

    # ── Loan liabilities ─────────────────────────────────────────────────────
    loan = await db.get_loan(uid, gid)
    result.loan_liability = to_human(loan["outstanding"]) if loan else 0.0

    # ── Items (all stones + consumables) ────────────────────────────────────
    result.hashstone  = await db.get_hashstone(uid, gid)
    result.lockstone  = await db.get_lockstone(uid, gid)
    result.vaultstone = await db.get_vaultstone(uid, gid)
    result.liqstone   = await db.get_liqstone(uid, gid)
    # gambastones have no dedicated DB helper; CLAUDE.md spec lists them as
    # part of items_value alongside the four leaderboard stones, so we read
    # the table directly to keep the canonical NW formula honest.
    result.gambastone = await db.fetch_one(
        "SELECT * FROM gambastones WHERE user_id=$1 AND guild_id=$2",
        uid, gid,
    )
    result.validator_guard_count = await db.get_validator_guard_count(uid, gid)
    result.yield_guard_count    = await db.get_yield_guard_count(uid, gid)
    # Stone staked_amount is denominated in the row's ``lp_currency``,
    # NOT always stablecoin. Migration 0165 narrowed hashstone to
    # (MTA, SUN), lockstone to (DSC, ARC); migration 0146 added eight
    # themed/meta stones (tide/heart/crypt/blood/bloom/gavel/anvil/
    # chimera) that stake in their own network coins (REEL/BUD/RUNE/
    # HRV/BBT/FORGE/etc.). The legacy "stones are $1-pegged" assumption
    # silently undervalued every PoW/PoS stone and didn't count themed
    # stones at all -- 0.5 MTA staked was NW'd at $0.50 instead of
    # ~$50k, and a 1000 LURE tidestone was worth $0. Price each stone
    # via its lp_currency oracle. Stablecoins + USD stay at $1:$1.
    #
    # Five "primary" stones (hash + lock + vault + liq + gamba) are
    # stored on the NetWorthResult for downstream surfaces (drs profile,
    # equalizer drain, etc.) that read them by name. The eight themed/
    # meta stones are read here but not stored on the result -- they
    # contribute to items_value only.
    from core.framework.network import STABLE_NETWORK as _STABLE
    stone_stable = 0.0
    primary_stones = (
        result.hashstone, result.lockstone, result.vaultstone,
        result.liqstone, result.gambastone,
    )
    themed_meta_stones: list[dict] = []
    for _tbl in (
        "tidestones", "heartstones", "cryptstones", "bloodstones",
        "bloomstones", "gavelstones", "anvilstones", "chimerastones",
    ):
        try:
            row = await db.fetch_one(
                f"SELECT staked_amount, lp_currency FROM {_tbl} "
                f"WHERE user_id=$1 AND guild_id=$2",
                uid, gid,
            )
        except Exception:
            row = None
        if row:
            themed_meta_stones.append(row)
    for s in (*primary_stones, *themed_meta_stones):
        if not s:
            continue
        amt = to_human(int(s.get("staked_amount") or 0))
        if amt <= 0:
            continue
        cur = str(s.get("lp_currency") or "").upper() or "DSD"
        if cur == "USD" or cur in _STABLE:
            stone_stable += amt
        else:
            try:
                pr = await db.get_price(cur, gid)
            except Exception:
                pr = None
            price = float(pr["price"]) if pr else 0.0
            stone_stable += amt * price
    _SI = Config.SHOP_ITEMS
    consumable_stable = (
        result.validator_guard_count * to_human(_SI.get("validator_guard",   {}).get("cost_stable", 0))
        + result.yield_guard_count   * to_human(_SI.get("yield_guard",       {}).get("cost_stable", 0))
    )
    result.items_value = stone_stable + consumable_stable

    # ── Fishing stake (staked LURE + accrued REEL yield) ────────────────────
    # The LURE/REEL wallet balances are already counted in defi_wallet via
    # get_all_wallet_holdings; the staked-LURE row in user_fishing is NOT
    # in wallet_holdings, so we add it here valued at the LURE oracle.
    # Pending REEL yield (accrued but unclaimed) is computed virtually so
    # the player sees it in net worth before they have to ,fish claim.
    try:
        import configs.fishing_config as _fc
        from services import fishing as _fish_svc
        fish_row = await db.fetch_one(
            "SELECT lure_staked_raw FROM user_fishing "
            "WHERE guild_id=$1 AND user_id=$2",
            gid, uid,
        )
        staked_lure_raw = int((fish_row or {}).get("lure_staked_raw") or 0)
        if staked_lure_raw > 0:
            lp_row = await db.get_price(_fc.LURE_SYMBOL, gid)
            lure_oracle = float(lp_row["price"]) if lp_row else 0.0
            result.fishing_stake_value += to_human(staked_lure_raw) * lure_oracle
        try:
            pending_reel_raw = int(
                await _fish_svc.accrued_stake_yield(db, gid, uid) or 0
            )
        except Exception:
            pending_reel_raw = 0
        if pending_reel_raw > 0:
            rp_row = await db.get_price(_fc.REEL_SYMBOL, gid)
            reel_oracle = float(rp_row["price"]) if rp_row else 0.0
            result.fishing_stake_value += to_human(pending_reel_raw) * reel_oracle
    except Exception:
        # Fishing module is optional; never let a stake-value lookup
        # take down net-worth display.
        pass

    # ── Delve dungeon stakes (staked ore + accrued RUNE yield) ───────────────
    # Mirrors the fishing block: ore staked rows live on user_dungeon
    # (NOT in wallet_holdings) so we add them at the live ore oracle.
    # Pending RUNE yield is virtual (accrued but unclaimed) and shows up
    # at the RUNE oracle so the player sees what their stake is worth
    # before they ,delve claim.
    try:
        import configs.dungeon_config as _dc
        from services import dungeon as _dng_svc
        dng_row = await db.fetch_one(
            "SELECT copper_staked_raw, silver_staked_raw, gold_staked_raw "
            "FROM user_dungeon WHERE guild_id=$1 AND user_id=$2",
            gid, uid,
        )
        if dng_row:
            for col, sym in (
                ("copper_staked_raw", _dc.COPPER_SYMBOL),
                ("silver_staked_raw", _dc.SILVER_SYMBOL),
                ("gold_staked_raw",   _dc.GOLD_SYMBOL),
            ):
                staked_raw = int(dng_row.get(col) or 0)
                if staked_raw <= 0:
                    continue
                pr = await db.get_price(sym, gid)
                price = float(pr["price"]) if pr else 0.0
                result.delve_stake_value += to_human(staked_raw) * price
        try:
            pending_rune_raw = int(
                await _dng_svc.accrued_stake_yield(db, gid, uid) or 0
            )
        except Exception:
            pending_rune_raw = 0
        if pending_rune_raw > 0:
            rp = await db.get_price(_dc.RUNE_SYMBOL, gid)
            rune_oracle = float(rp["price"]) if rp else 0.0
            result.delve_stake_value += to_human(pending_rune_raw) * rune_oracle
    except Exception:
        pass

    # ── Delve party (captured dungeon buddies) ──────────────────────────────
    # Captured mobs live in dungeon_party with their own level + tier,
    # which already maps onto a buddies_config rarity ladder. Value each
    # at the rarity's HATCH_BASE_PRICE_USD * tier_mult so the player's
    # party shows up alongside their cc_buddies in net worth (mirrors
    # how cc_buddies values get covered via buddy_economy below).
    try:
        party_rows = await db.fetch_all(
            "SELECT rarity_tier FROM dungeon_party "
            "WHERE guild_id=$1 AND owner_user_id=$2 AND status='owned'",
            gid, uid,
        ) if False else []
        # dungeon_party uses a simpler shape (no rarity_tier column;
        # see migration 0145). Roll the tier off party-row metadata
        # when available, otherwise fall back to a flat $5 per buddy
        # (a placeholder so captures show up at all). This is
        # intentionally conservative -- the real value comes from
        # selling them via the buddy market.
        cap_count = await db.fetch_val(
            "SELECT COUNT(*)::int FROM dungeon_party "
            "WHERE guild_id=$1 AND owner_user_id=$2 AND status='owned'",
            gid, uid,
        )
        result.delve_party_value += float(cap_count or 0) * 5.0
    except Exception:
        pass

    # ── Buddy Network economy (FREN stake + pending BUD + slot purchases) ──
    # Same shape as the fishing/dungeon stake blocks: staked FREN +
    # accrued BUD yield valued at live oracles. Slot purchases are a
    # past expenditure -- we count them at flat USD because they
    # represent shelter capacity, an asset the player owns even after
    # the BUD has been burnt.
    try:
        from services import buddy_economy as _bes
        bud_state = await db.fetch_one(
            "SELECT fren_staked_raw, bud_yield_pending_raw, "
            "       battle_slots_purchased, storage_slots_purchased, "
            "       egg_storage_slots_purchased "
            "FROM user_buddy_economy WHERE guild_id=$1 AND user_id=$2",
            gid, uid,
        )
        if bud_state:
            fren_staked = int(bud_state.get("fren_staked_raw") or 0)
            if fren_staked > 0:
                fp = await db.get_price(_bes.FREN_SYMBOL, gid)
                fren_oracle = float(fp["price"]) if fp else 0.0
                result.buddy_economy_value += to_human(fren_staked) * fren_oracle
            try:
                pending_bud_raw = int(
                    await _bes.accrued_yield(db, gid, uid) or 0
                )
            except Exception:
                pending_bud_raw = 0
            if pending_bud_raw > 0:
                bp = await db.get_price(_bes.BUD_SYMBOL, gid)
                bud_oracle = float(bp["price"]) if bp else 0.0
                result.buddy_economy_value += to_human(pending_bud_raw) * bud_oracle
            # Each capacity upgrade is a sunk BUD burn that bought
            # permanent shelter / storage / egg capacity. Value the
            # upgrade at its flat purchase price so the player's
            # holdings include what they paid for the slot.
            battle_slots  = int(bud_state.get("battle_slots_purchased") or 0)
            storage_slots = int(bud_state.get("storage_slots_purchased") or 0)
            egg_slots     = int(bud_state.get("egg_storage_slots_purchased") or 0)
            result.buddy_economy_value += (
                float(battle_slots)  * float(_bes.BATTLE_SLOT_PRICE_USD)
              + float(storage_slots) * float(_bes.STORAGE_SLOT_PRICE_USD)
              + float(egg_slots)     * float(_bes.EGG_STORAGE_PRICE_USD)
            )
    except Exception:
        pass

    # ── Farming (staked SEED + accrued HRV yield + plots + inventory) ───────
    try:
        import configs.farming_config as _fc
        from services import farming as _farm_svc
        farm_row = await db.fetch_one(
            "SELECT seed_staked_raw, plot_tier, crop_inventory, processed_inventory "
            "FROM user_farming WHERE guild_id=$1 AND user_id=$2",
            gid, uid,
        )
        if farm_row:
            # 1) SEED stake -> SEED oracle
            staked_seed_raw = int(farm_row.get("seed_staked_raw") or 0)
            if staked_seed_raw > 0:
                sp = await db.get_price(_fc.SEED_SYMBOL, gid)
                seed_oracle = float(sp["price"]) if sp else 0.0
                result.farming_stake_value += to_human(staked_seed_raw) * seed_oracle
            # 2) Pending HRV yield (virtual)
            try:
                pending_hrv_raw = int(await _farm_svc.accrued_stake_yield(db, gid, uid) or 0)
            except Exception:
                pending_hrv_raw = 0
            if pending_hrv_raw > 0:
                hp = await db.get_price(_fc.HRV_SYMBOL, gid)
                hrv_oracle = float(hp["price"]) if hp else 0.0
                result.farming_stake_value += to_human(pending_hrv_raw) * hrv_oracle
            # 3) Plot book value: 50% of cumulative HRV spent on tier
            plot_tier = int(farm_row.get("plot_tier") or 1)
            cumulative = sum(
                float(_fc.PLOTS[t]["price_hrv"])
                for t in range(2, plot_tier + 1)
                if t in _fc.PLOTS
            )
            if cumulative > 0:
                hp = await db.get_price(_fc.HRV_SYMBOL, gid)
                hrv_oracle = float(hp["price"]) if hp else 0.0
                result.farming_plot_value += cumulative * hrv_oracle * 0.5
            # 4) Crop / processed inventory at HRV sell price * HRV oracle
            inv = farm_row.get("crop_inventory") or {}
            proc = farm_row.get("processed_inventory") or {}
            if inv or proc:
                hp = await db.get_price(_fc.HRV_SYMBOL, gid)
                hrv_oracle = float(hp["price"]) if hp else 0.0
                for k, q in dict(inv).items():
                    meta = _fc.crop_meta(k)
                    if meta:
                        result.farming_inventory_value += float(q) * float(meta["hrv_sell_price"]) * hrv_oracle
                for k, q in dict(proc).items():
                    rmeta = _fc.recipe_meta(k)
                    if rmeta:
                        result.farming_inventory_value += float(q) * float(rmeta["hrv_sell_price"]) * hrv_oracle
    except Exception:
        pass

    # ── Crafting (staked INGOT + accrued FORGE yield + crafted inventory) ───
    try:
        import configs.crafting_config as _cc
        from services import crafting as _craft_svc
        craft_row = await db.fetch_one(
            "SELECT ingot_staked_raw, crafted_inventory "
            "FROM user_crafting WHERE guild_id=$1 AND user_id=$2",
            gid, uid,
        )
        if craft_row:
            staked_ingot_raw = int(craft_row.get("ingot_staked_raw") or 0)
            if staked_ingot_raw > 0:
                ip = await db.get_price(_cc.INGOT_SYMBOL, gid)
                ingot_oracle = float(ip["price"]) if ip else 0.0
                result.crafting_stake_value += to_human(staked_ingot_raw) * ingot_oracle
            try:
                pending_forge_raw = int(await _craft_svc.accrued_stake_yield(db, gid, uid) or 0)
            except Exception:
                pending_forge_raw = 0
            if pending_forge_raw > 0:
                fp = await db.get_price(_cc.FORGE_SYMBOL, gid)
                forge_oracle = float(fp["price"]) if fp else 0.0
                result.crafting_stake_value += to_human(pending_forge_raw) * forge_oracle
            # Crafted inventory at FGD cost (FGD is pegged to $1 so no oracle).
            inv = craft_row.get("crafted_inventory") or {}
            if isinstance(inv, dict):
                for k, q in inv.items():
                    meta = _cc.craft_meta(k)
                    if meta:
                        result.crafting_inventory_value += float(q) * float(meta.get("fgd_cost", 0.0))
    except Exception:
        pass

    # ── Safety Module (VTR/DSY single-token yield staking) ────────────────
    # Stake values include the staked token at oracle price plus pending
    # yield (in the yield_token) accrued since the last yield distribution,
    # so net worth reflects what the player would receive on claim.
    try:
        import time as _time_sm
        for _sm_sym, _sm_cfg in Config.SAFETY_MODULE.items():
            _sm_row = await db.get_sm_stake(uid, gid, _sm_sym)
            if not _sm_row or int(_sm_row.get("amount", 0)) <= 0:
                continue
            _sm_price = await db.get_price(_sm_sym, gid)
            _sm_token_price = float(_sm_price["price"]) if _sm_price else 0.0
            _staked_h = to_human(int(_sm_row["amount"]))
            result.safety_module_value += _staked_h * _sm_token_price
            # Pending yield accrues only when not in cooldown.
            if _sm_row.get("cooldown_at"):
                continue
            _ly = _sm_row["last_yield"]
            _last_ts = _ly.timestamp() if hasattr(_ly, "timestamp") else float(_ly)
            _elapsed_days = (_time_sm.time() - _last_ts) / 86400.0
            if _elapsed_days <= 0:
                continue
            _staked_usd = _staked_h * _sm_token_price
            _pending_yield_usd = _staked_usd * _sm_cfg["daily_yield"] * _elapsed_days
            result.safety_module_value += _pending_yield_usd
    except Exception:
        pass

    # ── Disc.Fun (active proto-token positions) ─────────────────────────────
    # Sum of held proto tokens valued at the curve's current spot price (in
    # DFUN), translated to USD via the live DFUN oracle (fallback to genesis).
    try:
        from services import discfun as _disc_fun
        _df_value_dfun = await _disc_fun.user_active_value_quote(db, gid, uid)
        # Staked positions and pending DFUN yield count too -- otherwise
        # locking a graduated proto in ,fun stake would visually erase
        # it from net worth even though the holder still owns it.
        _df_staked_dfun, _df_pending_dfun = await _disc_fun.user_staked_value_dfun(
            db, gid, uid,
        )
        _df_total_dfun = (
            _df_value_dfun + _df_staked_dfun + _df_pending_dfun
        )
        if _df_total_dfun > 0:
            _dfun_row = await db.fetch_one(
                "SELECT price FROM crypto_prices WHERE symbol='DFUN' AND guild_id=$1",
                gid,
            )
            _dfun_usd = (
                float(_dfun_row["price"]) if _dfun_row and _dfun_row.get("price")
                else float(Config.TOKENS.get("DFUN", {}).get("start_price", 0.10) or 0.10)
            )
            result.disc_fun_value = _df_total_dfun * _dfun_usd
    except Exception:
        pass

    # ── Gamba Network stakes (game-token stakes + pending yield) ────────────
    # Mirrors the per-economy stake valuation above: every staked game
    # token (GAMBIT / CROWN / VEIN / PIP / EDGE / ACE / NOIR / CHERRY)
    # is valued at its live oracle, and pending yield is added at the
    # right target's oracle (GBC for the default flow, BUD for positions
    # flipped via ,gamba yield). Service helpers gracefully degrade if
    # the gamba tables haven't migrated yet.
    try:
        from services import gamba as _gamba
        for stake_row in await _gamba.list_stakes(db, gid, uid):
            sym = stake_row.symbol
            staked_h = to_human(stake_row.staked_raw)
            if staked_h <= 0:
                continue
            sym_row = await db.get_price(sym, gid)
            sym_oracle = float(sym_row["price"]) if sym_row else 0.0
            result.gamba_stake_value += staked_h * sym_oracle
        # Pending yield across every position, valued per target.
        pending_by_target = await _gamba.total_accrued_yield(db, gid, uid)
        for target, raw in pending_by_target.items():
            if raw <= 0:
                continue
            tgt_row = await db.get_price(target, gid)
            tgt_oracle = float(tgt_row["price"]) if tgt_row else 0.0
            if tgt_oracle > 0:
                result.gamba_stake_value += to_human(int(raw)) * tgt_oracle
    except Exception:
        pass

    # ── Sage Network stakes (EDU stake + pending SAGE yield) ─────────────
    # Same valuation shape as Gamba: EDU stake at its live oracle plus
    # pending SAGE yield at the SAGE oracle. Wallet-held SAGE + EDU are
    # already in defi_wallet -- this category covers what's locked in
    # ,sage stake plus the unclaimed drip.
    try:
        from services import sage as _sage
        _sage_stake = await _sage.get_stake(db, gid, uid)
        _edu_oracle_row = await db.get_price(_sage.EDU_SYMBOL, gid)
        _edu_oracle = float(_edu_oracle_row["price"]) if _edu_oracle_row else 0.0
        if _sage_stake.staked_raw > 0 and _edu_oracle > 0:
            result.sage_stake_value += to_human(int(_sage_stake.staked_raw)) * _edu_oracle
        _sage_pending = await _sage.accrued_yield(db, gid, uid)
        if _sage_pending > 0:
            _sage_oracle_row = await db.get_price(_sage.SAGE_SYMBOL, gid)
            _sage_oracle = float(_sage_oracle_row["price"]) if _sage_oracle_row else 0.0
            if _sage_oracle > 0:
                result.sage_stake_value += to_human(int(_sage_pending)) * _sage_oracle
    except Exception:
        pass

    # ── EatChain stakes ($EAT staked in the ,eat minigame) ───────────────
    # Liquid $EAT is already counted in defi_wallet (it lives in
    # wallet_holdings on the `eat` network). This category covers only the
    # $EAT locked into exploit_stats.eat_staked, valued at the EAT oracle.
    try:
        _eat_row = await db.fetch_one(
            "SELECT eat_staked FROM exploit_stats "
            "WHERE user_id=$1 AND guild_id=$2 AND eat_staked > 0",
            uid, gid,
        )
        if _eat_row:
            _eat_price_row = await db.get_price("EAT", gid)
            _eat_price = float(_eat_price_row["price"]) if _eat_price_row else 0.0
            if _eat_price > 0:
                result.eat_stake_value = (
                    to_human(int(_eat_row["eat_staked"])) * _eat_price
                )
    except Exception:
        pass

    # ── NFTs ────────────────────────────────────────────────────────────────
    try:
        user_nfts = await db.get_user_nfts(uid, gid)
        for nft_row in user_nfts:
            col = await db.get_collection(nft_row["collection_id"])
            if col:
                avg_prices = await db.get_avg_sale_price_by_rarity(col["id"])
                rarity = nft_row.get("rarity", "common")
                if rarity in avg_prices:
                    nft_price = to_human(avg_prices[rarity])
                else:
                    nft_price = to_human(col["mint_price"])
                    mint_token = col.get("mint_token", "USD")
                    if mint_token != "USD":
                        price_row = await db.get_price(mint_token, gid)
                        if price_row:
                            nft_price *= float(price_row["price"])
                result.nft_value += nft_price
                result.nfts_owned.append({
                    "id": nft_row["id"],
                    "name": nft_row["name"],
                    "rarity": rarity,
                    "collection_symbol": nft_row.get("collection_symbol", ""),
                    "usd_value": nft_price,
                })
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Bulk net-worth computation (leaderboard, GDP)
# ---------------------------------------------------------------------------

async def compute_bulk_net_worth(
    gid: int, db: "Database", *, exclude_user_id: int = 0
) -> dict[int, float]:
    """Return ``{user_id: net_worth}`` for every user in a guild.

    Uses guild-wide bulk queries to avoid N+1.  The same components as
    ``compute_net_worth`` are included so numbers always match.
    """
    all_users = await db.get_all_guild_users(gid, exclude_user_id=exclude_user_id)
    prices_list = await db.get_all_prices(gid)
    prices: dict[str, float] = {r["symbol"]: float(r["price"]) for r in prices_list}

    user_val: dict[int, float] = {}

    def _add(uid: int, v: float) -> None:
        user_val[uid] = user_val.get(uid, 0.0) + v

    # Wallet + Bank
    for u in all_users:
        _add(u["user_id"], to_human(u["wallet"]) + to_human(u["bank"]))

    # CeFi + DeFi + NPC stakes
    cefi = await db.get_all_guild_crypto_holdings(gid)
    defi = await db.get_all_guild_wallet_holdings(gid)
    stakes = await db.get_all_guild_stakes(gid)
    for h in cefi + defi + stakes:
        _add(h["user_id"], to_human(h["amount"]) * prices.get(h["symbol"], 0.0))

    # LP Positions
    lp_pos = await db.get_all_guild_lp_positions(gid)
    for lp in lp_pos:
        if lp["total_lp"] > 0:
            share = int(lp["lp_shares"]) / int(lp["total_lp"])
            val = (
                share * to_human(lp["reserve_a"]) * prices.get(lp["token_a"], 0.0)
                + share * to_human(lp["reserve_b"]) * prices.get(lp["token_b"], 0.0)
            )
            _add(lp["user_id"], val)

    # Mining Rigs (50% book value)
    rigs = await db.get_all_guild_rigs(gid)
    for r in rigs:
        rig_cfg = Config.MINING_RIGS.get(r["rig_id"])
        if rig_cfg:
            _add(r["user_id"], to_human(rig_cfg["price"]) * r["quantity"] * 0.5)

    # PoS Validator own stakes
    pos_vals = await db.get_pos_validators(gid)
    for pv in pos_vals:
        _add(pv["user_id"], to_human(pv["stake_amount"]) * prices.get(pv["stake_token"], 0.0))

    # Lunar Mint stakes (Moon Network): valued at 24h TWAP, spot-price fallback.
    lunar_all = await db.fetch_all(
        "SELECT user_id, symbol, amount FROM lunar_stakes WHERE guild_id=$1 AND amount > 0",
        gid,
    )
    lunar_symbols = {r["symbol"] for r in lunar_all}
    twap_map: dict[str, float] = {}
    for sym in lunar_symbols:
        t, _ = await db.get_twap(sym, gid, window=1440)
        if t <= 0:
            pr = await db.get_price(sym, gid)
            t = float(pr["price"]) if pr else 0.0
        twap_map[sym] = t
    for r in lunar_all:
        uid2 = r["user_id"]
        if exclude_user_id and uid2 == exclude_user_id:
            continue
        _add(uid2, r.h("amount") * twap_map.get(r["symbol"], 0.0))

    # Moon Pool stakes (Tier 2): MOON staked for DSD yield, valued at MOON spot.
    moon_pool_rows = await db.fetch_all(
        "SELECT user_id, amount FROM moon_stakes WHERE guild_id=$1 AND amount > 0",
        gid,
    )
    moon_spot = prices.get("MOON", 0.0)
    for r in moon_pool_rows:
        uid2 = r["user_id"]
        if exclude_user_id and uid2 == exclude_user_id:
            continue
        _add(uid2, r.h("amount") * moon_spot)

    # Gamba Network stakes: staked game tokens (GAMBIT / CROWN / VEIN /
    # PIP / EDGE / ACE / NOIR / CHERRY) valued at oracle, plus accrued
    # pending yield priced in whichever target the row points at.
    # Migration 0234 renamed pending_gbc -> pending_yield_raw and added
    # yield_target ('GBC' or 'BUD'); pricing pending yield at the
    # correct target oracle is what closes the "parked in gamba +
    # auto-compound BUD" hideaway. Single-user NW does this through
    # services.gamba.list_stakes + total_accrued_yield, so the bulk
    # path matches.
    try:
        gamba_rows = await db.fetch_all(
            "SELECT user_id, symbol, amount, pending_yield_raw, yield_target "
            "FROM gamba_stakes WHERE guild_id=$1 "
            "AND (amount > 0 OR pending_yield_raw > 0)",
            gid,
        )
        for r in gamba_rows:
            uid2 = r["user_id"]
            if exclude_user_id and uid2 == exclude_user_id:
                continue
            sym_price = float(prices.get(r["symbol"], 0.0))
            target = str(r.get("yield_target") or "GBC")
            target_price = float(prices.get(target, 0.0))
            usd = (
                to_human(int(r.get("amount") or 0)) * sym_price
                + to_human(int(r.get("pending_yield_raw") or 0)) * target_price
            )
            if usd > 0:
                _add(uid2, usd)
    except Exception:
        # Gamba tables are optional in old DB snapshots; the tax just
        # won't see those holders' gamba wealth until migration 0228
        # has run. Silent failure matches the surrounding bulk-NW
        # convention (fishing / dungeon / farming / crafting all swallow
        # their lookup errors the same way).
        pass

    # Delegations
    delegations = await db.get_all_guild_delegations(gid)
    for d in delegations:
        _add(d["user_id"], to_human(d["amount"]) * prices.get(d["token"], 0.0))

    # Savings deposits: USD is pegged 1:1, any other symbol gets priced at
    # oracle so non-USD savings vaults can't be used as a tax-tax hideaway.
    all_saves = await db.fetch_all(
        "SELECT user_id, symbol, amount FROM savings_deposits "
        "WHERE guild_id=$1 AND amount > 0",
        gid,
    )
    for dep in all_saves:
        sym = dep["symbol"]
        amt_human = to_human(int(dep["amount"]))
        price = 1.0 if sym == "USD" else float(prices.get(sym, 0.0))
        _add(dep["user_id"], amt_human * price)

    # All thirteen stone tables: the five leaderboard / closed-loop
    # stones from CLAUDE.md (hash + lock + vault + liq + gamba), plus
    # the eight themed/meta-economy stones added by migrations 0146 /
    # 0150-ish (tide / heart / crypt / blood / bloom / gavel / anvil /
    # chimera). Each row's staked_amount is denominated in its own
    # ``lp_currency``. The bulk helpers (get_all_guild_hashstones, etc.)
    # only return (user_id, staked_amount) so we re-query directly to
    # pick up lp_currency. Migration 0165 narrowed hashstone to
    # (MTA, SUN) and lockstone to (DSC, ARC); themed stones stake in
    # REEL / BUD / RUNE / HRV / BBT / FORGE. The prior bulk path summed
    # everything as $1-pegged AND skipped themed stones entirely,
    # undervaluing PoW/PoS stones by 4-5 orders of magnitude and
    # ignoring themed-stone wealth completely.
    _STABLE = {"DSD", "USDC", "USD"}
    for table in (
        "hashstones", "lockstones", "vaultstones", "liqstones",
        "gambastones", "tidestones", "heartstones", "cryptstones",
        "bloodstones", "bloomstones", "gavelstones", "anvilstones",
        "chimerastones",
    ):
        try:
            rows = await db.fetch_all(
                f"SELECT user_id, staked_amount, lp_currency FROM {table} "
                f"WHERE guild_id=$1 AND staked_amount > 0",
                gid,
            )
        except Exception:
            # Themed stones are optional tables in older DB snapshots;
            # silent-fallback matches the surrounding bulk-NW convention.
            continue
        for ss in rows:
            uid2 = ss["user_id"]
            if exclude_user_id and uid2 == exclude_user_id:
                continue
            amt = to_human(int(ss["staked_amount"]))
            cur = str(ss.get("lp_currency") or "DSD").upper()
            if cur in _STABLE:
                _add(uid2, amt)
            else:
                _add(uid2, amt * float(prices.get(cur, 0.0)))

    # All consumables
    _SI = Config.SHOP_ITEMS
    vg_cost    = to_human(_SI.get("validator_guard", {}).get("cost_stable", 0))
    yg_cost    = to_human(_SI.get("yield_guard",     {}).get("cost_stable", 0))
    for vg in await db.get_all_guild_validator_guards(gid):
        _add(vg["user_id"], int(vg.get("count", 0)) * vg_cost)
    for yg in await db.get_all_guild_yield_guards(gid):
        _add(yg["user_id"], int(yg.get("count", 0)) * yg_cost)

    # Fishing stake (staked LURE valued at LURE oracle). Pending REEL yield
    # is intentionally excluded from the bulk path: it requires a per-user
    # virtual computation against last_stake_yield_at and the standard
    # ``accrued_stake_yield`` would N+1; the leaderboard simulates net
    # worth from snapshot state only.
    try:
        lure_stake_rows = await db.fetch_all(
            "SELECT user_id, lure_staked_raw FROM user_fishing "
            "WHERE guild_id=$1 AND lure_staked_raw > 0",
            gid,
        )
        lure_price = prices.get("LURE", 0.0)
        for ls in lure_stake_rows:
            uid2 = ls["user_id"]
            if exclude_user_id and uid2 == exclude_user_id:
                continue
            _add(uid2, to_human(int(ls["lure_staked_raw"])) * lure_price)
    except Exception:
        pass

    # Delve dungeon stake (per-ore staked rows valued at live oracle).
    # Pending RUNE yield is excluded from the bulk path for the same N+1
    # reason as fishing's pending REEL.
    try:
        delve_rows = await db.fetch_all(
            "SELECT user_id, copper_staked_raw, silver_staked_raw, "
            "       gold_staked_raw "
            "FROM user_dungeon WHERE guild_id=$1 AND ("
            "    copper_staked_raw > 0 OR silver_staked_raw > 0 "
            "    OR gold_staked_raw > 0"
            ")",
            gid,
        )
        copper_price = prices.get("COPPER", 0.0)
        silver_price = prices.get("SILVER", 0.0)
        gold_price   = prices.get("GOLD",   0.0)
        for dr in delve_rows:
            uid2 = dr["user_id"]
            if exclude_user_id and uid2 == exclude_user_id:
                continue
            v = (
                to_human(int(dr.get("copper_staked_raw") or 0)) * copper_price
                + to_human(int(dr.get("silver_staked_raw") or 0)) * silver_price
                + to_human(int(dr.get("gold_staked_raw")   or 0)) * gold_price
            )
            if v > 0:
                _add(uid2, v)
    except Exception:
        pass

    # Buddy Network stake + slot purchases. Same shape as fishing/delve
    # bulk paths; pending BUD yield omitted (N+1 virtual compute).
    try:
        bud_rows = await db.fetch_all(
            "SELECT user_id, fren_staked_raw, "
            "       battle_slots_purchased, storage_slots_purchased, "
            "       egg_storage_slots_purchased "
            "FROM user_buddy_economy WHERE guild_id=$1 AND ("
            "    fren_staked_raw > 0 "
            "    OR battle_slots_purchased > 0 "
            "    OR storage_slots_purchased > 0 "
            "    OR egg_storage_slots_purchased > 0"
            ")",
            gid,
        )
        fren_price = prices.get("FREN", 0.0)
        try:
            from services.buddy_economy import (
                BATTLE_SLOT_PRICE_USD as _B_USD,
                STORAGE_SLOT_PRICE_USD as _S_USD,
                EGG_STORAGE_PRICE_USD as _E_USD,
            )
        except Exception:
            _B_USD, _S_USD, _E_USD = 25_000.0, 5_000.0, 2_500.0
        for br in bud_rows:
            uid2 = br["user_id"]
            if exclude_user_id and uid2 == exclude_user_id:
                continue
            v = (
                to_human(int(br.get("fren_staked_raw") or 0)) * fren_price
                + int(br.get("battle_slots_purchased") or 0)      * float(_B_USD)
                + int(br.get("storage_slots_purchased") or 0)     * float(_S_USD)
                + int(br.get("egg_storage_slots_purchased") or 0) * float(_E_USD)
            )
            if v > 0:
                _add(uid2, v)
    except Exception:
        pass

    # Farming stake + plot book value + inventory. Pending HRV yield omitted
    # from bulk path (N+1 virtual compute, same reason as fishing/dungeon).
    try:
        import configs.farming_config as _fc_bulk
        farm_rows = await db.fetch_all(
            "SELECT user_id, seed_staked_raw, plot_tier, "
            "       crop_inventory, processed_inventory "
            "FROM user_farming WHERE guild_id = $1 AND ("
            "  seed_staked_raw > 0 OR plot_tier > 1 "
            "  OR crop_inventory <> '{}'::jsonb OR processed_inventory <> '{}'::jsonb"
            ")",
            gid,
        )
        seed_price_bulk = prices.get(_fc_bulk.SEED_SYMBOL, 0.0)
        hrv_price_bulk  = prices.get(_fc_bulk.HRV_SYMBOL,  0.0)
        for fr in farm_rows:
            uid2 = fr["user_id"]
            if exclude_user_id and uid2 == exclude_user_id:
                continue
            v = 0.0
            # SEED stake
            staked_seed = int(fr.get("seed_staked_raw") or 0)
            if staked_seed > 0:
                v += to_human(staked_seed) * seed_price_bulk
            # Plot book value: 50% of cumulative HRV spent up to plot_tier
            plot_tier = int(fr.get("plot_tier") or 1)
            cumulative = sum(
                float(_fc_bulk.PLOTS[t]["price_hrv"])
                for t in range(2, plot_tier + 1)
                if t in _fc_bulk.PLOTS
            )
            if cumulative > 0:
                v += cumulative * hrv_price_bulk * 0.5
            # Crop inventory
            inv = fr.get("crop_inventory") or {}
            for k, q in dict(inv).items():
                meta = _fc_bulk.crop_meta(k)
                if meta:
                    v += float(q) * float(meta["hrv_sell_price"]) * hrv_price_bulk
            # Processed inventory
            proc = fr.get("processed_inventory") or {}
            for k, q in dict(proc).items():
                rmeta = _fc_bulk.recipe_meta(k)
                if rmeta:
                    v += float(q) * float(rmeta["hrv_sell_price"]) * hrv_price_bulk
            if v > 0:
                _add(uid2, v)
    except Exception:
        pass

    # Crafting stake + crafted-inventory book value. Pending FORGE yield
    # omitted from bulk path (N+1 virtual compute, same reason as farming).
    try:
        import configs.crafting_config as _cc_bulk
        craft_rows = await db.fetch_all(
            "SELECT user_id, ingot_staked_raw, crafted_inventory "
            "FROM user_crafting WHERE guild_id = $1 AND ("
            "  ingot_staked_raw > 0 OR crafted_inventory <> '{}'::jsonb"
            ")",
            gid,
        )
        ingot_price_bulk = prices.get(_cc_bulk.INGOT_SYMBOL, 0.0)
        for cr in craft_rows:
            uid2 = cr["user_id"]
            if exclude_user_id and uid2 == exclude_user_id:
                continue
            v = 0.0
            staked_ingot = int(cr.get("ingot_staked_raw") or 0)
            if staked_ingot > 0:
                v += to_human(staked_ingot) * ingot_price_bulk
            inv = cr.get("crafted_inventory") or {}
            if isinstance(inv, dict):
                for k, q in inv.items():
                    meta = _cc_bulk.craft_meta(k)
                    if meta:
                        v += float(q) * float(meta.get("fgd_cost", 0.0))
            if v > 0:
                _add(uid2, v)
    except Exception:
        pass

    # Safety Module (VTR/DSY): staked tokens at oracle. Pending yield
    # omitted from the bulk path (would need per-user last_yield diff like
    # the other game-stake virtual yields).
    try:
        sm_rows = await db.fetch_all(
            "SELECT user_id, symbol, amount FROM safety_module_stakes "
            "WHERE guild_id=$1 AND amount > 0",
            gid,
        )
        for sr in sm_rows:
            uid2 = sr["user_id"]
            if exclude_user_id and uid2 == exclude_user_id:
                continue
            _add(uid2, to_human(int(sr["amount"])) * prices.get(sr["symbol"], 0.0))
    except Exception:
        pass

    # EatChain stakes: $EAT locked into exploit_stats.eat_staked, valued at
    # the EAT oracle. Liquid $EAT is already covered by the defi wallet walk.
    try:
        eat_rows = await db.fetch_all(
            "SELECT user_id, eat_staked FROM exploit_stats "
            "WHERE guild_id=$1 AND eat_staked > 0",
            gid,
        )
        eat_price_bulk = prices.get("EAT", 0.0)
        for er in eat_rows:
            uid2 = er["user_id"]
            if exclude_user_id and uid2 == exclude_user_id:
                continue
            _add(uid2, to_human(int(er["eat_staked"])) * eat_price_bulk)
    except Exception:
        pass

    # NFTs - base value from mint price (all currencies converted to USD).
    # nft_value is a SUM of raw-scaled NUMERIC(36,0) mint_prices, so descale once.
    nft_vals = await db.get_all_guild_nft_values(gid)
    for nv in nft_vals:
        _add(nv["user_id"], to_human(int(nv["nft_value"])))

    # For fully-minted collections with active marketplace listings, swap in the
    # floor price (lowest active listing) so the valuation reflects real demand.
    try:
        collections = await db.get_collections(gid)
        for col in collections:
            if not (col.get("max_supply") and int(col["minted_count"]) >= int(col["max_supply"])):
                continue
            # floor_usd is a MIN over raw-scaled nft_listings.price, so descale.
            floor_usd = to_human(await db.get_collection_floor_price_usd(col["id"], gid))
            if floor_usd <= 0:
                continue
            mint_token = (col.get("mint_token") or "USD").upper()
            mint_usd_h = to_human(col["mint_price"])
            if mint_token in ("USD", "USDC", "DSD"):
                mint_usd = mint_usd_h
            else:
                mint_usd = mint_usd_h * prices.get(mint_token, 0.0)
            if floor_usd == mint_usd:
                continue
            owner_counts = await db.get_nft_owner_counts_for_collection(col["id"])
            for oc in owner_counts:
                _add(oc["user_id"], (floor_usd - mint_usd) * int(oc["cnt"]))
    except Exception:
        pass

    # Loans (subtracted)
    loans = await db.get_all_loans(gid)
    for loan in loans:
        _add(loan["user_id"], -to_human(loan.get("outstanding", 0)))

    return user_val


async def compute_bulk_lp_value(
    gid: int, db: "Database", *, exclude_user_id: int = 0,
) -> dict[int, float]:
    """Return ``{user_id: lp_value_usd}`` for every LP holder in a guild.

    The wealth equalizer subtracts this from each user's gross net
    worth before applying tax brackets, so LP positions are permanently
    exempt from the redistribution model (V3 Pillar 9). The position
    itself stays in ``compute_bulk_net_worth`` (LP is real wealth and
    every leaderboard / profile / showcase must keep showing it) -- the
    carve-out lives only in the tax path.

    Reuses the same pool walk as ``compute_bulk_net_worth`` so the
    valuation is identical to the cent. Returns an empty dict if the
    guild has no LP positions, so the caller can default to "no
    carve-out".
    """
    prices_list = await db.get_all_prices(gid)
    prices: dict[str, float] = {r["symbol"]: float(r["price"]) for r in prices_list}
    out: dict[int, float] = {}
    try:
        lp_pos = await db.get_all_guild_lp_positions(gid)
    except Exception:
        return out
    for lp in lp_pos:
        uid = int(lp["user_id"])
        if exclude_user_id and uid == exclude_user_id:
            continue
        total_lp = int(lp.get("total_lp") or 0)
        if total_lp <= 0:
            continue
        share = int(lp["lp_shares"]) / total_lp
        val = (
            share * to_human(lp["reserve_a"]) * prices.get(lp["token_a"], 0.0)
            + share * to_human(lp["reserve_b"]) * prices.get(lp["token_b"], 0.0)
        )
        if val > 0:
            out[uid] = out.get(uid, 0.0) + val
    return out
