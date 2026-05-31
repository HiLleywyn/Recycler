# Progression

Progression is Discoin's long-term meta layer. While trading and the
[Activities](Activities) give you moment-to-moment things to do, progression
systems reward you for playing consistently and across many systems. They turn
scattered actions into badges, payouts, passives, and bragging rights.

This page covers six systems: Achievements, Quests, Mastery, Seasons and the
season pass, NFTs, and Predictions, plus the Sage learn-and-earn network.

> Command examples use the `,` prefix. The prefix is configurable per server,
> so yours may differ.

## Achievements

Achievements are permanent badges earned by hitting milestones across the whole
bot, from your first trade to slaying a dungeon boss to brewing ambrosia. They
are organized into categories and many track progress on counter-based goals.

| Command | What it does |
|---|---|
| `,achievements` (aliases `,ach`, `,badges`) | Browse every achievement, paginated by category, with earned/locked state |
| `,achievements show [@user]` | Show a player's earned achievements, grouped by category |
| `,achievements leaderboard` (aliases `,lb`, `,top`) | Top achievement earners |
| `,streak` | View your activity streak |
| `,streaks` (alias `,streaktop`) | Streak leaderboard |

Achievements are passive: just play, and badges unlock as you cross thresholds.
They feed your `,me` showcase and are a good map of content you have not tried
yet.

## Quests

Quests are short-term goals on a daily and weekly cadence. You get a small set
of slots each period, and quests are auto-assigned the first time you view them
after the period rolls over.

| Command | What it does |
|---|---|
| `,quests` (aliases `,quest`, `,q`) | View your daily and weekly quests with progress bars |
| `,quests claim <slot>` | Claim a completed quest by slot number |
| `,quests claim all` | Sweep every completed unclaimed quest at once |

Daily quests reset at 00:00 UTC; weekly quests reset at the ISO week boundary
(Monday 00:00 UTC). The roll is lazy, so new quests simply appear on your first
view after midnight. Quests are the steadiest source of regular rewards, and
many activities have quest triggers wired in.

## Mastery

Mastery is a cross-system passive progression layer. Every minigame feeds a
track; track levels grant points; points unlock nodes on a skill tree, and those
nodes apply passive bonuses across the entire bot.

| Command | What it does |
|---|---|
| `,mastery` | PNG board plus a summary embed |
| `,mastery tracks` | List all tracks and where their XP comes from |
| `,mastery branches` | Explain the four skill-tree branches |
| `,mastery unlock <id>` | Spend points to unlock a node |
| `,mastery info <id>` | Inspect a single node before spending |
| `,mastery reset` | Paid wipe; the cost doubles each reset |

There are ten tracks (fisher, farmer, delver, trader, gambler, raider, tamer,
validator, crafter, and sage scholar), each capped at level 100. The skill tree
is split into four branches: economy, combat, luck, and utility. Because mastery
nodes are global passives, mastery rewards you for breadth - the more systems you
touch, the more points you earn to spend.

## Seasons and the season pass

Seasons are timed, server-wide competitions run by admins. While a season is
active, players climb a net-worth leaderboard and earn season pass XP from
in-game events.

| Command | What it does |
|---|---|
| `,season` | View the active season and its leaderboard preview |
| `,season last` | Show results of the most recently finalized season |
| `,season history` (aliases `,past`, `,log`) | Past seasons |
| `,season pass` (aliases `,sp`, `,seasonpass`) | Your season pass progress and claimable tier rewards |
| `,season claim <tier\|all>` | Claim an unlocked pass tier reward |
| `,season top` (alias `,passtop`) | Season pass XP leaderboard |
| `,season themes` / `,season theme` | View seasonal themes |

The season pass has 30 tiers. Each tier you unlock with XP grants a reward you
claim with `,season claim`. Seasons can run under rotating themes (such as mining
madness, trading frenzy, or fishing frenzy) that multiply XP for matching
activities. Starting and ending seasons is an admin task; see
[Server Administration](Server-Administration).

## NFTs

NFTs are on-chain collectibles you mint, collect, and trade on Proof-of-Stake
networks (ARC and DSC). Each NFT belongs to an ERC-721 contract and has a unique
token hash, and NFT values count toward your net worth.

| Command | What it does |
|---|---|
| `,nft collections` | See all NFT collections on this server |
| `,nft view <symbol> [token_id]` | View a collection or a specific NFT |
| `,mint <symbol>` (or `,nft mint <symbol>`) | Mint an NFT for the mint price plus gas |
| `,nft inventory` (alias `,nft my`) | View all your NFTs |
| `,nft transfer @user <symbol> <token_id>` | Send an NFT (costs gas) |
| `,nft history <symbol> <token_id>` | Transaction history for an NFT |
| `,nft market` | Browse all listed NFTs |
| `,nft list <symbol> <token_id> <price>` | List an NFT for sale in the network coin |
| `,nft unlist <symbol> <token_id>` | Remove your listing |
| `,nft buy <symbol> <token_id>` | Buy a listed NFT |

Minting rolls a rarity: Common (50%), Uncommon (25%), Rare (15%), Epic (8%), or
Legendary (2%). Marketplace listings are priced in the network's native coin
(ARC or DSC), not USD. Deploying new collections is a developer-tier action; see
the [Developer Guide](Developer-Guide).

## Predictions

Predictions are Polymarket-style markets where you bet on the outcome of
real-world events. Payouts are parimutuel: winnings are proportional to your
share of the winning pool.

| Command | What it does |
|---|---|
| `,predict list` | See all open prediction markets |
| `,predict view <id>` | Market details, odds, and your bets |
| `,predict bet <id> <YES\|NO> <amount\|all>` | Place a bet using USD from your wallet |
| `,predict mybets` | See all your active bets |

A 5 percent house cut goes to the server treasury when a market resolves; the
rest is split among winning bettors by their share of the winning pool. Winners
are DM'd when a market is resolved. Amounts accept shorthand like `all`, `half`,
`$500`, and `1k`. Admins can toggle the predictions module on or off.

## Sage Network

The Sage Network is a learn-and-earn system: four timed quiz games that build
real crypto chart-reading skill and mint tokens on correct answers. It is
counted as a mastery track (sage scholar), so it doubles as a progression
system.

| Command | What it does |
|---|---|
| `,pattern` | Pattern Lab: identify a classical candlestick pattern |
| `,gauge` | Indicator Gauge: read indicators, pick Bearish/Neutral/Bullish |
| `,tknom` | Tokenomics Card: classify a token's supply profile |
| `,cycle` | Cycle Phase: classify the market cycle phase |
| `,sage shop` / `,sage buy <item>` | Buy one-run consumables |
| `,sage stake` / `,sage claim` / `,sage unstake` | Stake EDU to drip SAGE yield |
| `,sage cashout <amt\|all>` | Burn SAGE for USD |
| `,sage lb` / `,sage me` | Leaderboards and your Sage profile |

Each quiz is a survival run: one wrong answer ends it, and rewards scale per
round so the longer you last the bigger each correct pick pays. Correct answers
mint **SAGE** (the network coin) and **EDU** (the game token). Your Sage level
scales payouts further. SAGE can be cashed out to USD; both SAGE and EDU are
earn-only. The AI assistant deliberately refuses to help you mid-run, since the
whole point is to actually learn.

## See also

- [Activities](Activities) - the minigames that feed quests, achievements, and mastery
- [Premium](Premium) - which progression-adjacent features need a subscription
- [Trading](Trading) - the token economy NFTs and Predictions plug into
- [Server Administration](Server-Administration) - starting seasons and toggling modules
