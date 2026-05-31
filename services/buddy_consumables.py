"""services/buddy_consumables.py -- in-battle consumable effect logic.

Pure functions that mutate ``services.buddy_battle.Fighter`` state
based on ``buddies_config.BATTLE_CONSUMABLES``. The cog owns the
inventory side (decrement / round-CD bookkeeping); this module owns
the effect math.

The dropdown UI lives in cogs/buddy.py. Per-round CD bookkeeping uses
``Fighter.__dict__['item_cd']`` (a plain dict the cog sets up on Fighter
construction) and ``Fighter.__dict__['extra_atk_buff_rounds']`` etc.
We attach via ``__dict__`` because Fighter is a slotted dataclass --
adding new fields to the dataclass directly would require a coordinated
edit across the engine.

Public surface:

    can_use(fighter, item_key)            -> tuple[bool, str]
    apply(fighter, opponent, item_key,
          *, mastery_passives=None)       -> ApplyResult
    tick_cd(fighter, mastery_passives)    -> None
    consume_timed_buffs(fighter)          -> list[str]   # round-end ticks
    revive_if_armed(fighter)              -> str | None  # called on KO

Effects implemented (matches buddies_config.BATTLE_CONSUMABLES):
    heal_pct, atk_buff_temp, def_buff_temp, crit_next, spd_perm,
    cleanse_heal, shock_attack, revive
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from configs.buddies_config import battle_consumable

log = logging.getLogger(__name__)


# ── Result ──────────────────────────────────────────────────────────────

@dataclass(slots=True)
class ApplyResult:
    """Outcome of using a consumable in battle."""
    ok:          bool
    reason:      str
    log_line:    str
    hp_delta:    int = 0      # HP gained / lost on the user's fighter
    foe_damage:  int = 0      # damage dealt to the opponent (shock_bolt)
    foe_stun:    int = 0      # stun turns applied to the opponent
    cleared_debuffs: bool = False


# ── State accessors (attach via __dict__ since Fighter is slotted) ─────

def _state(f) -> dict:
    """Return the (lazy-created) per-battle consumable state dict on ``f``.

    Kept on ``Fighter.__dict__`` so we don't have to extend the dataclass
    slots. The dict is local to one battle.
    """
    d = getattr(f, "__dict__", None)
    if d is None:
        # Slotted; bypass via setattr on the underlying object
        try:
            f.__cstate__ = {}  # type: ignore[attr-defined]
            d = {"__cstate__": f.__cstate__}  # type: ignore[attr-defined]
        except Exception:
            return {}
    state = d.get("_consumable_state")
    if state is None:
        state = {
            "item_cd": {},          # {item_key: rounds_remaining}
            "atk_buff_rounds": 0,   # remaining rounds of atk_buff_temp
            "atk_buff_amount": 0.0, # buff currently applied (so we can revert)
            "def_buff_rounds": 0,
            "def_buff_amount": 0.0,
            "crit_next_pending": False,
            "revive_armed": False,
            "revive_pct": 0.0,
            "revive_used": False,
            "battle_log": [],
        }
        d["_consumable_state"] = state
    return state


def _passive(p: dict | None, key: str, default: float = 0.0) -> float:
    """Read a mastery passive value out of ``p`` (or default)."""
    if not p:
        return float(default)
    return float(p.get(key, default))


# ── Public API ──────────────────────────────────────────────────────────

def can_use(fighter, item_key: str) -> tuple[bool, str]:
    """Check whether ``fighter`` may use ``item_key`` right now.

    Verifies the catalogue entry exists and that the round-CD has
    expired. Inventory availability is checked by the cog before
    calling this.
    """
    meta = battle_consumable(item_key)
    if not meta:
        return False, "Unknown item."
    st = _state(fighter)
    cd = int(st.get("item_cd", {}).get(item_key) or 0)
    if cd > 0:
        return False, f"Cooldown: {cd} round(s)."
    if str(meta["effect"]) == "revive" and st.get("revive_used"):
        return False, "Already used a revive this battle."
    return True, ""


def apply(
    fighter,
    opponent,
    item_key: str,
    *,
    mastery_passives: dict | None = None,
) -> ApplyResult:
    """Resolve a consumable use by ``fighter`` against ``opponent``.

    Returns an ApplyResult describing the visible effect. The Fighter
    state is mutated in place (HP, atk_mult, dmg_taken_mult, spd,
    stunned_turns, status). The cog is responsible for:
      - confirming inventory has the item before calling
      - decrementing inventory after a successful ApplyResult
      - setting the item_cd on ``fighter`` after this call
    """
    meta = battle_consumable(item_key)
    if not meta:
        return ApplyResult(False, "Unknown item.", "")

    ok, reason = can_use(fighter, item_key)
    if not ok:
        return ApplyResult(False, reason, "")

    effect = str(meta["effect"])
    mag = float(meta.get("magnitude") or 0.0)
    dur = int(meta.get("duration") or 0)
    emoji = str(meta.get("emoji") or "")
    name = str(meta.get("name") or item_key)
    label = f"{emoji} {name}".strip()

    st = _state(fighter)

    if effect == "heal_pct":
        heal = max(1, int(round(fighter.max_hp * mag)))
        before = fighter.hp
        fighter.hp = min(fighter.max_hp, fighter.hp + heal)
        gained = fighter.hp - before
        line = (
            f"  {fighter.emoji} {fighter.name} uses **{label}** -- "
            f"restores **{gained}** HP "
            f"({fighter.hp}/{fighter.max_hp})."
        )
        return ApplyResult(True, "", line, hp_delta=int(gained))

    if effect == "crit_next":
        st["crit_next_pending"] = True
        line = (
            f"  {fighter.emoji} {fighter.name} uses **{label}** -- "
            f"next attack will crit."
        )
        return ApplyResult(True, "", line)

    if effect == "atk_buff_temp":
        st["atk_buff_amount"] = float(mag)
        st["atk_buff_rounds"] = int(dur)
        fighter.atk_mult *= (1.0 + float(mag))
        line = (
            f"  {fighter.emoji} {fighter.name} uses **{label}** -- "
            f"+{int(mag * 100)}% ATK for {dur} rounds."
        )
        return ApplyResult(True, "", line)

    if effect == "def_buff_temp":
        st["def_buff_amount"] = float(mag)
        st["def_buff_rounds"] = int(dur)
        fighter.dmg_taken_mult *= max(0.0, 1.0 - float(mag))
        line = (
            f"  {fighter.emoji} {fighter.name} uses **{label}** -- "
            f"-{int(mag * 100)}% damage taken for {dur} rounds."
        )
        return ApplyResult(True, "", line)

    if effect == "spd_perm":
        fighter.spd = float(min(1.5, fighter.spd + float(mag)))
        line = (
            f"  {fighter.emoji} {fighter.name} uses **{label}** -- "
            f"+{mag:.2f} SPD for this battle."
        )
        return ApplyResult(True, "", line)

    if effect == "cleanse_heal":
        # Clear status debuffs
        cleared = False
        if fighter.poison_turns > 0:
            fighter.poison_turns = 0
            cleared = True
        if fighter.stunned_turns > 0:
            fighter.stunned_turns = 0
            cleared = True
        if fighter.dmg_taken_mult > 1.0:
            fighter.dmg_taken_mult = 1.0
            cleared = True
        if fighter.atk_mult < 1.0:
            fighter.atk_mult = 1.0
            cleared = True
        heal = max(1, int(round(fighter.max_hp * mag)))
        before = fighter.hp
        fighter.hp = min(fighter.max_hp, fighter.hp + heal)
        gained = fighter.hp - before
        tag = "Cleansed!" if cleared else "Refreshed."
        line = (
            f"  {fighter.emoji} {fighter.name} uses **{label}** -- "
            f"{tag} +{gained} HP."
        )
        return ApplyResult(True, "", line, hp_delta=int(gained), cleared_debuffs=cleared)

    if effect == "shock_attack":
        # Direct damage at mag x ATK ignoring opponent dmg_taken_mult.
        raw = max(1, int(round(fighter.atk * fighter.atk_mult * float(mag))))
        before = opponent.hp
        opponent.hp = max(0, opponent.hp - raw)
        dealt = before - opponent.hp
        stun = int(dur)
        if stun > 0:
            opponent.stunned_turns = max(opponent.stunned_turns, stun)
        line = (
            f"  {fighter.emoji} {fighter.name} hurls a **{label}** at "
            f"{opponent.name} -- **{dealt}** dmg, stun {stun} turn(s)."
        )
        return ApplyResult(True, "", line, foe_damage=int(dealt), foe_stun=int(stun))

    if effect == "revive":
        st["revive_armed"] = True
        st["revive_pct"] = float(mag)
        line = (
            f"  {fighter.emoji} {fighter.name} uses **{label}** -- "
            f"will auto-revive at {int(mag * 100)}% HP if KO'd."
        )
        return ApplyResult(True, "", line)

    return ApplyResult(False, f"Unhandled effect: {effect}", "")


def set_cd(fighter, item_key: str, mastery_passives: dict | None = None) -> None:
    """Stamp the item's round-CD on the fighter.

    ``combat.consumable_cd`` mastery subtracts N rounds from the CD
    floor (min 1). Called by the cog after a successful apply().
    """
    meta = battle_consumable(item_key)
    if not meta:
        return
    cd = int(meta.get("round_cd") or 0)
    cut = int(_passive(mastery_passives, "combat.consumable_cd", 0.0))
    cd = max(1, cd - cut)
    _state(fighter)["item_cd"][item_key] = int(cd)


def tick_cd(fighter, mastery_passives: dict | None = None) -> None:
    """Decrement all per-item CDs by 1. Called at end-of-round.

    Items at CD 0 are removed so the dropdown view never shows '0 rounds'.
    """
    st = _state(fighter)
    cd_map = dict(st.get("item_cd", {}))
    nxt: dict[str, int] = {}
    for k, v in cd_map.items():
        n = int(v) - 1
        if n > 0:
            nxt[k] = n
    st["item_cd"] = nxt


def consume_timed_buffs(fighter) -> list[str]:
    """Tick down atk_buff_temp / def_buff_temp at round end.

    When a timer reaches zero the buff is reverted on the Fighter. Returns
    log lines for any reverts so the cog can show them in the embed.
    """
    st = _state(fighter)
    lines: list[str] = []
    if st.get("atk_buff_rounds", 0) > 0:
        st["atk_buff_rounds"] -= 1
        if st["atk_buff_rounds"] <= 0:
            amt = float(st.get("atk_buff_amount", 0.0))
            if amt > 0:
                fighter.atk_mult /= (1.0 + amt)
                st["atk_buff_amount"] = 0.0
                lines.append(
                    f"  {fighter.emoji} {fighter.name}'s rage fades."
                )
    if st.get("def_buff_rounds", 0) > 0:
        st["def_buff_rounds"] -= 1
        if st["def_buff_rounds"] <= 0:
            amt = float(st.get("def_buff_amount", 0.0))
            if amt > 0 and amt < 1.0:
                fighter.dmg_taken_mult /= max(0.05, 1.0 - amt)
                st["def_buff_amount"] = 0.0
                lines.append(
                    f"  {fighter.emoji} {fighter.name}'s iron skin softens."
                )
    return lines


def crit_next_pending(fighter) -> bool:
    """True if a Focus Berry crit is queued up. Consumed on read."""
    st = _state(fighter)
    if st.get("crit_next_pending"):
        st["crit_next_pending"] = False
        return True
    return False


def revive_if_armed(fighter) -> str | None:
    """If the fighter holds an armed Phoenix Tear and would KO, revive.

    Called by the cog right after damage is applied (when hp falls to 0).
    Returns a log line on success, None otherwise. Idempotent across
    repeated KOs in a battle (revive_used).
    """
    st = _state(fighter)
    if not st.get("revive_armed") or st.get("revive_used"):
        return None
    if fighter.hp > 0:
        return None
    pct = float(st.get("revive_pct") or 0.35)
    fighter.hp = max(1, int(round(fighter.max_hp * pct)))
    st["revive_used"] = True
    st["revive_armed"] = False
    return (
        f"  \U0001F525 **Phoenix Tear** -- {fighter.name} blazes back at "
        f"{fighter.hp}/{fighter.max_hp} HP."
    )


def selectable_options(
    fighter,
    inventory: dict[str, int],
    *,
    max_options: int = 25,
) -> list[dict]:
    """Return the dropdown options for the cog's Select UI.

    Each entry: {key, label, description, qty, cd_remaining, disabled}.
    Sorted by qty desc, then by rarity. Disables items at qty 0 or
    still on CD so the UI can grey them out cleanly.
    """
    st = _state(fighter)
    cd_map = st.get("item_cd", {}) or {}
    rows: list[dict] = []
    for k, qty in (inventory or {}).items():
        meta = battle_consumable(k)
        if not meta or int(qty or 0) <= 0:
            continue
        cd = int(cd_map.get(k) or 0)
        rows.append({
            "key": k,
            "label": f"{meta.get('emoji', '')} {meta.get('name', k)} x{int(qty)}".strip(),
            "description": _truncate(
                f"{meta.get('description', '')}  (CD {cd})"
                if cd > 0 else str(meta.get("description") or ""),
                100,
            ),
            "qty": int(qty),
            "cd_remaining": int(cd),
            "disabled": bool(cd > 0),
        })
    rows.sort(key=lambda r: (-r["qty"], _rarity_rank(r["key"])))
    return rows[:max_options]


def _rarity_rank(item_key: str) -> int:
    """Map item rarity to a sort key (common = 0, epic = 3)."""
    rarity_order = {"common": 0, "uncommon": 1, "rare": 2, "epic": 3, "legendary": 4}
    meta = battle_consumable(item_key) or {}
    return int(rarity_order.get(str(meta.get("rarity") or "common"), 0))


def _truncate(s: str, n: int) -> str:
    """Short helper -- Discord Select option descriptions cap at 100 chars."""
    s = str(s or "")
    return s if len(s) <= int(n) else s[: max(0, int(n) - 1)] + "…"
