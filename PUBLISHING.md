# Publishing to GitHub

## Before you push

**Check for secrets.** The `.env` file is in `.gitignore` and will not be committed, but double-check nothing sensitive is hardcoded in the source:

```bash
grep -r "TOKEN\|API_KEY\|SECRET\|PASSWORD" --include="*.py" . | grep -v ".gitignore" | grep -v "os.getenv"
```

If that returns anything other than `os.getenv(...)` calls, remove it before committing.

---

## First-time setup

```bash
cd econbot
git init
git add .
git commit -m "initial commit"
```

Create a repo on GitHub (no README, no .gitignore  -  you already have both), then:

```bash
git remote add origin https://github.com/<you>/<repo>.git
git branch -M main
git push -u origin main
```

---

## What gets committed

- All Python source (`cogs/`, `core/framework/`, `database/`, `api/`, `core/config.py`, `main.py`)
- `README.md`, `PUBLISHING.md`, `requirements.txt`, `.gitignore`
- `scripts/gen_docs.py`
- Dashboard HTML (`api/static/index.html`)

## What stays local (gitignored)

| File/Dir | Why |
|----------|-----|
| `.env` | Contains your bot token and API keys |
| `*.db`, `*.db-shm`, `*.db-wal` | SQLite database with user data |
| `venv/` | Python virtual environment |
| `COMMANDS.md` | Generated file, recreate with `python scripts/gen_docs.py` |

---

## Creating a `.env.example` for contributors

Run this once to create a safe template people can copy:

```bash
cat > .env.example << 'EOF'
DISCORD_TOKEN=your_bot_token_here
PREFIX=.
DB_PATH=economy.db
TX_SALT=pick_something_random
API_PORT=8080
API_KEY=your_dashboard_key
OPENROUTER_API_KEY=
SLASH_GUILD_ID=
AUTO_SEED_POOLS=false
EOF

git add .env.example
git commit -m "add env example"
```

---

## Ongoing workflow

```bash
# Make changes, then:
git add -p                    # review and stage hunks interactively
git commit -m "short message"
git push
```

Keep commits small and use present-tense messages: `"fix staking feed tx hash"`, `"add group mining mode"`, not `"fixed some stuff"`.

---

## Optional: add a license

If you want the code to be usable by others, pick one at [choosealicense.com](https://choosealicense.com). MIT is the most common for Discord bots:

```bash
# After creating LICENSE file:
git add LICENSE
git commit -m "add MIT license"
```

---

## Listing the bot publicly

Discoin supports the multi-tenant model out of the box: trading and gambling
are free everywhere, AI / fishing / crafting / delves / expeditions / buddy
games are paywalled per-guild via PayPal. Before you list the bot:

1. **Set the operator env vars** on Railway (or wherever you host):
   - `HOST_GUILD_ID`  -  your home server's guild_id (auto-unlocks every premium feature there)
   - `BOT_OWNER_ID`  -  your Discord user_id (lets you run `,admin premium ...`)
2. **Create the PayPal subscription plan(s)** in the PayPal dashboard
   ([sandbox](https://developer.paypal.com/dashboard/applications/sandbox)
   first, then live) and paste the `P-XXXXXXX...` plan id into
   `PAYPAL_PLAN_ID_MONTHLY` / `PAYPAL_PLAN_ID_YEARLY`.
3. **Subscribe the webhook** at `https://<your-host>/api/v2/paypal/webhook` to
   `BILLING.SUBSCRIPTION.*` and `PAYMENT.SALE.COMPLETED`. Paste the webhook id
   into `PAYPAL_WEBHOOK_ID`. Without this, premium activations will silently fail.
4. **Test in sandbox first.** Subscribe with a sandbox buyer, confirm
   `,premium status` flips to active, cancel, confirm it flips back. Only then
   set `PAYPAL_MODE=live` and swap to live plan/webhook ids.
5. **Generate an OAuth2 invite URL** in the Discord Developer Portal with the
   `bot` and `applications.commands` scopes plus the permissions Discoin needs
   (Send Messages, Embed Links, Manage Messages, Add Reactions, Use External
   Emojis, Read Message History, View Channels). Anyone you share that link
   with can add the bot to their server; the new server gets a welcome embed
   from `cogs/premium.py` explaining what's free and how to subscribe.
6. **Pick a directory** to list on (top.gg, discord.bots.gg, etc.). Most
   require a privacy policy and ToS link  -  draft those before submitting.

If a PayPal webhook is dropped (network blip, host restart) and a paying guild
is showing as non-premium, the bot owner can recover with
`,admin premium link <guild_id> <subscription_id>` (pulls live state from
PayPal) or `,admin premium sync` (re-fetches every PayPal-linked guild).
