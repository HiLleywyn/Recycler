# Commands

A complete command reference for Discoin, grouped by system. Every command
listed here comes from the in-bot help catalog or cog source.

Examples use a comma (`,`) prefix. The prefix is configurable per
server -- a server admin can change it with `,admin setprefix`, and the
current prefix is shown in `/help`.

## The in-bot help system

Discoin's authoritative help lives inside the bot:

| Command | What it does |
|---|---|
| `,help` | Interactive help home with category select menus |
| `,help <category>` | Detailed help for one category (for example `,help mining`) |
| `,help <SYMBOL>` | Routes a token symbol to the relevant game's help page |
| `,admin help` | Staff command reference (prefix-only) |
| `,gm help` | Game-master command reference (prefix-only) |

Slash commands (type `/` in Discord) are **informational only**: `/help`,
`/balance`, `/leaderboard`, `/notify`, `/inventory`, `/report`, `/reports`,
and `/2fa`. All actions use the prefix.

Many commands accept flexible amount expressions: `all` / `everything`,
`half`, `quarter`, `third`, `$500` (dollar value), `1.5k` / `2m` / `1b`
shorthand, and `1/3` fraction notation.

## Earning and Economy

Core wallet, banking, and income commands. See [Economy](Economy),
[Getting Started](Getting-Started), and [Activities](Activities).

### Economy (`,help economy`)

| Command | Description |
|---|---|
| `,balance` (`,bal`) | Paginated net worth; flags `crypto`, `staking`, `mining`, `network <net>` |
| `,deposit <amt>` (`,dep`) | Move USD from wallet to bank |
| `,withdraw <amt>` (`,with`) | Move USD from bank to wallet |
| `,transfer @user <amt>` (`,give`, `,pay`) | Send USD to another player |
| `,move <amt> <token> <from> <to>` (`,mv`) | Move assets between storage |
| `,leaderboard` (`,lb`, `,top`) | Rankings by net worth or category |
| `,wallet create/list/delete/info` | Manage on-chain DeFi wallets |
| `,wallet deposit/withdraw <TOKEN> <amt>` | Move tokens between CeFi and DeFi |
| `,send <@user\|addr> <amt> [network] [token]` | On-chain token transfer |

### Daily, Work and Jobs (`,help daily`, `,help jobs`)

| Command | Description |
|---|---|
| `,daily` | Claim the daily reward (once per 24h, streak bonus stacks) |
| `,work` | Earn job pay on a cooldown |
| `,job` / `,jobs` | View your job tier or list all tiers |
| `,promote` | Advance to the next job tier when eligible |
| `,ape` (`,degen`, `,yolo`) | Ape into a random shitcoin (degen mode) |
| `,beg` | All-or-nothing street begging |

### Rugpull and Eat the Rich (`,help daily`)

| Command | Description |
|---|---|
| `,rugpull [low\|med\|high]` | Challenge the reigning monarch for the throne |
| `,king` (`,queen`) | See the current monarch and active mechanics |
| `,taxdecree <0.25-1.0>` | Monarch: set the cut kept from failed wagers |
| `,rugbounty <amt>` | Add USD to the bounty pool |
| `,sabotage <amt>` | Decay the monarch's defense streak |
| `,rugdefend <amt>` | Monarch: buy a challenger debuff |
| `,rughistory` / `,rugstats [@user]` | Challenge history and personal stats |
| `,eat @target` | Pick a tactic (Type 1/2/3) and eat a player |
| `,eat bite @target [pool]` | Precision strike on one pool (wallet/crypto/defi/bank) |
| `,eat prep` / `,eat cook` | The two-stage eat powerup chain |
| `,eat salad` / `,eat rich` | View the salad bowl / 1% gamble to devour it (needs cook) |
| `,eat defend` | Hire a 2h security detail |
| `,eat stats/history/lb/help` | Eat the Rich stats and reference |

### Faucet (`,help faucet`)

| Command | Description |
|---|---|
| `,faucet` | Triggers the auto-faucet manually (mods); the faucet appears automatically |
| `,airdrop <amt> [symbol]` | Donate your tokens as a public drop |

### Wealth Bottleneck (`,help wealth`)

| Command | Description |
|---|---|
| `,bottleneck` (`,wealth`, `,bn`) | Your rank, gain multiplier, and recent flow |
| `,bottleneck curve` | The full rank-to-multiplier curve |
| `,bottleneck pool` | Community-pool snapshot and 24h flow |
| `,bottleneck me` / `,recent` | Your drag/boost history and recent guild events |

## Markets and Trading

Token prices, buying, selling, swapping, charts, and the chain explorer.
See [Trading](Trading) and [Economy](Economy).

### Crypto Market (`,help crypto`)

| Command | Description |
|---|---|
| `,crypto` (`,prices`, `,market`) | Full market by network; filter by network or symbol |
| `,buy <SYM> <amt>` | Buy a token with USD; flags `yes`/`-y`, `with <SYM>` |
| `,sell <SYM> <amt\|all>` | Sell a token for USD; `,sell everything` sells all CeFi holdings |
| `,swap <FROM> <TO> <amt>` | Swap a token pair through AMM pools (same network) |
| `,portfolio` (`,port`) | Your holdings with current value |
| `,tokeninfo <SYM>` (`,ti`) | Price, supply, fees, and LP liquidity for a token |

### Charts (`,help chart`)

| Command | Description |
|---|---|
| `,chart <PAIR> [timeframe] [indicators...]` (`,c`) | Price chart with 20+ indicators, comparisons, and themes |

### Chain Explorer (`,help chain`)

| Command | Description |
|---|---|
| `,chain block [number] [network]` | Block details |
| `,chain tx <hash>` (`,chain txinfo`) | Transaction lookup by hash |

### Real Markets (`$` prefix) (`,help realmarket`)

A separate prefix-only namespace for live cross-asset market data. It is
isolated from the simulated game market.

| Command | Description |
|---|---|
| `$chart <SYMBOL> [tf] [indicators...]` | Live candlestick chart |
| `$scan <SYMBOL> [tf]` | Pattern and indicator scout; append `ai` for commentary |
| `$info <SYMBOL>` | Full asset snapshot (crypto, stocks, ETFs, perps) |
| `$market <sub>` | Market-wide views (fear, heatmap, gainers, losers, dom, global) |
| `$compare <A> <B>` | Normalised view across 2-4 symbols |
| `$oracle / $funding / $oi <SYMBOL>` | Oracle quotes, funding rate, open interest |
| `$watch add/list/remove/clear` | Personal watchlist with price alerts |
| `$query <question>` | AI market Q&A with cited sources |
| `$status` | Diagnose provider health |

### Predictions (`,help predictions`)

| Command | Description |
|---|---|
| `,predict list` | Browse open prediction markets |
| `,predict view <id>` | Market details, odds, and your bets |
| `,predict bet <id> <YES\|NO> <amt>` | Place a bet (USD from wallet) |
| `,predict mybets` | View your active bets |

## Yield and DeFi

Staking, validators, pools, savings, smart contracts, and Moon Network
yield. See [Staking and Validators](Staking-and-Validators),
[DeFi](DeFi), and [Mining](Mining).

### Yield Farming / Staking (`,help staking`)

| Command | Description |
|---|---|
| `,stake list` | Browse all yield farms by network |
| `,stake farm <FARM_ID> <amt>` | Stake into a yield farm |
| `,stake unstake <FARM_ID> <amt>` | Withdraw from a farm |
| `,stake mine` | Your active staking positions |
| `,autocompound on/off/status` (`,ac`) | Auto-restake staking rewards each tick |

### PoS Validators (`,help validators`)

| Command | Description |
|---|---|
| `,stake validator register <network> <amt>` | Register as a validator |
| `,stake validator unregister <network>` | Stop validating |
| `,stake validator commission <network> <rate>` | Set your commission rate |
| `,stake validator list/stats/networks` | Validator listings and stats |
| `,stake validator mempool [network]` | View the pending mempool |
| `,stake validator submit <type> <net> <gas> <payload>` | Submit a mempool action |
| `,stake validator delegate @val <network> <amt>` | Delegate stake to a validator |
| `,stake validator undelegate @val <network> <amt>` | Withdraw a delegation |
| `,stake validator delegations` | Your active delegations |

### Pools and Swaps (`,help pools`)

| Command | Description |
|---|---|
| `,trade pool list [filter]` | List AMM liquidity pools |
| `,trade pool add <A> <B> <amt_a> <amt_b>` | Add liquidity |
| `,trade pool remove <A> <B> <shares\|all>` | Remove liquidity |
| `,trade pool lock <A> <B> <7\|30\|90>` | Time-lock a position for a Liqstone XP boost |
| `,trade pool unlock <A> <B>` | Break a lock early (burns 10% of shares) |
| `,trade pool price <PAIR>` | Pool price |
| `,swap <FROM> <TO> <amt>` | Swap through AMM pools |

### Savings (`,help savings`)

| Command | Description |
|---|---|
| `,save <amt\|all>` | Deposit USD to earn savings APY |
| `,unsave [amt\|all]` | Withdraw savings to your wallet |
| `,savings` (`,mysavings`) | Your savings balances and live rates |
| `,rates` (`,apy`) | The full rate curve |

### Smart Contracts (`,help contracts`)

| Command | Description |
|---|---|
| `,contract deploy <name> <network> [type]` (`,ct`) | Deploy an on-chain contract |
| `,contract call <address> <function>` | Call a contract function |
| `,contract info/list/events/txs <address>` | Inspect a contract |
| `,contract fund/withdraw <address> <TOKEN> <amt>` | Move tokens in or out |
| `,contract pause/resume <address>` | Pause or resume a contract |

### Moons and Moon Network (`,help moons`)

| Command | Description |
|---|---|
| `,moon stake <GROUP_SYM> <amt>` | Lunar Mint: stake a group token, earn MOON |
| `,moon unstake <GROUP_SYM> [amt]` | Withdraw a Lunar Mint position |
| `,moon info` / `,moon list` | Your lunar positions |
| `,moon pool stake/unstake/info` | Moon Pool: stake MOON, earn a MTA/ARC/DSC/SUN basket |
| `,moon burn <amt>` | Destroy MOON for a slice of every group token |
| `,moon wrap/unwrap <mta\|sun\|mmta\|msun> <amt>` | Wrap and unwrap MMTA / MSUN |

## Mining

PoW mining of SUN and MTA. See [Mining](Mining).

### PoW Mining (`,help mining`)

| Command | Description |
|---|---|
| `,chain mine rigs` | Rig catalog and your quantities |
| `,chain mine buy <RIG_ID> [qty] [mta\|sun]` | Purchase mining rigs |
| `,chain mine sell <RIG_ID> [qty\|all]` | Sell rigs at 50% price |
| `,chain mine assign <qty\|all> <RIG_ID> <mta\|sun>` | Move rigs between chains |
| `,chain mine status` | Your hashrate, mode, and earnings |
| `,chain mine history` | Recent blocks |
| `,chain mine solo/pool/group` | Switch mining mode |
| `,chain mine network <net>` | Network mining stats |

## Activities

Optional minigame economies, each with its own tokens. Several are premium
features. See [Activities](Activities) and [Gambling](Gambling).

### Gambling (`,help gambling`)

| Command | Description |
|---|---|
| `,play coinflip <amt> [token] [mode...]` (`,play cf`) | Coinflip, five modes |
| `,play slots <amt> [token]` (`,play sl`) | Three-reel slots |
| `,play dice <amt> [token] [mode...]` | Dice, six modes |
| `,play roulette <amt> [token] <bet_type>` (`,play rou`) | European roulette |
| `,play blackjack <amt> [token]` (`,play bj`) | Blackjack vs the dealer |
| `,play mines <amt> [bombs] [token]` | Minesweeper-style cash-out game |
| `,play stats [@user] [game] [period]` | Gambling stats and leaderboards |

### Fishing (`,help fishing`)

| Command | Description |
|---|---|
| `,fish` (`,cast`) | Cast your line and hook a catch |
| `,fish inv` / `,fish sell <target>` / `,fish history` | Inventory, selling, history |
| `,fish shop` / `,fish buy <item>` | Buy rods, bait, and traps |
| `,fish bait <key\|none>` / `,fish trap` | Equip bait, manage crab traps |
| `,fish zones` / `,fish zone <key>` | List and switch fishing zones |
| `,fish stats [@user]` / `,fish lb` | Stats and leaderboards |

### Delve Crawler (`,help dungeon`)

| Command | Description |
|---|---|
| `,delve class <warrior\|mage\|rogue>` | One-time permanent class pick |
| `,delve start/next/descend/rest` | Run lifecycle |
| `,delve attack/skill/flee/capture/use <item>` | Combat actions |
| `,delve mine` / `,delve open` | Mine ore veins, open chests |
| `,delve shop/buy/equip/inv/stats/lb` | Gear, inventory, and stats |
| `,delve party/summon/release` | Manage captured buddies |
| `,delve swap/stake/unstake/claim/cashout` | Crypt Network token economy |
| `,delve arena fight/duel/leaderboard/profile` | Ranked PvP |

### Farming (`,help farming`)

| Command | Description |
|---|---|
| `,farm` | Open the field view |
| `,farm plant/water/fertilize/harvest <slot>` | Tend your plots |
| `,farm zones` / `,farm zone <key>` / `,farm crops` | Zones and the crop catalog |
| `,farm shop/buy/equip` | Buy plots, fertilizer, and seeds |
| `,farm sell/process/bag/history` | Market, recipes, inventory |
| `,farm swap/stake/unstake/claim/cashout` | Harvest Network token economy |
| `,farm battle` | Engage a wild buddy that ambushed your field |

### Crafting / Forge Network (`,help crafting`)

| Command | Description |
|---|---|
| `,craft` | Forge dashboard |
| `,craft list/book/info <key>` | Browse recipes |
| `,craft make <key> [qty]` / `,craft apply <key>` | Craft items, apply them back |
| `,craft bag/history` | Crafted-item inventory and history |
| `,craft specialties/specialize/despecialize` | Manage crafting specialties |
| `,craft stake/swap/claim/unstake/cashout` | INGOT and FORGE token economy |

### Sage Network (`,help sage`)

Crypto learn-and-earn quiz games that mint SAGE and EDU.

| Command | Description |
|---|---|
| `,pattern` | Identify a classical chart pattern |
| `,gauge` | Read indicators and call bearish/neutral/bullish |
| `,tknom` | Classify a token's supply card |
| `,cycle` | Identify the market cycle phase |
| `,sage shop/buy` | One-run consumables |
| `,sage stake/claim/unstake/cashout` | EDU and SAGE token economy |
| `,sage lb` / `,sage me` | Leaderboards and progress |

### Buddies (`,buddy help`)

Companion pets with battling, breeding, and a BUD/FREN/BBT token economy.
See the [Buddies](Buddies) page; buddies also have an in-cog `,buddy help`.

| Command | Description |
|---|---|
| `,buddy` / `,buddy stats` | Your active buddy panel |
| `,buddy hatch` / `,buddy talk` / `,buddy rename` | Hatch, chat with, rename a buddy |
| `,buddy shop` / `,buddy gear shop/buy/equip` | Buddy shop and gear |
| `,buddy stake/unstake/claim/convert/cashout` | BUD / FREN / BBT token economy |
| `,buddy battle fight` / `,buddy arena fight` | Buddy PvP battles and ranked arena |
| `,buddy market/list/buy` | The buddy marketplace |
| `,buddy slot/nest/attractor` | Slot, nest, and attractor upgrades |

### Gamba Network (`,gamba info`)

The Gamba Network token economy tied to gambling. It has its own in-cog
help page.

| Command | Description |
|---|---|
| `,gamba` | Gamba Network panel |
| `,gamba stake/unstake/claim` | Stake game tokens for yield |
| `,gamba yield/stakes/autocompound` | Yield targets and positions |
| `,gamba cashout` | Burn the network coin for USD |
| `,gamba shop/buy/inventory` | Gamba Network shop |

### Auction House (`,help auction`)

| Command | Description |
|---|---|
| `,ah` / `,ah browse [kind]` | Open the categorised listings browser |
| `,ah search <text>` | Free-text search |
| `,ah list <kind> <ref> [qty] <price>` | List an item for sale |
| `,ah buy <id> [currency]` / `,ah inspect <id>` | Buy and inspect listings |
| `,ah cancel <id>` / `,ah mine` | Manage your own listings |

## Social and Groups

Mining groups, NFTs, and the auction house. See
[Groups and Social](Groups-and-Social) and [Shop and Items](Shop-and-Items).

### Mining Groups (`,help groups`)

| Command | Description |
|---|---|
| `,group create/join/leave/disband` | Group membership |
| `,group info/list` | Group details and listings |
| `,group invite/accept/decline/kick` | Manage members |
| `,group set` / `,group weightmode` / `,group setweight` | Group settings |
| `,group mine <mta\|sun>` | Founder: move all members to a chain |
| `,group token info/network` | Manage the group token |
| `,group pool propose/accept/decline/list/cancel` | Cross-group LP pools |
| `,group upgrade list/buy` / `,group hall open` | Hall upgrades |

### NFTs (`,help nfts`)

| Command | Description |
|---|---|
| `,nft collections` / `,nft view <symbol>` | Browse collections |
| `,mint <symbol>` (`,nft mint`) | Mint an NFT |
| `,nft inventory` / `,nft transfer` / `,nft history` | Your collection |
| `,nft market/list/unlist/buy` | NFT marketplace |
| `,nft deploy <config>` | Deploy a collection (Protocol Dev tier+) |
| `,token deploy ...` / `,token info <symbol>` | Deploy and inspect ERC-20 tokens |

## Progression

Items, stones, and the systems that track your growth. See
[Progression](Progression) and [Shop and Items](Shop-and-Items).

### Item Shop and Stones (`,help shop`, `,help stones`)

| Command | Description |
|---|---|
| `,shop` | Browse all items with ownership status |
| `,shop buy <item> [currency]` | Acquire an item by staking currency |
| `,shop sell <item>` | Sell a stone back |
| `,shop transfer <item> @user` | Peer-to-peer item transfer |
| `,inventory` (`,inv`) | View items: level, XP, staked amount, bonuses |
| `,inventory levelup <item> [currency]` | Pay to claim a level |
| `,inventory use <item>` | Activate a consumable |
| `,autolevelup on/off` | Auto-level stones when XP and funds are ready |

### Showcase (`,help showcase`)

| Command | Description |
|---|---|
| `,me` | Your single-pane stats/wallet/skills/buddies dashboard |
| `,me @other` | View another player's showcase (read-only) |

### Governance (`,help governance`)

| Command | Description |
|---|---|
| `,gov` | List active proposals |
| `,gov info <id>` | Proposal detail and live tally |
| `,gov vote <id> <yes\|no\|abstain>` | Vote with your DSC holdings |

## Utility

Notifications, charts, status, security, and bot info.

### Notifications (`,help notifications`)

| Command | Description |
|---|---|
| `,notify` | Show your DM notification preferences |
| `,notify <category> on\|off` | Toggle a notification category |
| `,notify <category> <network> on\|off` | Per-network muting |

### Security and 2FA (`,help security`)

| Command | Description |
|---|---|
| `,2fa` / `,2fa status` | Check whether 2FA is enabled |
| `,2fa setup` | Set up 2FA (QR code sent by DM) |
| `,2fa disable` | Disable 2FA (requires a code) |

### Bot status and info

| Command | Description |
|---|---|
| `,status` | Live health for all bot services |
| `,info` (`,about`) | About this Discoin instance |
| `,changelog` | Recent changes |
| `,alert add/list/remove/clear` | Token price alerts (beta feature) |

### Events and Vaults (`,help events`)

| Command | Description |
|---|---|
| `,event` / `,event list` | View the active market event or browse types |
| `,vault [network]` | View network vault progression levels |

## Staff

Staff command groups are **prefix-only** and are never available as slash
commands. See [Server Administration](Server-Administration).

| Group | Description |
|---|---|
| `,admin` | Server admin tooling: give/take, prices, modules, channels, events, halts, permissions (requires Manage Server) |
| `,gm` | Game-master tooling |
| `,drs` | DRS Terminal: trusted-player game management (granted via the `drs_commands` beta feature) |

## See also

- [Getting Started](Getting-Started)
- [Economy](Economy)
- [Activities](Activities)
- [FAQ](FAQ)
