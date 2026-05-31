"""Concrete market data adapters.

Each module here implements :class:`services.market.base.MarketProvider`.
Adapters share infra (cache, rate-limit, health) via the
:class:`services.market.registry.Registry` they're constructed with.
"""
