"""Shared test fixtures and mock database for the Discoin test suite."""
from __future__ import annotations


import pytest

# ── Default guild/user IDs used across tests ─────────────────────────────────
GUILD_ID = 111_000_000
USER_ID = 222_000_000
OTHER_USER_ID = 333_000_000


# ── Async context manager helper ─────────────────────────────────────────────

class AsyncCtxMgr:
    """Lightweight async context manager that just yields control."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ── MockGuilds helper ─────────────────────────────────────────────────────────

class MockGuilds:
    """Minimal guilds sub-object used by trade/savings service."""

    def __init__(self):
        self.fee_config = {
            "platform_fee_pct": 0.005,
            "platform_fee_min": 0.01,
            "platform_fee_max": 50.0,
        }

    async def get_fee_config(self, guild_id: int) -> dict:
        return self.fee_config


# ── MockDB ────────────────────────────────────────────────────────────────────

class MockDB:
    """Minimal in-memory mock database used by service-layer tests.

    Only implements the subset of methods actually called by the service
    functions under test.  Each test can patch individual methods as needed.
    """

    def __init__(self):
        self.guilds = MockGuilds()

        # Configurable state
        self.users: dict[tuple[int, int], dict] = {}
        self.holdings: dict[tuple[int, int, str], dict] = {}
        self.wallet_holdings: dict[tuple[int, int, str, str], dict] = {}
        self.prices: dict[tuple[str, int], dict] = {}
        self.tokens: dict[int, dict[str, dict]] = {}
        self.pools: dict[tuple[str, int], dict] = {}
        self.disabled_tokens: set[tuple[int, str]] = set()
        self.halted_networks: set[tuple[int, str]] = set()
        self.validators: dict[str, dict] = {}
        self.stakes: dict[tuple[int, int, str], dict] = {}
        self.defi_wallets: set[tuple[int, int, str]] = set()
        self.savings: dict[tuple[int, int, str], dict] = {}
        self._tx_counter = 0

    # ── Atomic context manager ──────────────────────────────────────────────

    def atomic(self):
        return AsyncCtxMgr()

    # ── Raw SQL surface ─────────────────────────────────────────────────────
    # Net worth + a handful of services call fetch_all / fetch_one / get_twap
    # directly (bypassing the typed helpers) for queries that don't yet have
    # a dedicated method. The default implementations return empty / zero so
    # tests that don't care about those rows still pass.

    async def fetch_all(self, query: str, *args) -> list[dict]:
        return []

    async def fetch_one(self, query: str, *args) -> dict | None:
        return None

    async def fetch_val(self, query: str, *args):
        return None

    async def execute(self, query: str, *args) -> str:
        return "OK"

    async def get_twap(self, symbol: str, guild_id: int, window: int = 80):
        return (0.0, 0.0)

    # ── Pool helpers ────────────────────────────────────────────────────────

    def make_pool_id(self, token_a: str, token_b: str) -> tuple[str, str, str]:
        a, b = sorted([token_a.upper(), token_b.upper()])
        return f"{a}_{b}", a, b

    async def get_pool(self, pool_id: str, guild_id: int) -> dict | None:
        return self.pools.get((pool_id, guild_id))

    async def update_pool_reserves(self, pool_id: str, guild_id: int, new_a: float, new_b: float, new_total_lp: float = 0.0):
        key = (pool_id, guild_id)
        if key in self.pools:
            self.pools[key]["reserve_a"] = new_a
            self.pools[key]["reserve_b"] = new_b
            self.pools[key]["total_lp"] = new_total_lp

    async def update_lp_position(self, user_id: int, guild_id: int, pool_id: str, delta: float):
        pass

    async def update_pool_total_lp(self, pool_id: str, guild_id: int, delta: float):
        key = (pool_id, guild_id)
        if key in self.pools:
            self.pools[key]["total_lp"] = self.pools[key].get("total_lp", 0.0) + delta

    async def get_user_lp(self, user_id: int, guild_id: int, pool_id: str) -> dict | None:
        return None

    # ── User helpers ────────────────────────────────────────────────────────

    async def get_user(self, user_id: int, guild_id: int) -> dict | None:
        return self.users.get((user_id, guild_id))

    async def ensure_user(self, user_id: int, guild_id: int) -> None:
        if (user_id, guild_id) not in self.users:
            self.users[(user_id, guild_id)] = {"wallet": 0.0, "bank": 0.0}

    async def update_wallet(self, user_id: int, guild_id: int, delta: float) -> float:
        row = self.users.setdefault((user_id, guild_id), {"wallet": 0.0, "bank": 0.0})
        row["wallet"] += delta
        return row["wallet"]

    async def update_bank(self, user_id: int, guild_id: int, delta: float) -> float:
        row = self.users.setdefault((user_id, guild_id), {"wallet": 0.0, "bank": 0.0})
        row["bank"] += delta
        return row["bank"]

    async def transfer_wallet(
        self, guild_id: int, sender_id: int, recipient_id: int, amount: float
    ) -> str:
        sender = self.users.get((sender_id, guild_id))
        if not sender or sender["wallet"] < amount:
            raise ValueError("Insufficient balance")
        sender["wallet"] -= amount
        recip = self.users.setdefault((recipient_id, guild_id), {"wallet": 0.0, "bank": 0.0})
        recip["wallet"] += amount
        self._tx_counter += 1
        return f"TXMOCK{self._tx_counter:06d}"

    # ── Token / price helpers ────────────────────────────────────────────────

    async def get_all_tokens_for_guild(self, guild_id: int) -> dict[str, dict]:
        return self.tokens.get(guild_id, {})

    async def get_price(self, symbol: str, guild_id: int) -> dict | None:
        return self.prices.get((symbol.upper(), guild_id))

    async def update_price(self, symbol: str, guild_id: int, new_price: float) -> None:
        key = (symbol.upper(), guild_id)
        if key in self.prices:
            self.prices[key]["price"] = new_price

    async def upsert_candle(
        self, guild_id: int, pair: str, minute: int, *,
        open_: float, high: float, low: float, close: float,
        volume_delta: float,
    ) -> None:
        """V3 Pillar 8: store the most recent candle keyed by (gid, pair, minute).

        Tests that want to assert chart impact read ``self.candles`` to see
        which OHLC rows the swap/trade path wrote.
        """
        if not hasattr(self, "candles"):
            self.candles = {}
        self.candles[(guild_id, pair.upper(), int(minute))] = {
            "open": open_, "high": high, "low": low, "close": close,
            "volume": volume_delta,
        }

    async def is_token_disabled(self, guild_id: int, symbol: str) -> bool:
        return (guild_id, symbol.upper()) in self.disabled_tokens

    async def is_network_halted(self, guild_id: int, net_key: str) -> bool:
        return (guild_id, net_key) in self.halted_networks

    # ── Holdings helpers ─────────────────────────────────────────────────────

    async def get_holding(self, user_id: int, guild_id: int, symbol: str) -> dict | None:
        return self.holdings.get((user_id, guild_id, symbol.upper()))

    async def update_holding(self, user_id: int, guild_id: int, symbol: str, delta: float) -> float:
        key = (user_id, guild_id, symbol.upper())
        row = self.holdings.setdefault(key, {"amount": 0.0, "symbol": symbol.upper()})
        row["amount"] += delta
        return row["amount"]

    async def get_holdings(self, user_id: int, guild_id: int) -> list[dict]:
        return [
            v for (uid, gid, _), v in self.holdings.items()
            if uid == user_id and gid == guild_id
        ]

    async def get_wallet_holding(
        self, user_id: int, guild_id: int, net_short: str, symbol: str
    ) -> dict | None:
        return self.wallet_holdings.get((user_id, guild_id, net_short, symbol.upper()))

    async def update_wallet_holding(
        self, user_id: int, guild_id: int, net_short: str, symbol: str, delta: float
    ) -> float:
        key = (user_id, guild_id, net_short, symbol.upper())
        row = self.wallet_holdings.setdefault(key, {"amount": 0.0, "symbol": symbol.upper()})
        row["amount"] += delta
        return row["amount"]

    async def get_all_wallet_holdings(self, user_id: int, guild_id: int) -> list[dict]:
        return [
            {**v, "network": net}
            for (uid, gid, net, _), v in self.wallet_holdings.items()
            if uid == user_id and gid == guild_id
        ]

    # ── Savings helpers ──────────────────────────────────────────────────────

    async def savings_deposit(self, user_id: int, guild_id: int, symbol: str, amount: float):
        key = (user_id, guild_id, symbol.upper())
        row = self.savings.setdefault(key, {"amount": 0.0, "symbol": symbol.upper()})
        row["amount"] += amount

    async def savings_withdraw(self, user_id: int, guild_id: int, symbol: str, amount: float):
        key = (user_id, guild_id, symbol.upper())
        row = self.savings.get(key)
        if not row or row["amount"] < amount:
            raise ValueError("Insufficient savings balance")
        row["amount"] -= amount

    async def get_savings_deposit(self, user_id: int, guild_id: int, symbol: str) -> dict | None:
        return self.savings.get((user_id, guild_id, symbol.upper()))

    # ── Validator / staking helpers ──────────────────────────────────────────

    async def get_validator(self, validator_id: str, guild_id: int) -> dict | None:
        return self.validators.get(validator_id)

    async def get_network_stake_token(self, guild_id: int, network: str) -> str | None:
        network_tokens = {
            "Sun Network": "SUN",
            "Arcadia Network": "ARC",
            "Moneta Chain": "MTA",
            "Discoin Network": "DSC",
        }
        return network_tokens.get(network)

    async def has_defi_wallet(self, user_id: int, guild_id: int, net_short: str) -> bool:
        return (user_id, guild_id, net_short) in self.defi_wallets

    async def update_stake(
        self, user_id: int, guild_id: int, validator_id: str, symbol: str, amount: float
    ):
        key = (user_id, guild_id, validator_id)
        row = self.stakes.setdefault(key, {"amount": 0.0, "staked_at": 0.0, "symbol": symbol})
        row["amount"] += amount

    async def get_stake(
        self, user_id: int, guild_id: int, validator_id: str
    ) -> dict | None:
        return self.stakes.get((user_id, guild_id, validator_id))

    async def get_user_stakes(self, user_id: int, guild_id: int) -> list[dict]:
        result = []
        for (uid, gid, vid), row in self.stakes.items():
            if uid == user_id and gid == guild_id:
                result.append({**row, "validator_id": vid})
        return result

    async def insert_stake_batch(
        self, user_id: int, guild_id: int, validator_id: str, symbol: str, amount: float
    ) -> None:
        key = (user_id, guild_id, validator_id)
        if not hasattr(self, "_stake_batches"):
            self._stake_batches: dict = {}
        self._stake_batches.setdefault(key, []).append({
            "id": len(self._stake_batches.get(key, [])) + 1,
            "user_id": user_id,
            "guild_id": guild_id,
            "validator_id": validator_id,
            "symbol": symbol,
            "amount": amount,
            "staked_at": __import__("time").time(),
        })

    async def get_stake_batches(
        self, user_id: int, guild_id: int, validator_id: str
    ) -> list[dict]:
        if not hasattr(self, "_stake_batches"):
            return []
        key = (user_id, guild_id, validator_id)
        return sorted(
            [b for b in self._stake_batches.get(key, []) if b["amount"] > 0],
            key=lambda b: b["staked_at"],
        )

    async def consume_stake_batches(
        self, user_id: int, guild_id: int, validator_id: str, amount: float, lock_secs: float
    ) -> tuple[float, list[dict]]:
        import time as _t
        now = _t.time()
        batches = await self.get_stake_batches(user_id, guild_id, validator_id)
        if not batches:
            return amount, []  # no batches = freely unlocked (backward compat)
        still_locked: list[dict] = []
        remaining = amount
        for b in batches:
            if b["staked_at"] + lock_secs > now:
                still_locked.append(b)
                continue
            take = min(float(b["amount"]), remaining)
            if take <= 0:
                break
            b["amount"] -= take
            remaining -= take
            if remaining <= 0:
                break
        consumed = amount - max(remaining, 0.0)
        return consumed, still_locked

    async def remove_stake(self, user_id: int, guild_id: int, validator_id: str, amount: float):
        key = (user_id, guild_id, validator_id)
        row = self.stakes.get(key)
        if not row or row["amount"] < amount:
            raise ValueError("Insufficient stake")
        row["amount"] -= amount

    async def get_pos_validators_for_network(self, guild_id: int, network: str) -> list[dict]:
        return []

    async def get_all_guild_rigs(self, guild_id: int) -> list[dict]:
        return []

    async def get_user_pos_validators(self, user_id: int, guild_id: int) -> list[dict]:
        return []

    async def get_user_lp_positions(self, user_id: int, guild_id: int) -> list[dict]:
        return []

    async def get_user_rigs(self, user_id: int, guild_id: int) -> list[dict]:
        return []

    async def get_user_delegations(self, user_id: int, guild_id: int) -> list[dict]:
        return []

    async def get_loan(self, user_id: int, guild_id: int) -> dict | None:
        return None

    async def get_hashstone(self, user_id: int, guild_id: int) -> dict | None:
        return None

    async def create_hashstone(self, user_id: int, guild_id: int, staked_amount: float) -> None:
        pass

    async def delete_hashstone(self, user_id: int, guild_id: int) -> None:
        pass

    async def transfer_hashstone(self, from_id: int, to_id: int, guild_id: int) -> None:
        pass

    async def update_hashstone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        pass

    async def add_hashstone_staked(self, user_id: int, guild_id: int, amount: float) -> None:
        pass

    async def get_lockstone(self, user_id: int, guild_id: int) -> dict | None:
        return None

    async def create_lockstone(self, user_id: int, guild_id: int, staked_amount: float) -> None:
        pass

    async def delete_lockstone(self, user_id: int, guild_id: int) -> None:
        pass

    async def transfer_lockstone(self, from_id: int, to_id: int, guild_id: int) -> None:
        pass

    async def update_lockstone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        pass

    async def add_lockstone_staked(self, user_id: int, guild_id: int, amount: float) -> None:
        pass

    async def get_vaultstone(self, user_id: int, guild_id: int) -> dict | None:
        return None

    async def create_vaultstone(self, user_id: int, guild_id: int, staked_amount: float) -> None:
        pass

    async def delete_vaultstone(self, user_id: int, guild_id: int) -> None:
        pass

    async def transfer_vaultstone(self, from_id: int, to_id: int, guild_id: int) -> None:
        pass

    async def update_vaultstone_xp(self, user_id: int, guild_id: int, new_xp: float, new_level: int) -> None:
        pass

    async def add_vaultstone_staked(self, user_id: int, guild_id: int, amount: float) -> None:
        pass

    async def get_liqstone(self, user_id: int, guild_id: int) -> dict | None:
        return None

    async def get_user_nfts(self, user_id: int, guild_id: int) -> list[dict]:
        return []

    async def get_validator_guard_count(self, user_id: int, guild_id: int) -> int:
        return 0

    async def add_validator_guard(self, user_id: int, guild_id: int, quantity: int = 1) -> int:
        return 0

    async def use_validator_guard(self, user_id: int, guild_id: int) -> bool:
        return False

    async def get_yield_guard_count(self, user_id: int, guild_id: int) -> int:
        return 0

    async def add_yield_guard(self, user_id: int, guild_id: int, quantity: int = 1) -> int:
        return 0

    async def use_yield_guard(self, user_id: int, guild_id: int) -> bool:
        return False

    async def get_group_upgrades(self, guild_id: int, group_id: str) -> list[dict]:
        return []

    async def has_group_upgrade(self, guild_id: int, group_id: str, upgrade_id: str) -> bool:
        return False

    async def add_group_upgrade(self, guild_id: int, group_id: str, upgrade_id: str) -> None:
        pass

    async def get_all_user_items(self, user_id: int, guild_id: int) -> dict:
        return {
            "hashstone": None, "lockstone": None, "vaultstone": None,
            "liqstone": None,
            "validator_guard_count": 0, "yield_guard_count": 0,
        }

    # ── Community / fee helpers ──────────────────────────────────────────────

    async def split_to_community_reserves(
        self, guild_id: int, symbol: str, fee: float, sun_price: float = 0.0
    ):
        pass

    async def update_circulating_supply(self, guild_id: int, symbol: str, delta: float) -> None:
        pass

    async def update_builtin_circulating_supply(self, guild_id: int, symbol: str, delta: float) -> None:
        pass

    async def get_user_job(self, user_id: int, guild_id: int) -> dict | None:
        return None

    # ── Transaction log ──────────────────────────────────────────────────────

    async def log_tx(self, *args, **kwargs) -> str:
        self._tx_counter += 1
        return f"TXMOCK{self._tx_counter:06d}"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_db():
    """Return a fresh MockDB instance for each test."""
    return MockDB()


@pytest.fixture
def guild_id():
    return GUILD_ID


@pytest.fixture
def user_id():
    return USER_ID


@pytest.fixture
def other_user_id():
    return OTHER_USER_ID
