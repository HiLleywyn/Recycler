"""NFT collections and marketplace repository (PostgreSQL)."""
from __future__ import annotations

import hashlib
import json
import pathlib
import secrets
import time

from .base import PgBaseRepo


def _rename_gallery_to_hash(old_url: str, token_hash: str, nft_images_root: pathlib.Path) -> str:
    """Rename a local gallery file from its slot-based name to {token_hash}{ext}.
    Returns the new public URL, or the original URL if the file is not local/not found."""
    if not old_url.startswith("/nft-images/"):
        return old_url
    # old_url: /nft-images/<guild_id>/<SYM>/00001.png
    rel = old_url.removeprefix("/nft-images/")
    old_path = nft_images_root / rel
    if not old_path.exists():
        return old_url
    suffix = old_path.suffix
    new_path = old_path.with_name(f"{token_hash}{suffix}")
    old_path.rename(new_path)
    return f"/nft-images/{rel.rsplit('/', 1)[0]}/{token_hash}{suffix}"


def _make_token_hash(guild_id: int, collection_id: int, token_id: int) -> str:
    """Generate a unique on-chain token hash for an individual NFT."""
    nonce = secrets.token_hex(8)
    raw = f"{guild_id}:{collection_id}:{token_id}:{time.time():.3f}:{nonce}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _make_contract_address(guild_id: int, creator_id: int, symbol: str) -> str:
    """Generate a unique ERC-721 contract address for a collection."""
    raw = f"{guild_id}:{creator_id}:{symbol}:{time.time():.6f}"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()[:40]


class PgNFTsRepo(PgBaseRepo):

    # -- Collections --

    async def create_collection(
        self, guild_id: int, name: str, symbol: str, network: str,
        description: str, image_url: str, max_supply: int | None,
        mint_price: float, mint_token: str, creator_id: int,
    ) -> dict:
        contract_address = _make_contract_address(guild_id, creator_id, symbol)
        return await self.fetch_one(
            "INSERT INTO nft_collections"
            " (guild_id, name, symbol, network, description, image_url,"
            "  max_supply, mint_price, mint_token, creator_id, contract_address)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)"
            " RETURNING *",
            guild_id, name, symbol.upper(), network.upper(), description,
            image_url, max_supply, mint_price, mint_token.upper(), creator_id,
            contract_address,
        )

    async def get_collection(self, collection_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM nft_collections WHERE id = $1", collection_id,
        )

    async def get_collection_by_symbol(self, guild_id: int, symbol: str) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM nft_collections WHERE guild_id = $1 AND symbol = $2",
            guild_id, symbol.upper(),
        )

    async def get_collections(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM nft_collections WHERE guild_id = $1 ORDER BY created_at DESC",
            guild_id,
        )

    async def update_collection_image(self, collection_id: int, image_url: str) -> None:
        await self.execute(
            "UPDATE nft_collections SET image_url = $1 WHERE id = $2",
            image_url, collection_id,
        )

    # -- NFTs --

    async def mint_nft(
        self, guild_id: int, collection_id: int, owner_id: int,
        name: str, description: str, image_url: str,
        rarity: str = "common", metadata: dict | None = None,
        nft_images_root: pathlib.Path | None = None,
    ) -> dict | None:
        """Mint a new NFT. Increments collection minted_count atomically.
        Generates a unique token_hash for on-chain identity.
        If a gallery image exists for the token's slot, it overrides image_url and
        the file is renamed to {token_hash}{ext} when nft_images_root is provided."""
        async with self.transaction() as conn:
            col = await conn.fetchrow(
                "SELECT max_supply, minted_count FROM nft_collections WHERE id = $1 FOR UPDATE",
                collection_id,
            )
            if not col:
                return None
            if col["max_supply"] is not None and col["minted_count"] >= col["max_supply"]:
                return None

            token_id = col["minted_count"] + 1
            token_hash = _make_token_hash(guild_id, collection_id, token_id)

            # Use gallery image for this slot if one has been uploaded
            gallery_row = await conn.fetchrow(
                "SELECT id, image_url FROM nft_collection_images"
                " WHERE collection_id = $1 AND slot = $2",
                collection_id, token_id,
            )
            if gallery_row:
                image_url = gallery_row["image_url"]
                if nft_images_root:
                    image_url = _rename_gallery_to_hash(image_url, token_hash, nft_images_root)
                    await conn.execute(
                        "UPDATE nft_collection_images SET image_url = $1 WHERE id = $2",
                        image_url, gallery_row["id"],
                    )

            await conn.execute(
                "UPDATE nft_collections SET minted_count = minted_count + 1 WHERE id = $1",
                collection_id,
            )

            row = await conn.fetchrow(
                "INSERT INTO nfts"
                " (guild_id, collection_id, token_id, owner_id, name,"
                "  description, image_url, rarity, metadata, token_hash, minted_by)"
                " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)"
                " RETURNING *",
                guild_id, collection_id, token_id, owner_id, name,
                description, image_url, rarity,
                json.dumps(metadata or {}), token_hash, owner_id,
            )
            from core.database import _row
            return _row(row)

    # -- Gallery --

    async def set_gallery_images(self, collection_id: int, images: list[str]) -> None:
        """Bulk-replace the gallery for a collection. Each item in images maps to slot N+1."""
        async with self.transaction() as conn:
            await conn.execute(
                "DELETE FROM nft_collection_images WHERE collection_id = $1",
                collection_id,
            )
            if images:
                await conn.executemany(
                    "INSERT INTO nft_collection_images (collection_id, slot, image_url)"
                    " VALUES ($1, $2, $3)",
                    [(collection_id, slot + 1, url) for slot, url in enumerate(images)],
                )

    async def get_gallery(self, collection_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT slot, image_url, created_at FROM nft_collection_images"
            " WHERE collection_id = $1 ORDER BY slot",
            collection_id,
        )

    async def get_nft(self, nft_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT n.*, c.name AS collection_name, c.symbol AS collection_symbol,"
            " c.network AS collection_network, c.contract_address AS collection_contract"
            " FROM nfts n JOIN nft_collections c ON n.collection_id = c.id"
            " WHERE n.id = $1",
            nft_id,
        )

    async def get_user_nfts(self, user_id: int, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT n.*, c.name AS collection_name, c.symbol AS collection_symbol,"
            " c.network AS collection_network, c.contract_address AS collection_contract"
            " FROM nfts n JOIN nft_collections c ON n.collection_id = c.id"
            " WHERE n.owner_id = $1 AND n.guild_id = $2"
            " ORDER BY n.minted_at DESC",
            user_id, guild_id,
        )

    async def get_collection_nfts(self, collection_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT * FROM nfts WHERE collection_id = $1 ORDER BY token_id",
            collection_id,
        )

    async def transfer_nft(self, nft_id: int, new_owner_id: int) -> bool:
        """Transfer ownership. Also removes any active listing."""
        async with self.transaction() as conn:
            status = await conn.execute(
                "UPDATE nfts SET owner_id = $1 WHERE id = $2",
                new_owner_id, nft_id,
            )
            await conn.execute(
                "DELETE FROM nft_listings WHERE nft_id = $1", nft_id,
            )
            return self._row_count(status) > 0

    # -- Marketplace --

    async def list_nft(self, guild_id: int, nft_id: int, seller_id: int, price: float, currency: str = "COIN") -> dict:
        return await self.fetch_one(
            "INSERT INTO nft_listings (guild_id, nft_id, seller_id, price, currency)"
            " VALUES ($1,$2,$3,$4,$5)"
            " ON CONFLICT (nft_id) DO UPDATE SET price = $4, currency = $5, listed_at = now()"
            " RETURNING *",
            guild_id, nft_id, seller_id, price, currency.upper(),
        )

    async def unlist_nft(self, nft_id: int, seller_id: int) -> bool:
        status = await self.execute(
            "DELETE FROM nft_listings WHERE nft_id = $1 AND seller_id = $2",
            nft_id, seller_id,
        )
        return self._row_count(status) > 0

    async def get_listing(self, nft_id: int) -> dict | None:
        return await self.fetch_one(
            "SELECT l.*, n.name AS nft_name, n.image_url AS nft_image, n.rarity,"
            " c.name AS collection_name, c.symbol AS collection_symbol,"
            " c.network AS collection_network, c.mint_token"
            " FROM nft_listings l"
            " JOIN nfts n ON l.nft_id = n.id"
            " JOIN nft_collections c ON n.collection_id = c.id"
            " WHERE l.nft_id = $1",
            nft_id,
        )

    async def get_listings(self, guild_id: int, limit: int = 25) -> list[dict]:
        return await self.fetch_all(
            "SELECT l.*, n.name AS nft_name, n.image_url AS nft_image, n.rarity,"
            " n.token_id, n.token_hash,"
            " c.name AS collection_name, c.symbol AS collection_symbol,"
            " c.network AS collection_network, c.contract_address AS collection_contract,"
            " c.mint_token"
            " FROM nft_listings l"
            " JOIN nfts n ON l.nft_id = n.id"
            " JOIN nft_collections c ON n.collection_id = c.id"
            " WHERE l.guild_id = $1"
            " ORDER BY l.listed_at DESC LIMIT $2",
            guild_id, limit,
        )

    async def is_listed(self, nft_id: int) -> bool:
        """Check if an NFT is currently listed on the marketplace."""
        row = await self.fetch_one(
            "SELECT 1 FROM nft_listings WHERE nft_id = $1", nft_id,
        )
        return row is not None

    async def buy_nft(self, nft_id: int, buyer_id: int, price: float, currency: str) -> dict | None:
        """Atomically transfer NFT, remove listing, and record sale. Returns the listing info or None."""
        async with self.transaction() as conn:
            listing = await conn.fetchrow(
                "DELETE FROM nft_listings WHERE nft_id = $1 RETURNING *", nft_id,
            )
            if not listing:
                return None
            await conn.execute(
                "UPDATE nfts SET owner_id = $1 WHERE id = $2",
                buyer_id, nft_id,
            )
            nft = await conn.fetchrow(
                "SELECT collection_id, guild_id FROM nfts WHERE id = $1", nft_id,
            )
            await conn.execute(
                "INSERT INTO nft_sales (guild_id, nft_id, collection_id, seller_id, buyer_id, price, currency)"
                " VALUES ($1,$2,$3,$4,$5,$6,$7)",
                nft["guild_id"], nft_id, nft["collection_id"],
                listing["seller_id"], buyer_id, price, currency,
            )
            from core.database import _row
            return _row(listing)

    # -- Sales --

    async def get_nft_sales(self, nft_id: int, limit: int = 10) -> list[dict]:
        """Get recent sales for a specific NFT."""
        return await self.fetch_all(
            "SELECT s.*, n.name AS nft_name, c.symbol AS collection_symbol"
            " FROM nft_sales s"
            " JOIN nfts n ON s.nft_id = n.id"
            " JOIN nft_collections c ON s.collection_id = c.id"
            " WHERE s.nft_id = $1"
            " ORDER BY s.sold_at DESC LIMIT $2",
            nft_id, limit,
        )

    async def get_collection_sales(self, collection_id: int, limit: int = 10) -> list[dict]:
        """Get recent sales for a collection."""
        return await self.fetch_all(
            "SELECT s.*, n.name AS nft_name, n.token_id, n.rarity, c.symbol AS collection_symbol"
            " FROM nft_sales s"
            " JOIN nfts n ON s.nft_id = n.id"
            " JOIN nft_collections c ON s.collection_id = c.id"
            " WHERE s.collection_id = $1"
            " ORDER BY s.sold_at DESC LIMIT $2",
            collection_id, limit,
        )

    async def get_avg_sale_price_by_rarity(self, collection_id: int) -> dict[str, float]:
        """Get average sale price per rarity tier for a collection."""
        rows = await self.fetch_all(
            "SELECT n.rarity, AVG(s.price) AS avg_price"
            " FROM nft_sales s"
            " JOIN nfts n ON s.nft_id = n.id"
            " WHERE s.collection_id = $1"
            " GROUP BY n.rarity",
            collection_id,
        )
        return {r["rarity"]: float(r["avg_price"]) for r in rows}

    async def get_nft_by_collection_token(self, collection_id: int, token_id: int) -> dict | None:
        """Get a specific NFT by collection and token ID."""
        return await self.fetch_one(
            "SELECT n.*, c.name AS collection_name, c.symbol AS collection_symbol,"
            " c.network AS collection_network, c.contract_address AS collection_contract"
            " FROM nfts n JOIN nft_collections c ON n.collection_id = c.id"
            " WHERE n.collection_id = $1 AND n.token_id = $2",
            collection_id, token_id,
        )

    async def get_user_nft_value(self, user_id: int, guild_id: int) -> float:
        """Get total USD value of user's NFTs based on mint price or avg sale price."""
        rows = await self.fetch_all(
            "SELECT n.id, n.collection_id, n.rarity, c.mint_price, c.mint_token"
            " FROM nfts n JOIN nft_collections c ON n.collection_id = c.id"
            " WHERE n.owner_id = $1 AND n.guild_id = $2",
            user_id, guild_id,
        )
        if not rows:
            return 0.0
        collection_ids = set(r["collection_id"] for r in rows)
        avg_prices = {}
        for cid in collection_ids:
            avg_prices[cid] = await self.get_avg_sale_price_by_rarity(cid)

        total = 0.0
        for r in rows:
            rarity_avgs = avg_prices.get(r["collection_id"], {})
            if r["rarity"] in rarity_avgs:
                total += rarity_avgs[r["rarity"]]
            else:
                total += float(r["mint_price"])
        return total

    async def get_all_guild_nft_values(self, guild_id: int) -> list[dict]:
        """Bulk NFT value per user using mint price converted to USD. Returns [{user_id, nft_value}]."""
        rows = await self.fetch_all(
            """SELECT n.owner_id AS user_id,
                  COALESCE(SUM(
                    CASE
                      WHEN UPPER(c.mint_token) IN ('USD', 'USDC', 'DSD') THEN c.mint_price
                      ELSE c.mint_price * COALESCE(
                        (SELECT p.price FROM crypto_prices p
                         WHERE p.symbol = c.mint_token AND p.guild_id = $1 LIMIT 1),
                        0
                      )
                    END
                  ), 0) AS nft_value
               FROM nfts n
               JOIN nft_collections c ON n.collection_id = c.id
               WHERE n.guild_id = $1
               GROUP BY n.owner_id""",
            guild_id,
        )
        return rows

    async def get_collection_floor_price_usd(self, collection_id: int, guild_id: int) -> float:
        """Return the lowest active listing price for a collection converted to USD.
        Returns 0.0 if no active listings exist."""
        row = await self.fetch_one(
            """SELECT MIN(
                 CASE
                   WHEN UPPER(l.currency) IN ('USD', 'USDC', 'DSD') THEN l.price
                   ELSE l.price * COALESCE(
                     (SELECT p.price FROM crypto_prices p
                      WHERE p.symbol = l.currency AND p.guild_id = $2 LIMIT 1),
                     0
                   )
                 END
               ) AS floor_usd
               FROM nft_listings l
               JOIN nfts n ON l.nft_id = n.id
               WHERE n.collection_id = $1""",
            collection_id, guild_id,
        )
        if row and row.get("floor_usd") is not None:
            return float(row["floor_usd"])
        return 0.0

    async def get_nft_owner_counts_for_collection(self, collection_id: int) -> list[dict]:
        """Return [{user_id, cnt}] for all owners of NFTs in a collection."""
        return await self.fetch_all(
            "SELECT owner_id AS user_id, COUNT(*) AS cnt FROM nfts WHERE collection_id = $1 GROUP BY owner_id",
            collection_id,
        )

    async def delete_collection_with_nfts(self, collection_id: int) -> dict:
        """Cascade-delete a collection and all its NFTs, listings, sales, and gallery images.
        Returns counts of deleted rows per table."""
        counts: dict[str, int] = {}
        async with self.transaction() as conn:
            for table, col in [
                ("nft_sales",             "collection_id"),
                ("nft_listings",          "nft_id"),
                ("nfts",                  "collection_id"),
                ("nft_collection_images", "collection_id"),
                ("nft_collections",       "id"),
            ]:
                if table == "nft_listings":
                    # listings don't have collection_id - join through nfts
                    r = await conn.execute(
                        "DELETE FROM nft_listings WHERE nft_id IN "
                        "(SELECT id FROM nfts WHERE collection_id = $1)",
                        collection_id,
                    )
                else:
                    r = await conn.execute(
                        f"DELETE FROM {table} WHERE {col} = $1", collection_id,
                    )
                try:
                    counts[table] = int(r.split()[-1])
                except Exception:
                    counts[table] = 0
        return counts

    async def update_collection_metadata(self, collection_id: int, slot_metadata: str) -> None:
        """Update slot metadata for a collection (JSON string)."""
        await self.execute(
            "UPDATE nft_collections SET slot_metadata = $1 WHERE id = $2",
            slot_metadata, collection_id,
        )

    async def lock_collection(self, collection_id: int) -> None:
        """Lock a collection's metadata (prevents further edits)."""
        await self.execute(
            "UPDATE nft_collections SET is_locked = TRUE WHERE id = $1",
            collection_id,
        )

    async def get_nft_by_symbol_and_token(self, guild_id: int, symbol: str, token_id: int) -> dict | None:
        """Resolve an NFT from collection symbol + token ID."""
        return await self.fetch_one(
            "SELECT n.*, c.name AS collection_name, c.symbol AS collection_symbol,"
            " c.network AS collection_network, c.contract_address AS collection_contract"
            " FROM nfts n JOIN nft_collections c ON n.collection_id = c.id"
            " WHERE c.guild_id = $1 AND c.symbol = $2 AND n.token_id = $3",
            guild_id, symbol.upper(), token_id,
        )
