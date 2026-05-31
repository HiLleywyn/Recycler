"""configs -- domain-specific config modules for Discoin.

Each module is a flat data surface (dicts / tuples / small helpers) for one
gameplay system: ``sage_config``, ``buddies_config``, ``crafting_config``,
``fishing_config``, ``items_config``, and so on.

The foundational ``core/config.py`` (``Config`` -- tokens, networks, fees, jobs)
deliberately stays at the repository root: it is imported almost everywhere
and is the canonical ``from config import Config`` entry point. Only the
per-domain ``*_config`` modules live here.

Import a domain config with::

    from configs import sage_config as sc
    from configs.sage_config import SAGE_SHOP_ITEMS
"""
