"""services/delve_arena_battle.py -- battle simulation for delve arena PvP.

Two modes share one engine:

* Async ranked: ``simulate_match(p1, p2)`` runs both sides via the
  per-side ``_arena_ai`` and returns the full ``BattleReplay``.
* Live duel: ``LiveDuel(p1, p2)`` lets the cog drive one round at a
  time by calling ``step(action_p1, action_p2)``. When either action
  is ``None`` the AI fills in (used for round-timeout fallback).

The math is intentionally simpler than ``services.dungeon.resolve_attack``
so the simulation stays fast and deterministic: ATK / DEF / SPD / crit
all flow from each player's delve combat profile, abilities use the
``mult`` / ``swings`` / ``heal_pct`` from ``dungeon_config.ABILITIES``,
and lifesteal / def_pierce / mark are honoured per-swing.

Per CLAUDE.md the file is pure ASCII -- hyphens not em/en dashes.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field

import configs.dungeon_config as dc

log = logging.getLogger(__name__)


# Actions a fighter can submit per round.
ACTION_STRIKE: str = "strike"
ACTION_ABILITY: str = "ability"   # carries ability_key
ACTION_BRACE: str = "brace"
ACTION_FLEE: str = "flee"

# Brace = take 50% less damage this round + heal 5% max HP.
BRACE_DR: float = 0.50
BRACE_HEAL_PCT: float = 0.05
# Risky strike: 60% huge (1.6x), 25% miss, 15% backfire (recoil 10% max HP).
RISKY_HUGE: float = 1.6
RISKY_MISS: float = 0.25
RISKY_BACKFIRE: float = 0.15
RISKY_RECOIL_PCT: float = 0.10
ARENA_MAX_ROUNDS: int = 25


@dataclass
class ArenaProfile:
    """Snapshot of one player used by the simulator."""
    uid: int = 0
    name: str = ""
    class_key: str = ""
    class_name: str = ""
    level: int = 1
    atk: float = 5.0
    defense: float = 2.0
    spd: float = 0.5
    int_stat: float = 0.0
    hp_max: int = 20
    weapon_kind: str = "melee"
    abilities: list[str] = field(default_factory=list)
    weapon_affixes: dict = field(default_factory=dict)
    armor_affixes: dict = field(default_factory=dict)
    crit_affix: float = 0.0
    relic_key: str | None = None
    rng_seed: int = 0


def profile_from_state(state: dict, *, uid: int, name: str) -> ArenaProfile:
    """Build an ``ArenaProfile`` from a delve user_dungeon row dict.

    Uses the existing ``services.dungeon.player_combat_stats`` so the
    arena fighter is exactly the same statline the player walks into a
    delve mob fight with.
    """
    from services.dungeon import player_combat_stats
    stats = player_combat_stats(state)
    class_key = str(stats.get("class_key") or state.get("class_key") or "warrior")
    cmeta = dc.class_meta(class_key) or {}
    abilities = list(dc.class_abilities(class_key) or ())
    return ArenaProfile(
        uid=int(uid),
        name=str(name or "")[:32],
        class_key=class_key,
        class_name=str(cmeta.get("name") or class_key.title()),
        level=int(stats.get("level") or 1),
        atk=float(stats.get("atk") or 5.0),
        defense=float(stats.get("def") or 2.0),
        spd=float(stats.get("spd") or 0.5),
        int_stat=float(stats.get("int") or 0.0),
        hp_max=int(stats.get("hp_max") or 20),
        weapon_kind=str(stats.get("attack_kind") or "melee"),
        abilities=abilities,
        weapon_affixes=dict(stats.get("weapon_affixes") or {}),
        armor_affixes=dict(stats.get("armor_affixes") or {}),
        crit_affix=float(stats.get("crit_affix") or 0.0),
        relic_key=stats.get("relic_key"),
    )


@dataclass
class FighterState:
    """Mutable per-fighter state across rounds."""
    profile: ArenaProfile
    hp: int = 0
    cooldowns: dict[str, int] = field(default_factory=dict)
    marked_swings: int = 0          # bonus auto-crit charges from Shadowstep
    bracing: bool = False
    stunned: int = 0                # rounds remaining of stun

    def __post_init__(self) -> None:
        if self.hp <= 0:
            self.hp = int(self.profile.hp_max)


@dataclass
class BattleEvent:
    """One round's worth of events for the replay."""
    round_num: int
    p1_action: str = ""
    p2_action: str = ""
    p1_dmg_dealt: int = 0
    p2_dmg_dealt: int = 0
    p1_hp_after: int = 0
    p2_hp_after: int = 0
    p1_log: list[str] = field(default_factory=list)
    p2_log: list[str] = field(default_factory=list)
    banner: str = ""


@dataclass
class BattleReplay:
    """Full replay of an arena match."""
    p1: ArenaProfile
    p2: ArenaProfile
    rounds: list[BattleEvent] = field(default_factory=list)
    winner_uid: int | None = None
    flawless: bool = False
    rng_seed: int = 0


# Internal swing math ----------------------------------------------------

def _crit_chance(attacker: FighterState) -> float:
    """Base 5% crit + weapon affix crit + arena bonus."""
    base = dc.CRIT_BASE
    return min(0.95, base + float(attacker.profile.crit_affix or 0.0))


def _swing_damage(
    attacker: FighterState, defender: FighterState, *,
    mult: float = 1.0, ability_meta: dict | None = None,
    rng: random.Random,
) -> tuple[int, bool, str]:
    """Resolve one swing and return ``(damage, crit, log_fragment)``.

    Mirrors the spirit of ``dc.resolve_attack`` without the dungeon
    helpers' DB side-effects.
    """
    ability_meta = ability_meta or {}
    def_pierce = float(ability_meta.get("def_pierce_pct") or 0.0)
    eff_def = max(0.0, defender.profile.defense * (1.0 - def_pierce))
    raw = (attacker.profile.atk * mult) - (eff_def * 0.4)
    # INT scaling for spell abilities.
    if str(ability_meta.get("kind") or attacker.profile.weapon_kind) == "spell":
        raw += attacker.profile.int_stat * 0.4 * mult
    raw = max(1.0, raw + rng.uniform(-1.5, 1.5))
    is_crit = bool(ability_meta.get("auto_crit") or False)
    extra_crit = float(ability_meta.get("crit_bonus") or 0.0)
    if not is_crit:
        is_crit = rng.random() < (_crit_chance(attacker) + extra_crit)
    if attacker.marked_swings > 0:
        is_crit = True
        attacker.marked_swings -= 1
    if is_crit:
        raw *= dc.CRIT_MULT
    # Defender bracing halves the swing.
    if defender.bracing:
        raw *= (1.0 - BRACE_DR)
    dmg = max(1, int(round(raw)))
    note = "CRIT! " if is_crit else ""
    return dmg, is_crit, note


def _apply_swing(
    attacker: FighterState, defender: FighterState,
    *, mult: float, ability_meta: dict | None = None,
    rng: random.Random, event: BattleEvent, side: str,
) -> int:
    dmg, _crit, note = _swing_damage(
        attacker, defender, mult=mult, ability_meta=ability_meta, rng=rng,
    )
    defender.hp = max(0, defender.hp - dmg)
    label = (ability_meta or {}).get("name") or "Strike"
    log_line = f"{note}{attacker.profile.name} -> {defender.profile.name} ({label}): {dmg}"
    if side == "p1":
        event.p1_log.append(log_line)
        event.p1_dmg_dealt += dmg
    else:
        event.p2_log.append(log_line)
        event.p2_dmg_dealt += dmg
    # Lifesteal -- ability + weapon affix.
    ls_pct = float((ability_meta or {}).get("lifesteal_pct") or 0.0)
    ls_pct += float(attacker.profile.weapon_affixes.get("lifesteal_pct") or 0.0)
    if ls_pct > 0 and dmg > 0:
        heal = int(round(dmg * ls_pct))
        if heal > 0:
            attacker.hp = min(attacker.profile.hp_max, attacker.hp + heal)
    return dmg


def _resolve_ability(
    attacker: FighterState, defender: FighterState, ability_key: str, *,
    rng: random.Random, event: BattleEvent, side: str,
) -> None:
    meta = dc.ability_meta(ability_key) or {}
    if not meta:
        # Fallback to a normal strike if the key is unknown.
        _apply_swing(attacker, defender, mult=1.0, rng=rng, event=event, side=side)
        return
    target = str(meta.get("target") or "mob")
    heal_pct = float(meta.get("heal_pct") or 0.0)
    if heal_pct > 0:
        gained = int(round(attacker.profile.hp_max * heal_pct))
        attacker.hp = min(attacker.profile.hp_max, attacker.hp + gained)
        log_line = f"{attacker.profile.name} heals {gained}"
        if side == "p1":
            event.p1_log.append(log_line)
        else:
            event.p2_log.append(log_line)
    if target == "self":
        attacker.cooldowns[ability_key] = int(meta.get("cd") or 0)
        return
    swings = int(meta.get("swings") or 1)
    mult = float(meta.get("mult") or 1.0)
    for _ in range(max(1, swings)):
        if defender.hp <= 0:
            break
        _apply_swing(
            attacker, defender, mult=mult, ability_meta=meta,
            rng=rng, event=event, side=side,
        )
    # Apply secondary effects.
    if int(meta.get("stun_rounds") or 0) > 0:
        defender.stunned = max(defender.stunned, int(meta.get("stun_rounds") or 0))
    if int(meta.get("mark_rounds") or 0) > 0:
        attacker.marked_swings = max(
            attacker.marked_swings, int(meta.get("mark_rounds") or 0),
        )
    attacker.cooldowns[ability_key] = int(meta.get("cd") or 0)


def _tick_cooldowns(fs: FighterState) -> None:
    new_cds = {k: v - 1 for k, v in fs.cooldowns.items() if v - 1 > 0}
    fs.cooldowns = new_cds
    if fs.stunned > 0:
        fs.stunned -= 1
    fs.bracing = False


def _arena_ai(fs: FighterState, opp: FighterState, rng: random.Random) -> tuple[str, str | None]:
    """Pick an action for a CPU-controlled fighter.

    Strategy: low HP -> brace. Cooldown-ready ability -> use it.
    Otherwise plain strike with occasional risky.
    """
    if fs.hp <= fs.profile.hp_max * 0.25 and rng.random() < 0.6:
        return ACTION_BRACE, None
    # Pick the strongest ready ability.
    best = None
    best_mult = 0.0
    for akey in fs.profile.abilities:
        if fs.cooldowns.get(akey, 0) > 0:
            continue
        meta = dc.ability_meta(akey) or {}
        mult = float(meta.get("mult") or 0.0) * int(meta.get("swings") or 1)
        # Druid regrowth has 0 swings; treat as situational heal.
        if str(meta.get("target") or "mob") == "self":
            if fs.hp >= fs.profile.hp_max * 0.6:
                continue
            mult = 1.0  # prefer some heal when low HP
        if mult > best_mult:
            best = akey
            best_mult = mult
    if best:
        return ACTION_ABILITY, best
    return ACTION_STRIKE, None


# Round resolution -------------------------------------------------------

def _action_label(action: str, key: str | None) -> str:
    if action == ACTION_ABILITY and key:
        meta = dc.ability_meta(key) or {}
        return str(meta.get("name") or key.title())
    return action.title()


def _resolve_round(
    f1: FighterState, f2: FighterState, *,
    action_p1: tuple[str, str | None],
    action_p2: tuple[str, str | None],
    round_num: int,
    rng: random.Random,
) -> BattleEvent:
    event = BattleEvent(
        round_num=round_num,
        p1_action=_action_label(*action_p1),
        p2_action=_action_label(*action_p2),
    )

    # Bracing fires up-front (DR for the round + small heal).
    if action_p1[0] == ACTION_BRACE:
        f1.bracing = True
        heal = int(round(f1.profile.hp_max * BRACE_HEAL_PCT))
        f1.hp = min(f1.profile.hp_max, f1.hp + heal)
        event.p1_log.append(f"{f1.profile.name} braces ({heal} hp)")
    if action_p2[0] == ACTION_BRACE:
        f2.bracing = True
        heal = int(round(f2.profile.hp_max * BRACE_HEAL_PCT))
        f2.hp = min(f2.profile.hp_max, f2.hp + heal)
        event.p2_log.append(f"{f2.profile.name} braces ({heal} hp)")

    # Determine swing order via SPD.
    spd1, spd2 = f1.profile.spd, f2.profile.spd
    if spd1 == spd2:
        first = "p1" if rng.random() < 0.5 else "p2"
    else:
        first = "p1" if spd1 > spd2 else "p2"

    def _act(side: str) -> None:
        atk, dfd = (f1, f2) if side == "p1" else (f2, f1)
        action, key = action_p1 if side == "p1" else action_p2
        if atk.hp <= 0 or dfd.hp <= 0:
            return
        if atk.stunned > 0:
            log_line = f"{atk.profile.name} is stunned"
            (event.p1_log if side == "p1" else event.p2_log).append(log_line)
            return
        if action == ACTION_BRACE:
            return  # already applied
        if action == ACTION_FLEE:
            # Flee fails 100% in the arena -- you fight or you fold.
            log_line = f"{atk.profile.name} tries to flee (blocked)"
            (event.p1_log if side == "p1" else event.p2_log).append(log_line)
            return
        if action == ACTION_ABILITY and key:
            _resolve_ability(atk, dfd, key, rng=rng, event=event, side=side)
            return
        # Strike default.
        _apply_swing(atk, dfd, mult=1.0, rng=rng, event=event, side=side)

    _act(first)
    second = "p2" if first == "p1" else "p1"
    _act(second)

    _tick_cooldowns(f1)
    _tick_cooldowns(f2)

    event.p1_hp_after = f1.hp
    event.p2_hp_after = f2.hp
    return event


# Public entry points ----------------------------------------------------

def simulate_match(
    p1: ArenaProfile, p2: ArenaProfile, *, rng_seed: int | None = None,
) -> BattleReplay:
    """Run a fully-deterministic CPU vs CPU match."""
    seed = int(rng_seed if rng_seed is not None else random.randint(1, 10_000_000))
    rng = random.Random(seed)
    f1 = FighterState(profile=p1)
    f2 = FighterState(profile=p2)
    replay = BattleReplay(p1=p1, p2=p2, rng_seed=seed)
    p1_took_dmg = False
    p2_took_dmg = False
    for round_num in range(1, ARENA_MAX_ROUNDS + 1):
        a1 = _arena_ai(f1, f2, rng)
        a2 = _arena_ai(f2, f1, rng)
        ev = _resolve_round(
            f1, f2, action_p1=a1, action_p2=a2,
            round_num=round_num, rng=rng,
        )
        replay.rounds.append(ev)
        if ev.p1_dmg_dealt > 0:
            p2_took_dmg = True
        if ev.p2_dmg_dealt > 0:
            p1_took_dmg = True
        if f1.hp <= 0 or f2.hp <= 0:
            break
    if f1.hp <= 0 and f2.hp <= 0:
        # Tie -- give to higher-HP-fraction at start of last round.
        # (Both at 0 means p1 hit first to drop p2 last round; lean p1.)
        replay.winner_uid = p1.uid
        replay.flawless = False
    elif f1.hp <= 0:
        replay.winner_uid = p2.uid
        replay.flawless = not p2_took_dmg
    elif f2.hp <= 0:
        replay.winner_uid = p1.uid
        replay.flawless = not p1_took_dmg
    else:
        # Rounds capped -- higher HP wins; tie -> higher level -> p1.
        if f1.hp > f2.hp:
            replay.winner_uid = p1.uid
        elif f2.hp > f1.hp:
            replay.winner_uid = p2.uid
        elif p1.level >= p2.level:
            replay.winner_uid = p1.uid
        else:
            replay.winner_uid = p2.uid
    return replay


class LiveDuel:
    """Drive a duel round-by-round from the cog.

    Usage::

        duel = LiveDuel(p1, p2)
        while not duel.over:
            ev = duel.step(a1, a2)   # a1/a2 may be None -> AI fallback
            await render(ev)
        result = duel.finalize()
    """

    def __init__(self, p1: ArenaProfile, p2: ArenaProfile, *, rng_seed: int | None = None) -> None:
        self.p1 = p1
        self.p2 = p2
        self.seed = int(rng_seed if rng_seed is not None else random.randint(1, 10_000_000))
        self.rng = random.Random(self.seed)
        self.f1 = FighterState(profile=p1)
        self.f2 = FighterState(profile=p2)
        self.replay = BattleReplay(p1=p1, p2=p2, rng_seed=self.seed)
        self.round_num = 0
        self.over = False
        self._p1_took_dmg = False
        self._p2_took_dmg = False

    def status(self) -> dict:
        return {
            "round": self.round_num,
            "p1_hp": self.f1.hp, "p1_max_hp": self.p1.hp_max,
            "p2_hp": self.f2.hp, "p2_max_hp": self.p2.hp_max,
            "p1_cooldowns": dict(self.f1.cooldowns),
            "p2_cooldowns": dict(self.f2.cooldowns),
            "p1_stunned": self.f1.stunned, "p2_stunned": self.f2.stunned,
        }

    def step(
        self,
        action_p1: tuple[str, str | None] | None,
        action_p2: tuple[str, str | None] | None,
    ) -> BattleEvent:
        """Resolve one round. Missing actions default to AI picks."""
        if self.over:
            raise RuntimeError("duel already finished")
        self.round_num += 1
        a1 = action_p1 or _arena_ai(self.f1, self.f2, self.rng)
        a2 = action_p2 or _arena_ai(self.f2, self.f1, self.rng)
        ev = _resolve_round(
            self.f1, self.f2, action_p1=a1, action_p2=a2,
            round_num=self.round_num, rng=self.rng,
        )
        self.replay.rounds.append(ev)
        if ev.p1_dmg_dealt > 0:
            self._p2_took_dmg = True
        if ev.p2_dmg_dealt > 0:
            self._p1_took_dmg = True
        if (
            self.f1.hp <= 0 or self.f2.hp <= 0
            or self.round_num >= ARENA_MAX_ROUNDS
        ):
            self.over = True
        return ev

    def finalize(self) -> BattleReplay:
        if self.f1.hp <= 0 and self.f2.hp <= 0:
            self.replay.winner_uid = self.p1.uid
        elif self.f1.hp <= 0:
            self.replay.winner_uid = self.p2.uid
            self.replay.flawless = not self._p2_took_dmg
        elif self.f2.hp <= 0:
            self.replay.winner_uid = self.p1.uid
            self.replay.flawless = not self._p1_took_dmg
        elif self.f1.hp > self.f2.hp:
            self.replay.winner_uid = self.p1.uid
        elif self.f2.hp > self.f1.hp:
            self.replay.winner_uid = self.p2.uid
        else:
            self.replay.winner_uid = (
                self.p1.uid if self.p1.level >= self.p2.level else self.p2.uid
            )
        return self.replay


__all__ = [
    "ACTION_STRIKE", "ACTION_ABILITY", "ACTION_BRACE", "ACTION_FLEE",
    "ARENA_MAX_ROUNDS",
    "ArenaProfile", "FighterState", "BattleEvent", "BattleReplay",
    "profile_from_state", "simulate_match", "LiveDuel",
    "_arena_ai",
]
