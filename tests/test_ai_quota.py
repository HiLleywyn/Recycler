"""Tests for core.framework.ai.quota reservation helpers."""
from __future__ import annotations

import pytest

from core.framework.ai.quota import (
    _AI_QUOTA_LIMIT,
    cancel_ai_quota_reservation,
    check_ai_quota,
    reserve_ai_quota,
    reset_ai_quota_state,
)


class TestAIQuota:
    def setup_method(self):
        reset_ai_quota_state()

    def teardown_method(self):
        reset_ai_quota_state()

    @pytest.mark.asyncio
    async def test_reserve_then_cancel_restores_capacity(self):
        allowed, remaining, ts = await reserve_ai_quota(1, 1)
        assert allowed is True
        assert ts is not None

        cancel_ai_quota_reservation(1, 1, ts)
        allowed_again, remaining_again = check_ai_quota(1, 1)
        assert allowed_again is True
        assert remaining_again == _AI_QUOTA_LIMIT - 1

    @pytest.mark.asyncio
    async def test_reserve_blocks_when_limit_exhausted(self):
        last_allowed = None
        for _ in range(_AI_QUOTA_LIMIT):
            last_allowed = await reserve_ai_quota(2, 2)
            assert last_allowed[0] is True

        allowed, remaining, ts = await reserve_ai_quota(2, 2)
        assert allowed is False
        assert remaining == 0
        assert ts is None
