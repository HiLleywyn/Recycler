"""
core/framework/tx.py  -  Transaction embed helper.

Single source of truth for attaching tx hash footers and dashboard deep-links
to all transaction embeds across Discoin.

Usage:
    from core.framework.tx import set_tx

    set_tx(embed, guild_id, tx_hash)
    set_tx(embed, guild_id, tx_hash, footer_extra="Slash risk: 5%")
"""
from __future__ import annotations

import discord


def set_tx(
    embed: discord.Embed,
    guild_id: int,
    tx_hash: str,
    footer_extra: str = "",
) -> discord.Embed:
    """
    Attach a tx hash footer and dashboard deep-link to an embed.

    - Footer: "tx:{hash}  •  {footer_extra}" (footer_extra optional)
    - embed.url: dashboard deep-link when DASHBOARD_URL is configured
      → clicking the embed title in Discord opens the transaction on the dashboard

    Args:
        embed:        The embed to modify in-place.
        guild_id:     Guild ID (used to build the dashboard URL).
        tx_hash:      Transaction hash string.
        footer_extra: Optional extra text appended after the hash (e.g. rate info).

    Returns: the same embed (for chaining).
    """
    if not tx_hash:
        return embed

    footer = f"tx:{tx_hash}"
    if footer_extra:
        footer = f"{footer}  •  {footer_extra}"
    embed.set_footer(text=footer)

    return embed
