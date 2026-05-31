# Security & Moderation

Discoin includes multiple layers of security: AI scam detection, an anti-bot CAPTCHA system, a passive economy security monitor, a player report system, and structural protections against common exploits.

---

## AI Scam Detection

Discoin's scam detection uses AI (via OpenRouter) to classify messages containing URLs. It requires `OPENROUTER_API_KEY` to be set in your environment.

### How it works

1. **Fast gate** -- Every message is checked for URLs using a regex filter. Messages without URLs are skipped. `discord.gg` invite links are intentionally excluded.
2. **AI verdict** -- Messages with URLs are sent to the AI classifier, which returns a scam/not-scam JSON verdict. The AI reads the full message context, so normal crypto discussion does not trigger false positives.
3. **Action pipeline** -- If the message is classified as a scam: the message is deleted, the user is timed out, a reply is posted, and mods are alerted via the configured channel and DM notifications.

Users with **Manage Messages** permission (mods) are never flagged.

### User-initiated checks

Any user can reply to a suspicious post and @mention the bot to trigger a manual scam check. This bypasses the URL gate and can catch DM solicitation attempts.

### Configuration

```
.admin scam status                   # view current settings
.admin scam on                       # enable scam detection
.admin scam off                      # disable scam detection
.admin scam channel #mod-alerts      # set alert channel
.admin scam timeout 60               # timeout scammers for 60 minutes
.admin scam timeout 0                # disable timeout (delete + alert only)
.admin scam notify @ModUser          # add a mod to DM notifications
.admin scam notify @ModUser          # run again to remove them
.admin scam notifylist               # list all notification recipients
```

| Setting | Default | Range |
|---|---|---|
| Timeout duration | 10 minutes | 0--10,080 minutes (0 = off, max 7 days) |

!!! tip
    Start with a moderate timeout (30--60 minutes) and review alerts in your mod channel. Adjust based on your server's scam volume.

---

## Anti-Bot System

The anti-bot system detects suspicious play patterns in gambling games and presents a word-based CAPTCHA challenge. It runs automatically with no admin configuration required.

### Detection triggers

The system tracks two types of patterns:

**Same-game streak** -- When a user plays the same game repeatedly, a random threshold is picked between `ANTIBOT_MIN_GAMES` (default 50) and `ANTIBOT_MAX_GAMES` (default 100). When the streak hits that threshold, a CAPTCHA appears.

**Cross-game frequency** -- If a user plays any combination of games more than 40 times within a 5-minute window, a CAPTCHA is triggered. This catches bots that rotate between games to dodge streak detection.

### CAPTCHA mechanics

- The CAPTCHA uses **word-based arithmetic** (e.g. "What is seven plus three?") -- harder for bots to parse than plain digits.
- Four answer buttons are shown (one correct, three decoys).
- The user has **30 seconds** to answer.
- Only the targeted user can interact with the buttons.

### Outcomes

| Outcome | Result |
|---|---|
| Correct answer (normal speed) | Streaks reset, play continues |
| Correct answer (< 0.8 seconds) | Flagged as suspiciously fast, 1-hour lockout |
| Wrong answer | 1-hour game lockout |
| Timeout (no answer in 30s) | 1-hour game lockout |

### Persistent lockouts

Game lockouts are stored both in-memory and in the database (`game_lockout_until` column on the users table). This means lockouts survive bot restarts.

### Admin notifications

On every lockout, the bot DMs the `REPORT_TARGET_USER_ID` with:

- The user's ID and mention
- The server name
- The trigger reason (e.g. "Failed CAPTCHA (same-game streak: slots x72)" or "Answered CAPTCHA in 0.3s (cross-game frequency)")

### Tuning

Set these via environment variables:

| Variable | Default | Description |
|---|---|---|
| `ANTIBOT_MIN_GAMES` | `50` | Minimum streak before CAPTCHA is possible |
| `ANTIBOT_MAX_GAMES` | `100` | Maximum streak before CAPTCHA is guaranteed |

The actual threshold is randomized within this range for each new streak, making it unpredictable for bots.

!!! warning
    Setting `ANTIBOT_MIN_GAMES` too low will annoy legitimate players. Setting it too high gives bots more room. The defaults are tuned for a balance.

---

## Economy Security Monitor

The economy security monitor is a **passive background system** that scans transaction patterns and alerts admins. It never mutes, times out, or penalizes players -- it is observation and reporting only.

### Detection types

| Detection | Trigger | Description |
|---|---|---|
| **Income velocity** | >20 income transactions in 5 minutes | Player earning far more than normal in a short window |
| **Gambling velocity** | >50 game results in 5 minutes | Playing games at inhuman speed or volume |
| **Wash trading** | >6 buy+sell cycles of the same token in 5 minutes | Buying then selling (or swap loops) with no economic purpose |
| **Transfer rings** | >4 transfers from one user in 5 minutes | Rapid fund movement suggesting money laundering |
| **LP churn** | >4 adds + >4 removes in 5 minutes | Rapid add/remove liquidity (sandwich-style extraction) |
| **Whale concentration** | >3 whale-tier actions in 5 minutes | Single user dominating transaction volume |
| **Transaction flood** | >80 total transactions in 5 minutes | Unusually high activity across all features |
| **Repeat offender** | Flagged 3+ times in one session | Escalation for users who repeatedly trigger alerts |

### How it operates

1. **Periodic scan** -- Every 2 minutes, the monitor queries the last 5 minutes of transactions from the ledger and groups them by user.
2. **Real-time events** -- The monitor subscribes to `whale_alert` and `game_result` bus events for instant detection of whale concentration patterns.
3. **AI-generated alerts** -- When suspicious activity is detected, an AI summary is generated and sent as a DM embed to the `REPORT_TARGET_USER_ID`.

### Alert format

Each alert DM includes:

- Player mention and ID
- Server name and ID
- Raw detection details (type, counts, dollar amounts)
- Whale activity history (if applicable)
- Flag count for the session (with "REPEAT" escalation label)
- AI-generated narrative summary

### Alert cooldown

To prevent spam, the monitor enforces a **10-minute cooldown** per user per server. The same user will not generate another alert within that window.

!!! tip
    The economy security monitor requires no configuration. It runs automatically on all servers. Set `REPORT_TARGET_USER_ID` in your environment to receive the DM alerts.

---

## Report System

The report system allows users to submit reports and admins to manage them through an interactive DM-based workflow.

### User side

Users submit reports with the slash command:

```
/report <category> <message>
```

Categories: `bugs`, `suggestions`, `users`, `other`. A 5-minute cooldown prevents spam.

### Admin triage workflow

When a report is submitted, the admin (`REPORT_TARGET_USER_ID`) receives a DM embed with interactive buttons:

**Stage 1: Triage**

- **Accept** -- Moves to accepted status, opens the management view
- **Reject** -- Closes the report
- **Message Reporter** (chat bubble button) -- Opens a modal to send a DM to the reporter

**Stage 2: Management** (after accepting)

- **In Progress** -- Marks work has begun
- **Resolve** -- Opens a modal for a resolution note
- **Close** -- Closes the report
- **Reward** (money bag button) -- Opens a modal to reward the reporter with currency
- **Tag selector dropdown** -- Apply admin tags to categorize the report

### Report lifecycle

```
open → accepted → in_progress → resolved → closed
open → rejected
```

Each status change DMs the reporter and updates the admin's DM embed in place.

### Tags

Admins can apply tags via the dropdown selector on the DM embed:

| Tag | Label |
|---|---|
| `high_priority` | High Priority |
| `low_priority` | Low Priority |
| `bug_confirmed` | Bug Confirmed |
| `wont_fix` | Won't Fix |
| `duplicate` | Duplicate |
| `ui` | UI Issue |
| `economy` | Economy |
| `crash` | Crash |
| `performance` | Performance |
| `feature_request` | Feature Request |

### Rewards and bounties

The **Reward** button lets admins credit the reporter's wallet with a specified USD amount. If a matching bounty exists for that report category, the bounty bonus is automatically applied on top of the reward and the bounty claim counter is incremented.

The reporter receives a DM showing the breakdown:

```
You received $500.00 for your report #42.
Plus a $200.00 bounty bonus for "Critical Bug Bounty"!
Total: $700.00 added to your wallet.
```

### Reports feed

Status changes can be posted to a public channel:

```
.admin setchannel reports #report-feed
.admin reportsfeed bugs,suggestions      # filter which categories appear
```

---

## What the Bot Protects Against

Discoin includes structural protections against common economy exploits. These are not configurable -- they are always active.

### Race conditions and double-spend

All balance modifications use atomic database operations. Concurrent commands from the same user are serialized through the transaction system, preventing double-spend exploits.

### Chain-hopping

The mempool and validator block system processes transactions in order. Admin rejection of pending actions (`admin reject`) properly refunds locked tokens while preserving gas fee penalties.

### Whale dominance

Multiple layers prevent a single player from dominating the economy:

- **Whale alert threshold** -- Large transactions trigger alerts to admins
- **Whale concentration detection** -- The security monitor flags users with 3+ whale-tier actions in a 5-minute window
- **LP concentration cap** -- No single LP can own more than 50% of a pool (`LP_MAX_CONCENTRATION`)
- **Per-user swap hourly limit** -- Rolling 1-hour volume cap of $500,000 per user
- **Max swap fraction** -- No single swap can move more than 15% of a pool's reserves (5% for low-liquidity pools)
- **Aggregate daily income cap** -- $1M/day ceiling across all income sources

### Wash trading

The economy security monitor specifically detects buy-then-sell cycles of the same token within short windows and flags them as wash trading alerts.

### Pool manipulation

- **Circuit breakers** -- Pools are halted if any reserve drops 20% within a 10-minute window, with a 5-minute cooldown before reopening
- **LP lock period** -- 2-hour minimum hold after adding liquidity prevents instant add-remove sandwich attacks
- **Large removal throttling** -- Removals exceeding 25% of a pool's value require a 10-minute cooldown between them
- **Oracle rebalancing** -- Pools automatically rebalance when the AMM price deviates more than 0.5% from the oracle price
- **Fee burn** -- 25% of all swap fees are permanently burned, making fee extraction less profitable

### MEV and sandwich attacks

- **Transaction shuffling** -- Transactions within the same gas tier are randomized (`MEV_SHUFFLE_WITHIN_TIER`)
- **Validator self-dealing prevention** -- A validator's own transactions execute last in their block (`MEV_VALIDATOR_LAST`)
- **Per-user swap limit** -- Maximum 2 swaps per user per validator block prevents sandwich patterns

### Lending safety

- **Collateral seasoning** -- 1-hour hold required before collateral is eligible for lending
- **Liquidation penalty** -- 5% burned on liquidation discourages risky borrowing
- **Forbidden collateral** -- Stablecoins cannot be used as loan collateral to prevent circular leverage

### Staking safety

- **Early unstake penalty** -- 5% burn on unstaking within 48 hours of staking
- **Warmup period** -- 12-hour linear ramp to full staking rewards
- **Slash risk distribution** -- Slash events are spread across hourly ticks to keep validators risky without being mathematically doomed

### Bot abuse

- **CAPTCHA system** -- Word-based arithmetic challenges with response time analysis (see Anti-Bot System above)
- **Persistent lockouts** -- Game lockouts survive bot restarts via database persistence
- **Cross-game detection** -- Bots that rotate between games are still caught by the frequency-based trigger
