"""Recycler data plane.

The framework imports ``database.Database`` lazily and calls ``connect()``
at boot. ``Database`` here is the slim :class:`PgDatabase` -- no economy.
"""
from database.database import PgDatabase as Database, get_database

__all__ = ["Database", "get_database"]
