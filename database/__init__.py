"""Recycler's slim, clanker-only data layer.

Exposes ``Database`` so the framework's ``from database import Database``
(``FrameworkBot._resolve_db_factory``) resolves to this package instead of the
economy data plane bundled with bot-framework.
"""
from .database import Database

__all__ = ["Database"]
