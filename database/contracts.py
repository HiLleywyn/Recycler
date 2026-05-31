"""Contracts repository (PostgreSQL)  -  smart contracts, events, token contracts."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from .base import PgBaseRepo


class PgContractsRepo(PgBaseRepo):

    async def deploy_contract(
        self,
        guild_id: int,
        owner_id: int,
        network: str,
        name: str,
        ctype: str,
        definition: dict,
        description: str = "",
    ) -> str:
        """Deploy a new smart contract. Returns the contract address."""
        import hashlib as _hl

        now = datetime.now(timezone.utc)
        raw = f"{guild_id}:{owner_id}:{name}:{now.timestamp()}"
        address = "0x" + _hl.sha256(raw.encode()).hexdigest()[:40]
        # virtual_uid: large stable int derived from address (negative range to avoid user_id collision)
        virtual_uid = -(int(address[2:10], 16) % (10 ** 9) + 10 ** 9)
        await self.execute(
            """INSERT INTO smart_contracts
               (address, guild_id, owner_id, name, network, type, definition, virtual_uid, deployed_at, description)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            address, guild_id, owner_id, name, network, ctype,
            json.dumps(definition), virtual_uid, now, description,
        )
        return address

    async def get_contract(self, guild_id: int, address: str) -> dict | None:
        d = await self.fetch_one(
            "SELECT * FROM smart_contracts WHERE guild_id = $1 AND address = $2",
            guild_id, address,
        )
        if d is None:
            return None
        d["definition"] = d["definition"] if isinstance(d["definition"], dict) else json.loads(d["definition"])
        d["state"] = d["state"] if isinstance(d["state"], dict) else json.loads(d["state"])
        return d

    async def get_contracts(self, guild_id: int, network: str | None = None) -> list[dict]:
        if network:
            rows = await self.fetch_all(
                "SELECT * FROM smart_contracts WHERE guild_id = $1 AND network = $2 ORDER BY deployed_at DESC",
                guild_id, network,
            )
        else:
            rows = await self.fetch_all(
                "SELECT * FROM smart_contracts WHERE guild_id = $1 ORDER BY deployed_at DESC",
                guild_id,
            )
        result = []
        for d in rows:
            d["definition"] = d["definition"] if isinstance(d["definition"], dict) else json.loads(d["definition"])
            d["state"] = d["state"] if isinstance(d["state"], dict) else json.loads(d["state"])
            result.append(d)
        return result

    async def get_all_active_contracts(self, guild_id: int) -> list[dict]:
        """All non-paused smart contracts for a guild (with definition/state parsed)."""
        rows = await self.fetch_all(
            "SELECT * FROM smart_contracts WHERE guild_id = $1 AND is_paused = FALSE ORDER BY deployed_at ASC",
            guild_id,
        )
        result = []
        for d in rows:
            d["definition"] = d["definition"] if isinstance(d["definition"], dict) else json.loads(d["definition"])
            d["state"] = d["state"] if isinstance(d["state"], dict) else json.loads(d["state"])
            result.append(d)
        return result

    async def update_contract_state(self, address: str, state: dict) -> None:
        await self.execute(
            "UPDATE smart_contracts SET state = $1 WHERE address = $2",
            json.dumps(state), address,
        )

    async def increment_contract_calls(self, address: str) -> None:
        await self.execute(
            "UPDATE smart_contracts SET call_count = call_count + 1 WHERE address = $1",
            address,
        )

    async def pause_contract(self, address: str, paused: bool) -> None:
        await self.execute(
            "UPDATE smart_contracts SET is_paused = $1 WHERE address = $2",
            paused, address,
        )

    async def log_contract_event(
        self,
        guild_id: int,
        address: str,
        event: str,
        data: dict,
        block_id: int | None = None,
    ) -> None:
        await self.execute(
            "INSERT INTO contract_events (guild_id, address, event, data, block_id, ts) VALUES ($1, $2, $3, $4, $5, $6)",
            guild_id, address, event, json.dumps(data), block_id, datetime.now(timezone.utc),
        )

    async def get_contract_events(
        self, guild_id: int, address: str, limit: int = 20
    ) -> list[dict]:
        rows = await self.fetch_all(
            """SELECT * FROM contract_events
               WHERE guild_id = $1 AND address = $2
               ORDER BY ts DESC LIMIT $3""",
            guild_id, address, limit,
        )
        result = []
        for d in rows:
            d["data"] = d["data"] if isinstance(d["data"], dict) else json.loads(d["data"])
            result.append(d)
        return result

    async def get_token_contract(self, guild_id: int, symbol: str) -> dict:
        """Return contract params dict for a token (empty dict if no contract)."""
        d = await self.fetch_one(
            "SELECT params FROM token_contracts WHERE guild_id = $1 AND symbol = $2",
            guild_id, symbol,
        )
        if not d:
            return {}
        params = d["params"]
        return params if isinstance(params, dict) else json.loads(params)

    async def set_token_contract(self, guild_id: int, symbol: str, params: dict) -> None:
        await self.execute(
            """INSERT INTO token_contracts (guild_id, symbol, params) VALUES ($1, $2, $3)
               ON CONFLICT (guild_id, symbol) DO UPDATE SET params = EXCLUDED.params""",
            guild_id, symbol, json.dumps(params),
        )

    async def delete_token_contract(self, guild_id: int, symbol: str) -> None:
        await self.execute(
            "DELETE FROM token_contracts WHERE guild_id = $1 AND symbol = $2",
            guild_id, symbol,
        )

    async def apply_contract_transfer(
        self, guild_id: int, symbol: str, amount: float
    ) -> tuple[float, float]:
        """Apply contract rules to a transfer. Returns (net_amount, burned)."""
        params = await self.get_token_contract(guild_id, symbol)
        if not params:
            return amount, 0.0
        fee_rate = float(params.get("transfer_fee", 0.0))
        burn_rate = float(params.get("burn_rate", 0.0))
        if not math.isfinite(fee_rate):
            fee_rate = 0.0
        if not math.isfinite(burn_rate):
            burn_rate = 0.0
        fee_rate = max(0.0, min(1.0, fee_rate))
        burn_rate = max(0.0, min(1.0, burn_rate))
        fee = amount * fee_rate
        burned = amount * burn_rate
        net = amount - fee - burned
        return max(0.0, net), burned
