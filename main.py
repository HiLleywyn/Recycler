import asyncio
import signal

from core.config import Config
from core.framework.bot import Discoin
from core.framework import log
from core.framework.log import setup_logging, print_banner


# Set to True once ``bot.start()`` has begun receiving events. Any
# exception caught after this is a SHUTDOWN-time issue, not a startup
# failure, and is logged accordingly. Avoids the misleading
# "Fatal startup error" line that fired on every Railway redeploy
# even though the bot had been live for hours.
_BOT_RUNNING: bool = False


def _install_shutdown_signals(bot: Discoin) -> None:
    """Wire SIGTERM/SIGINT to bot.close() so Railway redeploys drain
    active games instead of killing the process mid-resolution.

    The previous implementation called ``loop.stop()`` on a second
    SIGTERM, which forcibly killed in-flight tasks (including
    ``bot.close()`` itself) and left ``asyncio.run()`` raising
    ``RuntimeError: Event loop stopped before Future completed``.
    Railway's redeploy flow sends a SIGTERM, then SIGKILL ~10s
    later if the process is still up -- there's no point trying to
    "force exit" on the second SIGTERM ourselves; SIGKILL handles it.
    """
    loop = asyncio.get_running_loop()
    _triggered = False

    def _handle(sig: signal.Signals) -> None:
        nonlocal _triggered
        if _triggered:
            log.warn(f"Received {sig.name} again; close already in flight")
            return
        _triggered = True
        log.warn(f"Received {sig.name}; starting graceful shutdown")
        loop.create_task(bot.close())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle, sig)
        except NotImplementedError:
            pass  # Windows / non-Unix event loop


def _validate_runtime_config() -> None:
    if not Config.TOKEN.strip():
        raise RuntimeError("DISCORD_TOKEN is required before starting Discoin.")


async def main() -> None:
    setup_logging()
    print_banner(Config.PREFIX, Config.DATABASE_URL, Config.API_PORT)
    _validate_runtime_config()
    if Config.JWT_SECRET == "change-me-in-production":
        if Config.DEBUG:
            log.warn("JWT_SECRET is still using the default value; dashboard auth is not production-safe.")
        else:
            raise RuntimeError(
                "JWT_SECRET is still the default value. "
                "Set a secure JWT_SECRET environment variable before running in production. "
                "To bypass in development, set DEBUG=true."
            )
    if Config.TX_SALT == "econbot-default-salt":
        log.warn("TX_SALT is still using the default value; set a stable random salt before production.")

    # Retry with exponential backoff when Discord rate-limits the login.
    # Without this, Railway restarts the process immediately on crash,
    # creating an infinite 429 loop that never recovers.
    global _BOT_RUNNING
    max_retries = 5
    for attempt in range(max_retries):
        try:
            async with Discoin() as bot:
                _install_shutdown_signals(bot)
                # Wrap start in a small task-flag flip: once start()
                # actually fires bot's on_ready, _BOT_RUNNING goes True.
                # If start() is still pre-ready when an exception bubbles,
                # it was a startup problem; otherwise it was runtime /
                # shutdown, which gets a friendlier log line.
                async def _flip_running_when_ready() -> None:
                    global _BOT_RUNNING
                    await bot.wait_until_ready()
                    _BOT_RUNNING = True
                _ready_task = asyncio.create_task(_flip_running_when_ready())
                try:
                    await bot.start(Config.TOKEN)
                finally:
                    if not _ready_task.done():
                        _ready_task.cancel()
            log.info("Bot exited cleanly.")
            return  # clean shutdown
        except KeyboardInterrupt:
            log.warn("Shutdown requested; exiting cleanly.")
            return
        except ValueError as exc:
            # "I/O operation on closed file" is a known race during the
            # tear-down sequence: uvicorn's lifespan shutdown closes its
            # log handlers before our final bot.close() flush completes.
            # Harmless; the bot already drained its queues.
            msg = str(exc)
            if (
                "closed file" in msg.lower()
                or "closed pipe" in msg.lower()
            ) and _BOT_RUNNING:
                log.info(
                    "Shutdown teardown noise (safe to ignore): "
                    f"{type(exc).__name__}: {exc}"
                )
                return
            if _BOT_RUNNING:
                log.error(
                    f"Runtime error after startup: "
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                log.error(
                    f"Fatal startup error: {type(exc).__name__}: {exc}"
                )
            raise
        except Exception as exc:
            is_rate_limit = "429" in str(exc) or "rate limit" in str(exc).lower()
            if is_rate_limit and attempt < max_retries - 1:
                delay = 2 ** (attempt + 2)  # 4s, 8s, 16s, 32s
                log.warn(f"Rate limited on startup (attempt {attempt + 1}/{max_retries}), retrying in {delay}s…")
                await asyncio.sleep(delay)
                continue
            if _BOT_RUNNING:
                # Bot was up and serving; this is a runtime / shutdown
                # failure, not a startup one. Different label so Railway
                # doesn't flag clean redeploys as crashes.
                log.error(
                    f"Runtime error after startup: "
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                log.error(
                    f"Fatal startup error: {type(exc).__name__}: {exc}"
                )
            raise


if __name__ == "__main__":
    asyncio.run(main())
