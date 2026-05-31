"""core/framework/contracts.py - Reusable on-chain identity generation.

Generates deterministic-ish contract addresses and token hashes that mimic
real blockchain identifiers. Used by the NFT system, group token system,
and any future on-chain entity that needs a unique address or hash.
"""
from __future__ import annotations

import hashlib
import secrets
import time


def make_contract_address(guild_id: int, creator_id: int, symbol: str) -> str:
    """Generate a unique 42-char ERC-20 style contract address (0x-prefixed).

    Uses entropy (nonce + timestamp) so repeated calls produce different results.
    """
    nonce = secrets.token_hex(4)
    raw = f"{guild_id}:{creator_id}:{symbol}:{time.time():.6f}:{nonce}"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()[:40]


def make_token_hash(guild_id: int, symbol: str, prefix: str = "gt") -> str:
    """Generate a unique 64-char hex token hash for a token deployment.

    ``prefix`` is embedded in the entropy to distinguish NFT hashes (``nft``)
    from group token hashes (``gt``) and any other type.
    """
    nonce = secrets.token_hex(8)
    raw = f"{prefix}:{guild_id}:{symbol}:{time.time():.3f}:{nonce}"
    return hashlib.sha256(raw.encode()).hexdigest()
