# Quick Start

## Docker (Recommended)

```bash
git clone https://github.com/HiLleywyn/Discoin.git
cd Discoin
cp .env.example .env        # fill in your values
docker compose up -d --build
```

Dashboard: `http://localhost:8080/dashboard`
API docs: `http://localhost:8080/api/docs`
Health check: `http://localhost:8080/health`

## Manual Setup

**Requirements:** Python 3.11+, PostgreSQL 15+, Redis 7+, Node.js 18+ (for dashboard), [uv](https://docs.astral.sh/uv/)

Install uv if you don't already have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then clone and install dependencies:

```bash
git clone https://github.com/HiLleywyn/Discoin.git
cd Discoin
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt
uv pip install "playwright>=1.40.0"
playwright install chromium && playwright install-deps chromium
```

> Playwright + Chromium (~500MB) power the `,trade chart` command. In Docker
> they live in their own layer ahead of `requirements.txt`, so dependency
> bumps no longer invalidate the Chromium download.

Build the dashboard:

```bash
cd frontend && npm ci && npm run build && cd ..
```

Create the database and start:

```bash
createdb discoin
psql discoin < database/schema.sql
cp .env.example .env   # edit with your values
python main.py
```

## Discord Developer Portal

### 1. Create the application

Go to [discord.com/developers/applications](https://discord.com/developers/applications), click **New Application**, name it "Discoin" (or whatever you like).

Under **Bot**, click **Reset Token** and copy it into your `.env` as `DISCORD_TOKEN`.

### 2. Enable OAuth2 for the dashboard

Under **OAuth2 > General**, add a redirect URL matching your `DISCORD_REDIRECT_URI` (default: `http://localhost:8080/api/auth/callback`).

Copy the **Client ID** and **Client Secret** into your `.env`.

### 3. Enable Privileged Gateway Intents

Under **Bot > Privileged Gateway Intents**, enable:

- **Presence Intent**
- **Server Members Intent**
- **Message Content Intent**

### 4. Invite the bot

Under **OAuth2 > URL Generator**, select scopes: `bot`, `applications.commands`

Permissions: `Administrator` (or selectively: Manage Messages, Send Messages, Embed Links, Attach Files, Read Message History, Use External Emojis, Add Reactions, Manage Roles)

## First Commands

Once the bot is online in your server:

```
.admin setup          # initialize guild settings
.balance              # check your starting balance
.daily                # claim your daily reward
.help                 # full interactive command reference
```
