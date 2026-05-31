# Reports & Bounties

Discoin includes a built-in report and bug bounty system. Players can submit bug reports, suggestions, and feedback directly through Discord. Admins can triage, reward, and track reports.

## Submitting a Report

```
.report <category> <message>
```

Categories:

- `bugs` -- report a bug or broken feature
- `suggestions` -- suggest a new feature or improvement
- `users` -- report a player issue
- `other` -- anything that does not fit the above

Example:

```
.report bugs The .swap command shows wrong slippage when swapping more than 1000 SUN
```

```
.report suggestions Add a leaderboard for mining groups
```

After submitting, you receive a confirmation with your report number (e.g., Report #42). The admin is notified via DM and can accept, reject, or respond to your report.

!!! note "Cooldown"
    You can submit one report every **5 minutes**. If your last report is still open, the bot will suggest editing it instead of creating a new one.

## Editing a Report

If your report is still in **open** status, you can update its message:

```
.report-edit <report_id> <new message>
```

Aliases: `.reportedit`, `.editreport`

Example:

```
.report-edit 42 Actually, the slippage bug only happens with SUN/ARC swaps, not all tokens
```

!!! warning "Open reports only"
    You can only edit reports that are still in "open" status. Once an admin accepts or rejects a report, it can no longer be edited.

## Browsing Reports

View public reports submitted by the community:

```
.reports [category]
```

Only `bugs` and `suggestions` categories are publicly visible. User reports and "other" are private.

Examples:

```
.reports
.reports bugs
.reports suggestions
```

Reports are displayed in a paginated list showing the report number, category, status, and a preview of the message.

## Report Lifecycle

Reports go through these statuses:

| Status | Meaning |
|---|---|
| Open | Newly submitted, awaiting admin review |
| Accepted | Admin has accepted the report |
| Rejected | Admin has rejected the report |
| In Progress | Admin is working on the issue |
| Resolved | The issue has been fixed or addressed |
| Closed | Final state -- report is archived |

```
open --> accepted --> in_progress --> resolved --> closed
  |                                                  ^
  +--> rejected                                      |
                    (admin can reward at any stage) --+
```

You receive a DM notification whenever the status of your report changes.

## Bounties

Bounties are reward programs set up by server admins. When a bounty is active for a category, any qualifying report in that category can earn the reporter a bonus reward on top of the normal report reward.

### Viewing active bounties

```
.bounty list
```

Alias: `.bounty` (with no subcommand)

Shows all active bounties with their reward amounts, categories, and claim counts.

Active bounties also appear as a banner when you browse reports with `.reports`.

### How bounty rewards work

1. An admin creates a bounty targeting a specific category (e.g., "bugs")
2. You submit a `.report bugs <details>` with useful information
3. When the admin resolves your report and issues a reward, any matching bounty bonus is **automatically added** on top
4. You receive a DM with the total reward breakdown

Example:

- Admin creates a bounty: "$500 for critical bug reports" in the `bugs` category
- You submit a bug report and the admin rewards you $200 for the report
- You receive **$200** (report reward) + **$500** (bounty bonus) = **$700 total**

### Bounty commands (admin only)

Create a bounty:

```
.bounty create <reward> <category> <title>
```

Close a bounty:

```
.bounty close <bounty_id>
```

!!! tip "Check for bounties before reporting"
    Run `.bounty list` to see if there are active bounties. Submitting reports in bounty categories gives you a chance at bonus rewards.

## Rewards

When an admin resolves or closes your report, they can optionally attach a USD reward. The reward is deposited directly into your wallet and you receive a DM notification with the amount.

- Report rewards come from the admin (they set the amount manually)
- Bounty bonuses are automatic and stack on top of the manual reward
- The total reward is logged as a `REPORT_REWARD` transaction
