from __future__ import annotations

import inspect
import json
import random
import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import discord
from discord import app_commands
from discord.ext import commands

from core.config import Config
from constants.ui import (
    C_BLURPLE, C_DARK_BLUE, C_ERROR, C_GOLD, C_INFO, C_PURPLE,
    C_STEEL, C_SUCCESS, C_TEAL, C_WARNING,
)
from core.framework.ai import sanitize_output
from core.framework.embed import card
from core.framework.ui import mention


class Intent(str, Enum):
    SEARCH = "search"
    INDEXING = "indexing"
    TOOL_CALL = "tool_call"
    UNKNOWN = "unknown"


@dataclass
class Parameter:
    name: str
    type: type
    description: str
    required: bool = True
    default: Any = None


@dataclass
class Command:
    name: str
    description: str
    parameters: list[Parameter]
    func: Callable[..., Any]
    examples: list[str] = field(default_factory=list)
    required_permissions: set[str] = field(default_factory=set)
    domain: str = "general"
    maps_to: str | None = None


@dataclass
class ParsedPrompt:
    intent: Intent
    command_name: str | None
    arguments: dict[str, Any]
    confidence: float = 0.0
    steps: list["CommandStep"] = field(default_factory=list)


@dataclass
class CommandStep:
    command_name: str
    arguments: dict[str, Any]
    source_text: str = ""
    confidence: float = 0.0


@dataclass
class InternalCommandResult:
    intent: Intent
    command_name: str | None
    arguments: dict[str, Any]
    raw_result: dict[str, Any] | None
    confidence: float = 0.0
    steps: list[CommandStep] = field(default_factory=list)


class InternalCommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def command(
        self,
        *,
        name: str,
        description: str,
        parameters: list[Parameter] | None = None,
        examples: list[str] | None = None,
        required_permissions: set[str] | None = None,
        domain: str = "general",
        maps_to: str | None = None,
    ):
        parameters = parameters or []
        examples = examples or []
        required_permissions = required_permissions or set()

        def decorator(func: Callable[..., Any]):
            self._commands[name] = Command(
                name=name,
                description=description,
                parameters=parameters,
                func=func,
                examples=examples,
                required_permissions=required_permissions,
                domain=domain,
                maps_to=maps_to,
            )
            return func

        return decorator

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def all(self) -> list[Command]:
        return list(self._commands.values())

    def has_permission(self, user_roles: set[str], command_name: str) -> bool:
        cmd = self.get(command_name)
        if cmd is None:
            return False
        if not cmd.required_permissions:
            return True
        return cmd.required_permissions.issubset(user_roles)


registry = InternalCommandRegistry()


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
    raise ValueError("invalid boolean")


def _coerce_value(value: Any, expected_type: type) -> Any:
    if value is None:
        return None
    if expected_type is bool:
        return _coerce_bool(value)
    if expected_type is int:
        return int(value)
    if expected_type is float:
        return float(value)
    if expected_type is str:
        return str(value).strip()
    return value


def _parse_prompt_tokens(prompt: str) -> list[str]:
    try:
        return shlex.split(prompt)
    except Exception:
        return prompt.split()


def _strip_leading_invoker(prompt: str) -> str:
    stripped = prompt.strip()
    lowered = stripped.lower()
    for prefix in ("bot ", "disco ", "discoin ", "assistant "):
        if lowered.startswith(prefix):
            return stripped[len(prefix):].strip()
    return stripped


def _get_bot(message) -> Any:
    """Get the bot/client from a message, compatible with all discord.py versions."""
    # Direct attribute (set on _InteractionBackedMessage or some discord.py versions)
    client = getattr(message, "client", None)
    if client is not None:
        return client
    # discord.py internal: Message._state._get_client()
    state = getattr(message, "_state", None)
    if state is not None:
        return state._get_client()
    return None


def _find_command_path(bot, path: str):
    parts = [p for p in path.split() if p]
    if not parts:
        return None
    current = bot.all_commands.get(parts[0])
    for part in parts[1:]:
        if current is None or not hasattr(current, "all_commands"):
            return None
        current = current.all_commands.get(part)
    return current


def _build_replay_content(command_text: str) -> str:
    return f"{Config.PREFIX}{command_text.strip()}"



class _ReplayMessage:
    """Lightweight message proxy that delegates to the original but overrides content."""

    __slots__ = ("_original", "content", "_bypass_cooldown")

    def __init__(self, original, content: str) -> None:
        self._original = original
        self.content = content
        self._bypass_cooldown = True

    def __getattr__(self, name: str):
        return getattr(self._original, name)


async def _replay_existing_command(message: discord.Message, command_text: str) -> dict[str, Any]:
    bot = _get_bot(message)
    content = _build_replay_content(command_text)
    new_message = _ReplayMessage(message, content)
    await bot.process_commands(new_message)
    return {"type": "silent", "content": f"Triggered command: `{content}`"}


async def _replay_existing_command_with_ack(message: discord.Message, command_text: str, ack: str) -> dict[str, Any]:
    await _replay_existing_command(message, command_text)
    return {"type": "text", "content": ack}


class InternalToolExecutor:
    def __init__(self, registry_obj: InternalCommandRegistry) -> None:
        self.registry = registry_obj

    async def execute(self, command_name: str, arguments: dict[str, Any], message: discord.Message) -> dict[str, Any]:
        cmd = self.registry.get(command_name)
        if not cmd:
            raise ValueError(f"Unknown internal command: {command_name}")

        user_roles = {
            getattr(role, "name", "")
            for role in getattr(message.author, "roles", [])
            if getattr(role, "name", None)
        }
        if not self.registry.has_permission(user_roles, command_name):
            raise ValueError("You do not have permission to use that internal command.")

        validated_args: dict[str, Any] = {}
        for param in cmd.parameters:
            if param.name in arguments:
                raw = arguments[param.name]
            else:
                raw = param.default
            if param.required and raw is None:
                raise ValueError(f"Missing argument: {param.name}")
            validated_args[param.name] = _coerce_value(raw, param.type)

        result = cmd.func(message=message, **validated_args)
        if inspect.isawaitable(result):
            result = await result
        return result


class SearchService:
    def __init__(self, bot) -> None:
        self.bot = bot

    async def search(self, guild_id: int, query: str, limit: int = 5) -> list[dict[str, Any]]:
        db = getattr(self.bot, "db", None)
        if db is None:
            return []

        query_l = query.lower().strip()
        if not query_l:
            return []

        rows: list[dict[str, Any]] = []
        prices = await db.get_all_prices(guild_id)
        for price in prices:
            symbol = str(price.get("symbol", "")).upper()
            if query_l in symbol.lower():
                rows.append(
                    {
                        "title": f"Token {symbol}",
                        "content": f"{symbol} is trading at ${float(price.get('price', 0.0)):.4f}.",
                        "source": "prices",
                    }
                )

        if not rows:
            for key, data in registry._commands.items():
                hay = f"{key} {data.description} {' '.join(data.examples)}".lower()
                if query_l in hay:
                    rows.append(
                        {
                            "title": f"Command {key}",
                            "content": data.description,
                            "source": "internal_commands",
                        }
                    )

        if not rows:
            settings = await db.get_guild_settings(guild_id)
            prefix = settings.get("prefix") or Config.PREFIX
            rows.append(
                {
                    "title": "No indexed matches yet",
                    "content": f"Try `{prefix}help`, `/discoin prompt: what can you do`, or a token query like `search ARC`.",
                    "source": "hint",
                }
            )

        return rows[: max(1, min(limit, 10))]


class IndexerService:
    def __init__(self, bot) -> None:
        self.bot = bot

    async def index_discord_channel(self, channel_id: int, limit: int = 100) -> dict[str, Any]:
        if limit <= 0:
            limit = 100
        return {
            "type": "text",
            "content": f"Indexing stub ready: would index up to {limit} messages from channel `{channel_id}`.",
        }

    async def index_url(self, url: str) -> dict[str, Any]:
        return {
            "type": "text",
            "content": f"Indexing stub ready: would index content from {url}",
        }

    async def index_file(self, file_path: str) -> dict[str, Any]:
        return {
            "type": "text",
            "content": f"Indexing stub ready: would index file `{file_path}`.",
        }


class InternalResultFormatter:
    @staticmethod
    def format(result: dict[str, Any]) -> tuple[str | None, discord.Embed | None]:
        kind = result.get("type", "text")
        if kind == "silent":
            return None, None
        if kind == "embed":
            return None, result.get("embed")
        if kind == "text":
            return str(result.get("content", "")), None
        if kind == "json":
            body = json.dumps(result.get("content", {}), indent=2, ensure_ascii=False)
            return f"```json\n{body[:1800]}\n```", None
        return str(result), None


def _step_display_text(step: CommandStep) -> str:
    prompt = str(step.arguments.get("prompt", "") or "").strip()
    if prompt:
        return prompt
    if step.arguments:
        args = " ".join(f"{key}={value}" for key, value in step.arguments.items())
        return f"{step.command_name} {args}".strip()
    return step.command_name


class InternalCommandModule:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.registry = registry
        self.executor = InternalToolExecutor(self.registry)
        self.formatter = InternalResultFormatter()
        self.search_service = SearchService(bot)
        self.indexer_service = IndexerService(bot)

    async def _check_beta_access(self, guild: discord.Guild, member: discord.Member) -> bool:
        """Return True if member has internal_commands beta access (admins always pass)."""
        from core.framework.middleware import check_beta_access
        return await check_beta_access(self.bot, guild, member, "internal_commands")

    async def maybe_handle(self, message: discord.Message) -> bool:
        """Handle internal commands via the explicit "bot <command>" /
        "disco <command>" / "discoin <command>" / "assistant <command>"
        text invokers. Requires beta access.

        Plain @mentions are deliberately NOT handled here -- those route to
        the AI mention pipeline in :class:`Discoin.on_message`. Replies to
        bot messages auto-ping the bot too, so they would otherwise get
        intercepted as commands and never reach the AI follow-up handler.
        """
        if not message.guild or message.author.bot or not message.content:
            return False

        # Fast invocation check before the DB beta access lookup
        content = message.content.strip()
        if not content.lower().startswith(("bot ", "disco ", "discoin ", "assistant ")):
            return False

        # Beta feature gate: admins always pass, others need beta access (DB lookup only for actual commands)
        member = message.author
        if not isinstance(member, discord.Member):
            return False
        if not await self._check_beta_access(message.guild, member):
            return False

        # Direct command dispatch  -  strip invoker prefix, match first token to registry
        content = _strip_leading_invoker(content).strip()
        tokens = content.split(None, 1)
        if not tokens:
            return False

        cmd_name = tokens[0].lower()
        args_str = tokens[1] if len(tokens) > 1 else ""

        # Look up command in registry (exact match only, with and without cmd_ prefix)
        cmd = self.registry.get(cmd_name) or self.registry.get(f"cmd_{cmd_name}")
        if not cmd:
            return False

        try:
            result = await self.executor.execute(cmd.name, {"prompt": args_str}, message)
            if result is None:
                return False
            text, embed = self.formatter.format(result)
            if embed is not None:
                await message.reply(embed=embed, mention_author=False)
            elif text:
                await message.reply(sanitize_output(text), mention_author=False)
            return True
        except Exception as exc:
            await message.reply(f"Internal command failed: {sanitize_output(str(exc))}", mention_author=False)
            return True

    async def respond_to_interaction(self, interaction: discord.Interaction, prompt: str) -> None:
        """Handle /discoin slash command interactions. Requires beta access."""
        # Beta feature gate
        member = interaction.user
        guild = interaction.guild
        if isinstance(member, discord.Member) and guild:
            if not await self._check_beta_access(guild, member):
                await interaction.response.send_message(
                    "You don't have beta access to internal commands. Ask an admin to grant it.",
                    ephemeral=True,
                )
                return
        elif isinstance(member, discord.Member) and not member.guild_permissions.manage_guild:
            await interaction.response.send_message("Admin-only command.", ephemeral=True)
            return

        tokens = prompt.strip().split(None, 1)
        if not tokens:
            await interaction.response.send_message("Provide a command name.", ephemeral=True)
            return

        cmd_name = tokens[0].lower()
        args_str = tokens[1] if len(tokens) > 1 else ""

        cmd = self.registry.get(cmd_name) or self.registry.get(f"cmd_{cmd_name}")
        if not cmd:
            await interaction.response.send_message(f"Unknown command: `{cmd_name}`", ephemeral=True)
            return

        pseudo_message = _InteractionBackedMessage(interaction)
        try:
            result = await self.executor.execute(cmd.name, {"prompt": args_str}, pseudo_message)
            if result is None:
                await interaction.response.send_message("Command returned no result.", ephemeral=True)
                return
            text, embed = self.formatter.format(result)
            if embed is not None:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, ephemeral=False)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=False)
            else:
                body = sanitize_output(text) if text else "Done."
                if not interaction.response.is_done():
                    await interaction.response.send_message(body[:1900], ephemeral=False)
                else:
                    await interaction.followup.send(body[:1900], ephemeral=False)
        except Exception as exc:
            msg = f"Internal command failed: {sanitize_output(str(exc))}"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg[:1900], ephemeral=True)
            else:
                await interaction.followup.send(msg[:1900], ephemeral=True)

    async def execute_prefix(self, ctx, prompt: str) -> None:
        """Handle internal commands from a prefix context (ctx.interaction is None). Requires beta access."""
        member = ctx.author
        guild = ctx.guild
        if not isinstance(member, discord.Member) or not guild:
            await ctx.reply("This command must be used in a server.", mention_author=False)
            return
        if not await self._check_beta_access(guild, member):
            await ctx.reply(
                "You don't have beta access to internal commands. Ask an admin to grant it.",
                mention_author=False,
            )
            return
        tokens = prompt.strip().split(None, 1)
        if not tokens:
            await ctx.reply("Provide a command name.", mention_author=False)
            return
        cmd_name = tokens[0].lower()
        args_str = tokens[1] if len(tokens) > 1 else ""
        cmd = self.registry.get(cmd_name) or self.registry.get(f"cmd_{cmd_name}")
        if not cmd:
            await ctx.reply(f"Unknown command: `{cmd_name}`", mention_author=False)
            return
        try:
            result = await self.executor.execute(cmd.name, {"prompt": args_str}, ctx.message)
            if result is None:
                await ctx.reply("Command returned no result.", mention_author=False)
                return
            text, embed = self.formatter.format(result)
            if embed is not None:
                await ctx.send(embed=embed)
            else:
                body = sanitize_output(text) if text else "Done."
                await ctx.reply(body[:1900], mention_author=False)
        except Exception as exc:
            await ctx.reply(f"Internal command failed: {sanitize_output(str(exc))}", mention_author=False)


class _InteractionBackedMessage:
    def __init__(self, interaction: discord.Interaction) -> None:
        self.author = interaction.user
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.content = ""
        self.id = interaction.id
        self.mentions: list = []
        self._interaction = interaction
        # Derive client/state robustly across discord.py versions
        self.client = getattr(interaction, "client", None)
        self._state = getattr(interaction, "_state", None)
        if self._state is None and self.client is not None:
            self._state = getattr(self.client, "_connection", None)

    async def reply(self, content: str = None, *, embed=None, mention_author=False, **kwargs):
        if not self._interaction.response.is_done():
            await self._interaction.response.send_message(content=content, embed=embed, ephemeral=False)
        else:
            await self._interaction.followup.send(content=content, embed=embed)


@registry.command(
    name="command_catalog",
    description="Return the internal command catalog and parameter schemas.",
    examples=["bot internal commands", "bot command catalog"],
)
async def command_catalog_command(*, message: discord.Message) -> dict[str, Any]:
    module = getattr(_get_bot(message), "internal_commands", None)
    catalog = []
    if module:
        for cmd in module.registry.all():
            catalog.append({
                "name": cmd.name,
                "description": cmd.description,
                "parameters": [{"name": p.name, "type": p.type.__name__, "required": p.required} for p in cmd.parameters],
                "examples": cmd.examples,
                "domain": cmd.domain,
            })
    return {"type": "json", "content": {"commands": catalog}}


def _register_replay_command(name: str, description: str, maps_to: str, domain: str = "game", examples: list[str] | None = None, required_permissions: set[str] | None = None):
    examples = examples or []
    required_permissions = required_permissions or set()

    @registry.command(
        name=name,
        description=description,
        parameters=[Parameter("prompt", str, "Original normalized prompt")],
        examples=examples,
        required_permissions=required_permissions,
        domain=domain,
        maps_to=maps_to,
    )
    async def _replay(*, message: discord.Message, prompt: str) -> dict[str, Any]:
        normalized = _strip_leading_invoker(prompt)
        tokens = _parse_prompt_tokens(normalized)
        target_tokens = maps_to.split()
        if not tokens:
            command_text = maps_to
        elif tokens[0].lower() == target_tokens[0].lower():
            command_text = normalized
        else:
            remainder = normalized
            command_text = f"{maps_to} {remainder}".strip()
        return await _replay_existing_command(message, command_text)

    return _replay


def _register_literal_replay_command(name: str, description: str, literal_text: str, domain: str = "game", examples: list[str] | None = None, required_permissions: set[str] | None = None):
    examples = examples or []
    required_permissions = required_permissions or set()

    @registry.command(
        name=name,
        description=description,
        parameters=[Parameter("prompt", str, "Original normalized prompt")],
        examples=examples,
        required_permissions=required_permissions,
        domain=domain,
        maps_to=literal_text,
    )
    async def _replay(*, message: discord.Message, prompt: str) -> dict[str, Any]:
        return await _replay_existing_command(message, literal_text)

    return _replay


def _register_multi_prompt_aliases() -> None:
    aliases: list[tuple[re.Pattern[str], str, str, str]] = [
        (re.compile(r"\b(mysavings|my savings|savings)\b", re.I), "cmd_savings", "economy", "Savings overview and savings balances."),
        (re.compile(r"\b(rates|apy|interest rates)\b", re.I), "cmd_rates", "economy", "Savings and borrowing rate info."),
        (re.compile(r"\b(loan status|my loan|loan overview)\b", re.I), "cmd_loan", "economy", "Loan overview and current status."),
        (re.compile(r"\b(market|crypto market|show prices)\b", re.I), "cmd_prices", "trade", "Price board and market prices."),
        (re.compile(r"\b(holdings|my portfolio|portfolio)\b", re.I), "cmd_portfolio", "trade", "Portfolio holdings and current value."),
        (re.compile(r"\b(play stats|gambling stats|gambstats)\b", re.I), "cmd_play_stats", "games", "Gambling statistics."),
        (re.compile(r"\b(drop money|spawn drop|manual drop)\b", re.I), "cmd_drops", "drops", "Manual money drop command."),
        # ── Part 5: New multi-prompt aliases ─────────────────────────────────
        (re.compile(r"\b(top gainers?|what.?s pumping|biggest movers?)\b", re.I), "top_gainers", "market", "Tokens with highest 24h gains."),
        (re.compile(r"\b(top losers?|what.?s dumping|biggest drops?)\b", re.I), "top_losers", "market", "Tokens with largest 24h drops."),
        (re.compile(r"\b(market overview|market summary|how.?s the market)\b", re.I), "market_overview", "market", "Aggregate market overview."),
        (re.compile(r"\b(server|guild) stats?\b|\beconomy overview\b", re.I), "server_stats", "server", "Server economy overview."),
        (re.compile(r"\btreasury\b|\bguild treasury\b", re.I), "treasury_balance", "server", "Guild treasury balance."),
        (re.compile(r"\bgas fees?\b|\bgas prices?\b", re.I), "gas_fees", "chain", "Current gas fee info."),
        (re.compile(r"\bnetworks?\b|\bchains?\b|\bblockchains?\b", re.I), "network_list", "chain", "List of all networks."),
        (re.compile(r"\bexplorer\b|\bchain stats\b", re.I), "explorer_summary", "chain", "Chain explorer summary."),
        (re.compile(r"\b(my )?stakes?\b|\bstaking positions?\b", re.I), "cmd_stake_mine", "staking", "User staking positions."),
        (re.compile(r"\b(my )?rigs?\b|\bhashrate\b", re.I), "cmd_mine_rigs", "mining", "User mining rigs."),
        (re.compile(r"\b(my )?delegations?\b", re.I), "cmd_val_delegations", "validators", "User PoS delegations."),
        (re.compile(r"\b(validator|staking) networks?\b", re.I), "cmd_val_networks", "validators", "Validator networks info."),
        (re.compile(r"\b(my )?(net ?worth|total wealth)\b", re.I), "cmd_balance", "economy", "Full net worth breakdown."),
    ]
    for pattern, cmd_name, domain, desc in aliases:
        registry._commands.setdefault(
            f"alias_{cmd_name}",
            Command(
                name=f"alias_{cmd_name}",
                description=desc,
                parameters=[Parameter("prompt", str, "Original normalized prompt")],
                func=lambda **_: {"type": "text", "content": "alias placeholder"},
                examples=[],
                required_permissions=set(),
                domain=domain,
                maps_to=cmd_name,
            ),
        )
    return aliases


_PROMPT_ALIASES = _register_multi_prompt_aliases()


@registry.command(
    name="search_docs",
    description="Search internal bot/game data for a natural language query.",
    parameters=[
        Parameter("query", str, "Search query."),
        Parameter("limit", int, "Maximum number of results.", required=False, default=5),
    ],
    examples=["bot search ARC", "bot find validator commands"],
)
async def search_docs_command(*, message: discord.Message, query: str, limit: int = 5) -> dict[str, Any]:
    module = getattr(_get_bot(message), "internal_commands", None)
    results = await module.search_service.search(message.guild.id, query, limit) if module else []
    builder = card(f"Search results for: {query}", color=C_SUCCESS).timestamp()
    if not results:
        builder = builder.description("No matching indexed results yet.")
    else:
        for i, item in enumerate(results, start=1):
            builder = builder.field(
                f"{i}. {item['title']}",
                f"{item['content']}\nSource: `{item['source']}`",
                False,
            )
    return {"type": "embed", "embed": builder.build()}


_register_replay_command("cmd_help", "Route NLP help requests to the existing help command.", "help", domain="utility", examples=["help economy", "help mining"])
_register_replay_command("cmd_balance", "Route balance/profile requests to the existing balance command.", "balance", domain="economy", examples=["balance", "balance --crypto", "show my balance"])
_register_replay_command("cmd_leaderboard", "Route leaderboard requests.", "leaderboard", domain="economy", examples=["leaderboard", "lb --token ARC"])
_register_replay_command("cmd_deposit", "Route deposit requests.", "deposit", domain="economy", examples=["deposit 100", "deposit all"])
_register_replay_command("cmd_withdraw", "Route withdraw requests.", "withdraw", domain="economy", examples=["withdraw 100", "withdraw all"])
_register_replay_command("cmd_transfer", "Route transfer requests.", "transfer", domain="economy", examples=["transfer @user 50"])
_register_replay_command("cmd_move", "Route storage movement requests.", "move", domain="economy", examples=["move 100 USD cash bank"])
_register_replay_command("cmd_notify", "Route DM notification settings requests.", "notify", domain="economy", examples=["notify", "notify mining off"])
_register_replay_command("cmd_wallet", "Route wallet management requests.", "wallet", domain="wallet", examples=["wallet list", "wallet create arc main"])
_register_replay_command("cmd_send", "Route on-chain send requests.", "send", domain="wallet", examples=["send 0xabc ARC 1 --yes"])
_register_replay_command("cmd_daily", "Route daily reward requests.", "daily", domain="earn", examples=["daily"])
_register_replay_command("cmd_work", "Route work requests.", "work", domain="earn", examples=["work"])
_register_replay_command("cmd_job", "Route current job requests.", "job", domain="earn", examples=["job"])
_register_replay_command("cmd_jobs", "Route jobs listing requests.", "jobs", domain="earn", examples=["jobs"])
_register_replay_command("cmd_promote", "Route job promotion requests.", "promote", domain="earn", examples=["promote"])
_register_replay_command("cmd_buy", "Route token buy requests.", "buy", domain="trade", examples=["buy ARC 1", "buy ARC $100"])
_register_replay_command("cmd_sell", "Route token sell requests.", "sell", domain="trade", examples=["sell ARC all"])
_register_replay_command("cmd_swap", "Route swap requests.", "swap", domain="trade", examples=["swap ARC LINK 1"])
_register_replay_command("cmd_portfolio", "Route portfolio requests.", "portfolio", domain="trade", examples=["portfolio"])
_register_replay_command("cmd_prices", "Route price board requests.", "prices", domain="trade", examples=["prices", "price ARC"])
_register_replay_command("cmd_tokeninfo", "Route token info requests.", "tokeninfo", domain="trade", examples=["tokeninfo ARC"])
_register_replay_command("cmd_chart", "Route chart requests.", "chart", domain="trade", examples=["chart ARCUSD 1h rsi macd"])
_register_replay_command("cmd_pool", "Route AMM pool requests.", "pool", domain="pools", examples=["pool list", "pool create ARC LINK"])
_register_replay_command("cmd_addlp", "Route add liquidity requests.", "addlp", domain="pools", examples=["addlp ARC LINK 1 50"])
_register_replay_command("cmd_removelp", "Route remove liquidity requests.", "removelp", domain="pools", examples=["removelp ARC LINK all"])
_register_replay_command("cmd_shop", "Route shop requests.", "shop", domain="shop", examples=["shop", "shop buy hashstone"])
_register_replay_command("cmd_inventory", "Route inventory requests.", "inventory", domain="shop", examples=["inventory", "inventory levelup hashstone"])
_register_replay_command("cmd_group", "Route mining group requests.", "group", domain="groups", examples=["group list", "group create Degens"])
_register_replay_command("cmd_stake", "Route staking requests.", "stake", domain="staking", examples=["stake list", "stake farm ARC 1"])
_register_replay_command("cmd_validator", "Route validator requests.", "stake validator", domain="validators", examples=["validator list", "validator register arc 100"], required_permissions=set())
_register_replay_command("cmd_chain", "Route blockchain/mining requests.", "chain", domain="chain", examples=["chain block 1", "chain mine status"])
_register_replay_command("cmd_contract", "Route contract requests.", "chain contract", domain="contracts", examples=["contract list", "contract info 0xabc"])
_register_replay_command("cmd_play", "Route gambling/gameplay requests.", "play", domain="games", examples=["play coinflip 100", "play mines 50 3"])
_register_replay_command("cmd_report", "Route report submission requests.", "report", domain="reporting", examples=["report bugs chart broke"])
_register_replay_command("cmd_reports", "Route report browsing requests.", "reports", domain="reporting", examples=["reports bugs"])
_register_replay_command("cmd_health", "Route health check requests.", "health", domain="admin", examples=["health check"], required_permissions={"manage_guild"})
_register_replay_command("cmd_backup", "Route backup requests.", "backup", domain="admin", examples=["backup list"], required_permissions={"manage_guild"})
_register_literal_replay_command("cmd_savings", "Route savings overview requests.", "savings", domain="economy", examples=["savings", "mysavings"])
_register_literal_replay_command("cmd_rates", "Route savings/loan rate requests.", "rates", domain="economy", examples=["rates", "apy"])
_register_literal_replay_command("cmd_loan", "Route loan overview/status requests.", "loan", domain="economy", examples=["loan", "loan status"])
_register_literal_replay_command("cmd_crypto", "Route crypto board requests.", "crypto", domain="trade", examples=["crypto", "market"])
_register_literal_replay_command("cmd_trade", "Route trade overview requests.", "trade", domain="trade", examples=["trade"])
_register_literal_replay_command("cmd_play_stats", "Route gambling stats requests.", "play stats", domain="games", examples=["play stats", "gambstats"])
_register_literal_replay_command("cmd_drops", "Route manual drop requests.", "drop", domain="drops", examples=["drop"])
_register_literal_replay_command("cmd_airdrop", "Route user airdrop requests.", "airdrop 100", domain="drops", examples=["airdrop 100", "airdrop 1.5 SOL"])

# ── Part 1B: Additional replay registrations ────────────────────────────────
_register_replay_command("cmd_admin", "Route admin commands.", "admin", domain="admin", examples=["admin settings", "admin give @user 100"], required_permissions={"manage_guild"})
_register_replay_command("cmd_earn", "Route earn group commands.", "earn", domain="earn", examples=["earn work", "earn daily"])
_register_replay_command("cmd_mine", "Route mining commands.", "chain mine", domain="mining", examples=["mine rigs", "mine status", "mine buy SUN 1"])
_register_replay_command("cmd_ask", "Route AI chat requests.", "ask", domain="utility", examples=["ask how do pools work", "ask what is staking"])
_register_replay_command("cmd_tx", "Route transaction lookup.", "chain tx", domain="chain", examples=["tx abc123", "tx info abc123"])
_register_replay_command("cmd_delegate", "Route delegation requests.", "stake validator delegate", domain="validators", examples=["delegate VAL1 ARC 10"])
_register_replay_command("cmd_undelegate", "Route undelegation requests.", "stake validator undelegate", domain="validators", examples=["undelegate 1 50"])

_register_literal_replay_command("cmd_mine_rigs", "Show mining rigs.", "chain mine rigs", domain="mining", examples=["my rigs", "mining rigs"])
_register_literal_replay_command("cmd_mine_status", "Show mining status.", "chain mine status", domain="mining", examples=["mine status", "mining status"])
_register_literal_replay_command("cmd_mine_network", "Show mining network stats.", "chain mine network", domain="mining", examples=["mining network", "pow stats"])
_register_literal_replay_command("cmd_stake_list", "List available NPC validators.", "stake list", domain="staking", examples=["stake list", "validators"])
_register_literal_replay_command("cmd_stake_mine", "Show my active stakes.", "stake mine", domain="staking", examples=["my stakes", "staking positions"])
_register_literal_replay_command("cmd_val_list", "List PoS validators.", "stake validator list", domain="validators", examples=["validator list", "player validators"])
_register_literal_replay_command("cmd_val_stats", "Validator statistics.", "stake validator stats", domain="validators", examples=["validator stats", "my validators"])
_register_literal_replay_command("cmd_val_delegations", "Show my delegations.", "stake validator delegations", domain="validators", examples=["my delegations", "delegations"])
_register_literal_replay_command("cmd_val_networks", "Validator network info.", "stake validator networks", domain="validators", examples=["validator networks", "staking networks"])
_register_literal_replay_command("cmd_val_mempool", "Show mempool.", "stake validator mempool", domain="validators", examples=["mempool", "pending transactions"])
_register_literal_replay_command("cmd_pool_list", "List AMM pools.", "trade pool list", domain="pools", examples=["pool list", "show pools"])
_register_literal_replay_command("cmd_contract_list", "List smart contracts.", "chain contract list", domain="contracts", examples=["contract list", "contracts"])
_register_literal_replay_command("cmd_bank_savings", "Savings overview.", "bank savings", domain="economy", examples=["my savings", "savings balance"])
_register_literal_replay_command("cmd_bank_loan", "Loan overview.", "bank loan", domain="economy", examples=["my loans", "loan status"])
_register_literal_replay_command("cmd_admin_settings", "View server settings.", "admin settings", domain="admin", examples=["server settings", "guild config"], required_permissions={"manage_guild"})
_register_literal_replay_command("cmd_admin_listtokens", "List all tokens.", "admin listtokens", domain="admin", examples=["list tokens"], required_permissions={"manage_guild"})
_register_literal_replay_command("cmd_admin_listnetworks", "List all networks.", "admin listnetworks", domain="admin", examples=["list networks"], required_permissions={"manage_guild"})
_register_literal_replay_command("cmd_admin_blockstatus", "Block bundling status.", "admin blockstatus", domain="admin", examples=["block status"], required_permissions={"manage_guild"})
_register_literal_replay_command("cmd_admin_reports", "Admin report view.", "admin reports", domain="admin", examples=["admin reports"], required_permissions={"manage_guild"})
_register_literal_replay_command("cmd_admin_halt_list", "Show halted networks/tokens.", "admin halt list", domain="admin", examples=["halted networks", "what's halted"], required_permissions={"manage_guild"})


@registry.command(
    name="index_url",
    description="Trigger URL indexing workflow stub.",
    parameters=[Parameter("url", str, "URL to index.")],
    examples=["bot index url https://example.com/article"],
)
async def index_url_command(*, message: discord.Message, url: str) -> dict[str, Any]:
    module = getattr(_get_bot(message), "internal_commands", None)
    if not module:
        return {"type": "text", "content": "Indexer not available."}
    return await module.indexer_service.index_url(url)


@registry.command(
    name="index_file",
    description="Trigger file indexing workflow stub.",
    parameters=[Parameter("file_path", str, "File path to index.")],
    examples=["bot index file docs/api/endpoints.md"],
)
async def index_file_command(*, message: discord.Message, file_path: str) -> dict[str, Any]:
    module = getattr(_get_bot(message), "internal_commands", None)
    if not module:
        return {"type": "text", "content": "Indexer not available."}
    return await module.indexer_service.index_file(file_path)


@registry.command(
    name="index_channel",
    description="Trigger Discord channel indexing workflow stub.",
    parameters=[
        Parameter("channel_id", int, "Discord channel ID to index."),
        Parameter("limit", int, "Maximum messages to index.", required=False, default=100),
    ],
    examples=["bot index channel 123456789012345678"],
)
async def index_channel_command(*, message: discord.Message, channel_id: int, limit: int = 100) -> dict[str, Any]:
    module = getattr(_get_bot(message), "internal_commands", None)
    if not module:
        return {"type": "text", "content": "Indexer not available."}
    return await module.indexer_service.index_discord_channel(channel_id, limit)


@registry.command(
    name="ping",
    description="Check whether the internal command module is alive.",
    examples=["bot ping", "@bot are you alive?"],
)
async def ping_command(*, message: discord.Message) -> dict[str, Any]:
    latency_ms = round(_get_bot(message).latency * 1000) if getattr(_get_bot(message), "latency", None) is not None else 0
    return {"type": "text", "content": f"Pong. Internal commands are live. Latency: {latency_ms}ms."}


@registry.command(
    name="echo",
    description="Repeat back the provided text.",
    parameters=[Parameter("text", str, "Text to echo back.")],
    examples=["bot echo hello world"],
)
async def echo_command(*, message: discord.Message, text: str) -> dict[str, Any]:
    return {"type": "text", "content": text[:1800]}


@registry.command(
    name="roll_dice",
    description="Roll a die with a configurable number of sides.",
    parameters=[Parameter("sides", int, "How many sides the die has.", required=False, default=6)],
    examples=["bot roll dice", "bot roll a d20"],
)
async def roll_dice_command(*, message: discord.Message, sides: int = 6) -> dict[str, Any]:
    sides = max(2, min(int(sides), 1000))
    rolled = random.randint(1, sides)
    return {"type": "text", "content": f"🎲 Rolled **{rolled}** on a d{sides}."}


@registry.command(
    name="flip_coin",
    description="Flip a coin.",
    examples=["bot flip a coin"],
)
async def flip_coin_command(*, message: discord.Message) -> dict[str, Any]:
    return {"type": "text", "content": f"🪙 {random.choice(['Heads', 'Tails'])}."}


@registry.command(
    name="wallet_summary",
    description="Show the calling user's USD wallet and bank balances.",
    examples=["bot show my balance", "@bot how much money do I have"],
)
async def wallet_summary_command(*, message: discord.Message) -> dict[str, Any]:
    db = getattr(_get_bot(message), "db", None)
    if db is None:
        return {"type": "text", "content": "Database isn't ready yet."}
    user = await db.get_user(message.author.id, message.guild.id)
    if not user:
        await db.ensure_user(message.author.id, message.guild.id)
        user = await db.get_user(message.author.id, message.guild.id)
    wallet = float(user.get("wallet", 0.0) or 0.0)
    bank = float(user.get("bank", 0.0) or 0.0)
    content = (
        f"**{message.author.display_name}**\n"
        f"Wallet: **${wallet:,.2f}**\n"
        f"Bank: **${bank:,.2f}**\n"
        f"Total USD: **${wallet + bank:,.2f}**"
    )
    return {"type": "text", "content": content}


@registry.command(
    name="token_price",
    description="Show the current price of a token in this guild.",
    parameters=[Parameter("symbol", str, "Token symbol to look up.")],
    examples=["bot price of ARC", "how much is SUN"],
)
async def token_price_command(*, message: discord.Message, symbol: str) -> dict[str, Any]:
    db = getattr(_get_bot(message), "db", None)
    if db is None:
        return {"type": "text", "content": "Database isn't ready yet."}
    symbol = symbol.upper().strip()
    prices = await db.get_all_prices(message.guild.id)
    row = next((r for r in prices if str(r.get("symbol", "")).upper() == symbol), None)
    if not row:
        return {"type": "text", "content": f"I couldn't find a live price for `{symbol}` in this guild."}
    return {"type": "text", "content": f"**{symbol}** is **${float(row['price']):,.4f}** right now."}


@registry.command(
    name="dashboard_link",
    description="Return the configured dashboard link if available.",
    examples=["bot open dashboard", "dashboard link"],
)
async def dashboard_link_command(*, message: discord.Message) -> dict[str, Any]:
    if not Config.DASHBOARD_URL:
        return {"type": "text", "content": "No dashboard URL is configured for this bot instance."}
    return {"type": "text", "content": f"Dashboard: {Config.DASHBOARD_URL}"}


@registry.command(
    name="command_help",
    description="Show available internal admin commands.",
    examples=["bot help commands"],
)
async def command_help_command(*, message: discord.Message) -> dict[str, Any]:
    p = Config.PREFIX
    embed = (
        card(
            "Discoin  -  Admin Internal Commands",
            color=C_BLURPLE,
            description=(
                "Say **bot** (or @mention me) followed by a command name. Requires **Manage Server** permission.\n"
                "Example: `bot balance`, `bot admin settings`, `bot health`."
            ),
        )
        .timestamp()
        .field(
            "💰 Economy",
            "`bot balance` · `bot daily` · `bot work`\n"
            "`bot my net worth` · `bot portfolio`\n"
            "`bot savings` · `bot loan` · `bot job`",
            True,
        )
        .field(
            "📊 Trading",
            "`bot buy ARC 1` · `bot sell SOL all`\n"
            "`bot swap ARC LINK 1` · `bot prices`\n"
            "`bot chart ARCUSD 1h`",
            True,
        )
        .field(
            "📈 Market Intel",
            "`bot top gainers` · `bot top losers`\n"
            "`bot market overview`\n"
            "`bot compare ARC MTA` · `bot SUN whales`",
            True,
        )
        .field(
            "⛏ Mining",
            "`bot mine rigs` · `bot mine status`\n"
            "`bot mining network` · `bot mine buy SUN 1`",
            True,
        )
        .field(
            "🥩 Staking",
            "`bot stake list` · `bot my stakes`\n"
            "`bot validator list` · `bot my delegations`\n"
            "`bot mempool`",
            True,
        )
        .field(
            "🏊 Pools",
            "`bot pool list` · `bot addlp ARC LINK 1 50`\n"
            "`bot removelp ARC LINK all`",
            True,
        )
        .field(
            "🔗 Chain",
            "`bot chain` · `bot chain tx HASH`\n"
            "`bot contract list` · `bot networks`\n"
            "`bot gas fees` · `bot explorer`",
            True,
        )
        .field(
            "🎰 Gambling",
            "`bot play coinflip 100` · `bot slots 50`\n"
            "`bot blackjack 200` · `bot gambling stats`",
            True,
        )
        .field(
            "🛒 Shop & Items",
            "`bot shop` · `bot inventory`\n`bot my items`",
            True,
        )
        .field(
            "🏦 Server",
            "`bot server stats` · `bot treasury`\n"
            "`bot leaderboard` · `bot who's the richest`",
            True,
        )
        .field(
            "🔧 Utility",
            "`bot ping` · `bot echo hello`\n"
            "`bot roll d20` · `bot flip coin`\n"
            "`bot dashboard` · `bot ask <question>`",
            True,
        )
        .field(
            "🔒 Admin",
            "`bot admin settings` · `bot admin health`\n"
            "`bot admin audit log` · `bot halted networks`",
            True,
        )
        .field(
            "⛓ Command Chaining",
            f"Link commands with operator symbols in one message:\n"
            f"`>` sequential  `&&` strict AND  `;` fire-and-forget\n"
            f"`||` fallback OR  `|` pipe  `+` parallel\n"
            f"`{p}buy ARC 1 > {p}move all ARC bank wallet`\n"
            f"Delays: `in 5m` `after 1h` `wait 2d`  •  `{p}help chaining`",
            False,
        )
        .footer(f"Prefix commands: {p}help | Admin-only: requires Manage Server permission")
        .build()
    )
    return {"type": "embed", "embed": embed}


# ── Part 3: Native internal commands (no prefix equivalent) ──────────────────


def _pct_change(price: float, open_price: float) -> float:
    if open_price <= 0:
        return 0.0
    return (price - open_price) / open_price * 100.0


@registry.command(
    name="top_gainers",
    description="Show tokens with the highest 24h price increase.",
    parameters=[Parameter("limit", int, "Number of results.", required=False, default=5)],
    examples=["bot top gainers", "bot what's pumping", "bot biggest movers"],
    domain="market",
)
async def top_gainers_command(*, message: discord.Message, limit: int = 5) -> dict[str, Any]:
    db = getattr(_get_bot(message), "db", None)
    if db is None:
        return {"type": "text", "content": "Database isn't ready yet."}
    prices = await db.get_all_prices(message.guild.id)
    if not prices:
        return {"type": "text", "content": "No token prices available."}
    ranked = []
    for row in prices:
        p, o = float(row.get("price", 0)), float(row.get("open_price", 0) or row.get("price", 0))
        ranked.append((row["symbol"], p, _pct_change(p, o)))
    ranked.sort(key=lambda x: x[2], reverse=True)
    limit = max(1, min(limit, 15))
    lines = []
    for sym, price, chg in ranked[:limit]:
        arrow = "🟢" if chg >= 0 else "🔴"
        lines.append(f"{arrow} **{sym}**  -  ${price:,.4f} ({chg:+.2f}%)")
    embed = (
        card("📈 Top Gainers", color=C_SUCCESS, description="\n".join(lines))
        .timestamp().build()
    )
    return {"type": "embed", "embed": embed}


@registry.command(
    name="top_losers",
    description="Show tokens with the largest 24h price decrease.",
    parameters=[Parameter("limit", int, "Number of results.", required=False, default=5)],
    examples=["bot top losers", "bot what's dumping", "bot biggest drops"],
    domain="market",
)
async def top_losers_command(*, message: discord.Message, limit: int = 5) -> dict[str, Any]:
    db = getattr(_get_bot(message), "db", None)
    if db is None:
        return {"type": "text", "content": "Database isn't ready yet."}
    prices = await db.get_all_prices(message.guild.id)
    if not prices:
        return {"type": "text", "content": "No token prices available."}
    ranked = []
    for row in prices:
        p, o = float(row.get("price", 0)), float(row.get("open_price", 0) or row.get("price", 0))
        ranked.append((row["symbol"], p, _pct_change(p, o)))
    ranked.sort(key=lambda x: x[2])
    limit = max(1, min(limit, 15))
    lines = []
    for sym, price, chg in ranked[:limit]:
        arrow = "🟢" if chg >= 0 else "🔴"
        lines.append(f"{arrow} **{sym}**  -  ${price:,.4f} ({chg:+.2f}%)")
    embed = (
        card("📉 Top Losers", color=C_ERROR, description="\n".join(lines))
        .timestamp().build()
    )
    return {"type": "embed", "embed": embed}


@registry.command(
    name="market_overview",
    description="Show an aggregate market summary: total market cap, avg change, top gainer/loser.",
    examples=["bot market overview", "bot how's the market", "bot market summary"],
    domain="market",
)
async def market_overview_command(*, message: discord.Message) -> dict[str, Any]:
    db = getattr(_get_bot(message), "db", None)
    if db is None:
        return {"type": "text", "content": "Database isn't ready yet."}
    prices = await db.get_all_prices(message.guild.id)
    if not prices:
        return {"type": "text", "content": "No token prices available."}

    total_cap = 0.0
    changes: list[tuple[str, float]] = []
    for row in prices:
        p = float(row.get("price", 0))
        o = float(row.get("open_price", 0) or p)
        total_cap += p
        changes.append((row["symbol"], _pct_change(p, o)))

    changes.sort(key=lambda x: x[1], reverse=True)
    avg_chg = sum(c for _, c in changes) / len(changes) if changes else 0.0
    top_gainer = changes[0] if changes else (" - ", 0.0)
    top_loser = changes[-1] if changes else (" - ", 0.0)

    embed = (
        card("🌍 Market Overview", color=C_INFO)
        .timestamp()
        .field("Tokens Tracked", f"**{len(prices)}**", True)
        .field("Avg 24h Change", f"**{avg_chg:+.2f}%**", True)
        .blank(True)
        .field("🟢 Top Gainer", f"**{top_gainer[0]}** ({top_gainer[1]:+.2f}%)", True)
        .field("🔴 Top Loser", f"**{top_loser[0]}** ({top_loser[1]:+.2f}%)", True)
        .build()
    )
    return {"type": "embed", "embed": embed}


@registry.command(
    name="compare_tokens",
    description="Side-by-side comparison of two tokens.",
    parameters=[
        Parameter("symbol_a", str, "First token symbol."),
        Parameter("symbol_b", str, "Second token symbol."),
    ],
    examples=["bot compare ARC and MTA", "bot ARC vs MTA"],
    domain="market",
)
async def compare_tokens_command(*, message: discord.Message, symbol_a: str, symbol_b: str) -> dict[str, Any]:
    db = getattr(_get_bot(message), "db", None)
    if db is None:
        return {"type": "text", "content": "Database isn't ready yet."}
    symbol_a, symbol_b = symbol_a.upper().strip(), symbol_b.upper().strip()
    row_a = await db.get_price(symbol_a, message.guild.id)
    row_b = await db.get_price(symbol_b, message.guild.id)
    if not row_a:
        return {"type": "text", "content": f"Token `{symbol_a}` not found."}
    if not row_b:
        return {"type": "text", "content": f"Token `{symbol_b}` not found."}

    def _row_info(sym: str, row: dict) -> str:
        p = float(row.get("price", 0))
        o = float(row.get("open_price", 0) or p)
        chg = _pct_change(p, o)
        meta = Config.TOKENS.get(sym, {})
        net = meta.get("network", " - ")
        return f"Price: **${p:,.4f}**\n24h: **{chg:+.2f}%**\nNetwork: {net}"

    embed = (
        card(f"⚖ {symbol_a} vs {symbol_b}", color=C_PURPLE)
        .timestamp()
        .field(symbol_a, _row_info(symbol_a, row_a), True)
        .field(symbol_b, _row_info(symbol_b, row_b), True)
        .build()
    )
    return {"type": "embed", "embed": embed}


@registry.command(
    name="token_holders",
    description="Show top holders of a specific token.",
    parameters=[
        Parameter("symbol", str, "Token symbol."),
        Parameter("limit", int, "Number of results.", required=False, default=10),
    ],
    examples=["bot top ARC holders", "bot SUN whales", "bot who holds the most MTA"],
    domain="market",
)
async def token_holders_command(*, message: discord.Message, symbol: str, limit: int = 10) -> dict[str, Any]:
    db = getattr(_get_bot(message), "db", None)
    if db is None:
        return {"type": "text", "content": "Database isn't ready yet."}
    symbol = symbol.upper().strip()
    limit = max(1, min(limit, 25))
    rows = await db.get_leaderboard_by_token(message.guild.id, symbol, limit)
    if not rows:
        return {"type": "text", "content": f"No holders found for `{symbol}`."}
    lines = []
    for i, row in enumerate(rows, 1):
        uid = row.get("user_id", 0)
        amount = float(row.get("amount", 0))
        lines.append(f"**{i}.** {mention(uid, guild=message.guild, bot=_get_bot(message))}  -  {amount:,.4f} {symbol}")
    embed = (
        card(f"🐋 Top {symbol} Holders", color=C_TEAL, description="\n".join(lines[:15]))
        .timestamp().build()
    )
    return {"type": "embed", "embed": embed}


@registry.command(
    name="server_stats",
    description="Show a server-wide economy overview.",
    examples=["bot server stats", "bot economy overview", "bot guild info"],
    domain="server",
)
async def server_stats_command(*, message: discord.Message) -> dict[str, Any]:
    db = getattr(_get_bot(message), "db", None)
    if db is None:
        return {"type": "text", "content": "Database isn't ready yet."}
    gid = message.guild.id
    users = await db.get_all_guild_users(gid)
    total_wallet = sum(float(u.get("wallet", 0)) for u in users)
    total_bank = sum(float(u.get("bank", 0)) for u in users)
    treasury = await db.get_treasury(gid)
    all_rigs = await db.get_all_guild_rigs(gid)
    all_stakes = await db.get_all_guild_stakes(gid)
    miner_ids = {r["user_id"] for r in all_rigs}
    staker_ids = {s["user_id"] for s in all_stakes}

    embed = (
        card(f"🏦 {message.guild.name} Economy", color=C_GOLD)
        .timestamp()
        .field("Users", f"**{len(users)}**", True)
        .field("USD in Wallets", f"**${total_wallet:,.2f}**", True)
        .field("USD in Banks", f"**${total_bank:,.2f}**", True)
        .field("Treasury", f"**${treasury:,.2f}**", True)
        .field("Active Miners", f"**{len(miner_ids)}**", True)
        .field("Active Stakers", f"**{len(staker_ids)}**", True)
        .build()
    )
    return {"type": "embed", "embed": embed}


@registry.command(
    name="treasury_balance",
    description="Show the guild treasury balance.",
    examples=["bot treasury balance", "bot guild treasury", "bot how much is in the treasury"],
    domain="server",
)
async def treasury_balance_command(*, message: discord.Message) -> dict[str, Any]:
    db = getattr(_get_bot(message), "db", None)
    if db is None:
        return {"type": "text", "content": "Database isn't ready yet."}
    treasury = await db.get_treasury(message.guild.id)
    return {"type": "text", "content": f"🏦 Guild treasury: **${treasury:,.2f}**"}


@registry.command(
    name="explorer_summary",
    description="Show a chain explorer summary with recent transaction stats.",
    examples=["bot explorer summary", "bot chain stats", "bot blockchain stats"],
    domain="chain",
)
async def explorer_summary_command(*, message: discord.Message) -> dict[str, Any]:
    db = getattr(_get_bot(message), "db", None)
    if db is None:
        return {"type": "text", "content": "Database isn't ready yet."}
    summary = await db.get_explorer_summary(message.guild.id)
    embed = (
        card("🔗 Chain Explorer Summary", color=C_DARK_BLUE)
        .timestamp()
        .field("Total Transactions", f"**{summary.get('total_tx', 0):,}**", True)
        .field("Unique Users", f"**{summary.get('unique_users', 0):,}**", True)
        .field("24h Transactions", f"**{summary.get('tx_24h', 0):,}**", True)
        .field("Total Volume (USD)", f"**${summary.get('total_volume', 0):,.2f}**", True)
        .field("Latest Block", f"**{summary.get('latest_block', ' - ')}**", True)
        .build()
    )
    return {"type": "embed", "embed": embed}


@registry.command(
    name="network_list",
    description="List all blockchain networks with their native coins and stablecoins.",
    examples=["bot show networks", "bot list chains", "bot what networks are there"],
    domain="chain",
)
async def network_list_command(*, message: discord.Message) -> dict[str, Any]:
    lines = []
    for net_name, coin in Config.NETWORK_COINS.items():
        stable = Config.NETWORK_STABLECOIN.get(net_name, " - ")
        is_pow = coin in Config.POW_NETWORKS
        consensus = "PoW" if is_pow else "PoS"
        lines.append(f"**{net_name}**  -  Coin: `{coin}` · Stable: `{stable}` · {consensus}")
    if not lines:
        return {"type": "text", "content": "No networks configured."}
    embed = (
        card("🌐 Networks", color=C_STEEL, description="\n".join(lines))
        .timestamp().build()
    )
    return {"type": "embed", "embed": embed}


@registry.command(
    name="network_info",
    description="Show detailed info for a specific network.",
    parameters=[Parameter("network", str, "Network name (e.g. arcadia, solana, sun).")],
    examples=["bot arcadia network info", "bot sun network details"],
    domain="chain",
)
async def network_info_command(*, message: discord.Message, network: str) -> dict[str, Any]:
    network_lower = network.lower().strip()
    matched_name = None
    for name in Config.NETWORK_COINS:
        if network_lower in name.lower():
            matched_name = name
            break
    if not matched_name:
        return {"type": "text", "content": f"Unknown network: `{network}`. Try `bot networks` to see all."}

    coin = Config.NETWORK_COINS.get(matched_name, " - ")
    stable = Config.NETWORK_STABLECOIN.get(matched_name, " - ")
    tokens = [sym for sym, data in Config.TOKENS.items() if data.get("network") == matched_name]
    is_pow = coin in Config.POW_NETWORKS

    builder = (
        card(f"🌐 {matched_name}", color=C_STEEL)
        .timestamp()
        .field("Native Coin", f"`{coin}`", True)
        .field("Stablecoin", f"`{stable}`", True)
        .field("Consensus", "PoW" if is_pow else "PoS", True)
        .field(
            f"Tokens ({len(tokens)})",
            ", ".join(f"`{t}`" for t in tokens[:20]) or "None",
            False,
        )
    )
    if is_pow:
        db = getattr(_get_bot(message), "db", None)
        if db:
            pow_state = await db.get_pow_network(message.guild.id, coin)
            if pow_state:
                builder = (
                    builder
                    .field("Block Height", f"**{pow_state.get('block_height', 0):,}**", True)
                    .field("Difficulty", f"**{pow_state.get('difficulty', 0):,.0f}**", True)
                    .field("Hashrate", f"**{pow_state.get('total_hashrate', 0):,.0f} H/s**", True)
                )
    return {"type": "embed", "embed": builder.build()}


@registry.command(
    name="gas_fees",
    description="Show current gas fees for a network.",
    parameters=[Parameter("network", str, "Network name.", required=False, default="Sun Network")],
    examples=["bot gas fees", "bot gas prices on arcadia", "bot how much is gas"],
    domain="chain",
)
async def gas_fees_command(*, message: discord.Message, network: str = "Sun Network") -> dict[str, Any]:
    db = getattr(_get_bot(message), "db", None)
    if db is None:
        return {"type": "text", "content": "Database isn't ready yet."}
    network_lower = network.lower().strip()
    matched_name = None
    for name in Config.NETWORK_COINS:
        if network_lower in name.lower():
            matched_name = name
            break
    if not matched_name:
        matched_name = "Sun Network"
    base_fee = await db.get_base_fee(message.guild.id, matched_name)
    return {"type": "text", "content": f"⛽ Gas fee on **{matched_name}**: **{base_fee:.6f}** per unit"}


@registry.command(
    name="admin_audit_log",
    description="Show recent admin action history.",
    parameters=[Parameter("limit", int, "Number of entries.", required=False, default=10)],
    examples=["bot admin audit log", "bot recent admin actions"],
    domain="admin",
    required_permissions={"manage_guild"},
)
async def admin_audit_log_command(*, message: discord.Message, limit: int = 10) -> dict[str, Any]:
    db = getattr(_get_bot(message), "db", None)
    if db is None:
        return {"type": "text", "content": "Database isn't ready yet."}
    limit = max(1, min(limit, 25))
    rows = await db.fetch_all(
        "SELECT admin_user_id, action, details, ts FROM audit_log WHERE guild_id = $1 ORDER BY ts DESC LIMIT $2",
        message.guild.id, limit,
    )
    if not rows:
        return {"type": "text", "content": "No audit log entries found."}
    lines = []
    for row in rows:
        uid = row["admin_user_id"]
        action = row["action"]
        details = (row["details"] or "")[:60]
        lines.append(f"{mention(uid, guild=message.guild, bot=_get_bot(message))}  -  **{action}** {details}")
    embed = (
        card("📋 Admin Audit Log", color=C_WARNING, description="\n".join(lines[:15]))
        .timestamp().build()
    )
    return {"type": "embed", "embed": embed}


class InternalCommandsCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(name="discoin", description="Run an internal Discoin admin command.", with_app_command=False)
    @app_commands.describe(prompt="Command to execute (admin-only)")
    async def discoin(self, ctx, *, prompt: str) -> None:
        if not prompt.strip():
            await ctx.reply("Provide a prompt, e.g. `bot show my balance`", mention_author=False)
            return
        if ctx.interaction is not None:
            await self.bot.internal_commands.respond_to_interaction(ctx.interaction, prompt)
        else:
            await self.bot.internal_commands.execute_prefix(ctx, prompt)


async def setup_internal_commands_cog(bot) -> None:
    if bot.get_cog("InternalCommandsCog") is None:
        await bot.add_cog(InternalCommandsCog(bot))
