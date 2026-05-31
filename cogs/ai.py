"""
cogs/ai.py -- staff-facing AI control surface.

Moves every AI-related admin command out of the ,admin group into its own
,ai group so operators have one place to tune chat flags, heal AI provider,
model picker, and per-category model defaults. Also exposes an audit feed
and a dropdown help menu via CategoryPaginator.

Gated by Manage Server permission, same as ,admin and ,mod.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.ai import complete as ai_complete
from core.framework.ai import (
    TOOL_CATEGORIES,
    catalog_for,
    clear_guild_default,
    is_vision_capable_slug,
    list_guild_defaults,
    set_guild_default,
)
from core.framework.agent_tools import ToolRegistry, disrepo, lua_plugins, registry_state
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.fuzzy import suggest_subcommand
from core.framework.middleware import guild_only
from core.framework.staff_audit import (
    SCOPE_AI,
    SEVERITY_DANGER,
    SEVERITY_INFO,
    SEVERITY_WARN,
    build_audit_embeds,
    log_staff_action,
    recent_staff_actions,
)
from core.framework.ui import (
    C_ERROR,
    C_INFO,
    C_NAVY,
    C_PURPLE,
    C_SUCCESS,
    C_WARNING,
    CategoryPaginator,
    fmt_ts,
)

log = logging.getLogger(__name__)


def _require_manage_guild():
    async def predicate(ctx: DiscoContext) -> bool:
        if not ctx.guild:
            raise commands.CheckFailure("This command can only be used in a server.")
        if not ctx.author.guild_permissions.manage_guild:
            raise commands.CheckFailure("You need Manage Server permission to use ,ai commands.")
        return True
    return commands.check(predicate)


_AI_FLAGS: dict[str, str] = {
    "mm":          "ai_mm_enabled",
    "chat":        "ai_chat_enabled",
    "commentary":  "ai_commentary_enabled",
    "flavor":      "ai_flavor_enabled",
    "events":      "ai_events_enabled",
}
_AI_PROMPT_FEATURES = ("chat", "commentary", "events", "flavor")
_HEAL_BACKENDS = ("openrouter", "ollama")
_SEARCH_BACKENDS = ("ddg", "brave", "openrouter", "perplexity", "ollama")
_LOOP_BACKENDS = ("openrouter", "ollama")
_RISK_ICON = {"read": "\U0001F7E2", "safe": "\U0001F535", "mutate": "\U0001F7E0", "danger": "\U0001F534"}
_STATE_ICON = {True: "\U0001F7E2", False: "\U0001F534"}


class AI(commands.Cog):
    """Staff-facing AI control surface."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        self._emoji_refresh_task.start()

    def cog_unload(self) -> None:
        self._emoji_refresh_task.cancel()

    async def cog_check(self, ctx) -> bool:
        """Premium gate: every AI command costs the host real money in
        token spend, so the entire cog is paid. Host guild bypasses."""
        from services import entitlements
        from core.framework.premium import PremiumGateFailure
        if not await entitlements.is_premium(ctx.guild_id, ctx.db):
            raise PremiumGateFailure("ai")
        return True

    # ------------------------------------------------------------------
    # Help category builder
    # ------------------------------------------------------------------
    def _build_ai_categories(self, p: str) -> dict[str, list[discord.Embed]]:
        def _page(title: str, lines: list[str], color=C_PURPLE) -> discord.Embed:
            b = card(title, color=color)
            b.description("\n".join(lines))
            b.footer(f"Use {p}ai <subcommand> to run any command")
            return b.build()

        return {
            "\U0001F916 Overview": [_page("\U0001F916 AI Control Surface", [
                f"{p}ai is the single place to configure and observe Discoin AI.",
                "",
                "Sections:",
                "\u2022 \u2699 Config  -  feature flags, prompts, persona, conversation history",
                "\u2022 \U0001FA7A Heal AI  -  provider for .health analyze",
                "\u2022 \U0001F9E0 Model Picker  -  per-guild model default for each tool category",
                "\u2022 \U0001F6E0 Agent Tools  -  registered tool catalog + enable/disable",
                "\u2022 \U0001F9E9 Plugins / Hooks / Agents  -  loaded Lua extensions",
                "\u2022 \U0001F4E6 Disrepo  -  install/search/uninstall plugins from hilleywyn/disrepo",
                "\u2022 \U0001F4CB Audit  -  AI-scope audit feed",
                "",
                f"All commands require Manage Server.",
            ])],
            "\u2699 Config": [_page("\u2699 AI Config", [
                f"{p}ai status  -  feature flags + OpenRouter key status",
                f"{p}ai toggle <mm|chat|commentary|flavor|events>  -  flip a flag",
                f"{p}ai test  -  send a test prompt to OpenRouter",
                f"{p}ai prompt <feature> [text|reset]  -  custom system prompt",
                f"  Features: chat, commentary, events, flavor",
                f"{p}ai persona [name]  -  display name (blank to reset)",
                f"{p}ai clearhistory [@user]  -  wipe ,ask history",
                f"{p}ai forget  -  wipe just YOUR memory summary",
                f"{p}ai recontext  -  rebuild YOUR context (history, traits, facts, short-term)",
                f"{p}ai recontext @user  -  same wipe for someone else (Manage Server)",
                f"{p}ai recontext server  -  NUCLEAR: wipe every user + drama + facts (Manage Server)",
                f"{p}ai recontext channel  -  drop channel-context feed + short-term here (Manage Server)",
                f"{p}ai reloadtools  -  hot-reload tools.json + Lua plugins",
                f"{p}ai memory forget  -  clear short-term memory in this channel",
                f"{p}ai memory facts [scope]  -  list DiscoAI long-term facts",
                f"{p}ai memory remember <scope> <key> <value>  -  upsert a fact",
                f"{p}ai memory listen <on|off>  -  toggle passive episode capture",
            ])],
            "\U0001FA7A Heal AI": [_page("\U0001FA7A Heal AI Provider", [
                f"{p}ai heal status  -  current provider config",
                f"{p}ai heal backend <openrouter|ollama>",
                f"{p}ai heal model <name>",
                f"{p}ai heal baseurl <url|reset>",
                f"{p}ai heal reset  -  wipe all overrides",
            ])],
            "\U0001F9E0 Model Picker": [_page("\U0001F9E0 Per-Guild Model Defaults", [
                f"{p}ai model list  -  show picks for every category",
                f"{p}ai model show <category>  -  curated catalog for one category",
                f"{p}ai model set <category> <provider:model|index>",
                f"{p}ai model reset <category>  -  revert to env default",
                "",
                "Categories: chat, tools, vision, image, search, code, reason,",
                "automation, defi, economy_sim",
            ])],
            "\U0001F501 Agent Loop": [_page("\U0001F501 Agent Loop Backend", [
                f"{p}ai loop status  -  show current backend + effective model",
                f"{p}ai loop backend <openrouter|ollama>",
                f"{p}ai loop reset  -  revert to env default",
                "",
                "openrouter - hosted models via OpenRouter (default)",
                "ollama     - local Ollama endpoint (needs OLLAMA_BASE_URL env var)",
                "",
                "Note: if a per-guild model is set via ,ai model set tools,",
                "its embedded provider always wins over this setting.",
            ])],
            "\U0001F50D Web Search": [_page("\U0001F50D Web Search Backend", [
                f"{p}ai websearch status  -  show current backend + key status",
                f"{p}ai websearch backend <ddg|brave|openrouter|perplexity|ollama>",
                f"{p}ai websearch reset  -  revert to env default",
                "",
                "ddg        - DuckDuckGo HTML scraping (no key needed)",
                "brave      - Brave Search API (needs BRAVE_SEARCH_API_KEY env var)",
                "openrouter - route through OpenRouter using the search model",
                "perplexity - direct Perplexity API (needs PERPLEXITY_API_KEY env var)",
                "ollama     - local Ollama endpoint (needs OLLAMA_BASE_URL env var)",
                "",
                "API keys must be set as Railway env vars, not in Discord.",
            ])],
            "\U0001F6E0 Agent Tools": [_page("\U0001F6E0 Agent Tool Registry", [
                f"{p}ai tools list [category]  -  list registered tools",
                f"{p}ai tools info <tool_name>  -  full schema for one tool",
                f"{p}ai tools enable <tool_name>  -  turn a tool ON (built-in or installed)",
                f"{p}ai tools disable <tool_name>  -  turn a tool OFF",
                "",
                "Risk icons: \U0001F7E2 read \u00B7 \U0001F535 safe \u00B7 \U0001F7E0 mutate \u00B7 \U0001F534 danger",
            ])],
            "\U0001F9E9 Plugins": [_page("\U0001F9E9 Lua Plugins, Hooks, Agents", [
                f"{p}ai plugins  -  list loaded plugin files + status",
                f"{p}ai plugins enable <stem>  -  enable a whole plugin file",
                f"{p}ai plugins disable <stem>  -  disable a whole plugin file",
                f"{p}ai hooks  -  list chat-pipeline hooks",
                f"{p}ai hooks enable <stem>  -  enable a hook bundle",
                f"{p}ai hooks disable <stem>  -  disable a hook bundle",
                f"{p}ai agents  -  list installed persona bundles",
                f"{p}ai agents enable <name>  -  enable an agent persona",
                f"{p}ai agents disable <name>  -  disable an agent persona",
            ])],
            "\U0001F4E6 Disrepo": [_page("\U0001F4E6 Disrepo Plugin Installer", [
                f"Remote catalog: hilleywyn/disrepo (main)",
                "",
                f"{p}ai search [query]  -  browse the disrepo catalog",
                f"{p}ai install <type>/<name>  -  fetch + install an item",
                f"  Types: tools, agents, plugins, hooks",
                f"{p}ai uninstall <type>/<name>  -  remove + disable",
                f"{p}ai installed  -  show every disrepo-installed item",
                "",
                "Every install is DISABLED by default.",
                f"Flip the switch with {p}ai tools enable <tool_name>,",
                f"{p}ai hooks enable <stem>, or {p}ai plugins enable <stem>.",
            ])],
            "\U0001F3E5 Doctor": [_page("\U0001F3E5 AI Doctor  -  Live Auto-Repair", [
                f"{p}ai doctor           -  probe every AI backend live and auto-flip unhealthy ones",
                f"{p}ai doctor dryrun    -  probe only, don't mutate config",
                f"{p}ai doctor test      -  inject a broken backend + verify the repair works",
                "",
                "Backends probed: OpenRouter, Ollama (chat + vision),",
                "DuckDuckGo, Perplexity.",
                "",
                "Categories auto-repaired on failover:",
                "  tools       -  agent loop backend (guild_settings.tools_backend)",
                "  vision      -  per-guild ai_model_defaults",
                "  websearch   -  guild_settings.search_backend",
                "  heal_ai     -  guild_settings.heal_ai_backend",
                "",
                "Chat is covered indirectly via the tools backend repair.",
            ])],
            "\U0001F50D Emojis": [_page("\U0001F50D Custom Emoji Meanings", [
                f"{p}ai emojis          -  index coverage + staleness stats",
                f"{p}ai emojis index    -  refresh stale entries (>14 days)",
                f"{p}ai emojis index force  -  re-index every emoji",
                f"{p}ai emojis show     -  paginated list of indexed meanings",
                f"{p}ai emojis set <emoji> <text>  -  override a meaning",
                "",
                "Each emoji's nuanced description is produced from a vision",
                "pass on the image plus recent in-channel usage samples.",
                "Entries auto-refresh every 14 days so meanings stay in sync",
                "with how the server actually uses its palette.",
            ])],
            "\U0001F4CB Audit": [_page("\U0001F4CB AI Audit Feed", [
                f"{p}ai audit [limit]  -  show recent ,ai scope audit rows",
                "",
                "The AI scope captures model picks, prompt changes, test runs,",
                "and any other staff action performed through ,ai.",
            ])],
        }

    # ------------------------------------------------------------------
    # Top-level ,ai group
    # ------------------------------------------------------------------
    @commands.group(name="ai", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def ai(self, ctx: DiscoContext) -> None:
        """AI control surface. Run ,ai help for full reference."""
        if await suggest_subcommand(ctx, self.ai):
            return
        p = ctx.prefix or "."
        await CategoryPaginator.send(ctx, self._build_ai_categories(p))

    @ai.command(name="help")
    @_require_manage_guild()
    async def ai_help(self, ctx: DiscoContext) -> None:
        """Full ,ai command reference with a category dropdown."""
        p = ctx.prefix or "."
        await CategoryPaginator.send(ctx, self._build_ai_categories(p))

    # ------------------------------------------------------------------
    # Config block
    # ------------------------------------------------------------------
    @ai.command(name="status")
    @_require_manage_guild()
    async def ai_status(self, ctx: DiscoContext) -> None:
        """Show AI feature flags and provider status."""
        import asyncio as _asyncio
        flags, picks = await _asyncio.gather(
            ctx.db.get_ai_flags(ctx.guild_id),
            list_guild_defaults(ctx.db, ctx.guild_id),
        )
        has_key = bool(Config.OPENROUTER_API_KEY)

        def _resolved(cat_key: str) -> str:
            """Return the effective model string for a category.

            Checks the per-guild DB override first (set via ,ai model set),
            then falls back to the env-var default for that category so the
            display always matches what resolve_model() would return at
            runtime -- not just the raw OPENROUTER_MODEL env var.
            """
            pick = picks.get(cat_key) if isinstance(picks, dict) else None
            if pick and getattr(pick, "provider", None) and getattr(pick, "model", None):
                return f"{pick.provider}:{pick.model}"
            # Fall back to the env var for this specific category.
            cat = next((c for c in TOOL_CATEGORIES if c.key == cat_key), None)
            if cat:
                env_model = str(getattr(Config, cat.default_env[1], "") or "")
                return env_model or "unset (env)"
            return "unset"

        n_overrides = sum(
            1 for p in (picks.values() if isinstance(picks, dict) else [])
            if getattr(p, "model", None)
        )

        b = card("\U0001F916 AI Status", color=C_PURPLE)
        b.field("API Key", "Configured" if has_key else "Missing", True)
        b.field("Chat model", _resolved("chat"), True)
        b.field("Tools model", _resolved("tools"), True)
        for key, _col in _AI_FLAGS.items():
            # ``flags`` is keyed by the short feature name ("chat", "mm", ...)
            # not the underlying ``ai_*_enabled`` column, so look up by ``key``.
            val = bool(flags.get(key, False)) if isinstance(flags, dict) else False
            b.field(key, "ON" if val else "OFF", True)
        p = ctx.prefix or "."
        b.footer(
            f"{n_overrides} categor{'y' if n_overrides == 1 else 'ies'} overridden"
            f" -- {p}ai model list for full breakdown"
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai.command(name="queue")
    @_require_manage_guild()
    async def ai_queue(self, ctx: DiscoContext) -> None:
        """Show current AI chat queue depth per backend.

        Surfaces how many requests are in-flight vs waiting on each
        backend so operators can spot Ollama saturation (which is the
        usual cause of "AI didn't respond" timeouts) without tailing the
        logs. Per-backend caps are configured via ``AI_QUEUE_*`` env vars.
        """
        from core.framework.ai.client import chat_queue
        snapshots = chat_queue.stats()
        b = card("\U0001F916 AI Queue", color=C_INFO)
        for s in snapshots:
            value = (
                f"in-flight: **{s.in_flight}** / **{s.capacity}**\n"
                f"waiting: **{s.waiting}** (across {s.waiting_users} user"
                f"{'s' if s.waiting_users != 1 else ''})\n"
                f"system reserved: {s.system_reserved}"
            )
            b.field(s.backend, value, True)
        b.footer(
            f"caps: OPENROUTER={Config.AI_QUEUE_OPENROUTER_CAP}, "
            f"OLLAMA={Config.AI_QUEUE_OLLAMA_CAP}, "
            f"SYSTEM_RESERVED={Config.AI_QUEUE_SYSTEM_RESERVED}"
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai.command(name="toggle")
    @_require_manage_guild()
    async def ai_toggle(self, ctx: DiscoContext, feature: str) -> None:
        """Toggle an AI feature flag on/off."""
        key = (feature or "").strip().lower()
        col = _AI_FLAGS.get(key)
        if not col:
            await ctx.reply_error(f"Unknown feature {feature!r}. Valid: {', '.join(_AI_FLAGS)}")
            return
        flags = await ctx.db.get_ai_flags(ctx.guild_id)
        # ``flags`` is keyed by the short feature name, not the column name --
        # reading by ``col`` always missed and pinned ``current`` to False, so
        # the toggle was one-way (always wrote True regardless of actual state).
        current = bool(flags.get(key, False)) if isinstance(flags, dict) else False
        new_val = not current
        await ctx.db.update_guild_setting(ctx.guild_id, col, int(new_val))
        await ctx.reply_success(f"{key} is now {'ON' if new_val else 'OFF'}.", title="AI Toggle")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="toggle", severity=SEVERITY_INFO, details=f"{key}={new_val}",
        )

    @ai.command(name="test")
    @_require_manage_guild()
    async def ai_test(self, ctx: DiscoContext) -> None:
        """Send a test prompt to OpenRouter."""
        if not Config.OPENROUTER_API_KEY:
            await ctx.reply_error("OpenRouter API key is not configured.")
            return
        try:
            result = await ai_complete(
                [
                    {"role": "system", "content": "You are Discoin."},
                    {"role": "user", "content": "Say 'AI is working!' in one degen crypto sentence."},
                ],
                max_tokens=40,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("ai.test failed")
            await ctx.reply_error(f"AI call failed: {exc}")
            return
        if not result:
            await ctx.reply_error("AI returned an empty response.")
            return
        await ctx.reply_success(str(result), title="\U0001F916 AI Test")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="test", severity=SEVERITY_INFO, details="test prompt",
        )

    @ai.command(name="prompt")
    @_require_manage_guild()
    async def ai_prompt(self, ctx: DiscoContext, feature: str, *, prompt: str = "") -> None:
        """Set or reset a custom system prompt for an AI feature."""
        feat = (feature or "").strip().lower()
        if feat not in _AI_PROMPT_FEATURES:
            await ctx.reply_error(f"Unknown feature {feature!r}. Valid: {', '.join(_AI_PROMPT_FEATURES)}")
            return
        text = (prompt or "").strip()
        if not text or text.lower() == "reset":
            value = None
            msg = f"Reset {feat} prompt to default."
        else:
            value = text
            msg = f"Updated {feat} prompt ({len(text)} chars)."
        await ctx.db.update_guild_setting(ctx.guild_id, f"ai_prompt{feat}", value)
        await ctx.reply_success(msg, title="AI Prompt")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="prompt", severity=SEVERITY_INFO,
            details=f"{feat}={'reset' if value is None else f'{len(text)} chars'}",
        )

    @ai.command(name="persona")
    @_require_manage_guild()
    async def ai_persona(self, ctx: DiscoContext, *, name: str = "") -> None:
        """Set or reset the AI persona display name."""
        value = (name or "").strip() or None
        await ctx.db.update_guild_setting(ctx.guild_id, "ai_persona_name", value)
        if value:
            await ctx.reply_success(f"Persona name set to {value}.", title="AI Persona")
        else:
            await ctx.reply_success("Persona name reset to default.", title="AI Persona")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="persona", severity=SEVERITY_INFO, details=f"name={value or 'default'}",
        )

    @ai.command(name="clearhistory")
    @_require_manage_guild()
    async def ai_clearhistory(self, ctx: DiscoContext, member: discord.Member | None = None) -> None:
        """Wipe AI conversation history for a user or the whole guild."""
        if member is not None:
            await ctx.db.clear_ai_conversation(member.id, ctx.guild_id)
            await ctx.reply_success(f"Cleared AI history for {member.mention}.", title="AI History")
            await log_staff_action(
                ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
                action="clearhistory", severity=SEVERITY_INFO, details=f"target={member.id}",
            )
            return
        confirmed = await ctx.confirm("Clear all AI conversation history on this server?")
        if not confirmed:
            await ctx.reply_error("Cancelled.")
            return
        await ctx.db.clear_all_ai_conversations(ctx.guild_id)
        await ctx.reply_success("Cleared all AI conversation history for this server.", title="AI History")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="clearhistory", severity=SEVERITY_WARN, details="scope=guild",
        )

    @ai.command(name="forget", aliases=["forgetme", "wipeme", "forget-me"])
    async def ai_forget(self, ctx: DiscoContext) -> None:
        """Wipe the AI's persistent memory of YOU in this guild.

        Use this if the bot keeps parroting a stale claim ('you have $0',
        'you're broke', etc.) -- the memory row is overwritten every time
        it refreshes, so a fresh start lets the next conversation re-seed
        from correct data. Only your own memory is cleared; conversation
        history is separate and untouched.
        """
        cleared = await ctx.db.clear_ai_user_memory(ctx.author.id, ctx.guild_id)
        if cleared:
            await ctx.reply_success(
                "Your AI memory has been wiped. The next conversation will "
                "build fresh context from your current holdings.",
                title="🧠 Forgotten",
            )
        else:
            await ctx.reply_success(
                "Nothing to clear -- you had no stored AI memory in this guild.",
                title="🧠 Already Empty",
            )

    @ai.command(name="recontext", aliases=["refresh", "resync", "rebuild"])
    async def ai_recontext(
        self,
        ctx: DiscoContext,
        target: str | None = None,
    ) -> None:
        """Rebuild Disco's context from scratch -- stops it from looping on stale info.

        Three scopes:

        - ``,ai recontext`` (no arg) - wipes YOUR per-user context in this
          guild. Anyone can run it. Drops conversation history, memory
          summary, traits, reaction counters, tool memory, and your
          long-term facts; walks Redis for short-term buffers across every
          channel.
        - ``,ai recontext @user`` - same wipe, but for someone else.
          Manage Server only. Audit-logged.
        - ``,ai recontext server`` (aliases: ``all``, ``everything``) -
          NUCLEAR option. Wipes per-user context for EVERY user in the
          guild, drops the recent server-events drama feed and the
          channel-context (reactions / edits / deletes / banter) feed,
          deletes guild-scoped DiscoAI facts and episodes, and walks
          Redis for every channel's short-term buffer. Manage Server
          only. Configuration tables (custom prompts, AI-channel
          allowlist, model picks) are NOT touched.
        - ``,ai recontext channel`` (alias: ``here``) - drops the
          channel-context feed and Redis short-term buffers for THIS
          channel only. Manage Server only.

        After any wipe the next ``,ask`` / mention / reply rebuilds
        fresh from current portfolio data + clean history.
        """
        # Decide what we're targeting. Member mention -> user wipe of that
        # member; bare keyword -> server / channel wipe; nothing -> caller.
        scope: str = "user"
        target_member: discord.Member | None = None
        if target:
            tlower = target.strip().lstrip("@").lower()
            if tlower in ("server", "guild", "all", "everything", "everyone", "*"):
                scope = "server"
            elif tlower in ("channel", "here", "room", "this"):
                scope = "channel"
            else:
                # Try to resolve as a member mention or name.
                converter = commands.MemberConverter()
                try:
                    target_member = await converter.convert(ctx, target)
                except commands.BadArgument:
                    await ctx.reply_error(
                        f"Don't know what `{target}` means. Use a `@member`, "
                        f"`server`, or `channel`.",
                    )
                    return
                scope = "user"
        # else: scope = "user", target_member = None (caller)

        if scope in ("server", "channel") or (
            scope == "user" and target_member is not None
            and target_member.id != ctx.author.id
        ):
            if not ctx.author.guild_permissions.manage_guild:
                await ctx.reply_error(
                    "You need Manage Server for that scope. "
                    "Run `,ai recontext` with no argument to wipe your own state.",
                )
                return

        disco = self.bot.get_cog("DiscoAI")
        mem = getattr(disco, "_memory", None) if disco is not None else None
        help_cog = self.bot.get_cog("Help")

        if scope == "user":
            await self._do_user_recontext(
                ctx, target_member or ctx.author, mem, help_cog,
            )
        elif scope == "server":
            await self._do_server_recontext(ctx, mem, help_cog)
        else:  # scope == "channel"
            await self._do_channel_recontext(ctx, mem)

    async def _do_user_recontext(
        self,
        ctx: DiscoContext,
        target: discord.Member,
        mem,
        help_cog,
    ) -> None:
        deleted = await ctx.db.wipe_ai_user_state(target.id, ctx.guild_id)

        short_cleared = 0
        if mem is not None:
            try:
                short_cleared = await mem.clear_user_in_guild(
                    ctx.guild_id, target.id,
                )
            except Exception as exc:
                log.debug("ai recontext short-term wipe failed: %s", exc)

        if help_cog is not None:
            try:
                help_cog._ai_cooldowns.pop(target.id, None)
            except Exception:
                pass

        total_db = sum(deleted.values())
        if total_db == 0 and short_cleared == 0:
            await ctx.reply_success(
                f"Nothing to clear -- {target.display_name} had no AI state in this guild.",
                title="Already Clean",
            )
            return

        c = card(
            "AI Context Rebuilt -- User",
            description=(
                f"Wiped Disco's per-user context for **{target.display_name}** "
                f"in this guild. The next AI reply will rebuild from current "
                f"portfolio data and a clean history."
            ),
            color=C_SUCCESS,
        )
        for label, key in (
            ("Conversation history", "ai_conversations"),
            ("Memory summary", "ai_user_memory"),
            ("Inferred traits", "ai_user_traits"),
            ("Reaction counters", "ai_reaction_memory"),
            ("Tool memory", "ai_tool_memory"),
            ("Long-term facts", "disco_facts"),
        ):
            if deleted.get(key):
                c = c.field(label, str(deleted[key]), True)
        if short_cleared:
            c = c.field("Short-term buffers", str(short_cleared), True)
        await ctx.reply(embed=c.build(), mention_author=False)

        if target.id != ctx.author.id:
            await log_staff_action(
                ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id,
                actor_id=ctx.author.id, action="recontext",
                severity=SEVERITY_INFO,
                details=f"target=user:{target.id} db_rows={total_db} short={short_cleared}",
            )

    async def _do_server_recontext(
        self,
        ctx: DiscoContext,
        mem,
        help_cog,
    ) -> None:
        confirmed = await ctx.confirm(
            "**Wipe ALL Disco AI memory for this server?**\n\n"
            "This drops every user's conversation history, memory summary, "
            "traits, reaction counters, and tool memory; clears the recent "
            "server-events drama feed and channel-context banter feed; and "
            "deletes guild-scoped DiscoAI facts and episodes. "
            "Configuration (custom prompts, AI-channel allowlist, model "
            "picks) is left intact.",
        )
        if not confirmed:
            await ctx.reply_error("Cancelled.")
            return

        deleted = await ctx.db.wipe_ai_guild_state(ctx.guild_id)

        short_cleared = 0
        if mem is not None:
            try:
                short_cleared = await mem.clear_guild(ctx.guild_id)
            except Exception as exc:
                log.debug("ai recontext server short-term wipe failed: %s", exc)

        # Reset every cached cooldown -- this is a server-wide event, no
        # individual user should be rate-limited from the side effects.
        if help_cog is not None:
            try:
                help_cog._ai_cooldowns.clear()
            except Exception:
                pass

        total_db = sum(deleted.values())
        c = card(
            "AI Context Rebuilt -- Server",
            description=(
                "Wiped Disco's memory of this guild. Per-user state, drama "
                "feed, channel-context feed, guild-scoped facts and episodes "
                "are all gone. Configuration is untouched. The next AI reply "
                "in this server will start completely fresh."
            ),
            color=C_SUCCESS,
        )
        for label, key in (
            ("Conversation history", "ai_conversations"),
            ("Memory summaries", "ai_user_memory"),
            ("Trait rows", "ai_user_traits"),
            ("Reaction counters", "ai_reaction_memory"),
            ("Tool memory", "ai_tool_memory"),
            ("Server events", "server_events"),
            ("Channel context", "channel_context"),
            ("Per-user facts (guild)", "disco_facts.user_in_guild"),
            ("Guild facts", "disco_facts.guild"),
            ("Per-user episodes (guild)", "disco_episodes.user_in_guild"),
            ("Guild episodes", "disco_episodes.guild"),
        ):
            if deleted.get(key):
                c = c.field(label, str(deleted[key]), True)
        if short_cleared:
            c = c.field("Short-term buffers", str(short_cleared), True)
        await ctx.reply(embed=c.build(), mention_author=False)

        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="recontext",
            severity=SEVERITY_DANGER,
            details=f"target=server db_rows={total_db} short={short_cleared}",
        )

    async def _do_channel_recontext(
        self,
        ctx: DiscoContext,
        mem,
    ) -> None:
        cid = getattr(ctx.channel, "id", None)
        if cid is None:
            await ctx.reply_error("This command needs to run inside a channel.")
            return
        ctx_rows = await ctx.db.wipe_ai_channel_context(ctx.guild_id, cid)
        short_cleared = 0
        if mem is not None:
            try:
                short_cleared = await mem.clear_channel(ctx.guild_id, cid)
            except Exception as exc:
                log.debug("ai recontext channel short-term wipe failed: %s", exc)

        if ctx_rows == 0 and short_cleared == 0:
            await ctx.reply_success(
                "Nothing to clear in this channel.", title="Already Clean",
            )
            return
        c = card(
            "AI Context Rebuilt -- Channel",
            description=(
                f"Cleared the channel-context feed and short-term buffers for "
                f"<#{cid}>. Server-wide drama and per-user state are intact."
            ),
            color=C_SUCCESS,
        )
        if ctx_rows:
            c = c.field("Channel context rows", str(ctx_rows), True)
        if short_cleared:
            c = c.field("Short-term buffers", str(short_cleared), True)
        await ctx.reply(embed=c.build(), mention_author=False)

        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id,
            actor_id=ctx.author.id, action="recontext",
            severity=SEVERITY_INFO,
            details=f"target=channel:{cid} ctx_rows={ctx_rows} short={short_cleared}",
        )

    @ai.command(name="reloadtools")
    @_require_manage_guild()
    async def ai_reloadtools(self, ctx: DiscoContext) -> None:
        """Hot-reload tools.json and Lua plugins without restarting the bot."""
        try:
            from services.ai_agents import TOOLS as _t_before
            from services.ai_agents import reload_tools
            from core.framework.agent_tools.core import ToolRegistry as _Reg

            before_keys = [t.key for t in _t_before]
            reload_tools()
            from services.ai_agents import TOOLS as _t_after
            after_keys = [t.key for t in _t_after]

            lua_before = set(lua_plugins._loaded.keys())
            lua_count = lua_plugins.reload()
            lua_after = set(lua_plugins._loaded.keys())
        except Exception as exc:  # noqa: BLE001
            log.exception("ai.reloadtools failed")
            await ctx.reply_error(f"Reload failed: {exc}")
            return

        added = [k for k in after_keys if k not in before_keys]
        removed = [k for k in before_keys if k not in after_keys]
        lua_new = lua_after - lua_before

        lines = [
            f"Chat expertise topics (tools.json): {len(after_keys)} - "
            f"{', '.join(after_keys) or 'none'}"
        ]
        if added:
            lines.append(f"Added: {', '.join(added)}")
        if removed:
            lines.append(f"Removed: {', '.join(removed)}")
        lines.append(
            f"Lua plugins: {lua_count} callable tool(s) from {len(lua_after)} file(s)"
            + (f" (+{len(lua_new)} new)" if lua_new else "")
        )
        if lua_plugins._loaded:
            lines.append(lua_plugins.plugin_summary())
        lines.append(f"Chat hooks:\n{lua_plugins.hook_summary()}")

        all_specs = _Reg.all()
        reg_by_cat: dict[str, list[str]] = {}
        for _s in all_specs:
            _cat = str(getattr(_s, "category", "misc") or "misc")
            reg_by_cat.setdefault(_cat, []).append(_s.name)
        reg_lines = [f"Agent callable tools: {len(all_specs)} registered (use ,ai tools list to browse)"]
        for _cat, _names in sorted(reg_by_cat.items()):
            reg_lines.append(f"  {_cat}: {', '.join(_names)}")
        lines.extend(reg_lines)

        await ctx.reply_success("\n".join(lines), title="Tools Reloaded")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="reloadtools", severity=SEVERITY_INFO,
            details=f"tools={len(after_keys)} added={len(added)} removed={len(removed)} lua={lua_count}",
        )

    # ------------------------------------------------------------------
    # Plugins group (was a command, now a group with enable/disable)
    # ------------------------------------------------------------------
    @ai.group(name="plugins", invoke_without_command=True)
    @_require_manage_guild()
    async def ai_plugins(self, ctx: DiscoContext) -> None:
        """Inspect loaded Lua plugins or toggle individual plugin files."""
        if ctx.invoked_subcommand is not None:
            return
        if await suggest_subcommand(ctx, self.ai_plugins):
            return

        loaded: dict[str, list[str]] = dict(lua_plugins._loaded)
        hooks = lua_plugins._hooks

        total_tools = sum(len(v) for v in loaded.values())
        total_hooks = sum(len(v) for v in hooks.values())

        b = card("\U0001F9E9 Lua Plugins", color=C_PURPLE)
        b.field("Files loaded", str(len(loaded)), True)
        b.field("Tools registered", str(total_tools), True)
        b.field("Chat hooks", str(total_hooks), True)

        if loaded:
            plugin_lines: list[str] = []
            for stem, names in sorted(loaded.items()):
                enabled = registry_state.is_enabled("plugin", stem, default=True)
                icon = _STATE_ICON[enabled]
                joined = ", ".join(f"{n}" for n in names) if names else "(hooks only)"
                plugin_lines.append(f"{icon} {stem} ({len(names)}): {joined}")
            body = "\n".join(plugin_lines)
            if len(body) > 1024:
                body = body[:1000] + "\n... (truncated)"
            b.field("Plugin files", body, False)
        else:
            b.field(
                "Plugin files",
                "(no Lua plugins loaded - drop .lua files into plugins/ and run "
                f"{ctx.prefix or '.'}ai reloadtools)",
                False,
            )

        hook_body = lua_plugins.hook_summary()
        if len(hook_body) > 1024:
            hook_body = hook_body[:1000] + "\n... (truncated)"
        b.field("Chat hook pipeline", hook_body or "(none)", False)

        b.footer(f"Use {ctx.prefix or '.'}ai plugins enable|disable <stem>")
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_plugins.command(name="enable")
    @_require_manage_guild()
    async def ai_plugins_enable(self, ctx: DiscoContext, *, name: str) -> None:
        """Enable a whole plugin file (and everything it registered)."""
        stem = (name or "").strip()
        if not stem:
            await ctx.reply_error("Usage: ,ai plugins enable <stem>")
            return
        registry_state.set_enabled("plugin", stem, True)
        registry_state.set_enabled("hook", stem, True)
        owned = list(lua_plugins._loaded.get(stem, []))
        for tool_name in owned:
            registry_state.set_enabled("tool", tool_name, True)
        detail = f"+{len(owned)} tool(s)" if owned else "no tools owned"
        await ctx.reply_success(f"Plugin {stem} ENABLED ({detail}).", title="Plugins")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="plugin_enable", severity=SEVERITY_INFO,
            details=f"stem={stem} tools={len(owned)}",
        )

    @ai_plugins.command(name="disable")
    @_require_manage_guild()
    async def ai_plugins_disable(self, ctx: DiscoContext, *, name: str) -> None:
        """Disable a whole plugin file (and everything it registered)."""
        stem = (name or "").strip()
        if not stem:
            await ctx.reply_error("Usage: ,ai plugins disable <stem>")
            return
        registry_state.set_enabled("plugin", stem, False)
        registry_state.set_enabled("hook", stem, False)
        owned = list(lua_plugins._loaded.get(stem, []))
        for tool_name in owned:
            registry_state.set_enabled("tool", tool_name, False)
        detail = f"-{len(owned)} tool(s)" if owned else "no tools owned"
        await ctx.reply_success(f"Plugin {stem} DISABLED ({detail}).", title="Plugins")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="plugin_disable", severity=SEVERITY_WARN,
            details=f"stem={stem} tools={len(owned)}",
        )

    # ------------------------------------------------------------------
    # Hooks inspection / toggle
    # ------------------------------------------------------------------
    @ai.group(name="hooks", invoke_without_command=True)
    @_require_manage_guild()
    async def ai_hooks(self, ctx: DiscoContext) -> None:
        """Chat-pipeline hook inspection + enable/disable."""
        if ctx.invoked_subcommand is not None:
            return
        if await suggest_subcommand(ctx, self.ai_hooks):
            return
        hooks = lua_plugins._hooks
        b = card("\U0001F9E9 Chat Pipeline Hooks", color=C_PURPLE)
        any_hooks = False
        for htype in ("system_prompt", "user_message", "ai_reply"):
            entries = hooks.get(htype, [])
            if not entries:
                continue
            any_hooks = True
            lines: list[str] = []
            for stem, _fn in entries:
                enabled = registry_state.is_enabled("hook", stem, default=True)
                icon = _STATE_ICON[enabled]
                lines.append(f"{icon} {stem}")
            b.field(f"on{htype} ({len(entries)})", "\n".join(lines), False)
        if not any_hooks:
            b.description("(no Lua chat hooks registered)")
        b.footer(f"Toggle with {ctx.prefix or '.'}ai hooks enable|disable <stem>")
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_hooks.command(name="enable")
    @_require_manage_guild()
    async def ai_hooks_enable(self, ctx: DiscoContext, *, name: str) -> None:
        """Enable a hook bundle by plugin stem."""
        stem = (name or "").strip()
        if not stem:
            await ctx.reply_error("Usage: ,ai hooks enable <stem>")
            return
        registry_state.set_enabled("hook", stem, True)
        await ctx.reply_success(f"Hook {stem} ENABLED.", title="Hooks")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="hook_enable", severity=SEVERITY_INFO, details=f"stem={stem}",
        )

    @ai_hooks.command(name="disable")
    @_require_manage_guild()
    async def ai_hooks_disable(self, ctx: DiscoContext, *, name: str) -> None:
        """Disable a hook bundle by plugin stem."""
        stem = (name or "").strip()
        if not stem:
            await ctx.reply_error("Usage: ,ai hooks disable <stem>")
            return
        registry_state.set_enabled("hook", stem, False)
        await ctx.reply_success(f"Hook {stem} DISABLED.", title="Hooks")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="hook_disable", severity=SEVERITY_WARN, details=f"stem={stem}",
        )

    # ------------------------------------------------------------------
    # Agents (disrepo-installed persona bundles)
    # ------------------------------------------------------------------
    @ai.group(name="agents", invoke_without_command=True)
    @_require_manage_guild()
    async def ai_agents(self, ctx: DiscoContext) -> None:
        """List installed persona bundles or toggle them."""
        if ctx.invoked_subcommand is not None:
            return
        if await suggest_subcommand(ctx, self.ai_agents):
            return
        rows = registry_state.installed_items("agent")
        b = card("\U0001F9D1 Installed Agent Personas", color=C_PURPLE)
        if not rows:
            b.description(
                "(no agent personas installed -- use "
                f"{ctx.prefix or '.'}ai install agents/<name>)"
            )
        else:
            lines = []
            for r in rows:
                icon = _STATE_ICON[bool(r["enabled"])]
                meta = r.get("meta") or {}
                ver = meta.get("version") or "?"
                summary = meta.get("summary") or ""
                lines.append(f"{icon} {r['name']} v{ver}  -  {summary}")
            b.description("\n".join(lines))
        b.footer(f"Toggle with {ctx.prefix or '.'}ai agents enable|disable <name>")
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_agents.command(name="enable")
    @_require_manage_guild()
    async def ai_agents_enable(self, ctx: DiscoContext, *, name: str) -> None:
        """Enable an installed agent persona."""
        n = (name or "").strip()
        if not n:
            await ctx.reply_error("Usage: ,ai agents enable <name>")
            return
        registry_state.set_enabled("agent", n, True)
        await ctx.reply_success(f"Agent {n} ENABLED.", title="Agents")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="agent_enable", severity=SEVERITY_INFO, details=f"agent={n}",
        )

    @ai_agents.command(name="disable")
    @_require_manage_guild()
    async def ai_agents_disable(self, ctx: DiscoContext, *, name: str) -> None:
        """Disable an installed agent persona."""
        n = (name or "").strip()
        if not n:
            await ctx.reply_error("Usage: ,ai agents disable <name>")
            return
        registry_state.set_enabled("agent", n, False)
        await ctx.reply_success(f"Agent {n} DISABLED.", title="Agents")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="agent_disable", severity=SEVERITY_WARN, details=f"agent={n}",
        )

    # ------------------------------------------------------------------
    # Disrepo installer commands
    # ------------------------------------------------------------------
    @ai.command(name="install")
    @_require_manage_guild()
    async def ai_install(self, ctx: DiscoContext, *, ref: str) -> None:
        """Install a disrepo item. Usage: ,ai install tools/sample_price_check"""
        ref = (ref or "").strip()
        if not ref:
            await ctx.reply_error("Usage: ,ai install <type>/<name>")
            return
        try:
            item = await asyncio.to_thread(disrepo.install_item, ref)
        except disrepo.DisrepoError as exc:
            await ctx.reply_error(f"Install failed: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("ai.install failed")
            await ctx.reply_error(f"Install failed: {exc}")
            return

        reloaded = 0
        if item.type in ("tool", "plugin", "hook"):
            try:
                reloaded = lua_plugins.reload()
            except Exception as exc:  # noqa: BLE001
                log.warning("lua reload after install failed: %s", exc)

        b = card(f"\U0001F4E5 Installed {item.type}/{item.name}", color=C_SUCCESS)
        b.field("Version", str(item.manifest.get("version") or "?"), True)
        b.field("Author", str(item.manifest.get("author") or "?"), True)
        b.field("Summary", str(item.manifest.get("summary") or "(none)"), False)
        b.field("Path", f"{item.installed_path}", False)
        if item.tool_names:
            tool_lines = "\n".join(
                f"{n}  -  DISABLED (use ,ai tools enable {n})" for n in item.tool_names
            )
            b.field("Registers tools", tool_lines, False)
        b.field(
            "Status",
            "DISABLED by default. Activate with "
            f",ai tools enable <tool_name> or "
            f",ai {item.type}s enable {item.name}.",
            False,
        )
        b.footer(
            f"{reloaded} Lua tool(s) currently loaded"
            if reloaded else "Restart bot if anything looks off"
        )
        await ctx.reply(embed=b.build(), mention_author=False)
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="install", severity=SEVERITY_INFO,
            details=f"{item.type}/{item.name} v{item.manifest.get('version', '?')}",
        )

    @ai.command(name="uninstall", aliases=["remove"])
    @_require_manage_guild()
    async def ai_uninstall(self, ctx: DiscoContext, *, ref: str) -> None:
        """Uninstall a disrepo item. Usage: ,ai uninstall tools/sample_price_check"""
        ref = (ref or "").strip()
        if not ref:
            await ctx.reply_error("Usage: ,ai uninstall <type>/<name>")
            return
        try:
            parsed = await asyncio.to_thread(disrepo.uninstall_item, ref)
        except disrepo.DisrepoError as exc:
            await ctx.reply_error(f"Uninstall failed: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("ai.uninstall failed")
            await ctx.reply_error(f"Uninstall failed: {exc}")
            return

        await ctx.reply_success(
            f"Removed {parsed.type}/{parsed.name}. Restart the bot to fully "
            "unload its Python-side entries.",
            title="\U0001F4E4 Uninstalled",
        )
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="uninstall", severity=SEVERITY_WARN,
            details=f"{parsed.type}/{parsed.name}",
        )

    @ai.command(name="search")
    @_require_manage_guild()
    async def ai_search(self, ctx: DiscoContext, *, query: str = "") -> None:
        """Browse the disrepo catalog. Usage: ,ai search [query]"""
        try:
            results = await asyncio.to_thread(disrepo.search_disrepo, query)
        except disrepo.DisrepoError as exc:
            await ctx.reply_error(f"Search failed: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("ai.search failed")
            await ctx.reply_error(f"Search failed: {exc}")
            return
        if not results:
            await ctx.reply_error(f"No disrepo entries match {query!r}.")
            return

        grouped: dict[str, list[dict]] = {}
        for row in results:
            grouped.setdefault(row["type"], []).append(row)

        categories: dict[str, list[discord.Embed]] = {}
        for type_key, items in sorted(grouped.items()):
            lines: list[str] = []
            for r in items:
                if r["enabled"]:
                    icon = "\U0001F7E2"
                elif r["installed"]:
                    icon = "\U0001F4BE"
                else:
                    icon = "\u2B1C"
                tag_text = f" [{', '.join(r['tags'])}]" if r["tags"] else ""
                lines.append(
                    f"{icon} {r['name']} v{r['version']}{tag_text}\n"
                    f"\u00A0\u00A0\u00A0{r['summary']}"
                )
            b = card(f"\U0001F50D Disrepo - {type_key}s", color=C_INFO)
            b.description("\n\n".join(lines) if lines else "(empty)")
            b.footer(
                "\u2B1C remote  \u00B7  \U0001F4BE installed disabled  "
                "\u00B7  \U0001F7E2 enabled"
            )
            categories[f"\U0001F4E6 {type_key}s"] = [b.build()]

        if len(categories) > 1:
            await CategoryPaginator.send(ctx, categories)
        else:
            only = next(iter(categories.values()))[0]
            await ctx.reply(embed=only, mention_author=False)

    @ai.command(name="installed")
    @_require_manage_guild()
    async def ai_installed(self, ctx: DiscoContext) -> None:
        """Show every disrepo-installed item + its enabled state."""
        rows = registry_state.installed_items()
        if not rows:
            await ctx.reply_error("No disrepo items installed.")
            return
        grouped: dict[str, list[dict]] = {}
        for r in rows:
            grouped.setdefault(r["type"], []).append(r)
        b = card("\U0001F4E6 Installed Disrepo Items", color=C_PURPLE)
        for type_key in sorted(grouped.keys()):
            items = grouped[type_key]
            lines: list[str] = []
            for it in items:
                state = _STATE_ICON[bool(it["enabled"])]
                meta = it.get("meta") or {}
                ver = meta.get("version") or "?"
                lines.append(f"{state}  {it['name']} v{ver}")
            b.field(f"{type_key}s ({len(items)})", "\n".join(lines), False)
        b.footer(f"Use {ctx.prefix or '.'}ai <group> enable|disable <name> to toggle")
        await ctx.reply(embed=b.build(), mention_author=False)

    # ------------------------------------------------------------------
    # Heal sub-group
    # ------------------------------------------------------------------
    @ai.group(name="heal", invoke_without_command=True)
    @_require_manage_guild()
    async def ai_heal(self, ctx: DiscoContext) -> None:
        """Heal AI provider config for .health analyze."""
        if await suggest_subcommand(ctx, self.ai_heal):
            return
        await self.ai_heal_status(ctx)

    @ai_heal.command(name="status")
    @_require_manage_guild()
    async def ai_heal_status(self, ctx: DiscoContext) -> None:
        """Show heal AI provider config."""
        cfg = await ctx.db.get_heal_ai_config(ctx.guild_id) or {}
        b = card("\U0001FA7A Heal AI Provider", color=C_PURPLE)
        b.field("Backend", str(cfg.get("heal_ai_backend") or "default"), True)
        b.field("Model", str(cfg.get("heal_ai_model") or "default"), True)
        b.field("Base URL", str(cfg.get("heal_ai_base_url") or "default"), False)
        b.footer("Used by .health analyze")
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_heal.command(name="backend")
    @_require_manage_guild()
    async def ai_heal_backend(self, ctx: DiscoContext, backend: str) -> None:
        """Set heal AI backend (openrouter|ollama)."""
        val = (backend or "").strip().lower()
        if val not in _HEAL_BACKENDS:
            await ctx.reply_error(f"Unknown backend {backend!r}. Valid: {', '.join(_HEAL_BACKENDS)}")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "heal_ai_backend", val)
        await ctx.reply_success(f"Heal AI backend set to {val}.", title="Heal AI")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="heal_backend", severity=SEVERITY_INFO, details=f"backend={val}",
        )

    @ai_heal.command(name="model")
    @_require_manage_guild()
    async def ai_heal_model(self, ctx: DiscoContext, *, model: str) -> None:
        """Set heal AI model string."""
        val = (model or "").strip()
        if not val:
            await ctx.reply_error("Model name cannot be empty.")
            return
        await ctx.db.update_guild_setting(ctx.guild_id, "heal_ai_model", val)
        await ctx.reply_success(f"Heal AI model set to {val}.", title="Heal AI")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="heal_model", severity=SEVERITY_INFO, details=f"model={val}",
        )

    @ai_heal.command(name="baseurl")
    @_require_manage_guild()
    async def ai_heal_baseurl(self, ctx: DiscoContext, *, url: str) -> None:
        """Set heal AI base URL, or 'reset' to clear."""
        val = (url or "").strip()
        if not val or val.lower() == "reset":
            await ctx.db.update_guild_setting(ctx.guild_id, "heal_ai_base_url", None)
            await ctx.reply_success("Heal AI base URL reset to default.", title="Heal AI")
            details = "reset"
        else:
            await ctx.db.update_guild_setting(ctx.guild_id, "heal_ai_base_url", val)
            await ctx.reply_success(f"Heal AI base URL set to {val}.", title="Heal AI")
            details = f"url={val}"
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="heal_baseurl", severity=SEVERITY_INFO, details=details,
        )

    @ai_heal.command(name="reset")
    @_require_manage_guild()
    async def ai_heal_reset(self, ctx: DiscoContext) -> None:
        """Wipe all heal AI overrides."""
        for col in ("heal_ai_backend", "heal_ai_model", "heal_ai_base_url"):
            await ctx.db.update_guild_setting(ctx.guild_id, col, None)
        await ctx.reply_success("All heal AI overrides cleared.", title="Heal AI")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="heal_reset", severity=SEVERITY_WARN, details="all cleared",
        )

    # ------------------------------------------------------------------
    # Agent loop backend
    # ------------------------------------------------------------------
    @ai.group(name="loop", invoke_without_command=True)
    @_require_manage_guild()
    async def ai_loop(self, ctx: DiscoContext) -> None:
        """Agent loop backend config. Subcommands: status, backend, reset."""
        if await suggest_subcommand(ctx, self.ai_loop):
            return
        await self.ai_loop_status(ctx)

    @ai_loop.command(name="status")
    @_require_manage_guild()
    async def ai_loop_status(self, ctx: DiscoContext) -> None:
        """Show current agent loop backend and effective model."""
        row = await ctx.db.fetch_one(
            "SELECT tools_backend FROM guild_settings WHERE guild_id=$1",
            ctx.guild_id,
        )
        guild_backend = (row or {}).get("tools_backend")
        effective = guild_backend or Config.TOOLS_BACKEND or "openrouter"
        model = (
            Config.TOOLS_MODEL if effective == "ollama" else Config.OPENROUTER_MODEL
        ) or "env default"
        b = card("\U0001F501 Agent Loop Backend", color=C_PURPLE)
        b.field("Guild override", guild_backend or "not set", True)
        b.field("Env default", Config.TOOLS_BACKEND or "openrouter", True)
        b.field("Effective backend", effective, False)
        b.field("Env model", model, True)
        b.field(
            "OLLAMA_BASE_URL",
            "set" if os.getenv("OLLAMA_BASE_URL") else "not set",
            True,
        )
        b.footer("Per-guild model (,ai model set tools) overrides this backend")
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_loop.command(name="backend")
    @_require_manage_guild()
    async def ai_loop_backend(self, ctx: DiscoContext, backend: str) -> None:
        """Set agent loop backend: openrouter or ollama."""
        val = (backend or "").strip().lower()
        if val not in _LOOP_BACKENDS:
            await ctx.reply_error(
                f"Unknown backend {backend!r}. Valid: {', '.join(_LOOP_BACKENDS)}"
            )
            return
        hint = ""
        if val == "ollama" and not os.getenv("OLLAMA_BASE_URL"):
            hint = "\nOLLAMA_BASE_URL is not set - add it in Railway env vars."
        await ctx.db.update_guild_setting(ctx.guild_id, "tools_backend", val)
        await ctx.reply_success(
            f"Agent loop backend set to **{val}**.{hint}", title="Agent Loop"
        )
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="loop_backend", severity=SEVERITY_INFO, details=f"backend={val}",
        )

    @ai_loop.command(name="reset")
    @_require_manage_guild()
    async def ai_loop_reset(self, ctx: DiscoContext) -> None:
        """Revert agent loop backend to the TOOLS_BACKEND env default."""
        await ctx.db.update_guild_setting(ctx.guild_id, "tools_backend", None)
        await ctx.reply_success(
            f"Agent loop backend reset to env default ({Config.TOOLS_BACKEND or 'openrouter'}).",
            title="Agent Loop",
        )
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="loop_backend_reset", severity=SEVERITY_INFO, details="reset",
        )

    # ------------------------------------------------------------------
    # Web search backend
    # ------------------------------------------------------------------
    @ai.group(name="websearch", invoke_without_command=True)
    @_require_manage_guild()
    async def ai_websearch(self, ctx: DiscoContext) -> None:
        """Web search backend config. Subcommands: status, backend, reset."""
        if await suggest_subcommand(ctx, self.ai_websearch):
            return
        await self.ai_websearch_status(ctx)

    @ai_websearch.command(name="status")
    @_require_manage_guild()
    async def ai_websearch_status(self, ctx: DiscoContext) -> None:
        """Show current search backend config."""
        row = await ctx.db.fetch_one(
            "SELECT search_backend FROM guild_settings WHERE guild_id=$1",
            ctx.guild_id,
        )
        guild_backend = (row or {}).get("search_backend")
        effective = guild_backend or Config.SEARCH_BACKEND or "ddg"
        b = card("\U0001F50D Web Search Backend", color=C_PURPLE)
        b.field("Guild override", guild_backend or "not set", True)
        b.field("Env default", Config.SEARCH_BACKEND or "ddg", True)
        b.field("Effective backend", effective, False)
        b.field(
            "BRAVE_SEARCH_API_KEY",
            "set" if Config.BRAVE_SEARCH_API_KEY else "not set",
            True,
        )
        b.field(
            "PERPLEXITY_API_KEY",
            "set" if Config.PERPLEXITY_API_KEY else "not set",
            True,
        )
        b.field(
            "OLLAMA_BASE_URL",
            "set" if os.getenv("OLLAMA_BASE_URL") else "not set",
            True,
        )
        b.footer("API keys must be set as Railway env vars")
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_websearch.command(name="backend")
    @_require_manage_guild()
    async def ai_websearch_backend(self, ctx: DiscoContext, backend: str) -> None:
        """Set guild search backend: ddg, brave, openrouter, perplexity, or ollama."""
        val = (backend or "").strip().lower()
        if val not in _SEARCH_BACKENDS:
            await ctx.reply_error(
                f"Unknown backend {backend!r}. Valid: {', '.join(_SEARCH_BACKENDS)}"
            )
            return
        hint = ""
        if val == "brave" and not Config.BRAVE_SEARCH_API_KEY:
            hint = "\nBRAVE_SEARCH_API_KEY is not set - add it in Railway env vars."
        elif val == "perplexity" and not Config.PERPLEXITY_API_KEY:
            hint = "\nPERPLEXITY_API_KEY is not set - add it in Railway env vars."
        elif val == "ollama" and not os.getenv("OLLAMA_BASE_URL"):
            hint = "\nOLLAMA_BASE_URL is not set - add it in Railway env vars."
        await ctx.db.update_guild_setting(ctx.guild_id, "search_backend", val)
        await ctx.reply_success(
            f"Search backend set to **{val}**.{hint}", title="Web Search"
        )
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="search_backend", severity=SEVERITY_INFO, details=f"backend={val}",
        )

    @ai_websearch.command(name="reset")
    @_require_manage_guild()
    async def ai_websearch_reset(self, ctx: DiscoContext) -> None:
        """Revert search backend to the SEARCH_BACKEND env default."""
        await ctx.db.update_guild_setting(ctx.guild_id, "search_backend", None)
        await ctx.reply_success(
            f"Search backend reset to env default ({Config.SEARCH_BACKEND or 'ddg'}).",
            title="Web Search",
        )
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="search_backend_reset", severity=SEVERITY_INFO, details="reset",
        )

    # ------------------------------------------------------------------
    # Model picker
    # ------------------------------------------------------------------
    @ai.group(name="model", invoke_without_command=True)
    @_require_manage_guild()
    async def ai_model(self, ctx: DiscoContext) -> None:
        """Per-guild AI model defaults. Subcommands: list, show, set, reset."""
        if await suggest_subcommand(ctx, self.ai_model):
            return
        await self.ai_model_list(ctx)

    @ai_model.command(name="list")
    @_require_manage_guild()
    async def ai_model_list(self, ctx: DiscoContext) -> None:
        """Show per-category model defaults."""
        picks = await list_guild_defaults(ctx.db, ctx.guild_id)
        b = card("\U0001F9E0 AI Model Defaults", color=C_PURPLE)
        for cat in TOOL_CATEGORIES:
            pick = picks.get(cat.key) if isinstance(picks, dict) else None
            if pick and getattr(pick, "provider", None) and getattr(pick, "model", None):
                current = f"{pick.provider}:{pick.model}"
            else:
                current = "env default"
            catalog = catalog_for(cat.key) or []
            suggested = catalog[0].label if catalog else "(no catalog)"
            value = f"current: {current}\nsuggested: {suggested}"
            b.field(str(cat.label), value, True)
        b.footer("Use ,ai model set <category> provider:model|index to override.")
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_model.command(name="show")
    @_require_manage_guild()
    async def ai_model_show(self, ctx: DiscoContext, category: str) -> None:
        """Show curated catalog for one category."""
        cat_key = (category or "").strip().lower()
        cat = next((c for c in TOOL_CATEGORIES if c.key == cat_key), None)
        if cat is None:
            valid = ", ".join(c.key for c in TOOL_CATEGORIES)
            await ctx.reply_error(f"Unknown category {category!r}. Valid: {valid}")
            return
        catalog = catalog_for(cat.key) or []
        picks = await list_guild_defaults(ctx.db, ctx.guild_id)
        current = picks.get(cat.key) if isinstance(picks, dict) else None
        cur_provider = getattr(current, "provider", None)
        cur_model = getattr(current, "model", None)

        # Env wins over Discord pick. The active model is what the env var
        # holds when set; only when the env is empty does the guild pick
        # take effect. Surface this clearly so admins can see why their
        # ,ai model set choice may not be in use.
        provider_attr = cat.default_env[1]
        env_value = str(getattr(Config, provider_attr, "") or "")
        env_provider = cat.default_env[0]
        if env_value:
            active_provider, active_model = env_provider, env_value
            active_source = f"env `{provider_attr}`"
        elif cur_model:
            active_provider, active_model = cur_provider or "", cur_model
            active_source = "Discord ,ai model set"
        else:
            active_provider, active_model = env_provider, "(unset)"
            active_source = "no default configured"

        lines: list[str] = []
        if not catalog:
            lines.append("(no curated entries for this category)")
        for i, entry in enumerate(catalog, start=1):
            prov = getattr(entry, "provider", "")
            mdl = getattr(entry, "model", "")
            label = getattr(entry, "label", f"{prov}:{mdl}")
            marker = " \u2705" if (prov == active_provider and mdl == active_model) else ""
            lines.append(f"{i}. {label}  -  {prov}:{mdl}{marker}")
        b = card(f"\U0001F9E0 Catalog - {cat.label}", color=C_PURPLE)
        b.description("\n".join(lines))
        b.field(
            "Active",
            f"`{active_provider}:{active_model}`\nsource: {active_source}",
            False,
        )
        if env_value and cur_model and (cur_provider != env_provider or cur_model != env_value):
            b.field(
                "Discord pick (overridden)",
                f"`{cur_provider}:{cur_model}` -- env wins. Clear the env var "
                "to let this take effect.",
                False,
            )
        b.footer(f"Use ,ai model set {cat.key} <index|provider:model>")
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_model.command(name="set")
    @_require_manage_guild()
    async def ai_model_set(self, ctx: DiscoContext, category: str, *, value: str) -> None:
        """Set a per-guild model default for a category."""
        cat_key = (category or "").strip().lower()
        cat = next((c for c in TOOL_CATEGORIES if c.key == cat_key), None)
        if cat is None:
            valid = ", ".join(c.key for c in TOOL_CATEGORIES)
            await ctx.reply_error(f"Unknown category {category!r}. Valid: {valid}")
            return
        raw = (value or "").strip()
        if not raw:
            await ctx.reply_error("Value required: provide provider:model or catalog index.")
            return
        provider: str | None = None
        model: str | None = None
        if raw.isdigit():
            idx = int(raw)
            catalog = catalog_for(cat.key) or []
            if idx < 1 or idx > len(catalog):
                await ctx.reply_error(f"Index out of range (1-{len(catalog)}).")
                return
            entry = catalog[idx - 1]
            provider = getattr(entry, "provider", None)
            model = getattr(entry, "model", None)
        else:
            if ":" not in raw:
                await ctx.reply_error("Format must be provider:model (e.g. openrouter:gpt-4o-mini).")
                return
            provider, _, model = raw.partition(":")
            provider = provider.strip().lower()
            model = model.strip()
        if provider not in ("openrouter", "ollama"):
            await ctx.reply_error(f"Unknown provider {provider!r}. Valid: openrouter, ollama")
            return
        if not model:
            await ctx.reply_error("Model name cannot be empty.")
            return
        await set_guild_default(
            ctx.db, ctx.guild_id, cat.key, provider, model, updated_by=ctx.author.id,
        )

        # Build any non-fatal advisory notes the operator should see. Set
        # operations always succeed; the notes warn about likely-broken picks
        # (text-only model for vision) and clarify when env-wins means the
        # pick won't actually take effect at runtime.
        notes: list[str] = []
        if cat.key == "vision" and not is_vision_capable_slug(model):
            notes.append(
                f"Heads up: `{provider}:{model}` is not on the known-multimodal list, "
                "so the vision tool will skip it and fall through to the env fallback "
                "chain. Pick a known vision slug (gpt-4o, claude-3, gemini, llava, "
                "pixtral, qwen-vl, gemma3:, etc.) to actually use it."
            )
        provider_attr = cat.default_env[1]
        env_value = str(getattr(Config, provider_attr, "") or "")
        if env_value:
            notes.append(
                f"Note: env var `{provider_attr}={env_value}` outranks Discord picks. "
                f"Your `{provider}:{model}` will only take effect if the operator "
                "clears the env var."
            )

        title = "Model Picker"
        body = f"{cat.key} default set to {provider}:{model}."
        if notes:
            body = body + "\n\n" + "\n\n".join(notes)
        await ctx.reply_success(body, title=title)
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="model_set", severity=SEVERITY_INFO, details=f"{cat.key}={provider}:{model}",
        )

    @ai_model.command(name="reset")
    @_require_manage_guild()
    async def ai_model_reset(self, ctx: DiscoContext, category: str) -> None:
        """Revert a category to env default."""
        cat_key = (category or "").strip().lower()
        cat = next((c for c in TOOL_CATEGORIES if c.key == cat_key), None)
        if cat is None:
            valid = ", ".join(c.key for c in TOOL_CATEGORIES)
            await ctx.reply_error(f"Unknown category {category!r}. Valid: {valid}")
            return
        await clear_guild_default(ctx.db, ctx.guild_id, cat.key)
        await ctx.reply_success(f"{cat.key} reverted to env default.", title="Model Picker")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="model_reset", severity=SEVERITY_INFO, details=f"category={cat.key}",
        )

    # ------------------------------------------------------------------
    # Tools inspection + enable/disable
    # ------------------------------------------------------------------
    @ai.group(name="tools", invoke_without_command=True)
    @_require_manage_guild()
    async def ai_tools(self, ctx: DiscoContext) -> None:
        """Registered agent tool catalog."""
        if await suggest_subcommand(ctx, self.ai_tools):
            return
        await self.ai_tools_list(ctx)

    @ai_tools.command(name="list")
    @_require_manage_guild()
    async def ai_tools_list(self, ctx: DiscoContext, category: str | None = None) -> None:
        """List registered agent tools, optionally filtered by category."""
        if category:
            specs = list(ToolRegistry.by_category(category))
        else:
            specs = list(ToolRegistry.all())

        # Also surface tools that are installed (from disrepo) but not yet
        # loaded into ToolRegistry -- e.g. lupa is absent or file failed to run.
        loaded_names = {s.name for s in specs}
        unloaded_installed: list[dict] = []
        if not category:
            unloaded_installed = [
                r for r in registry_state.installed_items("tool")
                if r["name"] not in loaded_names
            ]

        if not specs and not unloaded_installed:
            await ctx.reply_error("No tools registered for that filter.")
            return

        grouped: dict[str, list] = {}
        for spec in specs:
            cat = str(getattr(spec, "category", "misc") or "misc")
            grouped.setdefault(cat, []).append(spec)
        categorized: dict[str, discord.Embed] = {}
        for cat, items in sorted(grouped.items()):
            lines: list[str] = []
            for s in items:
                risk_val = getattr(getattr(s, "risk", None), "value", "?")
                icon = _RISK_ICON.get(risk_val, "?")
                summary = str(getattr(s, "summary", "") or "")[:80]
                enabled = registry_state.is_enabled("tool", s.name, default=True)
                state = _STATE_ICON[enabled]
                lines.append(f"{state}{icon} {s.name}  -  {summary}")
            b = card(f"\U0001F6E0 Tools - {cat}", color=C_INFO)
            b.description("\n".join(lines) if lines else "(empty)")
            categorized[cat] = b.build()

        if unloaded_installed:
            lines = []
            for r in unloaded_installed:
                meta = r.get("meta") or {}
                summary = str(meta.get("summary") or "(no description)")[:80]
                state = _STATE_ICON[r["enabled"]]
                lines.append(f"{state}\U0001F4BE {r['name']}  -  {summary} (not loaded)")
            b = card("\U0001F6E0 Tools - installed (not loaded)", color=C_WARNING)
            b.description("\n".join(lines))
            b.footer("Installed from disrepo but not active. Check lupa is installed, then ,ai reloadtools.")
            categorized["_unloaded"] = b.build()

        if len(categorized) > 1:
            await CategoryPaginator.send(
                ctx, {label: [embed] for label, embed in categorized.items()},
            )
        else:
            only_embed = next(iter(categorized.values()))
            await ctx.reply(embed=only_embed, mention_author=False)

    @ai_tools.command(name="info")
    @_require_manage_guild()
    async def ai_tools_info(self, ctx: DiscoContext, *, name: str) -> None:
        """Show full schema for one registered tool."""
        tool_name = (name or "").strip()
        spec = ToolRegistry.get(tool_name)
        if spec is None:
            # Tool may be installed from disrepo but not yet loaded into ToolRegistry
            # (e.g. lupa is missing or the Lua file failed to execute).
            installed_row = next(
                (r for r in registry_state.installed_items("tool") if r["name"] == tool_name),
                None,
            )
            if installed_row is None:
                await ctx.reply_error(f"Tool {name!r} not found.")
                return
            meta = installed_row.get("meta") or {}
            b = card(f"\U0001F6E0 Tool - {tool_name} (not loaded)", color=C_WARNING)
            b.field("Name", tool_name, True)
            b.field("Status", "\U0001F534 installed, not loaded", True)
            b.field("Enabled", "\U0001F7E2 yes" if installed_row["enabled"] else "\U0001F534 no", True)
            if meta.get("summary"):
                b.field("Summary", str(meta["summary"])[:1024], False)
            if meta.get("version"):
                b.field("Version", str(meta["version"]), True)
            if meta.get("author"):
                b.field("Author", str(meta["author"]), True)
            b.footer("Installed but not active. Run ,ai reloadtools after confirming lupa is installed.")
            await ctx.reply(embed=b.build(), mention_author=False)
            return
        risk_val = getattr(getattr(spec, "risk", None), "value", "?")
        icon = _RISK_ICON.get(risk_val, "?")
        b = card(f"\U0001F6E0 Tool - {spec.name}", color=C_INFO)
        b.field("Name", f"{spec.name}", True)
        b.field("Category", str(getattr(spec, "category", "misc")), True)
        b.field("Risk", f"{icon} {risk_val}", True)
        enabled = registry_state.is_enabled("tool", spec.name, default=True)
        b.field("Status", f"{_STATE_ICON[enabled]} {'enabled' if enabled else 'disabled'}", True)
        b.field("Summary", str(getattr(spec, "summary", "") or "(none)"), False)
        params = list(getattr(spec, "params", []) or [])
        if params:
            param_lines: list[str] = []
            for p in params:
                p_name = getattr(p, "name", "?")
                p_type = getattr(p, "type", "?")
                p_req = bool(getattr(p, "required", False))
                p_default = getattr(p, "default", None)
                p_desc = str(getattr(p, "description", "") or "")
                suffix = " required" if p_req else f" default={p_default}"
                param_lines.append(f"{p_name} ({p_type}){suffix}\n{p_desc}")
            joined = "\n\n".join(param_lines)
            if len(joined) > 1024:
                joined = joined[:1000] + "\n... (truncated)"
            b.field("Params", joined, False)
        else:
            b.field("Params", "(none)", False)
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_tools.command(name="enable")
    @_require_manage_guild()
    async def ai_tools_enable(self, ctx: DiscoContext, *, name: str) -> None:
        """Enable a tool (built-in or installed)."""
        tool_name = (name or "").strip()
        if not tool_name:
            await ctx.reply_error("Usage: ,ai tools enable <tool_name>")
            return
        has_live_spec = ToolRegistry.get(tool_name) is not None
        is_installed = any(r["name"] == tool_name for r in registry_state.installed_items("tool"))
        if not has_live_spec and not is_installed:
            await ctx.reply_error(
                f"Unknown tool `{tool_name}` -- not installed and not a built-in tool.\n"
                "Use `,ai install tools/<name>` to install from disrepo, "
                "or `,ai tools list` to see available tools."
            )
            return
        registry_state.set_enabled("tool", tool_name, True)
        source = "live" if has_live_spec else "installed (active after reload)"
        await ctx.reply_success(f"Tool `{tool_name}` ENABLED ({source}).", title="Tools")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="tool_enable", severity=SEVERITY_INFO, details=f"tool={tool_name}",
        )

    @ai_tools.command(name="disable")
    @_require_manage_guild()
    async def ai_tools_disable(self, ctx: DiscoContext, *, name: str) -> None:
        """Disable a tool (built-in or installed)."""
        tool_name = (name or "").strip()
        if not tool_name:
            await ctx.reply_error("Usage: ,ai tools disable <tool_name>")
            return
        has_live_spec = ToolRegistry.get(tool_name) is not None
        is_installed = any(r["name"] == tool_name for r in registry_state.installed_items("tool"))
        if not has_live_spec and not is_installed:
            await ctx.reply_error(
                f"Unknown tool `{tool_name}` -- not installed and not a built-in tool.\n"
                "Use `,ai tools list` to see available tools."
            )
            return
        registry_state.set_enabled("tool", tool_name, False)
        await ctx.reply_success(f"Tool `{tool_name}` DISABLED.", title="Tools")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="tool_disable", severity=SEVERITY_WARN, details=f"tool={tool_name}",
        )

    # ------------------------------------------------------------------
    # Custom emoji meaning index
    # ------------------------------------------------------------------
    #
    # The chat AI previously had only a static substring hint to go on when
    # deciding what a custom server emoji "means". That gave shallow,
    # generic explanations. This group manages the guild_emoji_meanings
    # index (migration 0107 + core/framework/emoji_index.py) which combines a
    # vision pass on each emoji image with recent in-channel usage snippets
    # to produce a nuanced per-emoji description that the system prompt
    # surfaces to the model.

    # ------------------------------------------------------------------
    # Doctor  -  live probes + auto-failover for AI backends
    # ------------------------------------------------------------------
    #
    # ``,ai doctor`` pings every configured AI backend (OpenRouter, Ollama,
    # DuckDuckGo, Perplexity) with a real minimal request, then flips any
    # per-guild category (tools / vision / websearch / heal_ai) whose current
    # backend failed to the healthiest alternative whose probe succeeded.
    # Everything is rendered live in an animated embed so the operator can
    # watch each probe complete and each repair land in real time.
    #
    # ``,ai doctor dryrun`` only probes, does not mutate config. Useful for
    # verifying backend health without committing a failover.
    #
    # ``,ai doctor test`` injects a deliberately-broken backend (by pointing
    # websearch at a provider whose API key is missing), runs the doctor,
    # and reports whether the repair landed. A quick end-to-end sanity test
    # that the whole probe -> repair pipeline is wired up.

    _DOCTOR_SPINNER_FRAMES = ("◐", "◓", "◑", "◒")
    _DOCTOR_EDIT_THROTTLE = 1.0
    _DOCTOR_BACKENDS = (
        "openrouter", "ollama", "openrouter_vision", "ollama_vision",
        "ddg", "brave", "perplexity",
    )
    _DOCTOR_CATEGORIES = ("tools", "vision", "websearch", "heal_ai")

    @ai.group(name="doctor", aliases=["dr"], invoke_without_command=True)
    @_require_manage_guild()
    async def ai_doctor(self, ctx: DiscoContext) -> None:
        """Probe every AI backend live and auto-flip unhealthy ones to a healthy alternative."""
        if await suggest_subcommand(ctx, self.ai_doctor):
            return
        await self._run_doctor_live(ctx, dry_run=False)

    @ai_doctor.command(name="dryrun")
    @_require_manage_guild()
    async def ai_doctor_dryrun(self, ctx: DiscoContext) -> None:
        """Run the doctor but do not mutate config -- probe only."""
        await self._run_doctor_live(ctx, dry_run=True)

    @ai_doctor.command(name="test")
    @_require_manage_guild()
    async def ai_doctor_test(self, ctx: DiscoContext) -> None:
        """End-to-end sanity check: inject a broken backend, run doctor, verify repair."""
        # Pick a backend that will definitely probe unhealthy. Prefer
        # perplexity (no key), then brave (no key), then ollama (no URL).
        # If all three are configured we can't fabricate a clean failure
        # without breaking a working setup, so we bail out.
        if not Config.PERPLEXITY_API_KEY:
            broken = "perplexity"
            why = "PERPLEXITY_API_KEY is not set, so the probe will fail deterministically"
        elif not Config.BRAVE_SEARCH_API_KEY:
            broken = "brave"
            why = "BRAVE_SEARCH_API_KEY is not set, so the probe will fail deterministically"
        elif not os.getenv("OLLAMA_BASE_URL"):
            broken = "ollama"
            why = "OLLAMA_BASE_URL is not set, so the probe will fail deterministically"
        else:
            await ctx.reply_error(
                "Can't run the doctor test: all backends appear configured, so there's "
                "no way to fabricate a deterministic failure. Unset PERPLEXITY_API_KEY, "
                "BRAVE_SEARCH_API_KEY, or OLLAMA_BASE_URL in Railway to run this test."
            )
            return

        # Save the pre-test websearch backend so the test doesn't leave the
        # guild pinned to an unintended setting.
        before_row = await ctx.db.fetch_one(
            "SELECT search_backend FROM guild_settings WHERE guild_id=$1", ctx.guild_id,
        )
        pre_test_backend = (before_row or {}).get("search_backend")

        await ctx.db.update_guild_setting(ctx.guild_id, "search_backend", broken)

        injected = card("\U0001F9EA Doctor Test  -  step 1/2", color=C_WARNING)
        injected.description(
            f"Injected broken backend: **websearch -> {broken}**\n"
            f"_{why}_\n\nRunning doctor in 3 seconds..."
        )
        injected.footer("Step 2 will probe every backend and flip websearch back to a healthy one.")
        await ctx.reply(embed=injected.build(), mention_author=False)
        await asyncio.sleep(3)

        summary = await self._run_doctor_live(ctx, dry_run=False)

        # Verify: websearch should have been flipped off the broken backend.
        after_row = await ctx.db.fetch_one(
            "SELECT search_backend FROM guild_settings WHERE guild_id=$1", ctx.guild_id,
        )
        after_backend = (after_row or {}).get("search_backend")
        repaired = after_backend is not None and after_backend != broken
        repair_entry = next(
            (r for r in (summary or {}).get("repairs_performed", [])
             if r.get("category") == "websearch"),
            None,
        )

        verdict = card(
            "\U0001F9EA Doctor Test  -  result",
            color=C_SUCCESS if repaired else C_ERROR,
        )
        verdict.field("Injected", f"websearch -> {broken}", True)
        verdict.field("After repair", f"websearch -> {after_backend or '(unset)'}", True)
        verdict.field(
            "Pre-test value",
            str(pre_test_backend) if pre_test_backend else "(env default)",
            True,
        )
        if repair_entry:
            verdict.field(
                "Doctor reported",
                f"`{repair_entry['from']}` -> `{repair_entry['to']}`",
                False,
            )
        if repaired:
            verdict.description(
                "✅ **PASSED** - the doctor detected the broken backend and "
                "failed over to a healthy alternative."
            )
        else:
            verdict.description(
                "❌ **FAILED** - websearch is still pinned to the broken backend "
                "after the doctor ran. Check the logs."
            )
        verdict.footer(
            "The websearch backend is left at the repaired value. "
            f"Use ,ai websearch reset or ,ai websearch backend {pre_test_backend or 'ddg'} to revert."
        )
        await ctx.reply(embed=verdict.build(), mention_author=False)
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="doctor_test", severity=SEVERITY_INFO,
            details=f"injected={broken} after={after_backend} passed={repaired}",
        )

    # ── Live renderer ─────────────────────────────────────────────────────

    async def _run_doctor_live(self, ctx: DiscoContext, *, dry_run: bool) -> dict:
        """Run the auto-repair event stream and animate the result into one message.

        Returns the final ``done`` summary dict (as emitted by
        :func:`core.framework.ai.auto_repair.run_auto_repair`) so callers like
        :meth:`ai_doctor_test` can assert on repair outcomes.
        """
        from core.framework.ai.auto_repair import run_auto_repair

        state = {
            "phase": "starting",
            "probes": {b: {"status": "pending"} for b in self._DOCTOR_BACKENDS},
            "categories": {c: {"status": "pending"} for c in self._DOCTOR_CATEGORIES},
            "dry_run": dry_run,
            "frame_idx": 0,
        }

        placeholder = await ctx.reply(
            embed=self._build_doctor_embed(state, done=False).build(),
            mention_author=False,
        )
        last_edit = time.monotonic()

        async def _maybe_edit(force: bool = False) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if not force and (now - last_edit) < self._DOCTOR_EDIT_THROTTLE:
                return
            state["frame_idx"] += 1
            try:
                await placeholder.edit(
                    embed=self._build_doctor_embed(state, done=False).build(),
                )
                last_edit = now
            except discord.HTTPException:
                pass

        spinner_stop = asyncio.Event()

        async def _spinner_task() -> None:
            try:
                while not spinner_stop.is_set():
                    await asyncio.sleep(1.4)
                    if spinner_stop.is_set():
                        return
                    await _maybe_edit(force=True)
            except asyncio.CancelledError:
                pass

        animator = asyncio.create_task(_spinner_task())
        summary: dict | None = None

        try:
            async for event in run_auto_repair(ctx.db, ctx.guild_id, dry_run=dry_run):
                kind = event.get("type")
                if kind == "phase":
                    state["phase"] = f"{event['name']} ({event['status']})"
                elif kind == "probe_start":
                    state["probes"][event["backend"]] = {"status": "probing"}
                elif kind == "probe_result":
                    state["probes"][event["backend"]] = {
                        "status": "ok" if event["ok"] else "fail",
                        "latency_ms": event.get("latency_ms", 0),
                        "error": event.get("error"),
                        "detail": event.get("detail"),
                    }
                elif kind == "category_ok":
                    state["categories"][event["category"]] = {
                        "status": "ok",
                        "backend": event["backend"],
                        "latency_ms": event.get("latency_ms", 0),
                    }
                elif kind == "repair_plan":
                    state["categories"][event["category"]] = {
                        "status": "repairing",
                        "from": event["from"],
                        "to": event["to"],
                    }
                elif kind == "repair_result":
                    cat_state = state["categories"].get(event["category"], {})
                    cat_state["status"] = "repaired" if event.get("ok") else "stuck"
                    cat_state["message"] = event.get("message", "")
                    if "from" in event:
                        cat_state["from"] = event["from"]
                    if "to" in event:
                        cat_state["to"] = event["to"]
                    state["categories"][event["category"]] = cat_state
                elif kind == "category_stuck":
                    state["categories"][event["category"]] = {
                        "status": "stuck",
                        "backend": event["backend"],
                        "reason": event.get("reason", ""),
                    }
                elif kind == "done":
                    summary = event["summary"]
                    state["phase"] = "done"
                await _maybe_edit()
        finally:
            spinner_stop.set()
            animator.cancel()
            try:
                await animator
            except (asyncio.CancelledError, Exception):
                pass

        try:
            await placeholder.edit(
                embed=self._build_doctor_embed(state, done=True, summary=summary).build(),
            )
        except discord.HTTPException:
            pass

        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="doctor_dryrun" if dry_run else "doctor_run",
            severity=SEVERITY_INFO,
            details=(
                f"healthy={(summary or {}).get('backends_healthy', '?')}/"
                f"{(summary or {}).get('backends_probed', '?')} "
                f"repairs={len((summary or {}).get('repairs_performed', []))}"
            ),
        )
        return summary or {}

    def _build_doctor_embed(
        self, state: dict, *, done: bool, summary: dict | None = None,
    ) -> object:
        """Compose the live doctor embed from the current state map."""
        spinner = self._DOCTOR_SPINNER_FRAMES[
            state["frame_idx"] % len(self._DOCTOR_SPINNER_FRAMES)
        ]
        dry = state.get("dry_run")
        if done:
            title = "\U0001F3E5 AI Doctor  -  complete"
            color = C_SUCCESS
            if summary and summary.get("backends_healthy", 0) < summary.get("backends_probed", 1):
                color = C_WARNING
        else:
            title = f"\U0001F3E5 AI Doctor  -  {spinner} {state['phase']}"
            color = C_INFO

        b = card(title, color=color)
        if dry:
            b.description("_Dry run: probes only, config will not be changed._")

        # Backends pane
        backend_lines: list[str] = []
        for name in self._DOCTOR_BACKENDS:
            entry = state["probes"].get(name, {"status": "pending"})
            st = entry.get("status")
            if st == "pending":
                icon = "⏳"
                detail = "queued"
            elif st == "probing":
                icon = spinner
                detail = "probing..."
            elif st == "ok":
                icon = "✅"
                detail = f"{entry.get('latency_ms', 0)}ms"
            else:
                icon = "❌"
                err = entry.get("error") or "unknown"
                detail = f"{entry.get('latency_ms', 0)}ms  -  {err[:80]}"
            backend_lines.append(f"{icon} `{name}`  -  {detail}")
        b.field("Backends", "\n".join(backend_lines), False)

        # Categories pane
        cat_lines: list[str] = []
        for name in self._DOCTOR_CATEGORIES:
            entry = state["categories"].get(name, {"status": "pending"})
            st = entry.get("status")
            if st == "pending":
                cat_lines.append(f"⏳ `{name}`  -  waiting")
            elif st == "ok":
                bk = entry.get("backend", "?")
                lat = entry.get("latency_ms", 0)
                cat_lines.append(f"✅ `{name}`  -  healthy via `{bk}` ({lat}ms)")
            elif st == "repairing":
                cat_lines.append(
                    f"\U0001F527 `{name}`  -  flipping `{entry.get('from', '?')}` -> "
                    f"`{entry.get('to', '?')}`"
                )
            elif st == "repaired":
                cat_lines.append(
                    f"\U0001F7E2 `{name}`  -  repaired: `{entry.get('from', '?')}` -> "
                    f"`{entry.get('to', '?')}`"
                )
            elif st == "stuck":
                reason = entry.get("reason") or entry.get("message") or "no healthy alternative"
                cat_lines.append(f"\U0001F534 `{name}`  -  stuck: {reason[:80]}")
        b.field("Categories", "\n".join(cat_lines), False)

        if done and summary:
            healthy = summary.get("backends_healthy", 0)
            total = summary.get("backends_probed", 0)
            repairs = summary.get("repairs_performed", [])
            b.footer(
                f"Backends healthy: {healthy}/{total}  |  Repairs: {len(repairs)}"
                + ("  |  dry-run (no changes written)" if summary.get("dry_run") else "")
            )
        else:
            b.footer(
                "Probing each backend with one real request. "
                "Unhealthy backends are failed over to the next healthy alternative."
            )
        return b

    @ai.group(name="emojis", invoke_without_command=True)
    @_require_manage_guild()
    async def ai_emojis(self, ctx: DiscoContext) -> None:
        """Per-guild custom emoji meaning index (vision + usage context)."""
        if await suggest_subcommand(ctx, self.ai_emojis):
            return
        await self.ai_emojis_stats(ctx)

    @ai_emojis.command(name="index")
    @_require_manage_guild()
    async def ai_emojis_index(self, ctx: DiscoContext, flag: str | None = None) -> None:
        """Re-index all custom emojis for this guild.

        By default, entries refreshed within the last 14 days are skipped.
        Pass ``force`` (or ``--force``) to re-index everything, including
        manual overrides.
        """
        from core.framework.emoji_index import DEFAULT_MAX_AGE_DAYS, index_guild

        force = bool(flag and flag.lstrip("-").lower() in ("force", "f", "all"))
        emoji_count = len(getattr(ctx.guild, "emojis", []) or [])
        if emoji_count == 0:
            await ctx.reply_error("This server has no custom emojis.")
            return

        await ctx.reply_success(
            f"Indexing {emoji_count} emoji(s). Vision + usage synthesis takes "
            f"a few seconds per emoji, results will follow when done.",
            title="\U0001F50D Emoji Index",
        )

        try:
            stats = await index_guild(
                ctx.db, ctx.guild,
                force=force, max_age_days=DEFAULT_MAX_AGE_DAYS,
            )
        except Exception as exc:
            log.exception("ai.emojis.index failed")
            await ctx.reply_error(f"Index failed: {exc}")
            return

        vision_down = bool(stats.get("vision_down"))
        b = card(
            "\U0001F50D Emoji Index Complete",
            color=C_WARNING if vision_down else C_SUCCESS,
        )
        b.field("Total emojis", str(stats["total"]), True)
        b.field("Indexed", str(stats["indexed"]), True)
        b.field("Skipped (fresh)", str(stats["skipped"]), True)
        if stats["failed"]:
            b.field("Failed", str(stats["failed"]), True)
        if stats["pruned"]:
            b.field("Pruned (removed)", str(stats["pruned"]), True)
        if vision_down:
            b.field(
                "Vision backend unavailable",
                "Vision calls failed repeatedly, so entries were built from "
                "usage context and names alone. Re-run `,ai emojis index force` "
                "once the vision backend is back up for richer descriptions.",
                False,
            )
        b.footer(
            "Fresh entries aged < 14 days are skipped. Use ,ai emojis index force "
            "to re-index everything."
        )
        await ctx.reply(embed=b.build(), mention_author=False)
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="emojis_index", severity=SEVERITY_INFO,
            details=(
                f"force={force} total={stats['total']} indexed={stats['indexed']} "
                f"skipped={stats['skipped']} failed={stats['failed']} pruned={stats['pruned']}"
            ),
        )

    @ai_emojis.command(name="show")
    @_require_manage_guild()
    async def ai_emojis_show(self, ctx: DiscoContext) -> None:
        """Paginated list of indexed emoji meanings for this guild."""
        rows = await ctx.db.get_all_emoji_meanings(ctx.guild_id)
        if not rows:
            await ctx.reply_error(
                "No emoji meanings indexed yet. Run `,ai emojis index` first."
            )
            return

        # Attach a live raw markup for each row so the embed renders the
        # actual emoji; missing ones (emoji deleted after indexing) fall
        # back to `:name:` plain text.
        guild_emojis_by_id = {int(e.id): e for e in ctx.guild.emojis}

        pages: list[discord.Embed] = []
        per_page = 12
        for start in range(0, len(rows), per_page):
            chunk = rows[start : start + per_page]
            b = card(
                f"\U0001F50D Emoji Meanings  -  {start + 1}-{start + len(chunk)} of {len(rows)}",
                color=C_INFO,
            )
            lines: list[str] = []
            for r in chunk:
                eid = int(r["emoji_id"])
                live = guild_emojis_by_id.get(eid)
                raw = str(live) if live else f":{r['name']}:"
                desc = str(r.get("description") or "").strip()
                cat = r.get("category") or ""
                tag = f"[{cat}] " if cat else ""
                src = r.get("source") or "vision"
                updated = fmt_ts(r.get("updated_at"))
                lines.append(
                    f"{raw} **{r['name']}** {tag}({src}, {updated})\n{desc or '(no description)'}"
                )
            b.description("\n\n".join(lines))
            pages.append(b.build())

        if len(pages) == 1:
            await ctx.reply(embed=pages[0], mention_author=False)
        else:
            await CategoryPaginator.send(ctx, {"\U0001F50D Emoji Meanings": pages})

    @ai_emojis.command(name="set")
    @_require_manage_guild()
    async def ai_emojis_set(
        self, ctx: DiscoContext, emoji: discord.Emoji, *, description: str,
    ) -> None:
        """Manually override the stored description for one emoji.

        The override is re-evaluated on the 14-day refresh cycle like any
        vision-derived entry; run ``,ai emojis set`` again to lock in a
        different description.
        """
        description = (description or "").strip()
        if not description:
            await ctx.reply_error("Description cannot be empty.")
            return
        if emoji.guild_id != ctx.guild_id:
            await ctx.reply_error("That emoji isn't from this server.")
            return
        if len(description) > 220:
            description = description[:217].rstrip() + "..."

        await ctx.db.upsert_emoji_meaning(
            ctx.guild_id, int(emoji.id), emoji.name, description,
            animated=bool(emoji.animated), category=None, source="manual",
        )
        await ctx.reply_success(
            f"Meaning set for {emoji}  -  {description}",
            title="\U0001F50D Emoji Meaning",
        )
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="emojis_set", severity=SEVERITY_INFO,
            details=f"emoji={emoji.name}:{emoji.id}",
        )

    @ai_emojis.command(name="stats")
    @_require_manage_guild()
    async def ai_emojis_stats(self, ctx: DiscoContext) -> None:
        """Show index coverage + age + usage totals."""
        from core.framework.emoji_index import DEFAULT_MAX_AGE_DAYS

        guild_emojis = list(getattr(ctx.guild, "emojis", []) or [])
        total = len(guild_emojis)
        rows = await ctx.db.get_all_emoji_meanings(ctx.guild_id)
        stale = await ctx.db.get_stale_emoji_meaning_ids(
            ctx.guild_id, max_age_days=DEFAULT_MAX_AGE_DAYS,
        )
        covered = len(rows)
        by_source: dict[str, int] = {}
        for r in rows:
            by_source[r.get("source") or "vision"] = by_source.get(r.get("source") or "vision", 0) + 1

        usage_total = await ctx.db.fetch_val(
            "SELECT COUNT(*) FROM guild_emoji_usage WHERE guild_id=$1", ctx.guild_id,
        )

        b = card("\U0001F50D Emoji Index  -  Stats", color=C_NAVY)
        b.field("Server emojis", str(total), True)
        b.field("Indexed", f"{covered} / {total}", True)
        b.field("Stale (>14d)", str(len(stale)), True)
        if by_source:
            src_line = "  ".join(f"{k}: {v}" for k, v in sorted(by_source.items()))
            b.field("By source", src_line, False)
        b.field("Usage samples tracked", f"{int(usage_total or 0):,}", True)
        p = ctx.prefix or "."
        b.footer(
            f"{p}ai emojis index  -  refresh stale entries  |  "
            f"{p}ai emojis index force  -  re-index everything  |  "
            f"{p}ai emojis show  -  browse meanings"
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    # ------------------------------------------------------------------
    # 2-week auto-refresh loop
    # ------------------------------------------------------------------
    #
    # Runs every 24h, iterates guilds the bot is in, and re-indexes the
    # emojis whose meanings are older than the 14-day staleness window.
    # Concurrency-capped inside index_guild so a few big servers don't all
    # hammer the vision backend at once.

    @tasks.loop(hours=24)
    async def _emoji_refresh_task(self) -> None:
        from core.framework.emoji_index import DEFAULT_MAX_AGE_DAYS, index_guild

        for guild in list(self.bot.guilds):
            try:
                stats = await index_guild(
                    self.bot.db, guild,
                    force=False, max_age_days=DEFAULT_MAX_AGE_DAYS,
                )
                if stats["indexed"] or stats["pruned"]:
                    log.info(
                        "[ai.emojis] auto-refresh guild=%s indexed=%d pruned=%d skipped=%d",
                        guild.id, stats["indexed"], stats["pruned"], stats["skipped"],
                    )
            except Exception:
                log.exception("[ai.emojis] auto-refresh failed for guild=%s", guild.id)
                continue

    @_emoji_refresh_task.before_loop
    async def _before_emoji_refresh(self) -> None:
        await self.bot.wait_until_ready()
        # Delay first run by 10 minutes so startup isn't dominated by vision
        # calls on a cold cache.
        await asyncio.sleep(600)

    # ------------------------------------------------------------------
    # DiscoAI memory sidecar (moved here from the old ,disco group)
    # ------------------------------------------------------------------
    def _disco_cog(self):
        """The DiscoAI memory sidecar cog, or None if it failed to load."""
        return self.bot.get_cog("DiscoAI")

    @ai.group(name="memory", aliases=["mem"], invoke_without_command=True)
    @_require_manage_guild()
    async def ai_memory(self, ctx: DiscoContext) -> None:
        """DiscoAI memory sidecar controls."""
        if await suggest_subcommand(ctx, self.ai_memory):
            return
        p = ctx.prefix or "."
        await ctx.reply(
            embed=card(
                "DiscoAI memory controls",
                color=C_INFO,
                description=(
                    f"`{p}ai memory forget` -- clear short-term memory in this channel\n"
                    f"`{p}ai memory facts [scope]` -- list long-term facts\n"
                    f"`{p}ai memory remember <scope> <key> <value>` -- upsert a fact\n"
                    f"`{p}ai memory listen <on|off>` -- toggle passive episode capture"
                ),
            ).build(),
            mention_author=False,
        )

    @ai_memory.command(name="forget")
    @_require_manage_guild()
    async def ai_memory_forget(self, ctx: DiscoContext) -> None:
        """Clear DiscoAI's short-term memory of this channel's conversation."""
        disco = self._disco_cog()
        mem = getattr(disco, "memory", None) if disco else None
        if mem is None:
            await ctx.reply_error("DiscoAI memory is not initialized.")
            return
        n = await mem.clear(ctx.guild_id, ctx.channel.id, ctx.author.id)
        await ctx.reply_success(
            f"Cleared {n} short-term turn(s). Long-term facts are unchanged.",
            title="DiscoAI memory cleared",
        )

    @ai_memory.command(name="facts")
    @_require_manage_guild()
    async def ai_memory_facts(self, ctx: DiscoContext, *, scope: str | None = None) -> None:
        """List DiscoAI long-term facts for a scope (defaults to this guild)."""
        disco = self._disco_cog()
        mem = getattr(disco, "memory", None) if disco else None
        if mem is None:
            await ctx.reply_error("DiscoAI memory is not initialized.")
            return
        from ai import guild_scope
        target_scope = scope or guild_scope(ctx.guild_id)
        facts = await mem.get_facts(target_scope, limit=20)
        if not facts:
            await ctx.reply(
                embed=card(
                    "DiscoAI facts",
                    color=C_INFO,
                    description=f"No facts in scope `{target_scope}`.",
                ).build(),
                mention_author=False,
            )
            return
        b = card(
            f"DiscoAI facts · {target_scope}",
            color=C_INFO,
            description=f"{len(facts)} fact(s) (newest first):",
        )
        for f in facts[:20]:
            value = f.value if len(f.value) <= 800 else f.value[:797] + "..."
            b = b.field(
                f.key,
                f"{value}\n_conf: {f.confidence:.2f} · src: {f.source} · {fmt_ts(f.updated_at)}_",
                inline=False,
            )
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_memory.command(name="remember")
    @_require_manage_guild()
    async def ai_memory_remember(
        self, ctx: DiscoContext, scope: str, key: str, *, value: str,
    ) -> None:
        """Manually add or overwrite a DiscoAI fact."""
        disco = self._disco_cog()
        mem = getattr(disco, "memory", None) if disco else None
        if mem is None:
            await ctx.reply_error("DiscoAI memory is not initialized.")
            return
        await mem.upsert_fact(scope, key, value, confidence=1.0, source="admin")
        await ctx.reply_success(
            f"Recorded `{key}` in scope `{scope}`.",
            title="DiscoAI fact saved",
        )

    @ai_memory.command(name="listen")
    @_require_manage_guild()
    async def ai_memory_listen(self, ctx: DiscoContext, setting: str) -> None:
        """Toggle passive episode capture in this channel (on|off)."""
        disco = self._disco_cog()
        if disco is None:
            await ctx.reply_error("DiscoAI is not loaded.")
            return
        settings = getattr(disco, "settings", None)
        if settings is None or not settings.passive_learning:
            await ctx.reply_error(
                "Passive learning is disabled globally. Set "
                "`DISCOAI_PASSIVE_LEARNING=true` in env to enable."
            )
            return
        normalized = setting.strip().lower()
        if normalized in ("on", "enable", "enabled", "true", "1"):
            await self.bot.db.execute(
                """
                INSERT INTO disco_passive_channels (guild_id, channel_id, enabled_by)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, channel_id) DO NOTHING
                """,
                int(ctx.guild_id), int(ctx.channel.id), int(ctx.author.id),
            )
            await ctx.reply_success(
                "Passive episode capture is now ON in this channel.",
                title="DiscoAI listening",
            )
        elif normalized in ("off", "disable", "disabled", "false", "0"):
            await self.bot.db.execute(
                "DELETE FROM disco_passive_channels WHERE guild_id = $1 AND channel_id = $2",
                int(ctx.guild_id), int(ctx.channel.id),
            )
            await ctx.reply_success(
                "Passive episode capture is now OFF in this channel.",
                title="DiscoAI silent",
            )
        else:
            await ctx.reply_error("Use `on` or `off`.")

    # ------------------------------------------------------------------
    # Audit feed
    # ------------------------------------------------------------------
    @ai.command(name="audit")
    @_require_manage_guild()
    async def ai_audit(self, ctx: DiscoContext, limit: int = 50) -> None:
        """Show recent ,ai scope audit rows."""
        limit = max(1, min(250, int(limit)))
        entries = await recent_staff_actions(
            ctx.db, guild_id=ctx.guild_id, scope=SCOPE_AI, limit=limit,
        )
        pages = build_audit_embeds(entries, scope=SCOPE_AI, guild=ctx.guild)
        if not pages:
            b = card("\U0001F4CB AI Audit", color=C_NAVY)
            b.description("No audit entries found for the AI scope.")
            await ctx.reply(embed=b.build(), mention_author=False)
            return
        if len(pages) > 1:
            await CategoryPaginator.send(ctx, {"\U0001F4CB AI Audit": pages})
        else:
            await ctx.reply(embed=pages[0], mention_author=False)


async def setup(bot: Discoin) -> None:
    # Chain executor needs a bot ref stashed on the db handle.
    if not hasattr(bot.db, "_bot"):
        try:
            bot.db._bot = bot
        except Exception:
            pass
    await bot.add_cog(AI(bot))
