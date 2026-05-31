"""
core/framework/agent_tools/tools -- built-in agent tool implementations.

Importing this package registers every tool with the ToolRegistry. The
bot loader calls ``load_builtin_tools()`` once during startup.
"""
from __future__ import annotations


def load_builtin_tools() -> None:
    """Import every tool submodule so its @tool decorators run."""
    from . import wallet        # noqa: F401
    from . import market        # noqa: F401
    from . import economy       # noqa: F401
    from . import risk          # noqa: F401
    from . import alerts        # noqa: F401
    from . import data          # noqa: F401
    from . import social        # noqa: F401
    from . import vision        # noqa: F401
    from . import image_gen     # noqa: F401  (registers image.generate if IMAGE_GEN_ENABLED)
    # -- Read-only introspection tools (added for full AI visibility) --
    from . import shop          # noqa: F401
    from . import items         # noqa: F401
    from . import savings       # noqa: F401
    from . import loans         # noqa: F401
    from . import vault         # noqa: F401
    from . import staking       # noqa: F401
    from . import history       # noqa: F401
    from . import leaderboard   # noqa: F401
    from . import groups        # noqa: F401
    # -- Full-coverage tools (earn, mining, gambling, eat, nft) --
    from . import earn          # noqa: F401
    from . import mining        # noqa: F401
    from . import gambling      # noqa: F401
    from . import eat           # noqa: F401
    from . import nft           # noqa: F401
    # -- Thread memory graph: link / unlink / inspect (MUTATE + READ) --
    from . import threads       # noqa: F401
    # -- Autonomous agent: workflow, defi, economy simulation --
    from . import automation    # noqa: F401
    from . import defi          # noqa: F401
    from . import economy_sim   # noqa: F401
