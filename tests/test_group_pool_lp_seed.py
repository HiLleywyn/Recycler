from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.groups import Groups


def _make_group(group_id: str, name: str, sym: str, vault_bal: float = 100000.0) -> dict:
    return {
        "group_id": group_id,
        "name": name,
        "token_symbol": sym,
        "vault_token_bal": vault_bal,
    }


@pytest.mark.asyncio
async def test_seed_group_pool_uses_proposal_pair_pool_id() -> None:
    db = MagicMock()
    db.get_price = AsyncMock(side_effect=[{"price": 1.0}, {"price": 2.0}])
    db.deduct_group_vault_tokens = AsyncMock(return_value=True)
    db.mint_vault_tokens = AsyncMock()
    db.make_pool_id = MagicMock(return_value=("AAA-BBB", "AAA", "BBB"))
    db.seed_group_pool = AsyncMock(return_value={"pool_id": "AAA-BBB"})

    grp_a = _make_group("g1", "Alpha", "AAA")
    grp_b = _make_group("g2", "Beta", "BBB")

    note = await Groups._seed_group_pool_from_vault(
        db, 1, "AAA-BBB", grp_a, grp_b, "AAA", "BBB",
    )

    assert note.startswith("Auto-seeded with")
    db.seed_group_pool.assert_awaited_once()
    assert db.seed_group_pool.await_args.args[1] == "AAA-BBB"
    assert db.deduct_group_vault_tokens.await_count == 2
    ded_a = db.deduct_group_vault_tokens.await_args_list[0].args
    ded_b = db.deduct_group_vault_tokens.await_args_list[1].args
    assert ded_a[:2] == (1, "g1")
    assert ded_b[:2] == (1, "g2")
    assert ded_a[2] == pytest.approx(200.0)
    assert ded_b[2] == pytest.approx(100.0)
    db.mint_vault_tokens.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_group_pool_skips_if_symbols_changed_after_proposal() -> None:
    db = MagicMock()
    db.get_price = AsyncMock()
    db.deduct_group_vault_tokens = AsyncMock()
    db.mint_vault_tokens = AsyncMock()
    db.make_pool_id = MagicMock()
    db.seed_group_pool = AsyncMock()

    grp_a = _make_group("g1", "Alpha", "NEWA")
    grp_b = _make_group("g2", "Beta", "NEWB")

    note = await Groups._seed_group_pool_from_vault(
        db, 1, "OLDA-OLDB", grp_a, grp_b, "OLDA", "OLDB",
    )

    assert "changed after proposal" in note
    db.get_price.assert_not_awaited()
    db.seed_group_pool.assert_not_awaited()
