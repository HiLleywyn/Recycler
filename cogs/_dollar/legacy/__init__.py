"""Migrated bodies of the legacy ``$`` handlers.

Each handler that used to live as a method on
:class:`cogs.realmarket.RealMarket` now lives in its own module here.
The cog keeps a one-line shim per handler so the existing dispatcher
+ on_message glue keeps working unchanged.

Public API: ``await <module>.handle(ctx, raw_args, *, cog=cog_instance)``
where ``cog_instance`` is the live :class:`RealMarket` cog so callers
can reach ``cog.client`` (the CoinGecko :class:`RealMarketClient`) and
``cog.bot``.
"""
