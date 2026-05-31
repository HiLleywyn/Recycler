"""bot_manifest.py -- Recycler's identity + cog registry.

Recycler is the ``,clanker`` containment bot. It runs on the shared
``bot-framework`` and loads exactly one feature: Clanktank. Everything else
(the runtime, the data plane, the AI bridge to Sojourns) comes from the
framework. To add a clanker sub-feature, add its cog module here.
"""

APP_NAME = "Recycler"

# ALL ,clanker features live in a single cog and nothing else loads here.
COGS = [
    "cogs.clanktank",  # Clanktank: the ,clanker scammer/bot containment system
]
