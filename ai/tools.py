"""DiscoAI tool registry.

Tools are the model's only path to game state. Every tool that touches a
balance, price, validator, or any other Discoin entity calls the bot's
own FastAPI surface (`api/v2/...`) -- the only direct DB access from the
ai/ module is to its own tables (facts/episodes), and that goes through
the MemoryService, not raw queries.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

from ai.memory import MemoryService

log = logging.getLogger(__name__)


ToolHandler = Callable[[dict, dict], Awaitable[Any]]


@dataclass
class _ToolEntry:
    name: str
    description: str
    schema: dict
    handler: ToolHandler


class ToolRegistry:
    """Registry of callable tools.

    Consumers that want OpenAI tool-call format call `as_openai_tools()`.
    """

    def __init__(self) -> None:
        self._tools: dict[str, _ToolEntry] = {}

    def tool(
        self,
        *,
        name: str,
        description: str,
        schema: dict,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """Decorator: register an async function as a callable tool."""

        def _decorator(fn: ToolHandler) -> ToolHandler:
            self._tools[name] = _ToolEntry(
                name=name, description=description, schema=schema, handler=fn,
            )
            return fn

        return _decorator

    def register(
        self,
        name: str,
        description: str,
        schema: dict,
        handler: ToolHandler,
    ) -> None:
        """Imperative variant of `.tool()` for tools defined elsewhere."""
        self._tools[name] = _ToolEntry(
            name=name, description=description, schema=schema, handler=handler,
        )

    def merge(self, other: "ToolRegistry") -> "ToolRegistry":
        """Return a new registry combining self + other (other wins on collision)."""
        merged = ToolRegistry()
        merged._tools = {**self._tools, **other._tools}
        return merged

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def as_openai_tools(self) -> dict[str, tuple[dict, ToolHandler]]:
        """Format consumable by any OpenAI-compatible tool-call consumer.

        Returns name -> ({"description": ..., "parameters": ...}, handler).
        """
        return {
            entry.name: (
                {"description": entry.description, "parameters": entry.schema},
                entry.handler,
            )
            for entry in self._tools.values()
        }


# ── HTTP helpers (for tools that call the FastAPI surface) ─────────────

class _ApiClient:
    """Tiny aiohttp wrapper that calls back into the bot's own FastAPI."""

    def __init__(self, base_url: str, *, timeout_s: int = 10) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def get(self, path: str, **params: Any) -> dict:
        if self._session is None:
            await self.start()
        url = f"{self._base_url}{path}"
        try:
            assert self._session is not None
            async with self._session.get(url, params=params) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    return {"error": f"http_{resp.status}", "detail": body[:200], "url": url}
                return await resp.json()
        except aiohttp.ClientError as exc:
            log.debug("DiscoAI api call to %s failed: %s", url, exc)
            return {"error": "request_failed", "detail": str(exc), "url": url}


# ── Default registry ──────────────────────────────────────────────────────

def build_default_registry(api: _ApiClient, memory: MemoryService) -> ToolRegistry:
    """Wire up the initial tool set the orchestrator hands to the model."""

    registry = ToolRegistry()

    @registry.tool(
        name="get_wallet_balance",
        description="Get a player's balance for a specific token. Use this for any 'how much X does user Y have' question instead of guessing.",
        schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "Discord user ID"},
                "token": {"type": "string", "description": "Token symbol, e.g. SUN, DSC, MTA"},
            },
            "required": ["user_id", "token"],
        },
    )
    async def get_wallet_balance(args: dict, ctx: dict) -> dict:
        uid = int(args["user_id"])
        token = str(args["token"]).upper()
        result = await api.get(f"/api/v2/users/{uid}/holdings", token=token)
        return result

    @registry.tool(
        name="get_market_price",
        description="Get the current oracle price (USD) and 24h change for a token.",
        schema={
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Token symbol, e.g. SUN, DSC, MTA"},
            },
            "required": ["token"],
        },
    )
    async def get_market_price(args: dict, ctx: dict) -> dict:
        token = str(args["token"]).upper()
        return await api.get(f"/api/v2/market/prices/{token}")

    @registry.tool(
        name="get_player_stats",
        description="Look up a player's profile: net worth, level, badges, recent activity.",
        schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "Discord user ID"},
            },
            "required": ["user_id"],
        },
    )
    async def get_player_stats(args: dict, ctx: dict) -> dict:
        uid = int(args["user_id"])
        return await api.get(f"/api/v2/users/{uid}/profile")

    @registry.tool(
        name="get_validator_info",
        description="Look up a validator by ID: APY, total stake, uptime, slash history.",
        schema={
            "type": "object",
            "properties": {
                "validator_id": {"type": "string", "description": "Validator identifier"},
            },
            "required": ["validator_id"],
        },
    )
    async def get_validator_info(args: dict, ctx: dict) -> dict:
        vid = str(args["validator_id"])
        return await api.get(f"/api/v2/staking/validators/{vid}")

    @registry.tool(
        name="remember_fact",
        description=(
            "Record a long-term fact in DiscoAI's memory. Use this when the "
            "user tells you something stable about themselves, the server, "
            "or game lore that you should recall in future conversations. "
            "Pick a stable, lower-snake-case key like 'favorite_token'."
        ),
        schema={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "Memory scope, e.g. 'user:123:guild:456', 'guild:456', or 'lore'.",
                },
                "key": {"type": "string", "description": "Stable identifier for this fact"},
                "value": {"type": "string", "description": "The fact itself, in plain prose"},
                "confidence": {
                    "type": "number",
                    "description": "0..1, how sure you are. Defaults to 0.7.",
                },
            },
            "required": ["scope", "key", "value"],
        },
    )
    async def remember_fact(args: dict, ctx: dict) -> dict:
        scope = str(args["scope"])
        key = str(args["key"])
        value = str(args["value"])
        confidence = float(args.get("confidence", 0.7))
        await memory.upsert_fact(scope, key, value, confidence=confidence, source="model")
        return {"ok": True, "scope": scope, "key": key}

    @registry.tool(
        name="recall_facts",
        description=(
            "Search DiscoAI's long-term memory for facts in a given scope "
            "matching a query. Returns up to 5 matches."
        ),
        schema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Scope to search within"},
                "query": {"type": "string", "description": "Free-text query"},
            },
            "required": ["scope", "query"],
        },
    )
    async def recall_facts(args: dict, ctx: dict) -> dict:
        scope = str(args["scope"])
        query = str(args["query"])
        facts = await memory.search_facts(scope, query, limit=5)
        return {
            "facts": [
                {"key": f.key, "value": f.value, "confidence": f.confidence}
                for f in facts
            ]
        }

    @registry.tool(
        name="get_fishing_stats",
        description=(
            "Look up a player's fishing minigame stats: rod tier, current "
            "zone, level, lifetime payout, biggest fish, longest combo. "
            "Use this for any 'how good is X at fishing' question."
        ),
        schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "Discord user ID"},
            },
            "required": ["user_id"],
        },
    )
    async def get_fishing_stats(args: dict, ctx: dict) -> dict:
        uid = int(args["user_id"])
        return await api.get(f"/api/v2/users/{uid}/fishing")

    @registry.tool(
        name="get_fishing_leaderboard",
        description=(
            "Top fishers on this guild by lifetime payout, plus the "
            "biggest fish ever landed. Use this to answer 'who's the "
            "best fisher' or 'what's the heaviest catch'."
        ),
        schema={
            "type": "object",
            "properties": {
                "guild_id": {"type": "integer", "description": "Discord guild ID"},
                "kind": {
                    "type": "string",
                    "description": "'payout' (default) or 'biggest' for trophies",
                },
            },
            "required": ["guild_id"],
        },
    )
    async def get_fishing_leaderboard(args: dict, ctx: dict) -> dict:
        gid = int(args["guild_id"])
        kind = str(args.get("kind", "payout"))
        return await api.get(f"/api/v2/guilds/{gid}/fishing", kind=kind)

    @registry.tool(
        name="search_lore",
        description=(
            "Search Discoin lore: facts and episodic summaries tagged as "
            "lore-scope. Use this for questions about the game's history, "
            "events, or canonical mechanics."
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text query"},
            },
            "required": ["query"],
        },
    )
    async def search_lore(args: dict, ctx: dict) -> dict:
        query = str(args["query"])
        facts = await memory.search_facts("lore", query, limit=5)
        episodes = await memory.search_episodes("lore", query, limit=3)
        return {
            "facts": [{"key": f.key, "value": f.value} for f in facts],
            "episodes": [{"summary": e.summary, "tags": e.tags} for e in episodes],
        }

    return registry
