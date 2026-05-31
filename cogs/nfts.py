"""cogs/nfts.py - NFT collections, minting, marketplace, and token deployment.

Players can mint, collect, view, trade, list, and buy NFTs on PoS networks (ARC/DSC).
NFTs belong to collections with optional supply caps. Each NFT has a unique on-chain
token hash and belongs to a collection with a contract address  -  true non-fungible
tokens on the Discoin/Arcadia networks.

High-tier players (Protocol Dev+) can deploy custom tokens and NFT collections on PoS
networks, paying gas fees in the network's native coin.

Admin commands for collection management live in admin.py's nft subgroup.
"""
from __future__ import annotations

import logging
import math
import random

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

from core.config import Config
from constants.validators import NET_SHORT
from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import ensure_registered, guild_only, no_bots
from core.framework.scale import to_human, to_raw
from core.framework.ui import (
    C_AMBER, C_GOLD, C_PURPLE, C_SUCCESS, C_WARNING, ConfirmView,
    RARITY_SQUARE, fmt_ts,
)
from core.framework.fuzzy import suggest_subcommand

# Reverse map: short code -> long network name  (e.g. "arc" -> "Arcadia Network")
_SHORT_TO_LONG: dict[str, str] = {v: k for k, v in NET_SHORT.items()}

# PoS-only networks (token/NFT deployment is only allowed on PoS chains)
_POS_NETWORKS = {"ARC", "DSC"}
_POS_LONG = {"Arcadia Network", "Discoin Network"}

_DEFAULT_NFT_IMAGE = "https://placehold.co/400x400/1a1a2e/e94560?text=NFT"

_RARITIES = ["common", "uncommon", "rare", "epic", "legendary"]
_RARITY_WEIGHTS = [50, 25, 15, 8, 2]
_RARITY_EMOJI = RARITY_SQUARE


def _roll_rarity() -> str:
    return random.choices(_RARITIES, weights=_RARITY_WEIGHTS, k=1)[0]


def _fmt_gas(amount: float, symbol: str, emoji: str = "●") -> str:
    if amount < 0.0001:
        return f"{emoji} {amount:.10f} {symbol}"
    return f"{emoji} {amount:,.6f} {symbol}"


def _nft_image_url(collection_image: str, token_id: int, rarity: str) -> str:
    if collection_image:
        return collection_image
    color_map = {
        "common": "808080", "uncommon": "2ecc71", "rare": "3498db",
        "epic": "9b59b6", "legendary": "f1c40f",
    }
    bg = color_map.get(rarity, "808080")
    return f"https://placehold.co/400x400/{bg}/ffffff?text=%23{token_id}"


def _net_coin(network_short: str) -> str:
    net_long = _SHORT_TO_LONG.get(network_short.lower(), "")
    return Config.NETWORK_COINS.get(net_long, network_short.upper())


async def _to_usd(db, guild_id: int, amount: float, symbol: str) -> str:
    """Return a '(~$X.XX)' string for a coin amount, or '' if USD or unknown."""
    if symbol.upper() in ("USD", "USDC", "DSD"):
        return ""
    row = await db.get_price(symbol.upper(), guild_id)
    if row and row.get("price"):
        usd = amount * float(row["price"])
        return f" (~${usd:,.2f})"
    return ""


def _price_fmt(amount: float, symbol: str, usd_str: str = "") -> str:
    """Format a price like '10.0000 ARC (~$3.10)'."""
    return f"{amount:,.4f} {symbol}{usd_str}"


class NFTs(commands.Cog):
    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    # -- $nft group --

    @commands.hybrid_group(name="nft", aliases=["nfts"], invoke_without_command=True, with_app_command=False)
    @guild_only
    async def nft(self, ctx: DiscoContext) -> None:
        """NFT commands. Mint, collect, and trade NFTs on the blockchain."""
        if await suggest_subcommand(ctx, self.nft):
            return
        await ctx.send_group_help(self.nft, title="NFT Commands", color=C_PURPLE)

    # -- $nft collections --

    @nft.command(name="collections")
    @guild_only
    async def collections(self, ctx: DiscoContext) -> None:
        """View all NFT collections in this server."""
        cols = await ctx.db.get_collections(ctx.guild_id)
        if not cols:
            await ctx.reply_error("No NFT collections exist yet. Ask an admin to create one.")
            return

        embed = card("NFT Collections", color=C_PURPLE)
        for c in cols[:15]:
            supply_str = f"{c['minted_count']}/{c['max_supply']}" if c["max_supply"] else f"{c['minted_count']} minted"
            coin = c["mint_token"] or _net_coin(c["network"])
            _mp_h = to_human(int(c["mint_price"] or 0))
            price_str = f"{_mp_h:,.4f} {coin}" if _mp_h > 0 else "Free"
            contract = c.get("contract_address", "") or " - "
            embed.field(
                f"{c['symbol']} - {c['name']}",
                f"Network: {c['network']} | Supply: {supply_str} | Mint: {price_str}\nContract: `{contract}`",
                False,
            )
        embed.footer(f"Use {ctx.prefix}nft mint <symbol> to mint from a collection")
        await ctx.reply(embed=embed.build(), mention_author=False)

    # -- $nft view <identifier> [token_id] --
    # nft view SCAMPS       -> show collection overview
    # nft view SCAMPS 1     -> show specific NFT #1 from SCAMPS
    # nft view 42           -> show NFT by global ID (legacy compat)

    @nft.command(name="view", aliases=["info"])
    @guild_only
    async def nft_view(self, ctx: DiscoContext, identifier: str, token_id: int | None = None) -> None:
        """View a collection or a specific NFT.

        Usage:
          {prefix}nft view SCAMPS        -- view the SCAMPS collection
          {prefix}nft view SCAMPS 1      -- view NFT #1 from SCAMPS
          {prefix}nft view 42            -- view NFT with global ID 42
        """
        if token_id is not None:
            col = await ctx.db.get_collection_by_symbol(ctx.guild_id, identifier.upper())
            if not col:
                await ctx.reply_error(f"Collection `{identifier.upper()}` not found.")
                return
            nft = await ctx.db.get_nft_by_collection_token(col["id"], token_id)
            if not nft or nft["guild_id"] != ctx.guild_id:
                await ctx.reply_error(f"NFT #{token_id} not found in **{col['symbol']}**.")
                return
            await self._show_nft(ctx, nft)
            return

        col = await ctx.db.get_collection_by_symbol(ctx.guild_id, identifier.upper())
        if col:
            await self._show_collection(ctx, col)
            return

        try:
            nft_id = int(identifier)
        except (ValueError, TypeError):
            await ctx.reply_error(f"Collection `{identifier.upper()}` not found.")
            return

        nft = await ctx.db.get_nft(nft_id)
        if not nft or nft["guild_id"] != ctx.guild_id:
            await ctx.reply_error("NFT not found.")
            return
        await self._show_nft(ctx, nft)

    async def _show_collection(self, ctx: DiscoContext, col: dict) -> None:
        supply_str = f"{col['minted_count']}/{col['max_supply']}" if col["max_supply"] else f"{col['minted_count']} minted (unlimited)"
        coin = col["mint_token"] or _net_coin(col["network"])
        mint_price = to_human(int(col["mint_price"] or 0))
        if mint_price > 0:
            usd = await _to_usd(ctx.db, ctx.guild_id, mint_price, coin)
            price_str = _price_fmt(mint_price, coin, usd)
        else:
            price_str = "Free"

        embed = card(f"{col['symbol']} - {col['name']}", color=C_PURPLE)
        if col.get("description"):
            embed.description(col["description"])
        embed.field("Network", col["network"], True)
        embed.field("Supply", supply_str, True)
        embed.field("Mint Price", price_str, True)

        contract_addr = col.get("contract_address", "")
        if contract_addr:
            embed.field("Contract", f"`{contract_addr}`", True)
            embed.field("Type", "ERC-721", True)

        if col.get("image_url"):
            embed.thumbnail(col["image_url"])

        sales = await ctx.db.get_collection_sales(col["id"], limit=5)
        if sales:
            sales_lines = []
            for s in sales:
                emoji = _RARITY_EMOJI.get(s.get("rarity", "common"), "")
                s_coin = s.get("currency", coin)
                s_price = to_human(int(s["price"] or 0))
                s_usd = await _to_usd(ctx.db, ctx.guild_id, s_price, s_coin)
                date_str = fmt_ts(s["sold_at"]) if s.get("sold_at") else "?"
                sales_lines.append(
                    f"{emoji} #{s.get('token_id', '?')} {s.get('nft_name', '?')}  -  "
                    f"{_price_fmt(s_price, s_coin, s_usd)}  ({date_str})"
                )
            embed.field("Recent Sales", "\n".join(sales_lines), False)

        embed.footer(f"Collection ID: {col['id']} | Mint: {ctx.prefix}nft mint {col['symbol']}")
        await ctx.reply(embed=embed.build(), mention_author=False)

    async def _show_nft(self, ctx: DiscoContext, nft: dict) -> None:
        emoji = _RARITY_EMOJI.get(nft.get("rarity", "common"), "")
        col_net = nft.get("collection_network", nft.get("network", "?"))
        col_symbol = nft.get("collection_symbol", "?")

        embed = card(nft["name"], color=C_PURPLE)
        if nft.get("description"):
            embed.description(nft["description"])
        embed.field("Collection", f"{col_symbol} - {nft.get('collection_name', '?')}", True)
        embed.field("Rarity", f"{emoji} {nft.get('rarity', 'common').title()}", True)
        embed.field("Network", col_net, True)
        embed.field("Token ID", str(nft["token_id"]), True)

        owner = ctx.guild.get_member(nft["owner_id"])
        owner_name = owner.display_name if owner else f"User {nft['owner_id']}"
        embed.field("Owner", owner_name, True)

        # Blockchain identity
        token_hash = nft.get("token_hash", "")
        contract_addr = nft.get("collection_contract", "")
        if token_hash:
            embed.field("Token Hash", f"`{token_hash}`", True)
        if contract_addr:
            embed.field("Contract", f"`{contract_addr}`", True)

        if col_symbol and col_symbol != "?":
            contract = await ctx.db.get_token_contract(ctx.guild_id, f"NFT:{col_symbol}")
            if contract and any(contract.get(k) for k in ("max_supply", "burn_rate", "transfer_fee")):
                contract_parts = []
                if contract.get("max_supply"):
                    contract_parts.append(f"Max Supply: {contract['max_supply']:,.0f}")
                if contract.get("burn_rate"):
                    contract_parts.append(f"Burn: {contract['burn_rate']*100:.1f}%")
                if contract.get("transfer_fee"):
                    contract_parts.append(f"Transfer Fee: {contract['transfer_fee']*100:.1f}%")
                embed.field("Contract Details", " | ".join(contract_parts), False)

        listing = await ctx.db.get_listing(nft["id"])
        if listing:
            l_coin = listing.get("currency") or listing.get("mint_token") or _net_coin(col_net)
            l_price = to_human(int(listing["price"] or 0))
            l_usd = await _to_usd(ctx.db, ctx.guild_id, l_price, l_coin)
            embed.field("Listed For", _price_fmt(l_price, l_coin, l_usd), True)

        sales = await ctx.db.get_nft_sales(nft["id"], limit=5)
        if sales:
            sales_lines = []
            for s in sales:
                s_coin = s.get("currency", "?")
                s_price = to_human(int(s["price"] or 0))
                s_usd = await _to_usd(ctx.db, ctx.guild_id, s_price, s_coin)
                seller = ctx.guild.get_member(s.get("seller_id", 0))
                buyer = ctx.guild.get_member(s.get("buyer_id", 0))
                seller_name = seller.display_name if seller else f"#{s.get('seller_id', '?')}"
                buyer_name = buyer.display_name if buyer else f"#{s.get('buyer_id', '?')}"
                date_str = fmt_ts(s["sold_at"]) if s.get("sold_at") else "?"
                sales_lines.append(
                    f"{seller_name} -> {buyer_name}  "
                    f"{_price_fmt(s_price, s_coin, s_usd)}  ({date_str})"
                )
            embed.field("Sales History", "\n".join(sales_lines), False)

        img = nft.get("image_url") or _nft_image_url("", nft.get("token_id", 0), nft.get("rarity", "common"))
        embed.image(img)

        embed.footer(f"NFT ID: {nft['id']} | Minted by user {nft['minted_by']}")
        await ctx.reply(embed=embed.build(), mention_author=False)

    # -- $nft mint <collection_symbol> --

    @nft.command(name="mint")
    @guild_only
    @no_bots
    @ensure_registered
    async def mint(self, ctx: DiscoContext, *, collection_symbol: str) -> None:
        """Mint an NFT from a collection. Costs the collection's mint price + network gas.

        Usage: {prefix}nft mint <symbol>
        Example: {prefix}nft mint PUNKS
        """
        from cogs.validators import gas_fee_for_network

        col = await ctx.db.get_collection_by_symbol(ctx.guild_id, collection_symbol.upper())
        if not col:
            await ctx.reply_error(f"Collection `{collection_symbol.upper()}` not found. Use `{ctx.prefix}nft collections` to see available collections.")
            return

        if col["max_supply"] is not None and col["minted_count"] >= col["max_supply"]:
            await ctx.reply_error(f"**{col['name']}** is sold out! All {col['max_supply']} NFTs have been minted.")
            return

        mint_price_raw = int(col["mint_price"] or 0)
        mint_price = to_human(mint_price_raw)
        col_net_short = col["network"].upper()
        col_net_long = _SHORT_TO_LONG.get(col_net_short.lower(), "")
        mint_coin = col["mint_token"] or _net_coin(col_net_short)

        # Funds must be in DeFi wallet for NFT transactions
        if mint_price > 0:
            h = await ctx.db.get_wallet_holding(
                ctx.author.id, ctx.guild_id, col_net_short.lower(), mint_coin,
            )
            bal = h.h("amount") if h else 0.0
            if bal < mint_price:
                coin_cfg = Config.TOKENS.get(mint_coin, {})
                coin_em = coin_cfg.get("emoji", "●")
                await ctx.reply_error(
                    f"You need **{coin_em} {mint_price:,.4f} {mint_coin}** in your "
                    f"**{col_net_short}** DeFi wallet to mint. You have **{coin_em} {bal:,.4f}**.\n"
                    f"Move funds with `{ctx.prefix}defi deposit {col_net_short.lower()} {mint_coin} {mint_price}`."
                )
                return

        # Calculate gas fee on the collection's network
        gas_fee = 0.0
        gas_coin = ""
        gas_em = ""
        if col_net_long:
            active_v = [
                v for v in await ctx.db.get_pos_validators_for_network(ctx.guild_id, col_net_long)
                if v["is_active"]
            ]
            if active_v:
                gas_coin, gas_fee = await gas_fee_for_network(
                    ctx.db, ctx.guild_id, "contract_call", "medium", col_net_long,
                )
                gas_cfg = Config.TOKENS.get(gas_coin, {})
                gas_em = gas_cfg.get("emoji", "●")
                gas_h = await ctx.db.get_wallet_holding(
                    ctx.author.id, ctx.guild_id, col_net_short.lower(), gas_coin,
                )
                gas_bal = gas_h.h("amount") if gas_h else 0.0
                if gas_bal < gas_fee:
                    await ctx.reply_error(
                        f"Need **`{_fmt_gas(gas_fee, gas_coin, gas_em)}`** gas to mint on "
                        f"{col_net_short}. You have **`{_fmt_gas(gas_bal, gas_coin, gas_em)}`**."
                    )
                    return

        # Confirm BEFORE any state changes
        view = ConfirmView(ctx.author.id)
        coin_cfg = Config.TOKENS.get(mint_coin, {})
        coin_em = coin_cfg.get("emoji", "●")
        price_text = f"**{coin_em} {mint_price:,.4f} {mint_coin}**" if mint_price > 0 else "**Free**"
        gas_text = f" + **`{_fmt_gas(gas_fee, gas_coin, gas_em)}`** gas" if gas_fee > 0 else ""
        msg = await ctx.reply(
            f"Mint an NFT from **{col['name']}** ({col['symbol']}) for {price_text}{gas_text}?\n"
            f"Funds taken from your **{col_net_short}** DeFi wallet.",
            view=view, mention_author=False,
        )
        confirmed = await view.wait_result()
        if not confirmed:
            await msg.edit(content="Mint cancelled.", view=None)
            return

        # Everything after confirmation is atomic:
        # Deduct mint price from DeFi wallet
        if mint_price > 0:
            try:
                await ctx.db.update_wallet_holding(
                    ctx.author.id, ctx.guild_id, col_net_short.lower(), mint_coin, -mint_price_raw,
                )
            except ValueError:
                await msg.edit(content="Insufficient funds in DeFi wallet.", view=None)
                return

        # Deduct gas fee
        if gas_fee > 0:
            try:
                await ctx.db.update_wallet_holding(
                    ctx.author.id, ctx.guild_id, col_net_short.lower(), gas_coin, -to_raw(gas_fee),
                )
            except ValueError:
                # Refund mint price
                if mint_price > 0:
                    await ctx.db.update_wallet_holding(
                        ctx.author.id, ctx.guild_id, col_net_short.lower(), mint_coin, mint_price_raw,
                    )
                await msg.edit(content="Insufficient gas.", view=None)
                return

        # Roll rarity and mint  -  DB assigns token_id atomically
        rarity = _roll_rarity()
        image_url = _nft_image_url(col.get("image_url", ""), col["minted_count"] + 1, rarity)

        nft = await ctx.db.mint_nft(
            guild_id=ctx.guild_id,
            collection_id=col["id"],
            owner_id=ctx.author.id,
            name=f"{col['name']} #",  # placeholder, updated below
            description=col.get("description", ""),
            image_url=image_url,
            rarity=rarity,
        )

        if not nft:
            # Refund if mint failed (race condition on supply)
            if mint_price > 0:
                await ctx.db.update_wallet_holding(
                    ctx.author.id, ctx.guild_id, col_net_short.lower(), mint_coin, mint_price_raw,
                )
            if gas_fee > 0:
                await ctx.db.update_wallet_holding(
                    ctx.author.id, ctx.guild_id, col_net_short.lower(), gas_coin, to_raw(gas_fee),
                )
            await msg.edit(content="Mint failed  -  collection may be sold out.", view=None)
            return

        # Update name with the actual token_id assigned by the DB
        actual_token_id = nft["token_id"]
        nft_name = f"{col['name']} #{actual_token_id}"
        actual_image = _nft_image_url(col.get("image_url", ""), actual_token_id, rarity)
        await ctx.db.execute(
            "UPDATE nfts SET name = $1, image_url = $2 WHERE id = $3",
            nft_name, actual_image, nft["id"],
        )

        emoji = _RARITY_EMOJI.get(rarity, "")
        embed = card(f"Minted: {nft_name}", color=C_SUCCESS)
        embed.field("Collection", f"{col['symbol']} - {col['name']}", True)
        embed.field("Rarity", f"{emoji} {rarity.title()}", True)
        embed.field("Network", col["network"], True)
        embed.field("Token ID", str(actual_token_id), True)
        embed.field("Token Hash", f"`{nft.get('token_hash', '')}`", True)
        contract = col.get("contract_address", "")
        if contract:
            embed.field("Contract", f"`{contract}`", True)
        if gas_fee > 0:
            embed.field("Gas Paid", _fmt_gas(gas_fee, gas_coin, gas_em), True)
        if actual_image:
            embed.thumbnail(actual_image)
        embed.footer(f"View with {ctx.prefix}nft view {col['symbol']} {actual_token_id}")
        await msg.edit(content=None, embed=embed.build(), view=None)

    # -- $nft inventory / $nft my --

    @nft.command(name="inventory", aliases=["my", "inv"])
    @guild_only
    @no_bots
    @ensure_registered
    async def nft_inventory(self, ctx: DiscoContext) -> None:
        """View your NFT collection. Paginates if you own more than 10."""
        nfts = await ctx.db.get_user_nfts(ctx.author.id, ctx.guild_id)
        if not nfts:
            await ctx.reply_error(
                f"You don't own any NFTs yet. "
                f"Use `{ctx.prefix}nft collections` to browse and `{ctx.prefix}nft mint` to get started."
            )
            return

        page_size = 10
        total = len(nfts)
        chunks = [nfts[i:i + page_size] for i in range(0, total, page_size)]
        pages = []
        for page_idx, chunk in enumerate(chunks):
            b = card(
                f"{ctx.author.display_name}'s NFTs",
                color=C_PURPLE,
            )
            for n in chunk:
                emoji = _RARITY_EMOJI.get(n.get("rarity", "common"), "")
                col_sym = n.get("collection_symbol", "?")
                net = n.get("collection_network", n.get("network", "?"))
                listed_tag = ""
                b.field(
                    f"#{n['token_id']} - {n['name']}",
                    f"{emoji} {n.get('rarity', 'common').title()} | {col_sym} | {net}{listed_tag}",
                    True,
                )
            b.footer(
                f"Page {page_idx + 1}/{len(chunks)}  -  {total} NFT{'s' if total != 1 else ''}  |  "
                f"View: {ctx.prefix}nft view <symbol> <token_id>"
            )
            pages.append(b.build())

        if len(pages) == 1:
            await ctx.reply(embed=pages[0], mention_author=False)
        else:
            await ctx.paginate(pages)

    # -- $nft transfer @user <symbol> <token_id> --

    @nft.command(name="transfer", aliases=["send", "give"])
    @guild_only
    @no_bots
    @ensure_registered
    async def nft_transfer(self, ctx: DiscoContext, member: discord.Member, symbol: str, token_id: int) -> None:
        """Transfer an NFT to another player. Charges gas on the NFT's network.

        Usage: {prefix}nft transfer @user <symbol> <token_id>
        Example: {prefix}nft transfer @Alice TEST 1
        """
        from cogs.validators import gas_fee_for_network

        if member.id == ctx.author.id:
            await ctx.reply_error("You can't transfer to yourself.")
            return
        if member.bot:
            await ctx.reply_error("Can't transfer NFTs to bots.")
            return

        nft = await ctx.db.get_nft_by_symbol_and_token(ctx.guild_id, symbol, token_id)
        if not nft:
            await ctx.reply_error(
                f"NFT **{symbol.upper()} #{token_id}** not found.\n"
                f"Check your inventory with `{ctx.prefix}nft inventory`."
            )
            return
        if nft["owner_id"] != ctx.author.id:
            await ctx.reply_error("You don't own this NFT.")
            return

        recipient = await ctx.db.get_user(member.id, ctx.guild_id)
        if not recipient:
            await ctx.reply_error(f"{member.display_name} isn't registered yet.")
            return

        col_net = nft.get("collection_network", "")
        col_net_long = _SHORT_TO_LONG.get(col_net.lower(), "") if col_net else ""
        gas_fee = 0.0
        gas_coin = ""
        gas_em = ""
        if col_net_long:
            active_v = [
                v for v in await ctx.db.get_pos_validators_for_network(ctx.guild_id, col_net_long)
                if v["is_active"]
            ]
            if active_v:
                gas_coin, gas_fee = await gas_fee_for_network(
                    ctx.db, ctx.guild_id, "send", "medium", col_net_long,
                )
                gas_cfg = Config.TOKENS.get(gas_coin, {})
                gas_em = gas_cfg.get("emoji", "●")
                gas_h = await ctx.db.get_wallet_holding(
                    ctx.author.id, ctx.guild_id, col_net.lower(), gas_coin,
                )
                gas_bal = gas_h.h("amount") if gas_h else 0.0
                if gas_bal < gas_fee:
                    await ctx.reply_error(
                        f"Need **`{_fmt_gas(gas_fee, gas_coin, gas_em)}`** gas to transfer. "
                        f"You have **`{_fmt_gas(gas_bal, gas_coin, gas_em)}`**."
                    )
                    return

        nft_id = nft["id"]
        view = ConfirmView(ctx.author.id)
        gas_text = f"\nGas: **`{_fmt_gas(gas_fee, gas_coin, gas_em)}`**" if gas_fee > 0 else ""
        msg = await ctx.reply(
            f"Transfer **{nft['name']}** to **{member.display_name}**?{gas_text}",
            view=view, mention_author=False,
        )
        confirmed = await view.wait_result()
        if not confirmed:
            await msg.edit(content="Transfer cancelled.", view=None)
            return

        if gas_fee > 0:
            await ctx.db.update_wallet_holding(
                ctx.author.id, ctx.guild_id, col_net.lower(), gas_coin, -to_raw(gas_fee),
            )

        await ctx.db.transfer_nft(nft_id, member.id)

        result_text = f"Transferred **{nft['name']}** to **{member.display_name}**."
        if gas_fee > 0:
            result_text += f"\nGas: {_fmt_gas(gas_fee, gas_coin, gas_em)}"
        await msg.edit(content=result_text, view=None)

        # DM notification to recipient
        try:
            prefs = await ctx.db.get_user_prefs(member.id, ctx.guild_id)
            if prefs.get("dm_nft"):
                await member.send(
                    f"You received **{nft['name']}** from **{ctx.author.display_name}** "
                    f"in **{ctx.guild.name}**."
                )
        except (discord.Forbidden, discord.HTTPException):
            pass

    # -- $nft list <symbol> <token_id> <price> --

    @nft.command(name="list", aliases=["sell"])
    @guild_only
    @no_bots
    @ensure_registered
    async def nft_list(self, ctx: DiscoContext, symbol: str, token_id: int, price: float) -> None:
        """List an NFT for sale on the marketplace.

        Price is in the collection's network coin (ARC or DSC).
        If the NFT is already listed, updates the price.

        Usage: {prefix}nft list <symbol> <token_id> <price>
        Example: {prefix}nft list TEST 1 10.5
        """
        if price <= 0:
            await ctx.reply_error("Price must be greater than 0.")
            return
        if price > 1_000_000_000:
            await ctx.reply_error("That price is way too high.")
            return

        nft = await ctx.db.get_nft_by_symbol_and_token(ctx.guild_id, symbol, token_id)
        if not nft:
            await ctx.reply_error(
                f"NFT **{symbol.upper()} #{token_id}** not found.\n"
                f"Check your inventory with `{ctx.prefix}nft inventory`."
            )
            return
        if nft["owner_id"] != ctx.author.id:
            await ctx.reply_error("You don't own this NFT.")
            return

        nft_id = nft["id"]
        col_net = nft.get("collection_network", "ARC")
        coin = _net_coin(col_net)

        usd = await _to_usd(ctx.db, ctx.guild_id, price, coin)
        price_raw = to_raw(price)

        # Check if already listed
        already_listed = await ctx.db.is_listed(nft_id)
        if already_listed:
            # Update existing listing price
            await ctx.db.list_nft(ctx.guild_id, nft_id, ctx.author.id, price_raw, coin)
            embed = card("Listing Updated", color=C_AMBER)
            embed.field("NFT", nft["name"], True)
            embed.field("New Price", _price_fmt(price, coin, usd), True)
            embed.field("Network", col_net, True)
            embed.footer(f"Others can buy with {ctx.prefix}nft buy {symbol.upper()} {token_id}")
            await ctx.reply(embed=embed.build(), mention_author=False)
            return

        await ctx.db.list_nft(ctx.guild_id, nft_id, ctx.author.id, price_raw, coin)
        embed = card("NFT Listed", color=C_SUCCESS)
        embed.field("NFT", nft["name"], True)
        embed.field("Price", _price_fmt(price, coin, usd), True)
        embed.field("Network", col_net, True)
        embed.footer(f"Others can buy with {ctx.prefix}nft buy {symbol.upper()} {token_id}")
        await ctx.reply(embed=embed.build(), mention_author=False)

    # -- $nft unlist <symbol> <token_id> --

    @nft.command(name="unlist", aliases=["delist"])
    @guild_only
    @no_bots
    @ensure_registered
    async def nft_unlist(self, ctx: DiscoContext, symbol: str, token_id: int) -> None:
        """Remove your NFT listing from the marketplace.

        Usage: {prefix}nft unlist <symbol> <token_id>
        Example: {prefix}nft unlist TEST 1
        """
        nft = await ctx.db.get_nft_by_symbol_and_token(ctx.guild_id, symbol, token_id)
        if not nft:
            await ctx.reply_error(
                f"NFT **{symbol.upper()} #{token_id}** not found.\n"
                f"Check your inventory with `{ctx.prefix}nft inventory`."
            )
            return
        if nft["owner_id"] != ctx.author.id:
            await ctx.reply_error("You don't own this NFT.")
            return

        removed = await ctx.db.unlist_nft(nft["id"], ctx.author.id)
        if not removed:
            await ctx.reply_error("That NFT isn't currently listed.")
            return
        await ctx.reply_success("Listing removed.")

    # -- $nft market --

    @nft.command(name="market", aliases=["marketplace", "browse"])
    @guild_only
    async def nft_market(self, ctx: DiscoContext) -> None:
        """Browse NFTs currently listed for sale."""
        listings = await ctx.db.get_listings(ctx.guild_id, limit=20)
        if not listings:
            await ctx.reply_error("No NFTs are listed for sale right now.")
            return

        embed = card("NFT Marketplace", color=C_GOLD)
        for l in listings:
            emoji = _RARITY_EMOJI.get(l.get("rarity", "common"), "")
            coin = l.get("currency", _net_coin(l.get("collection_network", "ARC")))
            col_sym = l.get("collection_symbol", "?")
            tid = l.get("token_id", "?")
            l_price = to_human(int(l["price"] or 0))
            usd = await _to_usd(ctx.db, ctx.guild_id, l_price, coin)
            embed.field(
                f"{col_sym} #{tid} - {l.get('nft_name', '?')}",
                f"{emoji} {l.get('rarity', 'common').title()} | {_price_fmt(l_price, coin, usd)}",
                True,
            )
        embed.footer(f"Buy with {ctx.prefix}nft buy <symbol> <token_id>")
        await ctx.reply(embed=embed.build(), mention_author=False)

    # -- $nft buy <symbol> <token_id> --

    @nft.command(name="buy", aliases=["purchase"])
    @guild_only
    @no_bots
    @ensure_registered
    async def nft_buy(self, ctx: DiscoContext, symbol: str, token_id: int) -> None:
        """Buy a listed NFT from the marketplace.

        Charges gas on the NFT's network. Payment is from your DeFi wallet.

        Usage: {prefix}nft buy <symbol> <token_id>
        Example: {prefix}nft buy TEST 1
        """
        from cogs.validators import gas_fee_for_network

        nft = await ctx.db.get_nft_by_symbol_and_token(ctx.guild_id, symbol, token_id)
        if not nft:
            await ctx.reply_error(
                f"NFT **{symbol.upper()} #{token_id}** not found.\n"
                f"Browse the market with `{ctx.prefix}nft market`."
            )
            return

        nft_id = nft["id"]
        listing = await ctx.db.get_listing(nft_id)
        if not listing:
            await ctx.reply_error(
                f"**{nft['name']}** isn't listed for sale.\n"
                f"Browse available listings with `{ctx.prefix}nft market`."
            )
            return
        if listing["seller_id"] == ctx.author.id:
            await ctx.reply_error("You can't buy your own NFT.")
            return

        price_raw = int(listing["price"] or 0)
        price = to_human(price_raw)
        currency = listing.get("currency", "ARC")
        col_net = listing.get("collection_network", "ARC")
        col_net_long = _SHORT_TO_LONG.get(col_net.lower(), "") if col_net else ""

        # Funds must be in DeFi wallet
        h = await ctx.db.get_wallet_holding(
            ctx.author.id, ctx.guild_id, col_net.lower(), currency,
        )
        bal = h.h("amount") if h else 0.0
        if bal < price:
            coin_cfg = Config.TOKENS.get(currency, {})
            coin_em = coin_cfg.get("emoji", "●")
            await ctx.reply_error(
                f"You need **{coin_em} {price:,.4f} {currency}** in your "
                f"**{col_net}** DeFi wallet. You have **{coin_em} {bal:,.4f}**.\n"
                f"Move funds with `{ctx.prefix}defi deposit {col_net.lower()} {currency} {price}`."
            )
            return

        # Calculate gas fee on the NFT's network
        gas_fee = 0.0
        gas_coin = ""
        gas_em = ""
        if col_net_long:
            active_v = [
                v for v in await ctx.db.get_pos_validators_for_network(ctx.guild_id, col_net_long)
                if v["is_active"]
            ]
            if active_v:
                gas_coin, gas_fee = await gas_fee_for_network(
                    ctx.db, ctx.guild_id, "send", "medium", col_net_long,
                )
                gas_cfg = Config.TOKENS.get(gas_coin, {})
                gas_em = gas_cfg.get("emoji", "●")
                gas_h = await ctx.db.get_wallet_holding(
                    ctx.author.id, ctx.guild_id, col_net.lower(), gas_coin,
                )
                gas_bal = gas_h.h("amount") if gas_h else 0.0
                if gas_bal < gas_fee:
                    await ctx.reply_error(
                        f"Need **`{_fmt_gas(gas_fee, gas_coin, gas_em)}`** gas to buy on "
                        f"{col_net}. You have **`{_fmt_gas(gas_bal, gas_coin, gas_em)}`**."
                    )
                    return

        buy_usd = await _to_usd(ctx.db, ctx.guild_id, price, currency)
        view = ConfirmView(ctx.author.id)
        gas_text = f"\nGas: **`{_fmt_gas(gas_fee, gas_coin, gas_em)}`**" if gas_fee > 0 else ""
        msg = await ctx.reply(
            f"Buy **{listing.get('nft_name', nft['name'])}** for "
            f"**{_price_fmt(price, currency, buy_usd)}**?{gas_text}\n"
            f"Funds taken from your **{col_net}** DeFi wallet.",
            view=view, mention_author=False,
        )
        confirmed = await view.wait_result()
        if not confirmed:
            await msg.edit(content="Purchase cancelled.", view=None)
            return

        # Execute purchase atomically
        result = await ctx.db.buy_nft(nft_id, ctx.author.id, price_raw, currency)
        if not result:
            await msg.edit(content="Someone else bought it first. Listing is gone.", view=None)
            return

        seller_id = result["seller_id"]
        try:
            # Deduct from buyer's DeFi wallet
            await ctx.db.update_wallet_holding(
                ctx.author.id, ctx.guild_id, col_net.lower(), currency, -price_raw,
            )
            # Pay seller in DeFi wallet
            await ctx.db.update_wallet_holding(
                seller_id, ctx.guild_id, col_net.lower(), currency, price_raw,
            )
            # Deduct gas
            if gas_fee > 0:
                await ctx.db.update_wallet_holding(
                    ctx.author.id, ctx.guild_id, col_net.lower(), gas_coin, -to_raw(gas_fee),
                )
        except Exception:
            # Roll back: return NFT to seller
            await ctx.db.transfer_nft(nft_id, seller_id)
            await msg.edit(
                content="Purchase failed  -  your balance changed during the transaction. NFT returned to seller.",
                view=None,
            )
            return

        embed = card("NFT Purchased!", color=C_SUCCESS)
        embed.field("NFT", listing.get("nft_name", nft["name"]), True)
        embed.field("Price Paid", _price_fmt(price, currency, buy_usd), True)
        embed.field("Collection", listing.get("collection_symbol", "?"), True)
        if gas_fee > 0:
            embed.field("Gas Paid", _fmt_gas(gas_fee, gas_coin, gas_em), True)
        await msg.edit(content=None, embed=embed.build(), view=None)

        # DM notification to seller
        try:
            prefs = await ctx.db.get_user_prefs(seller_id, ctx.guild_id)
            if prefs.get("dm_nft"):
                seller_member = ctx.guild.get_member(seller_id)
                if seller_member:
                    await seller_member.send(
                        f"Your NFT **{listing.get('nft_name', f'#{nft_id}')}** was sold to "
                        f"**{ctx.author.display_name}** for **{price:,.4f} {currency}** "
                        f"in **{ctx.guild.name}**."
                    )
        except (discord.Forbidden, discord.HTTPException):
            pass

    # -- $nft history <symbol> <token_id> --

    @nft.command(name="history", aliases=["sales"])
    @guild_only
    async def nft_history(self, ctx: DiscoContext, symbol: str, token_id: int) -> None:
        """View the sale history of a specific NFT.

        Usage: {prefix}nft history <symbol> <token_id>
        Example: {prefix}nft history TEST 1
        """
        nft = await ctx.db.get_nft_by_symbol_and_token(ctx.guild_id, symbol, token_id)
        if not nft:
            await ctx.reply_error(f"NFT **{symbol.upper()} #{token_id}** not found.")
            return

        sales = await ctx.db.get_nft_sales(nft["id"], limit=15)
        if not sales:
            await ctx.reply_error(f"**{nft['name']}** has no sale history yet.")
            return

        embed = card(f"Sale History: {nft['name']}", color=C_GOLD)
        for s in sales:
            buyer = ctx.guild.get_member(s.get("buyer_id", 0))
            seller = ctx.guild.get_member(s.get("seller_id", 0))
            buyer_name = buyer.display_name if buyer else f"User {s.get('buyer_id', '?')}"
            seller_name = seller.display_name if seller else f"User {s.get('seller_id', '?')}"
            s_coin = s.get("currency", "?")
            s_price = to_human(int(s["price"] or 0))
            s_usd = await _to_usd(ctx.db, ctx.guild_id, s_price, s_coin)
            embed.field(
                _price_fmt(s_price, s_coin, s_usd),
                f"{seller_name} → {buyer_name}",
                True,
            )
        embed.footer(f"Token Hash: {nft.get('token_hash', '')}")
        await ctx.reply(embed=embed.build(), mention_author=False)

    # ─── Player NFT Collection Deployment ─────────────────────────────────────

    @nft.command(name="deploy", aliases=["create"])
    @guild_only
    @no_bots
    @ensure_registered
    async def nft_deploy(
        self, ctx: DiscoContext, symbol: str = None, name: str = None, network: str = None,
        mint_price: float = None, max_supply: int = None,
    ) -> None:
        """Deploy a new NFT collection on a PoS network.

        Requires Protocol Dev or Exploiter tier. Charges deployment gas.
        Mint price is denominated in the network's native coin.

        Usage: {prefix}nft deploy <symbol> <name> <network> <mint_price> [max_supply]
        Example: {prefix}nft deploy PUNKS "Cool Punks" ARC 0.05 100

        Arguments:
          symbol     - Short identifier for your collection (e.g. PUNKS)
          name       - Display name (use quotes for multi-word names)
          network    - ARC or DSC (PoS networks only)
          mint_price - Cost to mint one NFT (in network's native coin)
          max_supply - Optional max number of NFTs (omit for unlimited)
        """
        from cogs.validators import gas_fee_for_network

        # Show usage if missing required args
        if not symbol or not name or not network or mint_price is None:
            usage = (
                "**Deploy a new NFT collection on a PoS network.**\n"
                "```\n"
                f"{ctx.prefix}nft deploy <symbol> <name> <network> <mint_price> [max_supply]\n"
                "```\n"
                "**Arguments:**\n"
                "- `symbol`  -  Short identifier (e.g. PUNKS, max 10 chars)\n"
                "- `name`  -  Display name (use quotes for multi-word: \"Cool Punks\")\n"
                "- `network`  -  ARC or DSC (PoS networks only)\n"
                "- `mint_price`  -  Cost to mint in the network's native coin\n"
                "- `max_supply`  -  Optional max NFTs (omit for unlimited)\n\n"
                "**Example:**\n"
                f"```\n{ctx.prefix}nft deploy PUNKS \"Cool Punks\" ARC 0.05 100\n```\n"
                "**Requirements:** Protocol Dev or Exploiter job tier\n"
                "**Cost:** Deployment gas in the network's native coin\n"
                "Each collection gets a unique ERC-721 contract address on the blockchain."
            )
            await ctx.reply(usage, mention_author=False)
            return

        job = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        job_cfg = Config.JOBS.get(job["job_id"], {})
        if not job_cfg.get("perks", {}).get("can_deploy_token"):
            await ctx.reply_error(
                "You need **Protocol Dev** or **Exploiter** tier to deploy NFT collections. "
                f"Check your tier with `{ctx.prefix}job`."
            )
            return

        symbol = symbol.upper()
        network = network.upper()

        if network not in _POS_NETWORKS:
            await ctx.reply_error(
                f"NFT collections can only be deployed on PoS networks: "
                f"**{', '.join(sorted(_POS_NETWORKS))}**."
            )
            return

        if mint_price < 0:
            await ctx.reply_error("Mint price can't be negative.")
            return
        if max_supply is not None and max_supply < 1:
            await ctx.reply_error("Max supply must be at least 1.")
            return
        if len(symbol) > 10:
            await ctx.reply_error("Symbol must be 10 characters or fewer.")
            return
        if len(name) > 50:
            await ctx.reply_error("Collection name must be 50 characters or fewer.")
            return

        existing = await ctx.db.get_collection_by_symbol(ctx.guild_id, symbol)
        if existing:
            await ctx.reply_error(f"A collection with symbol `{symbol}` already exists.")
            return

        net_long = _SHORT_TO_LONG.get(network.lower(), "")
        if not net_long:
            await ctx.reply_error(f"Unknown network `{network}`.")
            return

        gas_coin = Config.NETWORK_COINS.get(net_long, "")
        gas_fee = 0.0
        gas_em = ""

        active_v = [
            v for v in await ctx.db.get_pos_validators_for_network(ctx.guild_id, net_long)
            if v["is_active"]
        ]
        if active_v:
            gas_coin, gas_fee = await gas_fee_for_network(
                ctx.db, ctx.guild_id, "contract_deploy", "medium", net_long,
            )
        else:
            gas_coin = Config.NETWORK_COINS.get(net_long, "")

        gas_cfg = Config.TOKENS.get(gas_coin, {})
        gas_em = gas_cfg.get("emoji", "●")

        if gas_fee > 0:
            gas_h = await ctx.db.get_wallet_holding(
                ctx.author.id, ctx.guild_id, network.lower(), gas_coin,
            )
            gas_bal = gas_h.h("amount") if gas_h else 0.0
            if gas_bal < gas_fee:
                await ctx.reply_error(
                    f"Deploying a collection costs **`{_fmt_gas(gas_fee, gas_coin, gas_em)}`** gas. "
                    f"You have **`{_fmt_gas(gas_bal, gas_coin, gas_em)}`**."
                )
                return

        view = ConfirmView(ctx.author.id)
        supply_str = str(max_supply) if max_supply else "Unlimited"
        gas_text = f"\nGas: **`{_fmt_gas(gas_fee, gas_coin, gas_em)}`**" if gas_fee > 0 else ""
        msg = await ctx.reply(
            f"Deploy NFT collection **{name}** (`{symbol}`) on **{network}**?\n"
            f"Mint Price: **{mint_price:,.4f} {gas_coin}** | Supply: **{supply_str}**"
            f"{gas_text}",
            view=view, mention_author=False,
        )
        confirmed = await view.wait_result()
        if not confirmed:
            await msg.edit(content="Deployment cancelled.", view=None)
            return

        if gas_fee > 0:
            await ctx.db.update_wallet_holding(
                ctx.author.id, ctx.guild_id, network.lower(), gas_coin, -to_raw(gas_fee),
            )

        col = await ctx.db.create_collection(
            guild_id=ctx.guild_id,
            name=name,
            symbol=symbol,
            network=network,
            description=f"Player-deployed collection by {ctx.author.display_name}",
            image_url="",
            max_supply=max_supply,
            mint_price=to_raw(mint_price),
            mint_token=gas_coin,
            creator_id=ctx.author.id,
        )

        if not col:
            if gas_fee > 0:
                await ctx.db.update_wallet_holding(
                    ctx.author.id, ctx.guild_id, network.lower(), gas_coin, to_raw(gas_fee),
                )
            await msg.edit(content="Collection creation failed.", view=None)
            return

        contract_data = {"type": "ERC-721", "network": network, "deployer": ctx.author.id}
        if max_supply:
            contract_data["max_supply"] = max_supply
        await ctx.db.set_token_contract(ctx.guild_id, f"NFT:{symbol}", contract_data)

        embed = card("NFT Collection Deployed", color=C_SUCCESS)
        embed.field("Name", name, True)
        embed.field("Symbol", symbol, True)
        embed.field("Network", network, True)
        embed.field("Mint Price", f"{mint_price:,.4f} {gas_coin}", True)
        embed.field("Max Supply", supply_str, True)
        embed.field("Contract", "ERC-721", True)
        contract_addr = col.get("contract_address", "")
        if contract_addr:
            embed.field("Address", f"`{contract_addr}`", True)
        if gas_fee > 0:
            embed.field("Gas Paid", _fmt_gas(gas_fee, gas_coin, gas_em), True)
        embed.footer(f"Collection ID: {col['id']} | Players can mint with {ctx.prefix}nft mint {symbol}")
        await msg.edit(content=None, embed=embed.build(), view=None)

    # ─── Error handler for missing/bad arguments ──────────────────────────────

    @nft.error
    async def nft_error(self, ctx: DiscoContext, error: commands.CommandError) -> None:
        """Catch missing argument errors and show helpful usage info."""
        if isinstance(error, commands.MissingRequiredArgument):
            cmd = ctx.command
            if cmd:
                usage_map = {
                    "list": (
                        f"**List an NFT for sale on the marketplace.**\n"
                        f"```\n{ctx.prefix}nft list <symbol> <token_id> <price>\n```\n"
                        f"**Example:** `{ctx.prefix}nft list TEST 1 10.5`\n"
                        f"Price is in the collection's network coin (ARC or DSC)."
                    ),
                    "buy": (
                        f"**Buy a listed NFT from the marketplace.**\n"
                        f"```\n{ctx.prefix}nft buy <symbol> <token_id>\n```\n"
                        f"**Example:** `{ctx.prefix}nft buy TEST 1`\n"
                        f"Browse listings with `{ctx.prefix}nft market`."
                    ),
                    "unlist": (
                        f"**Remove your NFT listing from the marketplace.**\n"
                        f"```\n{ctx.prefix}nft unlist <symbol> <token_id>\n```\n"
                        f"**Example:** `{ctx.prefix}nft unlist TEST 1`"
                    ),
                    "transfer": (
                        f"**Transfer an NFT to another player.**\n"
                        f"```\n{ctx.prefix}nft transfer @user <symbol> <token_id>\n```\n"
                        f"**Example:** `{ctx.prefix}nft transfer @Alice TEST 1`"
                    ),
                    "view": (
                        f"**View a collection or specific NFT.**\n"
                        f"```\n{ctx.prefix}nft view <symbol> [token_id]\n```\n"
                        f"**Examples:**\n"
                        f"  `{ctx.prefix}nft view TEST`  -  view the TEST collection\n"
                        f"  `{ctx.prefix}nft view TEST 1`  -  view NFT #1 from TEST"
                    ),
                    "mint": (
                        f"**Mint an NFT from a collection.**\n"
                        f"```\n{ctx.prefix}nft mint <symbol>\n```\n"
                        f"**Example:** `{ctx.prefix}nft mint TEST`\n"
                        f"Browse collections with `{ctx.prefix}nft collections`."
                    ),
                    "deploy": (
                        f"**Deploy a new NFT collection.**\n"
                        f"```\n{ctx.prefix}nft deploy <symbol> <name> <network> <mint_price> [max_supply]\n```\n"
                        f"**Example:** `{ctx.prefix}nft deploy PUNKS \"Cool Punks\" ARC 0.05 100`\n"
                        f"Requires Protocol Dev or Exploiter tier."
                    ),
                    "history": (
                        f"**View the sale history of an NFT.**\n"
                        f"```\n{ctx.prefix}nft history <symbol> <token_id>\n```\n"
                        f"**Example:** `{ctx.prefix}nft history TEST 1`"
                    ),
                }
                usage = usage_map.get(cmd.name)
                if usage:
                    embed = card("Missing Arguments", color=C_WARNING)
                    embed.description(usage)
                    await ctx.reply(embed=embed.build(), mention_author=False)
                    ctx.handled = True
                    return
            # Fallback: show the parameter name
            await ctx.reply_error(
                f"`{error.param.name}` is a required argument.\n"
                f"Use `{ctx.prefix}help nft {ctx.command.name if ctx.command else ''}` for usage info."
            )
            ctx.handled = True
        elif isinstance(error, commands.BadArgument):
            await ctx.reply_error(
                f"Invalid argument: {error}\n"
                f"Use `{ctx.prefix}help nft {ctx.command.name if ctx.command else ''}` for usage info."
            )
            ctx.handled = True


# ═══════════════════════════════════════════════════════════════════════════════
# Token Deployment  -  player-facing token creation for Protocol Dev+ tiers
# ═══════════════════════════════════════════════════════════════════════════════


class TokenDeploy(commands.Cog):
    """Player-facing token deployment on PoS networks.

    Protocol Dev and Exploiter tier players can deploy custom tokens with
    burn rates, transfer fees, max supply, and auto-seeded liquidity pools  - 
    just like real ERC-20 token deployments.
    """

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    @commands.hybrid_group(name="token", invoke_without_command=True, with_app_command=False)
    @guild_only
    async def token(self, ctx: DiscoContext) -> None:
        """Token commands. Deploy and manage custom tokens."""
        if await suggest_subcommand(ctx, self.token):
            return
        await ctx.send_group_help(self.token, title="Token Commands", color=C_GOLD)

    @token.command(name="deploy", aliases=["create"])
    @guild_only
    @no_bots
    @ensure_registered
    async def token_deploy(self, ctx: DiscoContext, *, raw: str = "") -> None:
        """Deploy a custom token on a PoS network.

        Requires Protocol Dev or Exploiter tier. Charges deployment gas in the
        network's native coin. Auto-seeds a liquidity pool with the network's
        stablecoin.

        Usage:
          {prefix}token deploy symbol=MYTKN name="My Token" emoji=🔥 network=ARC price=2.50
        Optional:
          vol=0.05  burn_rate=0.01  fee=0.005  max_supply=1000000  supply=500000

        Arguments:
          symbol   - Short identifier (required, max 10 chars)
          name     - Display name (required, use quotes for multi-word)
          emoji    - Token emoji (optional, defaults to ●)
          network  - ARC or DSC (required, PoS only)
          price    - Starting price in USD (required)
          vol      - Daily volatility (optional, default 5%)
          burn_rate - % burned per transfer (optional)
          fee      - % transfer fee (optional)
          max_supply - Hard supply cap (optional)
          supply   - Initial circulating supply (optional)
        """
        import re as _re
        from cogs.validators import gas_fee_for_network

        job = await ctx.db.get_user_job(ctx.author.id, ctx.guild_id)
        job_cfg = Config.JOBS.get(job["job_id"], {})
        if not job_cfg.get("perks", {}).get("can_deploy_token"):
            await ctx.reply_error(
                "You need **Protocol Dev** or **Exploiter** tier to deploy tokens. "
                f"Check your tier with `{ctx.prefix}job`."
            )
            return

        if not raw.strip():
            usage = (
                "**Deploy a custom token on a PoS network.**\n"
                "```\n"
                f"{ctx.prefix}token deploy symbol=MYTKN name=\"My Token\" emoji=🔥 network=ARC price=2.50\n"
                "```\n"
                "**Required:** `symbol` `name` `network` `price`\n"
                "**Optional:** `emoji` (default ●) · `vol` (daily volatility, default 5%) · `burn_rate` (% burned per transfer) · "
                "`fee` (% transfer fee) · `max_supply` · `supply` (initial circulating)\n\n"
                "**Networks:** ARC, DSC (PoS only  -  no PoW networks)\n"
                "**Cost:** Deployment gas in the network's native coin\n"
                "Two pools are auto-seeded:\n"
                "  - TOKEN / network-stablecoin -- standard tradeable pair\n"
                "  - TOKEN / MOON -- bidirectional swappable, opts the deploy into the Moon Network economy\n"
                "Each token gets a unique ERC-20 contract address on the blockchain."
            )
            await ctx.reply(usage, mention_author=False)
            return

        kv: dict[str, str] = {}
        flat = raw.replace("\n", " ")
        for m in _re.finditer(r'(\w+)=(?:"([^"]*)"|([\S]+))', flat):
            key = m.group(1).lower()
            val = m.group(2) if m.group(2) is not None else m.group(3)
            kv[key] = val

        sym = kv.get("symbol", "").upper()
        name = kv.get("name", "")
        emoji = kv.get("emoji", "●")
        net_raw = kv.get("network", "").upper()

        missing = []
        if not sym:
            missing.append("`symbol`")
        if not name:
            missing.append("`name`")
        if not net_raw:
            missing.append("`network`")
        if "price" not in kv:
            missing.append("`price`")

        if missing:
            await ctx.reply_error(
                f"Missing required keys: {', '.join(missing)}\n\n"
                f"**Usage:**\n```\n{ctx.prefix}token deploy symbol=MYTKN name=\"My Token\" network=ARC price=2.50\n```"
            )
            return

        if net_raw not in _POS_NETWORKS:
            await ctx.reply_error(
                f"Tokens can only be deployed on PoS networks: "
                f"**{', '.join(sorted(_POS_NETWORKS))}**. PoW networks (MTA, SUN) don't support token contracts."
            )
            return

        net_long = _SHORT_TO_LONG.get(net_raw.lower(), "")
        if not net_long:
            await ctx.reply_error(f"Unknown network `{net_raw}`.")
            return

        stablecoin = Config.NETWORK_STABLECOIN.get(net_long)
        if not stablecoin:
            await ctx.reply_error(f"Network **{net_raw}** has no stablecoin for liquidity pool seeding.")
            return

        try:
            start_price = float(kv.get("price", "0"))
            daily_vol = float(kv.get("vol", "0.05"))
            # Default to a 100M cap when the deployer omits max_supply -- the
            # mint chokepoint in database/users.py refuses to mint past this,
            # so an uncapped deploy would silently bypass tokenomics rules.
            max_supply = float(kv.get("max_supply", "100000000"))
            initial_supply = float(kv.get("initial_supply", kv.get("supply", "0")))
            # Default 0.5% burn so even cheap player deploys are deflationary
            # on swap fees -- matches the burn rate used by DFUN / GBC.
            burn_rate = float(kv.get("burn_rate", "0.005"))
            fee_rate = float(kv.get("fee", "0"))
        except ValueError as exc:
            await ctx.reply_error(f"Number parsing error: {exc}")
            return

        if len(sym) > 10:
            await ctx.reply_error("Symbol must be 10 characters or fewer.")
            return
        if sym == "ALL" or sym.isdigit():
            await ctx.reply_error("Invalid symbol.")
            return
        if len(name) > 50:
            await ctx.reply_error("Token name must be 50 characters or fewer.")
            return
        if not math.isfinite(start_price) or start_price <= 0:
            await ctx.reply_error("Price must be a positive number.")
            return
        if not math.isfinite(daily_vol) or daily_vol < 0:
            await ctx.reply_error("Volatility must be non-negative.")
            return
        if not math.isfinite(max_supply) or max_supply <= 0:
            await ctx.reply_error(
                "Max supply must be a positive number. Example: `max_supply=100000000` (100M)."
            )
            return
        if burn_rate < 0 or burn_rate > 0.5:
            await ctx.reply_error("Burn rate must be between 0 and 0.5 (50%).")
            return
        if fee_rate < 0 or fee_rate > 0.5:
            await ctx.reply_error("Transfer fee must be between 0 and 0.5 (50%).")
            return

        existing_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        if sym in existing_tokens:
            await ctx.reply_error(f"Token `{sym}` already exists on this server.")
            return

        gas_coin = Config.NETWORK_COINS.get(net_long, "")
        gas_fee = 0.0
        gas_em = ""

        active_v = [
            v for v in await ctx.db.get_pos_validators_for_network(ctx.guild_id, net_long)
            if v["is_active"]
        ]
        if active_v:
            gas_coin, gas_fee = await gas_fee_for_network(
                ctx.db, ctx.guild_id, "contract_deploy", "medium", net_long,
            )

        gas_cfg = Config.TOKENS.get(gas_coin, {})
        gas_em = gas_cfg.get("emoji", "●")

        if gas_fee > 0:
            gas_h = await ctx.db.get_wallet_holding(
                ctx.author.id, ctx.guild_id, net_raw.lower(), gas_coin,
            )
            gas_bal = gas_h.h("amount") if gas_h else 0.0
            if gas_bal < gas_fee:
                await ctx.reply_error(
                    f"Deploying a token costs **`{_fmt_gas(gas_fee, gas_coin, gas_em)}`** gas. "
                    f"You have **`{_fmt_gas(gas_bal, gas_coin, gas_em)}`**."
                )
                return

        view = ConfirmView(ctx.author.id)
        details = (
            f"Deploy token **{emoji} {sym}** ({name}) on **{net_raw}**?\n"
            f"Price: **${start_price:,.4f}** | Vol: **{daily_vol*100:.1f}%/day**"
        )
        if max_supply > 0:
            details += f" | Max Supply: **{max_supply:,.0f}**"
        if burn_rate > 0:
            details += f"\nBurn Rate: **{burn_rate*100:.1f}%**"
        if fee_rate > 0:
            details += f" | Transfer Fee: **{fee_rate*100:.1f}%**"
        if gas_fee > 0:
            details += f"\nGas: **`{_fmt_gas(gas_fee, gas_coin, gas_em)}`**"
        details += (
            f"\n\nTwo pools will be auto-seeded:\n"
            f"  - **{sym}/{stablecoin}** -- standard tradeable pair\n"
            f"  - **{sym}/MOON** -- bidirectional swappable (Moon Network on-ramp)"
        )

        msg = await ctx.reply(details, view=view, mention_author=False)
        confirmed = await view.wait_result()
        if not confirmed:
            await msg.edit(content="Token deployment cancelled.", view=None)
            return

        if gas_fee > 0:
            await ctx.db.update_wallet_holding(
                ctx.author.id, ctx.guild_id, net_raw.lower(), gas_coin, -to_raw(gas_fee),
            )

        await ctx.db.add_guild_token(
            ctx.guild_id, sym, name, emoji, "PoS", net_long, start_price, daily_vol,
            max_supply=to_raw(max_supply),
        )
        await ctx.db.execute(
            "UPDATE guild_tokens SET token_type=$1 WHERE guild_id=$2 AND symbol=$3",
            "utility", ctx.guild_id, sym,
        )
        await ctx.db.execute(
            "INSERT INTO crypto_prices (symbol, guild_id, price, open_price, day_high, day_low) "
            "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING",
            sym, ctx.guild_id, start_price, start_price, start_price, start_price,
        )
        if initial_supply > 0:
            await ctx.db.execute(
                "UPDATE guild_tokens SET circulating_supply=$1 WHERE guild_id=$2 AND symbol=$3",
                initial_supply, ctx.guild_id, sym,
            )
        await ctx.db.add_token_to_network_wallet(ctx.guild_id, net_long, sym)

        contract_data: dict = {
            "type": "ERC-20",
            "network": net_raw,
            "deployer": ctx.author.id,
            # Flag the deploy as moon-swappable so services/swap.py
            # ::is_moon_swappable_pair lets MOON flow into AND out of the
            # auto-seeded TOKEN/MOON pool below. Token deploys are always
            # opted in: the MOON pair is what makes a fresh deploy
            # tradeable against Moon-Network value before any LP shows up.
            "moon_swappable": True,
        }
        if max_supply > 0:
            contract_data["max_supply"] = max_supply
        if burn_rate > 0:
            contract_data["burn_rate"] = burn_rate
        if fee_rate > 0:
            contract_data["transfer_fee"] = fee_rate
        await ctx.db.set_token_contract(ctx.guild_id, sym, contract_data)

        pool_lines: list[str] = []
        pool_id, ca, cb = ctx.db.make_pool_id(sym, stablecoin)
        existing_pool = await ctx.db.get_pool(pool_id, ctx.guild_id)
        if not existing_pool:
            seed_usd = Config.POOL_SEED_STABLECOIN
            token_reserve = seed_usd / start_price
            stable_reserve = seed_usd
            ra = token_reserve if ca == sym else stable_reserve
            rb = stable_reserve if ca == sym else token_reserve
            await ctx.db.create_pool(pool_id, ctx.guild_id, ca, cb, ra, rb)
            pool_lines.append(
                f"Pool **{ca}/{cb}** seeded: **{ra:,.4f} {ca}** / **{rb:,.4f} {cb}**"
            )

        # Bidirectional MOON pair so the deploy is tradeable against the
        # Moon Network economy from minute zero. seed_moon_swap_pool is a
        # no-op if MOON has no oracle price yet on this guild (e.g. brand-
        # new install) -- the next bot boot's seed_pools backfills missing
        # MOON pools for any contract flagged moon_swappable.
        try:
            await ctx.db.seed_moon_swap_pool(ctx.guild_id, sym)
            moon_pool_id, _mca, _mcb = ctx.db.make_pool_id(sym, "MOON")
            moon_pool_row = await ctx.db.get_pool(moon_pool_id, ctx.guild_id)
            if moon_pool_row and float(moon_pool_row.get("total_lp") or 0) > 0:
                pool_lines.append(
                    f"Pool **{_mca}/{_mcb}** seeded (bidirectional swappable)"
                )
        except Exception:
            log.warning(
                "token_deploy: MOON pair seed failed for sym=%s gid=%s -- "
                "next boot will retry.",
                sym, ctx.guild_id,
            )
        pool_line = ("\n" + "\n".join(pool_lines)) if pool_lines else ""

        if initial_supply > 0:
            await ctx.db.update_wallet_holding(
                ctx.author.id, ctx.guild_id, net_raw.lower(), sym, initial_supply,
            )

        embed = card("Token Deployed", color=C_SUCCESS)
        embed.field("Token", f"{emoji} {sym} ({name})", False)
        embed.field("Network", net_raw, True)
        embed.field("Price", f"${start_price:,.4f}", True)
        embed.field("Contract", "ERC-20", True)
        if max_supply > 0:
            embed.field("Max Supply", f"{max_supply:,.0f}", True)
        if initial_supply > 0:
            embed.field("Circulating", f"{initial_supply:,.0f}", True)
        if burn_rate > 0:
            embed.field("Burn Rate", f"{burn_rate*100:.1f}%", True)
        if fee_rate > 0:
            embed.field("Transfer Fee", f"{fee_rate*100:.1f}%", True)
        if gas_fee > 0:
            embed.field("Gas Paid", _fmt_gas(gas_fee, gas_coin, gas_em), True)
        if pool_line:
            embed.field("Liquidity", pool_line.strip(), False)
        embed.footer(
            f"Token visible in {ctx.prefix}crypto | Trade with {ctx.prefix}buy {sym} / {ctx.prefix}sell {sym}"
        )
        await msg.edit(content=None, embed=embed.build(), view=None)

    @token.command(name="info", aliases=["contract"])
    @guild_only
    async def token_info(self, ctx: DiscoContext, symbol: str = None) -> None:
        """View a token's on-chain contract details.

        Usage: {prefix}token info <symbol>
        Example: {prefix}token info ARC
        """
        if not symbol:
            await ctx.reply_error(
                f"**View a token's contract details.**\n"
                f"```\n{ctx.prefix}token info <symbol>\n```\n"
                f"**Example:** `{ctx.prefix}token info ARC`"
            )
            return

        symbol = symbol.upper()
        all_tokens = await ctx.db.get_all_tokens_for_guild(ctx.guild_id)
        token_data = all_tokens.get(symbol)
        if not token_data:
            await ctx.reply_error(f"Token `{symbol}` not found.")
            return

        contract = await ctx.db.get_token_contract(ctx.guild_id, symbol)

        embed = card(f"Token Contract: {symbol}", color=C_GOLD)
        embed.field("Name", token_data.get("name", symbol), True)
        embed.field("Network", token_data.get("network", "None"), True)
        embed.field("Consensus", token_data.get("consensus", "?"), True)

        contract_type = contract.get("type", "ERC-20" if token_data.get("network") else "Native")
        embed.field("Contract Type", contract_type, True)

        if contract.get("max_supply"):
            embed.field("Max Supply", f"{contract['max_supply']:,.0f}", True)
        if contract.get("burn_rate"):
            embed.field("Burn Rate", f"{contract['burn_rate']*100:.2f}% per transfer", True)
        if contract.get("transfer_fee"):
            embed.field("Transfer Fee", f"{contract['transfer_fee']*100:.2f}%", True)
        if contract.get("deployer"):
            deployer = ctx.guild.get_member(contract["deployer"])
            deployer_name = deployer.display_name if deployer else f"User {contract['deployer']}"
            embed.field("Deployed By", deployer_name, True)

        price_row = await ctx.db.fetch_one(
            "SELECT price FROM crypto_prices WHERE symbol=$1 AND guild_id=$2",
            symbol, ctx.guild_id,
        )
        if price_row:
            embed.field("Current Price", f"${float(price_row['price']):,.4f}", True)

        embed.footer(f"Trade: {ctx.prefix}buy {symbol} | {ctx.prefix}sell {symbol}")
        await ctx.reply(embed=embed.build(), mention_author=False)

    @token.error
    async def token_error(self, ctx: DiscoContext, error: commands.CommandError) -> None:
        """Catch missing argument errors for token commands."""
        if isinstance(error, commands.MissingRequiredArgument):
            usage_map = {
                "info": (
                    f"**View a token's contract details.**\n"
                    f"```\n{ctx.prefix}token info <symbol>\n```\n"
                    f"**Example:** `{ctx.prefix}token info ARC`"
                ),
            }
            usage = usage_map.get(ctx.command.name if ctx.command else "")
            if usage:
                embed = card("Missing Arguments", color=C_WARNING)
                embed.description(usage)
                await ctx.reply(embed=embed.build(), mention_author=False)
                ctx.handled = True
                return
            await ctx.reply_error(
                f"`{error.param.name}` is a required argument.\n"
                f"Use `{ctx.prefix}help token {ctx.command.name if ctx.command else ''}` for usage info."
            )
            ctx.handled = True
        elif isinstance(error, commands.BadArgument):
            await ctx.reply_error(
                f"Invalid argument: {error}\n"
                f"Use `{ctx.prefix}help token {ctx.command.name if ctx.command else ''}` for usage info."
            )
            ctx.handled = True


async def setup(bot: Discoin) -> None:
    await bot.add_cog(NFTs(bot))
    await bot.add_cog(TokenDeploy(bot))
