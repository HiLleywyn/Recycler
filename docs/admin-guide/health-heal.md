# `.health heal`  -  Self-Healing Reference

## What it does

`.health heal` scans every auto-checkable system on the bot and fixes what it can in a single pass:

| System | Auto-fix |
|---|---|
| Feed channels pointing at deleted channels | Clears the stale ID from DB |
| MM webhook deleted from Discord | Removes the stale DB record |
| Redis bus offline or ghost connection | Force-closes then reconnects |
| Failed `discord.ext.tasks` loops | Cancels → waits 1 s → restarts |
| Self-heal scheduler not running | Restarts it |

Anything it cannot fix automatically lands in the **⚠️ Manual Action Required** bucket (wrong permissions, forum channels used as feed targets, circuit-breaker-tripped loops).

---

## Scenario: Redis drops overnight, two mining loops die with it

### What happened

It is 3 AM. The Redis container on Railway runs out of memory and restarts. The bot's internal listener loop hits a read error on the dead socket and exits. Two task loops  -  `Trades._mining_tick` and `Trades._pow_mining_tick`  -  were mid-iteration when the connection died, raise an unhandled exception, and enter a `failed()` state. No blocks are being mined. No API events are being dispatched.

### What the self-heal scheduler does (automatic, within 60 s)

The background scheduler catches this on its next 60-second tick:

- `is_connected` returns `False`  -  the listener task is done
- Both loops show `failed() = True`
- A Redis reconnect is scheduled with 5-second backoff
- `_restart_loop_safe` fires for each loop: cancel → sleep 1 s → start

Redis comes back up. The scheduler reconnects, resets its retry counter to 0, and both loops resume cleanly.

### What the admin sees when running `.health heal`

By the time the admin notices the gap in the mining feed and runs the command, the scheduler has already recovered everything. The command re-scans, finds nothing broken, and replies:

```
🩺 Heal  -  All Clear
✅ All auto-checkable systems are healthy. No fixes needed.
```

This is the successful outcome. The clean result confirms the self-healer did its job. The admin can correlate the feed gap with the Redis restart timestamp in the logs rather than chasing a live incident.

### What the embed looks like if the admin runs it before the scheduler ticks

If the command is run within the first 60-second window before the scheduler has a chance to act, the command performs the same fixes itself:

```
🩺 Heal Results

✅ Fixed
🔌 Redis bus: reconnected successfully
🔄 Task loop Trades._mining_tick: restarted (ConnectionError: …)
🔄 Task loop Trades._pow_mining_tick: restarted
```

Both paths land in the same place  -  fully operational bot. The only difference is whether the scheduler or the admin command got there first.

---

## Result bucket reference

| Bucket | Meaning |
|---|---|
| **✅ Fixed** | Issue detected and resolved automatically |
| **❌ Still Broken** | Issue detected, fix attempted, still failing (e.g. Redis unreachable) |
| **⚠️ Manual Action Required** | Issue detected but cannot be auto-fixed (missing permissions, circuit-breaker tripped) |

### Circuit-breaker note

If a task loop fails to restart 5 consecutive times (across scheduler ticks, not within a single `.health heal` run), the self-healer trips its circuit-breaker and stops touching that loop. It will appear in the **⚠️ Manual Action Required** bucket:

```
⚠️ Manual Action Required
🔄 Task loop Trades._mining_tick: circuit-breaker tripped
   (≥5 consecutive failures)  -  check logs for root cause
```

At this point the loop has a deeper problem (a persistent permission error, a missing channel, a code bug). Check the bot logs for the exception that keeps killing it, fix the root cause, then either reload the cog or restart the bot.
