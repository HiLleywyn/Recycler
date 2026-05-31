"""cogs/migrate.py  -  One-time SQLite → PostgreSQL migration command.

Allows a server admin to upload an old SQLite .db file as a Discord
attachment and migrate its data into the running PostgreSQL database.

Usage:
  ,migrate upload   (attach .db file)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tempfile
from decimal import Decimal, InvalidOperation
from typing import Any

import discord
from discord.ext import commands

from core.framework.bot import Discoin
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.middleware import guild_only
from core.framework.fuzzy import suggest_subcommand
from core.framework.ui import C_AMBER, C_BLURPLE, C_SUCCESS

log = logging.getLogger(__name__)

# Tables we know how to migrate, mapped to their PG primary-key columns.
# Order matters: parents before children (foreign key deps).
_MIGRATABLE_TABLES: list[dict[str, Any]] = [
    {
        "sqlite": "guild_settings",
        "pg": "guild_settings",
        "pk": ["guild_id"],
        "columns": [
            "guild_id", "trade_channel", "mine_channel", "staking_channel",
            "validators_channel", "contracts_channel", "crypto_channel",
            "gambling_channel", "pools_channel", "drops_channel", "job_channel",
            "drops_spawn_channel", "wallet_channel", "error_channel",
            "scam_channel", "prefix", "embed_color", "server_name",
            "currency_name",
        ],
        "scope_guild": False,  # guild_settings IS the guild row
    },
    {
        "sqlite": "users",
        "pg": "users",
        "pk": ["user_id", "guild_id"],
        "columns": [
            "user_id", "guild_id", "wallet", "bank", "daily_streak",
        ],
        "numeric": ["wallet", "bank"],
        "scope_guild": True,
    },
    {
        "sqlite": "crypto_prices",
        "pg": "crypto_prices",
        "pk": ["symbol", "guild_id"],
        "columns": [
            "symbol", "guild_id", "price", "open_price", "day_high",
            "day_low", "circulating_supply",
        ],
        "numeric": ["price", "open_price", "day_high", "day_low", "circulating_supply"],
        "scope_guild": True,
    },
    {
        "sqlite": "crypto_holdings",
        "pg": "crypto_holdings",
        "pk": ["user_id", "guild_id", "symbol"],
        "columns": ["user_id", "guild_id", "symbol", "amount"],
        "numeric": ["amount"],
        "scope_guild": True,
    },
    {
        "sqlite": "transactions",
        "pg": "transactions",
        "pk": ["tx_hash"],
        "columns": [
            "tx_hash", "guild_id", "user_id", "tx_type",
            "symbol_in", "amount_in", "symbol_out", "amount_out",
            "price_at", "gas_fee",
        ],
        "numeric": ["amount_in", "amount_out", "price_at", "gas_fee"],
        "scope_guild": True,
    },
    {
        "sqlite": "pools",
        "pg": "pools",
        "pk": ["pool_id", "guild_id"],
        "columns": [
            "pool_id", "guild_id", "token_a", "token_b",
            "reserve_a", "reserve_b", "total_lp",
        ],
        "numeric": ["reserve_a", "reserve_b", "total_lp"],
        "scope_guild": True,
    },
]


def _to_decimal(val: Any) -> Decimal | None:
    """Convert a value to Decimal, returning None for NULL/empty."""
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _to_int(val: Any) -> int | None:
    """Convert a value to int, returning None for NULL/empty."""
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _read_sqlite_table(
    db_path: str, table_name: str, columns: list[str], guild_id: int | None = None,
) -> list[dict[str, Any]]:
    """Read rows from a SQLite table. Runs in a thread (blocking I/O)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Check if the table exists
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        if not cur.fetchone():
            return []

        # Get actual columns in the SQLite table
        pragma = conn.execute(f"PRAGMA table_info({table_name})")  # noqa: S608
        actual_cols = {row["name"] for row in pragma.fetchall()}

        # Only select columns that exist in both SQLite and our mapping
        usable = [c for c in columns if c in actual_cols]
        if not usable:
            return []

        col_list = ", ".join(usable)
        query = f"SELECT {col_list} FROM {table_name}"  # noqa: S608
        params: tuple = ()
        if guild_id is not None and "guild_id" in actual_cols:
            query += " WHERE guild_id = ?"
            params = (guild_id,)

        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _get_sqlite_tables(db_path: str) -> list[str]:
    """Return list of table names in the SQLite database."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _require_manage_guild():
    async def predicate(ctx: DiscoContext) -> bool:
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.reply_error("You need **Manage Guild** to use this command.")
            return False
        return True
    return commands.check(predicate)


class Migrate(commands.Cog):
    """One-time SQLite → PostgreSQL migration tools."""

    def __init__(self, bot: Discoin) -> None:
        self.bot = bot

    @commands.group(name="migrate", invoke_without_command=True)
    @guild_only
    @_require_manage_guild()
    async def migrate_cmd(self, ctx: DiscoContext) -> None:
        """Database migration commands. Usage: ,migrate upload (attach .db file)"""
        if await suggest_subcommand(ctx, self.migrate_cmd):
            return
        embed = card(
            "Database Migration",
            description=(
                "Migrate data from an old SQLite database to PostgreSQL.\n\n"
                "**Usage:** Attach a `.db` file and run:\n"
                f"`{ctx.prefix}migrate upload`\n\n"
                "The migration will import users, holdings, prices, "
                "transactions, and pools for this server."
            ),
            color=C_BLURPLE,
        ).build()
        await ctx.reply(embed=embed, mention_author=False)

    @migrate_cmd.command(name="upload")
    @guild_only
    @_require_manage_guild()
    async def migrate_upload(self, ctx: DiscoContext) -> None:
        """Upload a SQLite .db file to migrate data into PostgreSQL.

        Attach the .db file to the message containing this command.
        Only data belonging to this server will be imported.
        """
        # Validate attachment
        if not ctx.message.attachments:
            await ctx.reply_error(
                "Please attach a `.db` SQLite file to your message."
            )
            return

        attachment = ctx.message.attachments[0]
        if not attachment.filename.endswith(".db"):
            await ctx.reply_error(
                f"Expected a `.db` file, got `{attachment.filename}`."
            )
            return

        if attachment.size > 100 * 1024 * 1024:  # 100 MB limit
            await ctx.reply_error("File too large (max 100 MB).")
            return

        guild_id = ctx.guild.id

        # Confirmation
        confirm_embed = card(
            "Confirm Migration",
            description=(
                f"This will import data from `{attachment.filename}` into the "
                f"live PostgreSQL database for this server.\n\n"
                f"Existing rows will **not** be overwritten (duplicates are skipped).\n\n"
                f"Type `confirm` to proceed, or anything else to cancel."
            ),
            color=C_AMBER,
        ).build()
        await ctx.reply(embed=confirm_embed, mention_author=False)

        def check(m: discord.Message) -> bool:
            return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=30.0)
        except asyncio.TimeoutError:
            await ctx.reply_error("Migration cancelled  -  timed out.")
            return

        if msg.content.strip().lower() != "confirm":
            await ctx.reply_error("Migration cancelled.")
            return

        # Download the file
        status_msg = await ctx.reply(
            embed=card("Migrating...", description="Downloading file...", color=C_BLURPLE).build(),
            mention_author=False,
        )

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(tmp_fd)
        try:
            await attachment.save(tmp_path)

            # Discover tables
            sqlite_tables = await asyncio.to_thread(_get_sqlite_tables, tmp_path)
            log.info(
                "SQLite migration: found tables %s in %s",
                sqlite_tables, attachment.filename,
            )

            stats: dict[str, int] = {}
            errors: list[str] = []
            pool = self.bot.db._pool

            # Ensure guild_settings row exists first (FK parent)
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
                    guild_id,
                )

            for spec in _MIGRATABLE_TABLES:
                sqlite_name = spec["sqlite"]
                pg_name = spec["pg"]
                columns = spec["columns"]
                pk_cols = spec["pk"]
                numeric_cols = spec.get("numeric", [])
                scope = guild_id if spec.get("scope_guild") else None

                if sqlite_name not in sqlite_tables:
                    continue

                try:
                    rows = await asyncio.to_thread(
                        _read_sqlite_table, tmp_path, sqlite_name, columns, scope,
                    )
                except Exception as exc:
                    errors.append(f"`{sqlite_name}`: read error  -  {exc}")
                    continue

                if not rows:
                    stats[pg_name] = 0
                    continue

                # Determine usable columns from the first row
                usable_cols = [c for c in columns if c in rows[0]]
                if not usable_cols:
                    continue

                # Build INSERT ... ON CONFLICT DO NOTHING
                placeholders = ", ".join(f"${i+1}" for i in range(len(usable_cols)))
                col_list = ", ".join(usable_cols)
                pk_list = ", ".join(c for c in pk_cols if c in usable_cols)
                insert_sql = (
                    f"INSERT INTO {pg_name} ({col_list}) VALUES ({placeholders}) "
                    f"ON CONFLICT ({pk_list}) DO NOTHING"
                )

                inserted = 0
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        for row in rows:
                            args = []
                            for col in usable_cols:
                                val = row.get(col)
                                if col in numeric_cols:
                                    val = _to_decimal(val)
                                elif col in ("user_id", "guild_id") or col.endswith("_channel"):
                                    val = _to_int(val)
                                args.append(val)
                            try:
                                result = await conn.execute(insert_sql, *args)
                                if "INSERT 0 1" in result:
                                    inserted += 1
                            except Exception as exc:
                                log.warning(
                                    "Migration row error in %s: %s (row=%s)",
                                    pg_name, exc, row,
                                )

                stats[pg_name] = inserted
                log.info("Migrated %d rows into %s", inserted, pg_name)

            # Build result embed
            lines = []
            for table, count in stats.items():
                lines.append(f"`{table}`: **{count}** rows imported")
            if errors:
                lines.append("")
                lines.append("**Errors:**")
                lines.extend(errors)

            result_embed = card(
                "Migration Complete",
                description="\n".join(lines) if lines else "No matching tables found.",
                color=C_SUCCESS if not errors else C_AMBER,
            ).build()
            await status_msg.edit(embed=result_embed)

        except Exception as exc:
            log.error("Migration failed: %s", exc, exc_info=True)
            await ctx.reply_error(f"Migration failed: `{exc}`")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def setup(bot: Discoin) -> None:
    await bot.add_cog(Migrate(bot))
