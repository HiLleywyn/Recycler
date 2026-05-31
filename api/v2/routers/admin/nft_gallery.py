"""Admin NFT gallery  -  upload a set of images for sequential minting assignment."""
from __future__ import annotations

import hashlib
import pathlib
import time
from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.framework.scale import to_human, to_raw
from api.v2.dependencies import get_db, require_admin
from api.v2.exceptions import NotFoundError, ValidationError

router = APIRouter(prefix="/nfts", tags=["admin"])


class AdminDeployRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=10)
    name: str = Field(..., min_length=1, max_length=50)
    description: str = Field("", max_length=500)
    network: str = Field(..., description="ARC or DSC")
    mint_price: float = Field(..., ge=0)
    mint_token: str = Field("ARC", max_length=10)
    max_supply: int | None = Field(None, ge=1)

_ALLOWED_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MAX_FILE_BYTES = 8 * 1024 * 1024  # 8 MB per image
_MAX_FILES = 10_000


def _gallery_dir(static_root: pathlib.Path, guild_id: int, symbol: str) -> pathlib.Path:
    return static_root / "nft-images" / str(guild_id) / symbol.upper()


@router.get("")
async def list_collections(
    db=Depends(get_db),
    user: dict[str, Any] = Depends(require_admin),
):
    """List all NFT collections for the guild."""
    guild_id = int(user["guild_id"])
    rows = await db.fetch(
        "SELECT id, symbol, name, network, minted_count, max_supply, mint_price,"
        " mint_token, image_url, contract_address, created_at"
        " FROM nft_collections WHERE guild_id = $1 ORDER BY created_at DESC",
        guild_id,
    )
    return [
        {
            "id": r["id"],
            "symbol": r["symbol"],
            "name": r["name"],
            "network": r["network"],
            "minted_count": r["minted_count"],
            "max_supply": r["max_supply"],
            "mint_price": to_human(int(r["mint_price"])) if r["mint_price"] else 0.0,
            "mint_token": r["mint_token"],
            "image_url": r["image_url"],
            "contract_address": r["contract_address"] or "",
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


@router.post("")
async def admin_create_collection(
    body: AdminDeployRequest,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(require_admin),
):
    """Admin: create an NFT collection without tier check or gas fees."""
    guild_id = int(user["guild_id"])
    user_id = int(user["user_id"])

    symbol = body.symbol.upper()
    network = body.network.upper()

    if network not in {"ARC", "DSC"}:
        raise ValidationError("Network must be ARC or DSC.")

    existing = await db.fetchrow(
        "SELECT id FROM nft_collections WHERE guild_id = $1 AND symbol = $2",
        guild_id, symbol,
    )
    if existing:
        raise ValidationError(f"Collection symbol '{symbol}' already exists.")

    contract_address = (
        "0x"
        + hashlib.sha256(
            f"{guild_id}:{user_id}:{symbol}:{time.time():.6f}".encode()
        ).hexdigest()[:40]
    )

    row = await db.fetchrow(
        "INSERT INTO nft_collections"
        " (guild_id, name, symbol, network, description, image_url,"
        "  max_supply, mint_price, mint_token, creator_id, contract_address)"
        " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)"
        " RETURNING *",
        guild_id, body.name, symbol, network, body.description,
        "", body.max_supply, to_raw(body.mint_price), body.mint_token.upper(),
        user_id, contract_address,
    )

    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "name": row["name"],
        "network": row["network"],
        "contract_address": row["contract_address"],
        "mint_price": to_human(int(row["mint_price"])),
        "mint_token": row["mint_token"],
        "max_supply": row["max_supply"],
    }


@router.delete("/{symbol}")
async def admin_delete_collection(
    symbol: str,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(require_admin),
):
    """Admin: delete a collection (cascade deletes gallery images via FK)."""
    guild_id = int(user["guild_id"])

    col = await db.fetchrow(
        "SELECT id FROM nft_collections WHERE guild_id = $1 AND symbol = $2",
        guild_id, symbol.upper(),
    )
    if not col:
        raise NotFoundError(f"Collection '{symbol.upper()}' not found.")

    await db.execute(
        "DELETE FROM nft_collections WHERE id = $1", col["id"]
    )
    return {"deleted": True, "symbol": symbol.upper()}


@router.post("/{symbol}/gallery")
async def upload_gallery(
    symbol: str,
    request: Request,
    files: list[UploadFile] = File(..., description="Image files  -  one per NFT slot in order"),
    db=Depends(get_db),
    user: dict[str, Any] = Depends(require_admin),
):
    """Upload a gallery of images for an NFT collection.

    Each file maps to an NFT slot in the order provided (file[0] → token #1,
    file[1] → token #2, …). When a token is minted its slot image is used
    automatically instead of the collection-level image or placeholder.

    Uploading again **replaces** the entire gallery.
    Allowed types: JPEG, PNG, GIF, WEBP. Max 8 MB per file, 10 000 files.
    """
    guild_id = int(user["guild_id"])

    if not files:
        raise ValidationError("No files provided")
    if len(files) > _MAX_FILES:
        raise ValidationError(f"Too many files (max {_MAX_FILES})")

    col = await db.fetchrow(
        "SELECT id, symbol FROM nft_collections WHERE guild_id = $1 AND symbol = $2",
        guild_id, symbol.upper(),
    )
    if not col:
        raise NotFoundError(f"Collection '{symbol.upper()}' not found")

    collection_id = col["id"]
    sym = col["symbol"]

    # Store uploads under api/static/nft-images/<guild_id>/<SYMBOL>/
    static_root = pathlib.Path(__file__).resolve().parents[3] / "static"
    dest_dir = _gallery_dir(static_root, guild_id, sym)
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved_urls: list[str] = []
    errors: list[str] = []

    for idx, upload in enumerate(files):
        content_type = upload.content_type or ""
        if content_type not in _ALLOWED_MIME:
            errors.append(f"file[{idx}] '{upload.filename}': unsupported type '{content_type}'")
            continue

        data = await upload.read()
        if len(data) > _MAX_FILE_BYTES:
            errors.append(f"file[{idx}] '{upload.filename}': exceeds 8 MB limit")
            continue

        suffix = pathlib.Path(upload.filename or "img").suffix or ".bin"
        fname = f"{idx + 1:05d}{suffix}"
        (dest_dir / fname).write_bytes(data)

        saved_urls.append(f"/nft-images/{guild_id}/{sym}/{fname}")

    if errors and not saved_urls:
        raise ValidationError(str({"errors": errors}))

    # Replace gallery atomically
    await db.execute(
        "DELETE FROM nft_collection_images WHERE collection_id = $1", collection_id
    )
    if saved_urls:
        await db.executemany(
            "INSERT INTO nft_collection_images (collection_id, slot, image_url)"
            " VALUES ($1, $2, $3)",
            [(collection_id, slot + 1, url) for slot, url in enumerate(saved_urls)],
        )

    response: dict = {"uploaded": len(saved_urls), "slots": len(saved_urls)}
    if errors:
        response["skipped_errors"] = errors
    return JSONResponse(response, status_code=207 if errors else 200)


@router.get("/{symbol}/gallery")
async def get_gallery(
    symbol: str,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(require_admin),
):
    """List the uploaded gallery images for a collection."""
    guild_id = int(user["guild_id"])

    col = await db.fetchrow(
        "SELECT id FROM nft_collections WHERE guild_id = $1 AND symbol = $2",
        guild_id, symbol.upper(),
    )
    if not col:
        raise NotFoundError(f"Collection '{symbol.upper()}' not found")

    rows = await db.fetch(
        "SELECT slot, image_url FROM nft_collection_images"
        " WHERE collection_id = $1 ORDER BY slot",
        col["id"],
    )
    return {"symbol": symbol.upper(), "count": len(rows), "images": [dict(r) for r in rows]}


@router.delete("/{symbol}/gallery")
async def delete_gallery(
    symbol: str,
    db=Depends(get_db),
    user: dict[str, Any] = Depends(require_admin),
):
    """Clear the gallery for a collection. Subsequent mints fall back to
    the collection-level image or the rarity-coloured placeholder."""
    guild_id = int(user["guild_id"])

    col = await db.fetchrow(
        "SELECT id FROM nft_collections WHERE guild_id = $1 AND symbol = $2",
        guild_id, symbol.upper(),
    )
    if not col:
        raise NotFoundError(f"Collection '{symbol.upper()}' not found")

    result = await db.execute(
        "DELETE FROM nft_collection_images WHERE collection_id = $1", col["id"]
    )
    deleted = int(result.split()[-1]) if result else 0
    return {"deleted": deleted}
