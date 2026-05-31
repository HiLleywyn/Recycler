"""``$status`` -- provider + data-point health snapshot.

Probes every registered market provider with a CAPABILITY-AWARE call:

- Providers with ``RESOLVE`` -> resolve + quote on a canary (MTA for
  crypto, AAPL for equities).
- Providers with ``ORACLE_PRICE`` but no ``RESOLVE`` (Pyth / RedStone
  / Switchboard) -> build a synthetic ``ResolvedSymbol`` and probe
  ``oracle_quote`` directly.
- Providers with ``FUNDING`` or ``OPEN_INTEREST`` but no ``RESOLVE``
  (CoinGlass / Coinalyze) -> probe ``funding(MTA-as-perp)``.
- Providers with only ``NEWS`` / ``FUNDAMENTALS`` -> trust the
  registry's health entry (no synthetic probe).

The previous "resolve everything" probe was falsely flagging
derivatives + oracle providers as red because their adapters
correctly return None on the generic ``resolve()`` path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from core.config import Config
from core.framework.context import DiscoContext
from core.framework.embed import card
from core.framework.ui import C_BEAR, C_BULL, C_VOLATILE

from services.market.base import AssetClass, Capability, ResolvedSymbol
from services.market.registry import get_registry
from services.market.router import get_router

log = logging.getLogger(__name__)


_PROBE_TIMEOUT_S = 4.0
_CANARY_CRYPTO = "MTA"
_CANARY_EQUITY = "AAPL"


@dataclass(slots=True)
class _ProbeResult:
    name: str
    enabled: bool
    health: str            # "healthy" / "degraded" / "down" / "disabled"
    reason: str            # registry's last failure reason, if any
    probe_ok: bool | None  # True/False after live probe; None = not probed
    probe_detail: str      # short human-readable line
    latency_ms: int | None


def _status_emoji(p: _ProbeResult) -> str:
    if not p.enabled or p.health == "disabled":
        return "⚪"
    if p.probe_ok is False:
        return "🔴"
    if p.health == "down":
        return "🔴"
    if p.health == "degraded":
        return "🟡"
    if p.probe_ok is True:
        return "🟢"
    return "🟢" if p.health == "healthy" else "🟡"


async def _probe_provider(name: str, provider: Any, registry: Any) -> _ProbeResult:
    entry = registry.health.get(name)
    enabled = entry.status.value != "disabled"
    reason = entry.reason or ""

    if not enabled:
        return _ProbeResult(
            name=name, enabled=False,
            health="disabled", reason=reason or "not configured",
            probe_ok=None, probe_detail="not configured",
            latency_ms=None,
        )

    caps = provider.capabilities()
    start = time.monotonic()
    detail = ""
    probe_ok: bool | None = None

    try:
        if Capability.RESOLVE in caps:
            # Normal resolve + quote path.
            canary = _CANARY_CRYPTO
            if AssetClass.EQUITY in provider.asset_classes:
                canary = _CANARY_EQUITY
            elif name == "dexscreener":
                # DexScreener indexes long-tail DEX pairs, not CEX
                # majors. "MTA" lands on a stale wrapped-MTA pair with
                # no recent volume -> false-red. USDC is on every
                # major chain with deep liquidity, so resolve+quote
                # consistently succeeds when the upstream is up.
                canary = "USDC"
            resolved = await asyncio.wait_for(provider.resolve(canary), _PROBE_TIMEOUT_S)
            if resolved is None:
                probe_ok = False
                detail = f"resolve({canary}) returned None"
            else:
                quote = await asyncio.wait_for(provider.quote(resolved), _PROBE_TIMEOUT_S)
                if quote is None or not getattr(quote, "price_usd", 0):
                    probe_ok = False
                    detail = f"quote({canary}) returned no price"
                else:
                    probe_ok = True
                    detail = f"{canary} = ${quote.price_usd:,.2f}"

        elif Capability.ORACLE_PRICE in caps and hasattr(provider, "oracle_quote"):
            # Pyth / RedStone / Switchboard: synthesise a ResolvedSymbol
            # and probe oracle_quote directly. The adapters look up
            # their own feed map / feed-id internally.
            resolved = ResolvedSymbol(
                symbol=_CANARY_CRYPTO,
                name=_CANARY_CRYPTO,
                asset_class=AssetClass.ORACLE,
                provider=name,
                provider_id=_CANARY_CRYPTO,
            )
            oq = await asyncio.wait_for(
                provider.oracle_quote(resolved), _PROBE_TIMEOUT_S,
            )
            if oq is None or not getattr(oq, "price_usd", 0):
                probe_ok = False
                detail = f"oracle_quote({_CANARY_CRYPTO}) returned no price"
                # Switchboard-specific hint: empty Crossbar results
                # almost always mean the feed is on a different chain.
                if name == "switchboard":
                    detail += " (try SWITCHBOARD_NETWORK=sui|aptos|arc/mainnet)"
            else:
                probe_ok = True
                detail = f"{_CANARY_CRYPTO} oracle = ${oq.price_usd:,.2f}"

        elif (Capability.FUNDING in caps or Capability.OPEN_INTEREST in caps) and hasattr(provider, "funding"):
            # CoinGlass / Coinalyze: probe the funding endpoint with a
            # MTA perp ResolvedSymbol.
            resolved = ResolvedSymbol(
                symbol=_CANARY_CRYPTO,
                name=_CANARY_CRYPTO,
                asset_class=AssetClass.PERP,
                provider=name,
                provider_id=_CANARY_CRYPTO,
            )
            data = await asyncio.wait_for(
                provider.funding(resolved), _PROBE_TIMEOUT_S,
            )
            if not data:
                probe_ok = False
                detail = f"funding({_CANARY_CRYPTO}) returned empty"
            else:
                rate = float(data.get("weighted_rate") or 0.0)
                probe_ok = True
                detail = f"{_CANARY_CRYPTO} funding = {rate * 100:+.4f}%"

        elif Capability.QUOTE in caps and Capability.OHLC in caps:
            # OHLC-primary providers without RESOLVE (Binance / Bybit):
            # synth a MTA ResolvedSymbol and probe quote() directly.
            # These are the providers that actually serve $chart mta 1m,
            # so probing them is what tells you whether your deploy
            # region has access (Binance.com is 451'd in many US
            # datacentres; Bybit is global).
            resolved = ResolvedSymbol(
                symbol=_CANARY_CRYPTO,
                name=_CANARY_CRYPTO,
                asset_class=AssetClass.CRYPTO,
                provider=name,
                provider_id=_CANARY_CRYPTO,
            )
            quote = await asyncio.wait_for(
                provider.quote(resolved), _PROBE_TIMEOUT_S,
            )
            if quote is None or not getattr(quote, "price_usd", 0):
                probe_ok = False
                detail = f"quote({_CANARY_CRYPTO}) returned no price"
                # Binance-specific hint: most-common cause is geo-block.
                if name == "binance":
                    detail += " (binance.com may be geo-blocked from this region)"
            else:
                probe_ok = True
                detail = f"{_CANARY_CRYPTO} = ${quote.price_usd:,.2f}"

        else:
            # NEWS / FUNDAMENTALS / EARNINGS only providers (Finnhub
            # falls in this bucket since we let Yahoo own resolve).
            # Nothing meaningful to probe synthetically -- trust the
            # registry health entry.
            probe_ok = None
            detail = "no probe (lookup-only provider)"

    except asyncio.TimeoutError:
        probe_ok = False
        detail = f"timeout after {_PROBE_TIMEOUT_S:.1f}s"
    except Exception as exc:
        probe_ok = False
        detail = f"{type(exc).__name__}: {exc}"[:100]

    latency_ms = int((time.monotonic() - start) * 1000)
    return _ProbeResult(
        name=name,
        enabled=True,
        health=entry.status.value,
        reason=reason,
        probe_ok=probe_ok,
        probe_detail=detail,
        latency_ms=latency_ms,
    )


async def _probe_redis(bot: Any) -> tuple[str, str]:
    """Returns (status_chip, detail)."""
    bus = getattr(bot, "bus", None)
    r = getattr(bus, "_redis", None) if bus is not None else None
    if r is None:
        return ("⚪", "not configured")
    try:
        start = time.monotonic()
        pong = await asyncio.wait_for(r.ping(), _PROBE_TIMEOUT_S)
        latency_ms = int((time.monotonic() - start) * 1000)
        if pong:
            return ("🟢", f"PONG in {latency_ms}ms")
        return ("🟡", "ping returned falsy")
    except asyncio.TimeoutError:
        return ("🔴", "timeout")
    except Exception as exc:
        return ("🔴", f"{type(exc).__name__}: {exc}"[:100])


async def _probe_ai(user_id: int | None) -> tuple[str, str]:
    if not getattr(Config, "OPENROUTER_API_KEY", ""):
        return ("⚪", "OPENROUTER_API_KEY unset")
    try:
        from core.framework.ai.client import complete
    except Exception as exc:
        return ("🔴", f"import failed: {type(exc).__name__}")
    # 20s probe budget: Perplexity Sonar + slow Ollama models can take
    # >10s on cold start. The actual $query call uses the AI client's
    # internal timeout (much longer) so a slow probe doesn't mean
    # $query will fail.
    _AI_PROBE_TIMEOUT_S = 20.0
    start = time.monotonic()
    try:
        out = await asyncio.wait_for(
            complete(
                [
                    {"role": "system", "content": "Reply with the single word: OK"},
                    {"role": "user", "content": "ping"},
                ],
                max_tokens=4, temperature=0.0,
                user_id=user_id, kind="system",
            ),
            _AI_PROBE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return ("🔴", f"timeout after {_AI_PROBE_TIMEOUT_S:.0f}s")
    except Exception as exc:
        return ("🔴", f"{type(exc).__name__}: {exc}"[:100])
    latency_ms = int((time.monotonic() - start) * 1000)
    if not out:
        return ("🟡", f"empty reply ({latency_ms}ms)")
    text = str(out).strip().splitlines()[0][:32]
    return ("🟢", f"{text} ({latency_ms}ms)")


async def _probe_udf(bot: Any) -> tuple[str, str]:
    url = (getattr(Config, "TRADINGVIEW_UDF_URL", "") or "").rstrip("/")
    if not url:
        return ("⚪", "TRADINGVIEW_UDF_URL unset")
    try:
        from services.market.providers._base_http import fetch_json
    except Exception:
        return ("🔴", "http client unavailable")
    start = time.monotonic()
    try:
        data = await asyncio.wait_for(
            fetch_json("udf-probe", f"{url}/config", timeout=6),
            8.0,
        )
    except asyncio.TimeoutError:
        return ("🔴", "timeout")
    except Exception as exc:
        return ("🔴", f"{type(exc).__name__}: {exc}"[:100])
    latency_ms = int((time.monotonic() - start) * 1000)
    if not isinstance(data, dict):
        return ("🟡", f"non-JSON config ({latency_ms}ms)")
    res = data.get("supported_resolutions") or []
    return ("🟢", f"{len(res)} resolutions ({latency_ms}ms)")


def _summary_color(results: list[_ProbeResult]) -> int:
    has_red = any(r.health == "down" or r.probe_ok is False for r in results)
    has_yellow = any(r.health == "degraded" for r in results)
    if has_red:
        return C_BEAR
    if has_yellow:
        return C_VOLATILE
    return C_BULL


async def handle_status(ctx: DiscoContext, _raw_args: str) -> None:
    """Probe every provider + the ancillary services. Reports a single
    summary embed."""
    bot = ctx.bot
    registry = get_registry(bot)
    # Make sure router lazy-init has run so capabilities reflect runtime.
    get_router(bot)

    provider_tasks: list[asyncio.Task[_ProbeResult]] = []
    names = sorted(registry.names())
    for name in names:
        provider = registry.get(name)
        if provider is None:
            continue
        provider_tasks.append(asyncio.create_task(
            _probe_provider(name, provider, registry),
        ))

    redis_task = asyncio.create_task(_probe_redis(bot))
    ai_task = asyncio.create_task(_probe_ai(ctx.author.id))
    udf_task = asyncio.create_task(_probe_udf(bot))

    results: list[_ProbeResult] = await asyncio.gather(*provider_tasks) if provider_tasks else []
    redis_chip, redis_detail = await redis_task
    ai_chip, ai_detail = await ai_task
    udf_chip, udf_detail = await udf_task

    healthy = sum(1 for r in results if r.health == "healthy" and r.probe_ok is not False)
    degraded = sum(1 for r in results if r.health == "degraded")
    down = sum(1 for r in results if r.health == "down" or r.probe_ok is False)
    disabled = sum(1 for r in results if not r.enabled)

    embed = card(
        "📡 $status · provider + data-point health",
        description=(
            f"🟢 healthy `{healthy}`  ·  🟡 degraded `{degraded}`  ·  "
            f"🔴 down `{down}`  ·  ⚪ disabled `{disabled}`"
        ),
        color=_summary_color(results),
    )

    # Provider grid -- one field per provider so failures are easy to
    # spot at a glance.
    for r in results:
        emoji = _status_emoji(r)
        label = r.name
        body_lines = []
        if not r.enabled:
            body_lines.append(f"_{r.reason or 'not configured'}_")
        else:
            body_lines.append(f"state: `{r.health}`")
            if r.probe_detail:
                body_lines.append(f"probe: `{r.probe_detail}`")
            if r.latency_ms is not None:
                body_lines.append(f"latency: `{r.latency_ms}ms`")
            if r.reason and r.health != "healthy":
                body_lines.append(f"last: _{r.reason[:80]}_")
        embed = embed.field(f"{emoji} {label}", "\n".join(body_lines) or "—", True)

    # Ancillary services.
    embed = (
        embed
        .blank(False)
        .field(f"{redis_chip} Redis cache", redis_detail, True)
        .field(f"{ai_chip} AI gate", ai_detail, True)
        .field(f"{udf_chip} TradingView UDF bridge", udf_detail, True)
        .footer(
            f"$status · probed {len(results)} providers in "
            f"≤{_PROBE_TIMEOUT_S:.0f}s per call"
        )
    )

    await ctx.reply(embed=embed.build(), mention_author=False)
