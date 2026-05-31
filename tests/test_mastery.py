"""V3 Pillar 2 unit tests.

Pure-Python: pin the level/XP curve, the points formula, and the
node-tree invariants. The DB-touching service paths are exercised in
the verification plan against a live Postgres.
"""
from __future__ import annotations


from configs.mastery_config import (
    BRANCHES,
    NODES,
    NODES_BY_ID,
    TRACKS,
    TRACK_MAX_LEVEL,
    level_for_xp,
    points_for_level,
    xp_for_level,
)


def test_track_count_is_ten() -> None:
    assert len(TRACKS) == 10


def test_xp_for_level_one_is_zero() -> None:
    assert xp_for_level(1) == 0


def test_xp_for_level_monotonic() -> None:
    prev = 0
    for lvl in range(1, 100):
        cur = xp_for_level(lvl)
        assert cur >= prev
        prev = cur


def test_level_for_xp_round_trip() -> None:
    for lvl in (1, 5, 10, 50, 99, 100):
        threshold = xp_for_level(lvl)
        assert level_for_xp(threshold) == lvl
        # One XP below the threshold drops us a level (except L1).
        if lvl > 1:
            assert level_for_xp(threshold - 1) == lvl - 1


def test_level_for_xp_clamps_at_cap() -> None:
    # 10x past the L100 threshold still resolves to L100.
    cap_xp = xp_for_level(TRACK_MAX_LEVEL)
    assert level_for_xp(cap_xp * 10) == TRACK_MAX_LEVEL


def test_points_for_level_monotonic() -> None:
    prev = 0
    for lvl in range(1, 100):
        cur = points_for_level(lvl)
        assert cur >= prev
        prev = cur


def test_points_for_level_milestones() -> None:
    # Every multiple of 10 awards a bonus point on top of the linear pace.
    # L10 -> 9 base + 1 milestone = 10. L20 -> 19 + 2 = 21.
    assert points_for_level(10) == 10
    assert points_for_level(20) == 21


# ── Node tree invariants ───────────────────────────────────────────────
def test_every_node_has_a_known_branch() -> None:
    for node in NODES:
        assert node["branch"] in BRANCHES, node["id"]


def test_every_prereq_resolves() -> None:
    ids = {n["id"] for n in NODES}
    for node in NODES:
        for pre in node.get("prereqs", []):
            assert pre in ids, f"{node['id']} prereq {pre} missing"


def test_no_self_prereq() -> None:
    for node in NODES:
        assert node["id"] not in node.get("prereqs", [])


def test_node_costs_are_positive() -> None:
    for node in NODES:
        assert int(node["cost"]) > 0


def test_node_effects_have_keys() -> None:
    for node in NODES:
        assert node.get("effect_key"), node["id"]
        assert isinstance(node.get("effect_value"), (int, float))


def test_nodes_by_id_round_trip() -> None:
    for node in NODES:
        assert NODES_BY_ID[node["id"]] is node


# ── Renderer smoke ─────────────────────────────────────────────────────
def test_mastery_board_renders() -> None:
    from services.mastery_render import render_mastery_board
    from services.mastery import MasterySummary
    summary = MasterySummary(
        tracks={
            "fisher": {"level": 12, "xp": 1500, "next_threshold": 2000,
                       "progress": 0.6},
        },
        points_available=5, points_spent=3,
        unlocked={"econ.daily_bonus.1"},
    )
    png = render_mastery_board(summary, display_name="Test")
    assert png.startswith(b"\x89PNG")
    assert len(png) > 5000


def test_levelup_renders() -> None:
    from services.mastery_render import render_track_levelup
    png = render_track_levelup("fisher", 42, display_name="Test")
    assert png.startswith(b"\x89PNG")


# ── xp_for_action curve ─────────────────────────────────────────────────
# Pins the /50 divisor + 1500 cap. If this regresses to /10 / /5 again a
# single big cashout will single-shot a player to L20+ (the
# "Raider L27 from one heist" bug the curve was rewritten to fix).

def test_xp_for_action_zero_or_negative_returns_zero() -> None:
    from services.mastery import xp_for_action
    assert xp_for_action(0) == 0
    assert xp_for_action(-100) == 0


def test_xp_for_action_small_action_floor() -> None:
    from services.mastery import xp_for_action
    # $10 cashout maps to floor(10/50) = 0, clamped up to the min of 1.
    assert xp_for_action(10) == 1


def test_xp_for_action_typical_payout() -> None:
    from services.mastery import xp_for_action
    # $500 -> 10 XP, $5,000 -> 100 XP. Linear up to the cap.
    assert xp_for_action(500) == 10
    assert xp_for_action(5_000) == 100


def test_xp_for_action_capped_at_whale_scale() -> None:
    from services.mastery import xp_for_action, XP_PER_ACTION_CAP
    # $100k or $1M should both clamp to the cap so a single whale
    # action can't single-shot mid-tier (the regression we're pinning).
    assert xp_for_action(100_000) == XP_PER_ACTION_CAP
    assert xp_for_action(1_000_000) == XP_PER_ACTION_CAP


def test_xp_for_action_multiplier() -> None:
    from services.mastery import xp_for_action
    # multiplier=2.0 doubles the curve at every point, but still
    # clamps to the cap at the whale tier.
    assert xp_for_action(500, multiplier=2.0) == 20
    assert xp_for_action(100_000, multiplier=2.0) == 1500


def test_l27_requires_multiple_meaningful_sessions() -> None:
    """Regression for 'Raider L27 from one heist'. Even a max-scale
    single action grants at most cap XP; reaching L27 needs many
    sessions, not one."""
    from services.mastery import xp_for_action, XP_PER_ACTION_CAP
    one_action_max = xp_for_action(10_000_000)
    assert one_action_max == XP_PER_ACTION_CAP
    assert one_action_max < xp_for_level(20), (
        "A single capped action shouldn't get a fresh player past L20"
    )
    # Pin the curve: L27 needs > 100 capped actions under the new curve
    # (was 15 under the old 1.15 / 100 base curve; bumped after player
    # report of L27 from one session).
    assert xp_for_level(27) // one_action_max >= 100


def test_attach_listeners_subscribes_to_micro_action_events() -> None:
    """The micro-action listener should wire ONE handler per event in
    ``_MICRO_XP`` so every track gets per-action XP, not just the
    cashout grant. Regression for the player report 'why the fuck
    aren't fishing mastery and craft mastery and shit leveling'."""
    from services import mastery as _svc

    seen: dict[str, list] = {}

    class _FakeBus:
        def subscribe(self, event: str, handler) -> None:
            seen.setdefault(event, []).append(handler)

    class _FakeBot:
        bus = _FakeBus()
        db = None

    _svc.attach_listeners(_FakeBot())

    # Every entry in the dispatch table must have been subscribed.
    for event_name in _svc._MICRO_XP:
        assert event_name in seen, f"{event_name} not subscribed"
        assert len(seen[event_name]) == 1

    # Coverage check: every track in TRACKS has at least one event
    # feeding it, so no track is silent.
    tracks_covered = {track for track, _ in _svc._MICRO_XP.values()}
    assert tracks_covered == set(TRACKS), (
        f"Tracks without a micro-action grant: {set(TRACKS) - tracks_covered}"
    )


def test_xp_curve_milestones_pinned() -> None:
    """Lock the curve thresholds so a future tweak to BASE / GROWTH that
    silently makes mastery a sprint again gets caught here.

    Sample milestones (cap is 1500 XP / capped action):
        L5    1,656  XP     (~     1 capped action)
        L10   6,797  XP     (~     4)
        L20   58,267 XP     (~    38)
        L27   238,537 XP    (~   159)
    """
    assert 1_500 <= xp_for_level(5) <= 2_000
    assert 6_000 <= xp_for_level(10) <= 8_000
    assert 50_000 <= xp_for_level(20) <= 70_000
    assert 200_000 <= xp_for_level(27) <= 280_000
    # And L50 should be unreachable in a session.
    assert xp_for_level(50) >= 10_000_000
