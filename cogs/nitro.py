"""Nitro Lottery -- a sniper-safe way for players to share Discord Nitro.

Pasting a Discord Nitro gift link straight into a channel is a lost cause:
auto-claim "Nitro bots" snipe it within milliseconds, so the human the host
wanted to gift never gets it. This module fixes that.

The safe system:
  * The host's gift code is collected through a PRIVATE modal -- it is never
    shown in any channel, only stored on the bot.
  * Players join a lottery with an Enter button. The winner is drawn at
    RANDOM, so a sniper's speed buys it nothing: it has the same 1/N odds as
    everyone else.
  * The code is delivered only to the winner -- by DM, plus a winner-locked
    "Reveal my gift" button on the announcement as a fallback.

Commands live on a dedicated ``.`` prefix, separate from the bot's main
prefix, dispatched via an ``on_message`` listener -- the same pattern the
``$`` real-market namespace uses:

    .help          -- usage screen, works in any channel
    .nitro host    -- host a Nitro / Nitro Basic lottery
    .nitro list    -- open lotteries in this server

Nitro and Nitro Basic are tracked separately (nitro_lotteries.nitro_type)
and labelled distinctly in every embed, reply and DM.
"""
from __future__ import annotations

import logging
import random
import re
import time

import discord
from discord.ext import commands, tasks

from core.config import Config
from core.framework.bot import Discoin
from core.framework.content_filter import has_discord_entities, has_scam_patterns
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.heartbeat import pulse, register_interval
from core.framework.ui import C_BLURPLE, C_NEUTRAL, C_PURPLE, C_SUBTLE, C_SUCCESS, fmt_ts

log = logging.getLogger(__name__)

# Subcommands the "." dispatcher will act on. Anything else after a "." is
# left alone so "...", ".5", ".gg" etc. never get intercepted.
_DISPATCH_ALLOWLIST: frozenset[str] = frozenset({"help", "h", "nitro", "n"})

# Background draw loop cadence (seconds).
_DRAW_TICK = 30
# How long the tier-select setup panel stays clickable.
_SETUP_TIMEOUT = 300.0
# Lottery length bounds (minutes) accepted from the host modal.
_MIN_MINUTES = 1
_MAX_MINUTES = 1440
# Soft anti-spam: most open lotteries one host may run at once per guild.
_MAX_OPEN_PER_HOST = 3

# Nitro tier metadata -- the single source of truth for how each tier is
# labelled, coloured and described. Every embed/reply pulls from here so
# Nitro and Nitro Basic are never conflated.
_NITRO_TIERS: dict[str, dict] = {
    "nitro": {
        "label": "Nitro",
        "emoji": "\U0001F48E",  # gem stone
        "color": C_BLURPLE,
        "blurb": (
            "HD streaming, 500 MB uploads, custom emoji & stickers everywhere, "
            "an animated avatar, and 2 server boosts."
        ),
    },
    "nitro_basic": {
        "label": "Nitro Basic",
        "emoji": "\U00002728",  # sparkles
        "color": C_PURPLE,
        "blurb": (
            "50 MB uploads and custom emoji & stickers -- no HD streaming, no "
            "animated avatar, and no server boosts."
        ),
    },
}

# ── Gift-link validation: the single security chokepoint ──────────────────
#
# A scammer must NEVER be able to make the bot relay anything other than a
# genuine Discord Nitro gift link. Two structural guarantees enforce that:
#
#   1. The host's input must be EXACTLY a Discord gift link -- matched with
#      fullmatch, never search -- so no other URL, no surrounding text, and
#      no second link can ride alongside a real code. Anything else is
#      rejected outright; nothing is "salvaged" from a padded field.
#   2. The bot keeps ONLY the alphanumeric gift code and always rebuilds the
#      delivered link itself as https://discord.gift/<code>. The host's raw
#      input is never stored or echoed, so the bot physically cannot hand a
#      player a link on any other domain.

# The host's input, in full: optional scheme/www, the discord gift host, a
# "/" + alphanumeric code, an optional trailing slash. Nothing else allowed.
_GIFT_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?"
    r"(?:discord\.gift|discord(?:app)?\.com/gift)"
    r"/([A-Za-z0-9]{8,64})/?",
    re.IGNORECASE,
)

# A bare gift code, exactly: alphanumeric only, length-bounded.
_GIFT_CODE_RE = re.compile(r"[A-Za-z0-9]{8,64}")

# Final guard: the link the bot actually delivers must itself be a clean
# discord.gift URL -- re-checked on the way in (parse) and on the way out.
_SAFE_GIFT_URL_RE = re.compile(r"https://discord\.gift/[A-Za-z0-9]{8,64}")


def _tier(key: str) -> dict:
    """Return the metadata block for a nitro_type, defaulting to full Nitro."""
    return _NITRO_TIERS.get(key, _NITRO_TIERS["nitro"])


def _safe_gift_url(code: str) -> str | None:
    """Rebuild the redeemable link from a stored gift code, or None if the
    code is not a clean Discord gift code. Every place that shows or sends a
    gift link goes through here, so the bot can never emit a link on any
    domain other than discord.gift -- not even if the DB row were tampered
    with or a future code path stored something unexpected."""
    code = (code or "").strip()
    if not _GIFT_CODE_RE.fullmatch(code):
        return None
    url = f"https://discord.gift/{code}"
    return url if _SAFE_GIFT_URL_RE.fullmatch(url) else None


def _parse_gift(raw: str) -> str | None:
    """Return the bare gift code IFF the input is EXACTLY a Discord Nitro
    gift link. Returns None for anything else: any other URL, any extra text
    before/after the link, a bare code with no link, or junk.

    This is the single gate that decides what the bot will escrow. Because it
    uses fullmatch (never search), a scammer cannot pad the field with a
    phishing URL and have a real gift code salvaged out of the rest -- the
    entire input is rejected unless it is, start to end, a gift link.
    """
    m = _GIFT_LINK_RE.fullmatch((raw or "").strip())
    if not m:
        return None
    code = m.group(1)
    # Defense in depth: confirm the link the bot will deliver is itself clean.
    if _safe_gift_url(code) is None:
        return None
    return code


# Host notes are plain text only. Anything resembling a link, invite, bare
# domain, or spaced-out evasion gets the whole submission rejected, so the
# bot can never render an attacker-supplied link inside one of its embeds.
_NOTE_BLOCK_RE = re.compile(
    r"https?://"
    r"|www\."
    r"|discord\s*\.\s*(?:gg|gift|com)"
    r"|\b[a-z0-9][a-z0-9-]*\.(?:com|net|org|io|gg|gift|xyz|link|app|co|me|ru|"
    r"tk|info|biz|online|site|club|vip|win|live|store|shop|pro|cc|to|us)\b",
    re.IGNORECASE,
)


def _note_is_clean(note: str) -> bool:
    """A host note may be plain prose only. Returns False if it contains a
    link, invite, bare domain, Discord mention/ID, or known scam phrasing --
    in which case the caller rejects the whole submission."""
    if not note:
        return True
    if _NOTE_BLOCK_RE.search(note):
        return False
    if has_scam_patterns(note) or has_discord_entities(note):
        return False
    return True


def _parse_minutes(raw: str) -> int | None:
    """Parse a lottery length in minutes, or None if out of range/invalid."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        val = int(float(raw))
    except ValueError:
        return None
    if val < _MIN_MINUTES or val > _MAX_MINUTES:
        return None
    return val


def _fmt_left(seconds: float) -> str:
    """Compact human countdown, e.g. '~7m left' / '~2h 15m left'."""
    seconds = int(max(0, seconds))
    if seconds <= 0:
        return "closing now"
    if seconds < 3600:
        return f"~{max(1, seconds // 60)}m left"
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"~{hours}h {mins}m left" if mins else f"~{hours}h left"


def _resolve_target(
    ctx: DiscoContext, arg_str: str
) -> "discord.Member | discord.User | None":
    """Resolve the recipient of a direct gift from a mention or a raw id."""
    bot_id = ctx.bot.user.id if ctx.bot.user else 0
    for member in ctx.message.mentions:
        if member.id != bot_id:
            return member
    digits = "".join(ch for ch in (arg_str or "") if ch.isdigit())
    if digits and ctx.guild:
        member = ctx.guild.get_member(int(digits))
        if member:
            return member
    return None


def _help_embed() -> discord.Embed:
    """The ``.help`` screen -- explains the whole sharing system."""
    tier_n, tier_b = _tier("nitro"), _tier("nitro_basic")
    return (
        card(
            "Nitro Sharing -- safe Nitro gifting",
            description=(
                "Dropping a Discord Nitro gift link into chat is a lost "
                "cause: auto-claim 'Nitro bots' snipe it within "
                "milliseconds, so the person you meant to gift never gets "
                "it. Discoin shares Nitro safely -- your gift code is taken "
                "on a private form, never shown in any channel, and "
                "delivered straight to the right person by DM. There are "
                "two ways to share:"
            ),
            color=C_BLURPLE,
        )
        .field(
            "\U0001F381 Direct gift -- .nitro gift @user",
            "Send a gift straight to one specific person. Pick a tier, "
            "paste the link on the private form, and the recipient gets it "
            "by DM plus a private **Reveal my gift** button. The code never "
            "appears in chat, so there is nothing for a sniper to grab.",
            False,
        )
        .field(
            "\U0001F389 Lottery -- .nitro host",
            "Put a gift up for grabs. Players join with an **Enter** button "
            "and one entrant is drawn **at random** when the timer ends (or "
            "you press **Draw Now**). Because the winner is random, not "
            "first-click, a sniper's speed buys nothing. The winner gets it "
            "by DM and a Reveal button.",
            False,
        )
        .field(
            f"{tier_n['emoji']} Nitro vs {tier_b['emoji']} Nitro Basic",
            f"**Nitro** -- {tier_n['blurb']}\n"
            f"**Nitro Basic** -- {tier_b['blurb']}\n"
            "Every embed, reply and DM states which tier is being shared.",
            False,
        )
        .field(
            "Commands ('.' prefix)",
            "`.help` -- this screen, usable in any channel\n"
            "`.nitro gift @user` -- gift Nitro straight to one person\n"
            "`.nitro host` -- host a Nitro / Nitro Basic lottery\n"
            "`.nitro list` -- open lotteries in this server",
            False,
        )
        .footer(
            "Discoin holds the code privately but cannot verify a gift is "
            "valid -- only share gifts you actually own."
        )
        .build()
    )


# ── Setup panel: tier picker ──────────────────────────────────────────────

class NitroGiftModal(discord.ui.Modal):
    """Private form where the host pastes the gift link. The code typed
    here is visible only to the host -- it never reaches a channel.

    Adapts to ``mode``: a 'lottery' form has a duration field; a 'direct'
    gift goes to one person immediately, so it has no timer."""

    def __init__(
        self,
        cog: "NitroShare",
        tier_key: str,
        setup_message: discord.Message,
        setup_view: "NitroSetupView",
        *,
        mode: str = "lottery",
        target_id: int | None = None,
    ) -> None:
        tier = _tier(tier_key)
        is_direct = mode == "direct"
        super().__init__(
            title=(
                f"Gift {tier['label']}"
                if is_direct
                else f"Host a {tier['label']} Lottery"
            ),
            timeout=600.0,
        )
        self.cog = cog
        self.tier_key = tier_key
        self.setup_message = setup_message
        self.setup_view = setup_view
        self.mode = mode
        self.target_id = target_id

        self.gift = discord.ui.TextInput(
            label="Discord Nitro gift link",
            placeholder="https://discord.gift/xxxxxxxxxxxxxxxx",
            required=True,
            max_length=120,
        )
        self.add_item(self.gift)
        # A direct gift goes to one person right away -- no entry timer.
        self.minutes: discord.ui.TextInput | None = None
        if not is_direct:
            self.minutes = discord.ui.TextInput(
                label=f"Length in minutes ({_MIN_MINUTES}-{_MAX_MINUTES})",
                default="10",
                required=True,
                max_length=5,
            )
            self.add_item(self.minutes)
        self.note = discord.ui.TextInput(
            label=(
                "Note for the recipient (optional)"
                if is_direct
                else "Note for entrants (optional)"
            ),
            placeholder="Plain text only -- links are not allowed here.",
            required=False,
            max_length=200,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        code = _parse_gift(self.gift.value)
        if not code:
            await interaction.response.send_message(
                "That is not a valid Discord Nitro gift link. Discoin will "
                "only ever share a genuine `https://discord.gift/...` link "
                "and **nothing else** -- no other links, no plain text. "
                "Copy the gift link straight from Discord and paste it on "
                "its own, with no extra text around it.",
                ephemeral=True,
            )
            return
        note = (self.note.value or "").strip()
        if note and not _note_is_clean(note):
            await interaction.response.send_message(
                "Your note was rejected: it contains a link, invite, domain, "
                "mention, or blocked wording. The note must be **plain text "
                "only** -- no links of any kind. Remove it and try again.",
                ephemeral=True,
            )
            return
        if self.mode == "direct":
            await self.cog.create_direct_gift(
                interaction,
                self.tier_key,
                code,
                note,
                self.setup_message,
                self.setup_view,
                int(self.target_id or 0),
            )
            return
        minutes = _parse_minutes(self.minutes.value if self.minutes else "")
        if minutes is None:
            await interaction.response.send_message(
                f"Lottery length must be a whole number of minutes between "
                f"{_MIN_MINUTES} and {_MAX_MINUTES}.",
                ephemeral=True,
            )
            return
        await self.cog.create_lottery(
            interaction,
            self.tier_key,
            code,
            minutes,
            note,
            self.setup_message,
            self.setup_view,
        )


class NitroSetupView(discord.ui.View):
    """Host-only panel for choosing the Nitro tier before the gift modal.

    Shared by both ``.nitro host`` (mode='lottery') and ``.nitro gift``
    (mode='direct'); ``target_id`` carries the recipient for a direct gift."""

    def __init__(
        self,
        cog: "NitroShare",
        host_id: int,
        *,
        mode: str = "lottery",
        target_id: int | None = None,
    ) -> None:
        super().__init__(timeout=_SETUP_TIMEOUT)
        self.cog = cog
        self.host_id = int(host_id)
        self.mode = mode
        self.target_id = target_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.host_id:
            await interaction.response.send_message(
                "Only the player setting this up can use these buttons. "
                "Run `.nitro host` or `.nitro gift @user` to start your own.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        retry = ".nitro gift @user" if self.mode == "direct" else ".nitro host"
        try:
            await self.message.edit(
                embed=card(
                    "Nitro -- setup expired",
                    description=(
                        f"You took too long to configure this. Run "
                        f"`{retry}` to try again."
                    ),
                    color=C_NEUTRAL,
                ).build(),
                view=None,
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="Nitro", emoji="\U0001F48E", style=discord.ButtonStyle.primary
    )
    async def _pick_nitro(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            NitroGiftModal(
                self.cog, "nitro", interaction.message, self,
                mode=self.mode, target_id=self.target_id,
            )
        )

    @discord.ui.button(
        label="Nitro Basic",
        emoji="\U00002728",
        style=discord.ButtonStyle.secondary,
    )
    async def _pick_basic(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            NitroGiftModal(
                self.cog, "nitro_basic", interaction.message, self,
                mode=self.mode, target_id=self.target_id,
            )
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def _cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=card(
                "Nitro Lottery -- cancelled",
                description="Setup cancelled. Nothing was shared.",
                color=C_NEUTRAL,
            ).build(),
            view=None,
        )


# ── Live lottery: Enter / Draw Now / Cancel ───────────────────────────────

class NitroLotteryView(discord.ui.View):
    """Persistent controls on a live (open) lottery. Custom IDs carry the
    lottery id so the buttons keep working after a bot restart."""

    def __init__(self, cog: "NitroShare", lottery_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.lottery_id = int(lottery_id)

        self.enter_btn = discord.ui.Button(
            label="Enter",
            emoji="\U0001F389",
            style=discord.ButtonStyle.success,
            custom_id=f"nitro:enter:{self.lottery_id}",
        )
        self.draw_btn = discord.ui.Button(
            label="Draw Now",
            style=discord.ButtonStyle.primary,
            custom_id=f"nitro:draw:{self.lottery_id}",
        )
        self.cancel_btn = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            custom_id=f"nitro:cancel:{self.lottery_id}",
        )
        self.enter_btn.callback = self._enter
        self.draw_btn.callback = self._draw
        self.cancel_btn.callback = self._cancel
        self.add_item(self.enter_btn)
        self.add_item(self.draw_btn)
        self.add_item(self.cancel_btn)

    async def _enter(self, interaction: discord.Interaction) -> None:
        db = self.cog.bot.db
        lot = await db.fetch_one(
            "SELECT id, host_id, nitro_type, note, status, ends_at "
            "FROM nitro_lotteries WHERE id = $1",
            self.lottery_id,
        )
        if not lot or lot["status"] != "open":
            await interaction.response.send_message(
                "This lottery is closed -- entries are no longer accepted.",
                ephemeral=True,
            )
            return
        tier = _tier(lot["nitro_type"])
        if interaction.user.id == int(lot["host_id"]):
            await interaction.response.send_message(
                "You are hosting this lottery -- you cannot enter your own "
                "gift. Let someone else win it!",
                ephemeral=True,
            )
            return
        status = await db.execute(
            "INSERT INTO nitro_lottery_entries (lottery_id, user_id) "
            "VALUES ($1, $2) ON CONFLICT DO NOTHING",
            self.lottery_id,
            interaction.user.id,
        )
        count = int(
            await db.fetch_val(
                "SELECT COUNT(*) FROM nitro_lottery_entries "
                "WHERE lottery_id = $1",
                self.lottery_id,
            )
            or 0
        )
        newly_entered = status.split()[-1] != "0"
        if newly_entered:
            await interaction.response.send_message(
                f"You are entered for the {tier['emoji']} **{tier['label']}** "
                f"lottery -- **{count}** entrant(s) so far. The winner is "
                f"drawn at random when the timer ends. Good luck!",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"You are already entered for this {tier['label']} lottery. "
                f"Sit tight -- **{count}** entrant(s) in the draw.",
                ephemeral=True,
            )
        if interaction.message is not None:
            try:
                await interaction.message.edit(
                    embed=self.cog.render_open(lot, count)
                )
            except discord.HTTPException:
                pass

    async def _draw(self, interaction: discord.Interaction) -> None:
        db = self.cog.bot.db
        lot = await db.fetch_one(
            "SELECT id, host_id, status FROM nitro_lotteries WHERE id = $1",
            self.lottery_id,
        )
        if not lot or lot["status"] != "open":
            await interaction.response.send_message(
                "This lottery has already been drawn or closed.",
                ephemeral=True,
            )
            return
        if interaction.user.id != int(lot["host_id"]):
            await interaction.response.send_message(
                "Only the host can draw this lottery early.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self.cog.draw_lottery(self.lottery_id, reason="host")

    async def _cancel(self, interaction: discord.Interaction) -> None:
        db = self.cog.bot.db
        lot = await db.fetch_one(
            "SELECT id, host_id, nitro_type, status "
            "FROM nitro_lotteries WHERE id = $1",
            self.lottery_id,
        )
        if not lot:
            await interaction.response.send_message(
                "Lottery not found.", ephemeral=True
            )
            return
        if interaction.user.id != int(lot["host_id"]):
            await interaction.response.send_message(
                "Only the host can cancel this lottery.", ephemeral=True
            )
            return
        if lot["status"] != "open":
            await interaction.response.send_message(
                "This lottery is no longer open.", ephemeral=True
            )
            return
        claimed = await db.fetch_one(
            "UPDATE nitro_lotteries SET status = 'cancelled', drawn_at = NOW() "
            "WHERE id = $1 AND status = 'open' RETURNING id",
            self.lottery_id,
        )
        if not claimed:
            await interaction.response.send_message(
                "This lottery just closed -- nothing to cancel.",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            embed=self.cog.render_cancelled(lot), view=None
        )


# ── Drawn lottery: winner-only Reveal ──────────────────────────────────────

class NitroWinnerView(discord.ui.View):
    """Persistent winner-only 'Reveal my gift' control on a drawn lottery.
    A fallback for the winner if their DMs are closed."""

    def __init__(self, cog: "NitroShare", lottery_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.lottery_id = int(lottery_id)
        btn = discord.ui.Button(
            label="Reveal my gift",
            emoji="\U0001F381",
            style=discord.ButtonStyle.success,
            custom_id=f"nitro:reveal:{self.lottery_id}",
        )
        btn.callback = self._reveal
        self.add_item(btn)

    async def _reveal(self, interaction: discord.Interaction) -> None:
        db = self.cog.bot.db
        lot = await db.fetch_one(
            "SELECT id, winner_id, nitro_type, gift_code, status "
            "FROM nitro_lotteries WHERE id = $1",
            self.lottery_id,
        )
        if not lot or lot["status"] != "drawn":
            await interaction.response.send_message(
                "There is no gift to reveal here.", ephemeral=True
            )
            return
        if interaction.user.id != int(lot["winner_id"] or 0):
            await interaction.response.send_message(
                "Only the lottery winner can reveal this gift.",
                ephemeral=True,
            )
            return
        tier = _tier(lot["nitro_type"])
        url = _safe_gift_url(lot["gift_code"])
        if url is None:
            # Unreachable in practice -- _parse_gift only ever stores a clean
            # code -- but the bot refuses to show anything that is not a
            # verified discord.gift link rather than risk relaying a bad one.
            log.error(
                "[nitro] lottery %s has a non-conforming gift_code; "
                "refusing to reveal", self.lottery_id,
            )
            await interaction.response.send_message(
                "This gift could not be loaded safely -- please contact the "
                "host. Discoin only ever hands out verified Discord gift "
                "links.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=card(
                f"{tier['emoji']} Your {tier['label']} gift",
                description=f"[Redeem {tier['label']}]({url})\n`{url}`",
                color=tier["color"],
            )
            .footer(
                "Visible only to you. This is a genuine discord.gift link; "
                "Discoin cannot verify the code is unused -- redeem it "
                "promptly."
            )
            .build(),
            ephemeral=True,
        )


# ── Cog ────────────────────────────────────────────────────────────────────

class NitroShare(commands.Cog):
    """Sniper-safe Nitro sharing + lottery on a dedicated '.' prefix."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot
        # 5s per-user cooldown so a double-tap does not spam panels.
        self._cooldown = commands.CooldownMapping.from_cooldown(
            1, 5.0, commands.BucketType.user
        )
        register_interval("nitro_lottery", _DRAW_TICK)

    async def cog_load(self) -> None:
        # Re-register persistent views so Enter / Draw / Reveal buttons keep
        # working after a restart.
        try:
            rows = await self.bot.db.fetch_all(
                "SELECT id, status FROM nitro_lotteries "
                "WHERE status IN ('open', 'drawn')"
            )
        except Exception:
            rows = []
        for r in rows:
            if r["status"] == "open":
                self.bot.add_view(NitroLotteryView(self, int(r["id"])))
            else:
                self.bot.add_view(NitroWinnerView(self, int(r["id"])))
        self._draw_loop.start()

    def cog_unload(self) -> None:
        self._draw_loop.cancel()

    # ── background draw loop ──────────────────────────────────────────────

    @tasks.loop(seconds=_DRAW_TICK)
    async def _draw_loop(self) -> None:
        """Draw any open lottery whose timer has elapsed. DB-side clock
        (``ends_at <= NOW()``) so container/DB skew never matters."""
        try:
            due = await self.bot.db.fetch_all(
                "SELECT id FROM nitro_lotteries "
                "WHERE status = 'open' AND ends_at <= NOW()"
            )
        except Exception:
            log.exception("[nitro] draw loop query failed")
            due = []
        for r in due:
            try:
                await self.draw_lottery(int(r["id"]), reason="timer")
            except Exception:
                log.exception("[nitro] draw failed for lottery %s", r["id"])
        pulse("nitro_lottery")

    @_draw_loop.before_loop
    async def _before_draw_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ── "." prefix dispatch ───────────────────────────────────────────────

    @commands.Cog.listener("on_message")
    async def _route_dot(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        content = (message.content or "").strip()
        if not content.startswith(".") or len(content) < 2:
            return
        # Everything after the "." must start with a letter so "...", ".5"
        # and similar are never intercepted.
        body = content[1:].lstrip()
        if not body or not body[0].isalpha():
            return
        parts = body.split(maxsplit=1)
        sub = parts[0].lower()
        if sub not in _DISPATCH_ALLOWLIST:
            return
        rest = parts[1] if len(parts) > 1 else ""

        # Clanked users cannot use any commands, dot-prefix included.
        try:
            clanktank = self.bot.cogs.get("Clanktank")
            if clanktank and await clanktank.is_clanker(message.author.id, message.guild.id):
                return
        except Exception:
            pass

        # If the guild's own command prefix is literally ".", the standard
        # command framework already owns this message -- do not double-handle.
        try:
            settings = await self.bot.db.get_guild_settings(message.guild.id)
            if (settings.get("prefix") or Config.PREFIX) == ".":
                return
        except Exception:
            pass

        # No cooldown here on purpose: ``.help`` and ``.nitro list`` are
        # cheap reads and must always respond instantly in any channel.
        # The 5s cooldown is applied per-command inside the panel-posting
        # handlers (``.nitro host`` / ``.nitro gift``) and is *visible*
        # there -- a silent drop looks like the command is broken.
        try:
            ctx = await self.bot.get_context(message, cls=DiscoContext)
        except Exception:
            log.exception("[nitro] get_context failed")
            return
        try:
            await self._dispatch(ctx, sub, rest)
        except Exception:
            log.exception("[.%s] handler crashed", sub)
            try:
                await ctx.reply_error(
                    f"`.{sub}` failed -- the host logs have the details."
                )
            except Exception:
                pass

    def _panel_on_cooldown(self, ctx: DiscoContext) -> float:
        """Per-user rate limit for the panel-posting commands. Returns the
        retry-after in seconds (0.0 when the user may proceed)."""
        bucket = self._cooldown.get_bucket(ctx.message)
        return bucket.update_rate_limit() or 0.0

    async def _dispatch(self, ctx: DiscoContext, sub: str, rest: str) -> None:
        if sub in ("help", "h"):
            await ctx.reply(embed=_help_embed(), mention_author=False)
            return
        # sub is "nitro" / "n"
        parts = rest.split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        if not action or action in ("help", "h", "?"):
            await ctx.reply(embed=_help_embed(), mention_author=False)
        elif action in ("host", "share", "start", "new", "create",
                        "lottery", "raffle"):
            await self._cmd_host(ctx)
        elif action in ("gift", "give", "send", "to", "direct"):
            await self._cmd_gift(ctx, parts[1] if len(parts) > 1 else "")
        elif action in ("list", "active", "ls", "open"):
            await self._cmd_list(ctx)
        else:
            await ctx.reply_error(
                f"Unknown action `{action}`. Try `.nitro gift @user`, "
                f"`.nitro host`, `.nitro list`, or `.help`."
            )

    # ── commands ──────────────────────────────────────────────────────────

    async def _cmd_host(self, ctx: DiscoContext) -> None:
        retry = self._panel_on_cooldown(ctx)
        if retry:
            await ctx.reply_cooldown(retry)
            return
        tier_n, tier_b = _tier("nitro"), _tier("nitro_basic")
        embed = (
            card(
                "Host a Nitro Lottery",
                description=(
                    "Choose which tier you are gifting. The next step opens "
                    "a **private form** -- your gift link is never shown in "
                    "this channel, and only the randomly drawn winner ever "
                    "sees it."
                ),
                color=C_BLURPLE,
            )
            .field(f"{tier_n['emoji']} Nitro", tier_n["blurb"], False)
            .field(f"{tier_b['emoji']} Nitro Basic", tier_b["blurb"], False)
            .footer("Only you can use this panel. It expires in 5 minutes.")
            .build()
        )
        view = NitroSetupView(self, ctx.author.id)
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = msg

    async def _cmd_gift(self, ctx: DiscoContext, arg_str: str) -> None:
        retry = self._panel_on_cooldown(ctx)
        if retry:
            await ctx.reply_cooldown(retry)
            return
        target = _resolve_target(ctx, arg_str)
        if target is None:
            await ctx.reply_error(
                "Tell me who to gift -- mention them, e.g. "
                "`.nitro gift @user`."
            )
            return
        if target.bot:
            await ctx.reply_error("You cannot gift Nitro to a bot.")
            return
        if target.id == ctx.author.id:
            await ctx.reply_error(
                "You cannot gift Nitro to yourself. Mention someone else, "
                "or run `.nitro host` for a lottery."
            )
            return
        tier_n, tier_b = _tier("nitro"), _tier("nitro_basic")
        embed = (
            card(
                f"Gift Nitro to {target.display_name}",
                description=(
                    f"You are sending a gift straight to {target.mention}. "
                    f"Pick a tier -- the next step opens a **private form** "
                    f"where you paste the gift link. It is never shown in "
                    f"this channel; only {target.mention} ever sees the code."
                ),
                color=C_BLURPLE,
            )
            .field(f"{tier_n['emoji']} Nitro", tier_n["blurb"], False)
            .field(f"{tier_b['emoji']} Nitro Basic", tier_b["blurb"], False)
            .footer("Only you can use this panel. It expires in 5 minutes.")
            .build()
        )
        view = NitroSetupView(
            self, ctx.author.id, mode="direct", target_id=target.id
        )
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = msg

    async def _cmd_list(self, ctx: DiscoContext) -> None:
        rows = await self.bot.db.fetch_all(
            "SELECT id, channel_id, host_id, nitro_type, ends_at "
            "FROM nitro_lotteries WHERE guild_id = $1 AND status = 'open' "
            "ORDER BY ends_at ASC LIMIT 20",
            ctx.guild.id,
        )
        if not rows:
            await ctx.reply(
                embed=card(
                    "Nitro Lotteries",
                    description=(
                        "No open lotteries right now. Host one with "
                        "`.nitro host`."
                    ),
                    color=C_BLURPLE,
                ).build(),
                mention_author=False,
            )
            return
        lines: list[str] = []
        for r in rows:
            count = int(
                await self.bot.db.fetch_val(
                    "SELECT COUNT(*) FROM nitro_lottery_entries "
                    "WHERE lottery_id = $1",
                    int(r["id"]),
                )
                or 0
            )
            tier = _tier(r["nitro_type"])
            remaining = float(r["ends_at"]) - time.time()
            lines.append(
                f"`#{r['id']}` {tier['emoji']} **{tier['label']}** in "
                f"<#{r['channel_id']}> -- host <@{r['host_id']}> -- "
                f"**{count}** entrant(s) -- {_fmt_left(remaining)}"
            )
        await ctx.reply(
            embed=card(
                "Open Nitro Lotteries",
                description="\n".join(lines),
                color=C_BLURPLE,
            )
            .footer("Click Enter on a lottery message to join. .help for more.")
            .build(),
            mention_author=False,
        )

    # ── lottery lifecycle ─────────────────────────────────────────────────

    async def create_lottery(
        self,
        interaction: discord.Interaction,
        tier_key: str,
        code: str,
        minutes: int,
        note: str,
        setup_message: discord.Message,
        setup_view: NitroSetupView,
    ) -> None:
        """Persist a new lottery and publish it in place of the setup panel."""
        db = self.bot.db
        guild = interaction.guild

        open_count = int(
            await db.fetch_val(
                "SELECT COUNT(*) FROM nitro_lotteries "
                "WHERE guild_id = $1 AND host_id = $2 AND status = 'open'",
                guild.id,
                interaction.user.id,
            )
            or 0
        )
        if open_count >= _MAX_OPEN_PER_HOST:
            await interaction.response.send_message(
                f"You already have {_MAX_OPEN_PER_HOST} open lotteries in "
                f"this server. Let one finish before hosting another.",
                ephemeral=True,
            )
            return

        row = await db.fetch_one(
            """
            INSERT INTO nitro_lotteries
                (guild_id, channel_id, message_id, host_id, nitro_type,
                 gift_code, note, status, ends_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'open',
                    NOW() + ($8::int * INTERVAL '1 minute'))
            RETURNING id, guild_id, channel_id, message_id, host_id,
                      nitro_type, note, status, winner_id, ends_at
            """,
            guild.id,
            setup_message.channel.id,
            setup_message.id,
            interaction.user.id,
            tier_key,
            code,
            note or None,
            minutes,
        )

        tier = _tier(tier_key)
        view = NitroLotteryView(self, int(row["id"]))
        try:
            await setup_message.edit(embed=self.render_open(row, 0), view=view)
        except discord.HTTPException:
            log.exception("[nitro] failed to publish lottery %s", row["id"])
        setup_view.stop()

        try:
            await interaction.response.send_message(
                f"Your {tier['emoji']} **{tier['label']}** lottery is live "
                f"in {setup_message.channel.mention}! The winner is drawn "
                f"{_fmt_left(minutes * 60)} from now, or whenever you press "
                f"**Draw Now**.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        log.info(
            "[nitro] lottery %s created host=%s type=%s mins=%d",
            row["id"],
            interaction.user.id,
            tier_key,
            minutes,
        )

    async def create_direct_gift(
        self,
        interaction: discord.Interaction,
        tier_key: str,
        code: str,
        note: str,
        setup_message: discord.Message,
        setup_view: NitroSetupView,
        target_id: int,
    ) -> None:
        """Persist a direct gift (kind='direct', already 'drawn') and DM the
        recipient. No entry phase and no draw -- the recipient is fixed."""
        db = self.bot.db
        guild = interaction.guild

        if not target_id or target_id == interaction.user.id:
            await interaction.response.send_message(
                "That recipient is no longer valid -- start over with "
                "`.nitro gift @user`.",
                ephemeral=True,
            )
            return

        row = await db.fetch_one(
            """
            INSERT INTO nitro_lotteries
                (guild_id, channel_id, message_id, host_id, nitro_type,
                 gift_code, note, kind, status, winner_id, ends_at, drawn_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7,
                    'direct', 'drawn', $8, NOW(), NOW())
            RETURNING id, host_id, winner_id, nitro_type, gift_code, note, kind
            """,
            guild.id,
            setup_message.channel.id,
            setup_message.id,
            interaction.user.id,
            tier_key,
            code,
            note or None,
            target_id,
        )

        tier = _tier(tier_key)
        view = NitroWinnerView(self, int(row["id"]))
        try:
            await setup_message.edit(embed=self.render_direct(row), view=view)
        except discord.HTTPException:
            log.exception(
                "[nitro] failed to publish direct gift %s", row["id"]
            )
        setup_view.stop()

        try:
            await interaction.response.send_message(
                f"Your {tier['emoji']} **{tier['label']}** gift was sent to "
                f"<@{target_id}> -- the code went straight to their DMs and "
                f"never touched the channel.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass

        # DM after responding so a slow DM never times out the interaction.
        await self._dm_winner(row, target_id)
        log.info(
            "[nitro] direct gift %s host=%s -> %s type=%s",
            row["id"],
            interaction.user.id,
            target_id,
            tier_key,
        )

    async def draw_lottery(self, lottery_id: int, *, reason: str) -> None:
        """Pick a random winner (or expire on no entries). The guarded
        UPDATE makes a host Draw-Now and the timer loop race-safe."""
        db = self.bot.db
        lot = await db.fetch_one(
            "SELECT id, guild_id, channel_id, message_id, host_id, "
            "nitro_type, gift_code, note, kind, status "
            "FROM nitro_lotteries WHERE id = $1",
            lottery_id,
        )
        if not lot or lot["status"] != "open":
            return

        entrants = await db.fetch_all(
            "SELECT user_id FROM nitro_lottery_entries WHERE lottery_id = $1",
            lottery_id,
        )
        entrant_ids = [int(r["user_id"]) for r in entrants]

        if not entrant_ids:
            claimed = await db.fetch_one(
                "UPDATE nitro_lotteries SET status = 'expired', "
                "drawn_at = NOW() WHERE id = $1 AND status = 'open' "
                "RETURNING id",
                lottery_id,
            )
            if claimed:
                await self._finalize_message(lot, status="expired")
                log.info(
                    "[nitro] lottery %s expired with no entries", lottery_id
                )
            return

        winner_id = random.choice(entrant_ids)
        claimed = await db.fetch_one(
            "UPDATE nitro_lotteries SET status = 'drawn', winner_id = $1, "
            "drawn_at = NOW() WHERE id = $2 AND status = 'open' RETURNING id",
            winner_id,
            lottery_id,
        )
        if not claimed:
            return  # the host's Draw-Now and the timer loop raced -- lost it

        await self._finalize_message(
            lot, status="drawn", winner_id=winner_id, count=len(entrant_ids)
        )
        await self._dm_winner(lot, winner_id)
        await self._dm_host(lot, winner_id, len(entrant_ids))
        log.info(
            "[nitro] lottery %s drawn (%s) winner=%s entrants=%d",
            lottery_id,
            reason,
            winner_id,
            len(entrant_ids),
        )

    async def _finalize_message(
        self,
        lot: dict,
        *,
        status: str,
        winner_id: int | None = None,
        count: int = 0,
    ) -> None:
        """Edit the public lottery message into its terminal state."""
        channel = self.bot.get_channel(int(lot["channel_id"]))
        msg_id = lot.get("message_id")
        if channel is None or not msg_id:
            return
        try:
            msg = await channel.fetch_message(int(msg_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        if status == "drawn":
            embed = self.render_drawn(lot, int(winner_id or 0), count)
            view: discord.ui.View | None = NitroWinnerView(self, int(lot["id"]))
        elif status == "expired":
            embed = self.render_expired(lot)
            view = None
        else:
            embed = self.render_cancelled(lot)
            view = None
        try:
            await msg.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass

    async def _dm_winner(self, lot: dict, winner_id: int) -> None:
        """DM the gift to the recipient. Handles both a lottery winner and
        a direct-gift recipient (kind == 'direct')."""
        tier = _tier(lot["nitro_type"])
        url = _safe_gift_url(lot["gift_code"])
        if url is None:
            # Should be unreachable -- _parse_gift only stores clean codes --
            # but the bot will not DM anything it cannot prove is a genuine
            # discord.gift link.
            log.error(
                "[nitro] lottery %s has a non-conforming gift_code; "
                "refusing to DM", lot.get("id"),
            )
            return
        if lot.get("kind") == "direct":
            title = f"{tier['emoji']} You received a {tier['label']} gift!"
            desc = (
                f"<@{lot['host_id']}> sent you a **{tier['label']}** gift "
                f"through Discoin. Redeem it below."
            )
        else:
            title = f"{tier['emoji']} You won a {tier['label']} gift!"
            desc = (
                f"You were drawn as the winner of a **{tier['label']}** "
                f"lottery hosted by <@{lot['host_id']}>. Redeem it below."
            )
        embed = (
            card(title, description=desc, color=C_SUCCESS)
            .field(
                "Claim your gift",
                f"[Redeem {tier['label']}]({url})\n`{url}`",
                False,
            )
            .footer(
                "Discoin relays the code but cannot verify it -- redeem it "
                "promptly and tell the sender if it does not work."
            )
            .build()
        )
        try:
            user = self.bot.get_user(winner_id) or await self.bot.fetch_user(
                winner_id
            )
            await user.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            log.info(
                "[nitro] could not DM recipient %s -- Reveal button covers it",
                winner_id,
            )

    async def _dm_host(self, lot: dict, winner_id: int, count: int) -> None:
        tier = _tier(lot["nitro_type"])
        try:
            host = self.bot.get_user(int(lot["host_id"])) or (
                await self.bot.fetch_user(int(lot["host_id"]))
            )
            await host.send(
                embed=card(
                    f"{tier['emoji']} Your {tier['label']} lottery drew a winner",
                    description=(
                        f"<@{winner_id}> won your **{tier['label']}** lottery "
                        f"out of **{count}** entrant(s). The gift link was "
                        f"sent straight to them."
                    ),
                    color=tier["color"],
                ).build()
            )
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass

    # ── embed renderers ───────────────────────────────────────────────────

    def render_open(self, lot: dict, count: int) -> discord.Embed:
        tier = _tier(lot["nitro_type"])
        ends_at = float(lot["ends_at"])
        remaining = ends_at - time.time()
        eb = (
            card(
                f"{tier['emoji']} {tier['label']} Lottery",
                description=(
                    f"A **{tier['label']}** gift is up for grabs. Click "
                    f"**Enter** below -- the winner is drawn **at random**, "
                    f"so there is no point racing anyone."
                ),
                color=tier["color"],
            )
            .field(
                "Prize", f"{tier['emoji']} **{tier['label']}** -- {tier['blurb']}", False
            )
            .field("Host", f"<@{lot['host_id']}>", True)
            .field("Entries", f"**{count}**", True)
            .field("Closes", f"{fmt_ts(ends_at)}  ({_fmt_left(remaining)})", True)
        )
        note = (lot.get("note") or "").strip()
        if note:
            eb.field("Note from host", note[:1000], False)
        eb.footer(
            f"Lottery #{lot['id']} -- gift code held privately by Discoin"
        )
        return eb.build()

    def render_drawn(
        self, lot: dict, winner_id: int, count: int
    ) -> discord.Embed:
        tier = _tier(lot["nitro_type"])
        return (
            card(
                f"{tier['emoji']} {tier['label']} Lottery -- Winner Drawn",
                description=(
                    f"Out of **{count}** entrant(s), <@{winner_id}> won the "
                    f"**{tier['label']}** gift! It was sent by DM -- the "
                    f"winner can also click **Reveal my gift** below."
                ),
                color=C_SUCCESS,
            )
            .field("Winner", f"<@{winner_id}>", True)
            .field("Prize", f"{tier['emoji']} **{tier['label']}**", True)
            .field("Entries", f"**{count}**", True)
            .footer(f"Lottery #{lot['id']} -- only the winner can reveal the code")
            .build()
        )

    def render_expired(self, lot: dict) -> discord.Embed:
        tier = _tier(lot["nitro_type"])
        return (
            card(
                f"{tier['emoji']} {tier['label']} Lottery -- No Entries",
                description=(
                    f"Nobody entered the **{tier['label']}** lottery in time, "
                    f"so there is no winner. The gift was never shared in "
                    f"chat and stays with the host."
                ),
                color=C_SUBTLE,
            )
            .footer(f"Lottery #{lot['id']}")
            .build()
        )

    def render_cancelled(self, lot: dict) -> discord.Embed:
        tier = _tier(lot["nitro_type"])
        return (
            card(
                f"{tier['emoji']} {tier['label']} Lottery -- Cancelled",
                description=(
                    f"The host cancelled this **{tier['label']}** lottery. "
                    f"The gift was never shared in chat."
                ),
                color=C_NEUTRAL,
            )
            .footer(f"Lottery #{lot['id']}")
            .build()
        )

    def render_direct(self, lot: dict) -> discord.Embed:
        tier = _tier(lot["nitro_type"])
        eb = (
            card(
                f"{tier['emoji']} {tier['label']} Gift",
                description=(
                    f"<@{lot['host_id']}> is gifting **{tier['label']}** to "
                    f"<@{lot['winner_id']}>! The code was sent privately by "
                    f"DM and never appeared in this channel. "
                    f"<@{lot['winner_id']}> can also click **Reveal my "
                    f"gift** below."
                ),
                color=tier["color"],
            )
            .field("From", f"<@{lot['host_id']}>", True)
            .field("To", f"<@{lot['winner_id']}>", True)
            .field("Prize", f"{tier['emoji']} **{tier['label']}**", True)
        )
        note = (lot.get("note") or "").strip()
        if note:
            eb.field("Note from sender", note[:1000], False)
        eb.footer(
            f"Gift #{lot['id']} -- only the recipient can reveal the code"
        )
        return eb.build()


async def setup(bot: Discoin) -> None:
    await bot.add_cog(NitroShare(bot))
