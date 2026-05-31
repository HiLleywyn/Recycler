# Groups and Social

Discoin is built to be played with other people. This page covers the social
systems: mining **Groups** with shared treasuries and group tokens, the
**Showcase** profile dashboard, the **Auction House**, on-chain
**Governance**, and **command chaining** for stringing commands together.

> The command prefix shown here is `,`, but it is configurable per server.
> Server admins can change it, so your server may use a different prefix.

## Mining groups

A mining group is a shared crew. Members pool mining rewards, build up a
group reserve, mint a group token, and unlock a private Hall.

### Membership commands

| Command | What it does |
|---|---|
| `,group create <name> [private]` | Create a group |
| `,group join <name>` | Join an existing group |
| `,group leave` | Leave your group |
| `,group disband` | Disband the group (founder) |
| `,group info [name]` | View a group's details |
| `,group list` / `,group ls` | List groups |
| `,group invite @user` | Invite a player |
| `,group accept` / `,group decline` | Respond to an invite |
| `,group kick @user` | Remove a member (founder only) |
| `,group rename <name>` | Rename the group ($1K, 24h cooldown, founder) |
| `,group privacy public\|private` | Toggle invite-only |

### Group settings

| Command | What it does |
|---|---|
| `,group set description="..." tag=TAG image=url` | Set group profile fields |
| `,group weightmode hashrate\|equal\|custom` | Choose how rewards are split |
| `,group setweight @user <weight>` | Set a member's reward weight (custom mode) |
| `,group reserve` | View the group reserve |
| `,group reserveset <0-100>` | Set the % cut taken from rewards (founder) |

The weight mode decides how shared mining rewards are divided: by hashrate
contribution, equally, or by a custom weight the founder assigns.

### Group mine

`,group mine <mta|sun>` is a founder-only command on a 12-hour cooldown that
reassigns every member's rig to the chosen chain in one move.

```
,group mine mta   move everyone to Moneta mining
,group mine sun   move everyone to SUN mining
```

If your group token is bound to a network, mining the wrong chain still earns
the base crypto reward but mints no group tokens. Mine on your token's network
to earn both.

### Group token

Your group's tag becomes its token symbol. The token is minted into a shared
group vault as members mine.

| Command | What it does |
|---|---|
| `,group token info` | View the token, its network, and the vault balance |
| `,group token network <sun\|mta>` | Bind the token to a PoW network (founder only) |

Once a group token is bound to a network, mining that chain mints tokens into
the group vault, and the vault tokens form an LP pool with the network coin.
Open-market trading of the token must be enabled by a server admin with
`,admin grouptoken enable <SYM>`.

### Cross-group LP pools

Two groups can agree to a shared liquidity pool.

| Command | What it does |
|---|---|
| `,group pool propose <group name or tag>` | Propose a shared LP pool |
| `,group pool accept <proposal id>` | Accept an incoming proposal (target founder) |
| `,group pool decline <proposal id>` | Decline a proposal |
| `,group pool list` | See pending proposals for your group |
| `,group pool cancel` | Cancel your outgoing proposal |

Both groups need a group token. Once a proposal is accepted, both groups can
add liquidity with `,addlp <TOKENA> <TOKENB> <amount_a> <amount_b>`.

### Hall upgrades

The group reserve funds permanent Hall upgrades that buff members. Buy them
with `,group upgrade buy <id>` and browse them with `,group upgrade list`.
Open your Hall with `,group hall open`.

| Upgrade | Cost | Effect |
|---|---|---|
| `hearth` | $35,000 | +5% gambling in the Hall |
| `trophy_wall` | $90,000 | +5% daily in the Hall (requires hearth) |
| `gilded_arch` | $280,000 | +5% work in the Hall (requires trophy_wall) |
| `command_board` | $75,000 | Unlock Earn commands in the Hall |
| `trading_desk` | $225,000 | Unlock trading commands and group token trading |
| `defi_terminal` | $650,000 | Unlock DeFi and LP commands in the Hall |
| `member_wing` | $120,000 | +5 member slots |
| `grand_vault` | $480,000 | +8% gambling in the Hall |

More tiers exist beyond these. Some Hall upgrade lines give group-wide bonuses
that apply to members anywhere, not only inside the Hall.

## Showcase

The Showcase is your single-pane profile dashboard.

```
,me            -- your own showcase
,me @other     -- another player's showcase (read-only)
```

Aliases: `,profile`. Switch tabs with the Select menu on the message. Tabs are
Overview, Wallet, Fishing, Farming, Dungeon, Crafting, Buddies, and
Achievements.

- **Overview** - name, net worth, and the wallet / bank / CeFi / DeFi / LP /
  stake split.
- **Wallet** - your USD balance plus every held token, sorted by symbol.
- **Fishing / Farming / Dungeon** - level, XP, lifetime token earnings, and
  wild-battle counters.
- **Crafting** - forge level and lifetime INGOT and FORGE earned.
- **Buddies** - your top 8 buddies by level, with the active one starred.
- **Achievements** - your 8 most recent badges and total count.

## Auction House

The Auction House is one shared listings table that accepts any item kind:
buddies, eggs, fish, crops, ore, weapons, armors, consumables, and crafted
items.

| Command | What it does |
|---|---|
| `,ah` | Open the categorised browser |
| `,ah browse [kind]` | Browser, optionally pre-filtered by kind |
| `,ah search <text>` | Free-text search by name, species, or token id |
| `,ah list <kind> <ref> [qty] <price> [currency] [--ttl=days]` | Post a listing |
| `,ah buy <id> [pay_currency]` | Buy a listing |
| `,ah inspect <id>` | Full listing details and token id |
| `,ah cancel <id>` | Pull your own listing |
| `,ah mine [status]` | Your listings (active, sold, cancelled, expired) |

Currency and slippage: each kind has a home-network currency (buddy and egg
default to BUD, fish to LURE, crop to HRV, ore/weapon/armor/consumable to RUNE,
crafted to INGOT). Sellers can override it. Paying in the listed currency is a
direct trade with no slippage; paying in a different token is AMM-routed at
oracle price minus roughly 1% impact.

Every listed item gets a stable `<network>:<hex>` token id (for example
`bud:k889ka2c`). Listings expire after 7 days by default; pass `--ttl=N` to
change it (0 means never). The house fee is 5% of the sale price, burned as an
economy sink.

```
,ah list buddy 1234 50000          buddy id 1234 for 50k BUD
,ah list fish bass 10 25 LURE      10 bass at 25 LURE total
,ah buy 17                         buy listing #17 in its listed currency
,ah buy 17 USD                     buy #17 with USD (cross-currency, slippage)
```

## Governance

Governance is on-chain style voting using DSC. One DSC equals one vote, counted
across all of your positions.

| Command | What it does |
|---|---|
| `,gov` | List all active proposals |
| `,gov info <id>` | Full detail, live tally, and your vote |
| `,gov vote <id> yes` | Vote YES |
| `,gov vote <id> no` | Vote NO |
| `,gov vote <id> abstain` | Vote ABSTAIN |

Your voting power is all your DSC: CeFi balance plus DeFi wallet plus staked
plus delegated. You can change your vote until the proposal closes. Abstaining
counts toward quorum but not the yes/no ratio.

GM and admin staff create and finalize proposals with
`,gov propose <hours> Title | Description` and `,gov tally <id>`. Proposals run
1 to 336 hours (up to 2 weeks). A proposal passes when quorum reaches at least
5% of the DSC supply and yes votes are more than 51% of yes plus no. DSC
holders are DM'd when a proposal opens, and voters are DM'd when it is
finalized.

## Command chaining

Chaining lets you run multiple commands in one message using operator symbols.
Put the prefix before each command (or omit it in designated bot channels). A
confirmation embed always appears before a chain runs.

```
,buy 0.5 ARC > ,move all ARC bank wallet
,work ; ,daily
,buy MTA + ,buy ARC > ,move all bank wallet
```

### Operators

| Operator | Name | Behavior |
|---|---|---|
| `>` | Sequential | Next step runs only if the previous one succeeded |
| `&&` | Strict AND | Identical to `>`, an explicit form |
| `;` | Fire and forget | Next step always runs, regardless of outcome |
| `\|\|` | Fallback OR | Next step runs only if the previous one failed |
| `\|` | Pipe | Like `>`, but forwards the prior result into the next step |
| `+` | Parallel | Adjacent steps run concurrently |

A `+` group runs concurrently, and a following `>` waits for all of them to
finish before continuing.

### Amount expressions

Chain steps (and all commands) accept flexible amounts:

| Expression | Meaning |
|---|---|
| `all` / `everything` | Full balance of that token or USD |
| `half` / `quarter` / `third` | 50% / 25% / ~33% of balance |
| `$500` | A dollar-value amount ($500 worth) |
| `1.5k` / `2m` / `1b` | Shorthand (k = thousand, m = million, b = billion) |
| `1/3` | Fraction notation |
| `100` / `50.5` | A plain number |

### Scheduled delays

Append a delay phrase to any chain step to schedule it for later:

```
,buy 100 MTA in 5m
,sell ARC all > ,buy DSC in 1h
```

Use `in`, `after`, or `wait` followed by a number and a unit (`s`, `m`, `h`,
`d`). The maximum delay is one week.

### Fuzzy matching

Chain steps tolerate typos and alternate names: `buy` also matches `purchase`,
`acquire`, `long`, or `get`; `sell` matches `dump`, `liquidate`, `short`, or
`unload`; `swap` matches `exchange`, `convert`, or `trade`. Filler words like
`my`, `some`, `please`, `from`, and `the` are stripped from arguments, so
`,please sell some of my ARC` resolves to `,sell ARC`.

## Why it matters

The social layer is where Discoin becomes a shared economy. Groups turn solo
mining into a coordinated operation with its own token and treasury. The
Auction House gives every item a real market. Governance puts economy
decisions in players' hands. Command chaining ties it all together so a full
routine - earn, move, stake - runs in a single message.

## See also

- [Mining](Mining)
- [DeFi](DeFi)
- [Trading](Trading)
- [Activities](Activities)
