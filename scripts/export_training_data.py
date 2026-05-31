"""Export DiscoAI training turns to a ShareGPT-format JSONL file.

Usage:
    python -m scripts.export_training_data \\
        --since 2026-01-01 \\
        --min-score 1 \\
        --out training.jsonl

Connects via the same `database/` pool helpers the bot uses, so the only
runtime requirement is a reachable Postgres + the same DATABASE_URL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export DiscoAI training turns.")
    p.add_argument(
        "--since",
        required=True,
        help="ISO date or datetime, e.g. 2026-01-01 or 2026-01-01T00:00:00.",
    )
    p.add_argument(
        "--min-score",
        type=int,
        default=None,
        help=(
            "Optional minimum feedback_score to include. Without this flag, "
            "any turn with feedback_score < 0 is dropped; turns with NULL "
            "score are kept."
        ),
    )
    p.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output JSONL path. One ShareGPT conversation per line.",
    )
    return p.parse_args()


def _parse_since(raw: str) -> datetime:
    try:
        if "T" in raw or " " in raw:
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"Could not parse --since {raw!r}: {exc}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _run() -> int:
    args = _parse_args()
    since = _parse_since(args.since)

    # Set up the bot's DB pool exactly as the runtime does.
    from core.config import Config
    from database import Database
    from ai.training_logger import TrainingLogger

    if not Config.DATABASE_URL:
        raise SystemExit("DATABASE_URL is not set.")

    db = Database(Config.DATABASE_URL)
    await db.connect()
    try:
        logger = TrainingLogger(db)
        rows = await logger.export_sharegpt(since, min_score=args.min_score)
    finally:
        await db.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # Print a tiny summary to stderr so the file on stdout/--out stays clean.
    sys.stderr.write(
        f"Exported {len(rows)} turn(s) since {since.isoformat()} "
        f"(min_score={args.min_score!r}) → {args.out}\n"
    )
    return 0


def main() -> None:
    # Keep this file self-contained -- the script is meant to be run from
    # repo root via `python -m scripts.export_training_data`.
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
