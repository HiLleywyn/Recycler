"""Per-handler modules for the ``$`` namespace dispatcher.

This package keeps the new commands (``$query``, ``$watch``, ``$compare``,
``$oracle``, ``$funding``, ``$oi``, ``$market``, ``$scan ai`` modifier)
out of the main ``cogs/realmarket.py`` file. The dispatcher imports
handlers lazily so a syntax error in one module doesn't take the rest of
the namespace down.
"""

from .args import DollarArgs, parse_dollar_args  # noqa: F401
