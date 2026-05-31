# API Authentication

The Discoin API uses two authentication methods depending on the endpoint.

## Discord OAuth2 + JWT (Dashboard)

Dashboard users authenticate via Discord OAuth2. The flow:

1. Redirect user to `/api/auth/discord`
2. Discord redirects back to your `DISCORD_REDIRECT_URI` with a code
3. API exchanges code for Discord tokens, creates a JWT
4. All subsequent requests include `Authorization: Bearer <jwt>`

```
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
```

JWTs contain: `user_id`, `guild_id`, `is_admin`, and expiry. Default session length is 7 days (`JWT_EXPIRE_SECONDS`).

### Guild Selection

After initial OAuth, the user must select a guild. The JWT is then scoped to that guild  -  all data queries are guild-isolated.

## Admin API Key

Server-admin endpoints also accept an API key for programmatic access:

```
X-API-Key: your_api_key_here
```

Set via the `API_KEY` environment variable. Leave blank to disable.

## Endpoints by Auth Level

| Auth Level | Endpoints |
|---|---|
| **None** | `/health`, `/api/v2/health` |
| **Bearer JWT** | `/api/v2/users/*`, `/api/v2/portfolio/*`, `/api/v2/market/*`, `/api/v2/trading/*`, `/api/v2/pools/*`, `/api/v2/staking/*`, `/api/v2/games/*`, `/api/v2/savings/*`, `/api/v2/lending/*` |
| **Admin (JWT + is_admin)** | `/api/v2/admin/*` |

## Interactive Docs

When the server is running:

- **Swagger UI** (dark/light mode): `/api/docs`
- **ReDoc**: `/api/redoc`
- **OpenAPI JSON**: `/api/openapi.json`

Click "Authorize" in Swagger UI and paste your JWT to test authenticated endpoints.
