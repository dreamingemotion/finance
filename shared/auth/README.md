# Finance Auth

Standalone OAuth 2.1 Authorization Server for the Finance MCP stack.

Runs as a separate service on port 8001. The MCP server (port 8000) validates
Bearer JWTs using a shared `JWT_SECRET` — no calls to the auth server on every
request, just local signature verification.

---

## How it works

```
MCP Client  ──(1) GET /.well-known/oauth-authorization-server──►  MCP Server :8000
MCP Client  ◄──(2) { authorization_endpoint: "https://…:8001/authorize", … } ──
MCP Client  ──(3) redirect browser to :8001/authorize?…──────────────────────────►
                                                                    Auth Server :8001
User        ──(4) submits email + password ──────────────────────────────────────►
MCP Client  ◄──(5) redirect to redirect_uri?code=… ──────────────────────────────
MCP Client  ──(6) POST /token  code + code_verifier ─────────────────────────────►
MCP Client  ◄──(7) { access_token, refresh_token } ──────────────────────────────
MCP Client  ──(8) GET /sse  Authorization: Bearer <access_token> ─────────────►  MCP Server :8000
            ◄──(9) SSE stream ──────────────────────────────────────────────────
```

Access tokens expire in 15 minutes. The client auto-refreshes using the
refresh token (valid 30 days, rotated on each use).

---

## Setup

### 1. Postgres database

On your Postgres server at 10.0.0.139:

```sql
CREATE DATABASE finance_auth;
CREATE USER finance_auth WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE finance_auth TO finance_auth;
-- Required on Postgres 15+ (revoked public schema access by default):
\c finance_auth
GRANT USAGE, CREATE ON SCHEMA public TO finance_auth;
```

The schema (users, auth_codes, refresh_tokens) is created automatically on
first startup.

### 2. Environment variables

Both the **auth server** and the **MCP server** need these set (they share
the same `JWT_SECRET`):

```bash
# Auth server
export DATABASE_URL="postgresql://finance_auth:your_password@10.0.0.139:5432/finance_auth"
export JWT_SECRET="$(openssl rand -hex 32)"   # generate once, use everywhere

# MCP server (in addition to Tastytrade creds)
export JWT_SECRET="<same value as above>"
export AUTH_SERVER_URL="https://your-server:8001"   # public URL of the auth server
```

Put these in `/etc/environment` or your systemd service files so they persist
across reboots.

### 3. Install dependencies

```bash
cd /path/to/finance
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Create your first user

```bash
cd /path/to/finance/shared
python -m auth.server add-user --email you@example.com --password yourpassword
```

---

## Running

### Auth server (port 8001)

```bash
cd /path/to/finance/shared
python -m auth.server serve --host 0.0.0.0 --port 8001
```

### MCP server with auth (port 8000)

```bash
cd /path/to/finance/shared
python transport.py --transport sse --require-auth
```

### Systemd services

**/etc/systemd/system/finance-auth.service**
```ini
[Unit]
Description=Finance OAuth Server
After=network.target postgresql.service

[Service]
User=ubuntu
WorkingDirectory=/path/to/finance/shared
ExecStart=/path/to/finance/.venv/bin/python -m auth.server serve
EnvironmentFile=/etc/finance.env
Restart=always

[Install]
WantedBy=multi-user.target
```

**/etc/systemd/system/finance-mcp.service**
```ini
[Unit]
Description=Finance MCP Server
After=network.target finance-auth.service

[Service]
User=ubuntu
WorkingDirectory=/path/to/finance/shared
ExecStart=/path/to/finance/.venv/bin/python transport.py --transport sse --require-auth
EnvironmentFile=/etc/finance.env
Restart=always

[Install]
WantedBy=multi-user.target
```

**/etc/finance.env** (chmod 600, owned by root)
```
DATABASE_URL=postgresql://finance_auth:your_password@10.0.0.139:5432/finance_auth
JWT_SECRET=your_generated_secret
AUTH_SERVER_URL=https://your-server:8001
TT_CLIENT_ID=your_tt_client_id
TT_CLIENT_SECRET=your_tt_client_secret
TT_REFRESH_TOKEN=your_tt_refresh_token
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable finance-auth finance-mcp
sudo systemctl start finance-auth finance-mcp
```

---

## MCP client configuration

MCP clients that support OAuth 2.1 discovery (the connection URL is enough):

```json
{
  "mcpServers": {
    "finance": {
      "type": "sse",
      "url": "http://your-server:8000/sse"
    }
  }
}
```

The client will hit `/.well-known/oauth-authorization-server` automatically,
discover the auth endpoints, and walk the user through the login flow.

### Manual token (for clients that don't support OAuth discovery)

Generate a token pair on the server:

```bash
cd /path/to/finance/shared
python -m auth.server generate-token --email you@example.com
```

Then configure the client with a static Bearer header:

```json
{
  "mcpServers": {
    "finance": {
      "type": "sse",
      "url": "http://your-server:8000/sse",
      "headers": {
        "Authorization": "Bearer <access_token>"
      }
    }
  }
}
```

Access tokens expire in 15 minutes, so use the refresh token via `POST /token`
(`grant_type=refresh_token`) or just regenerate when needed.

---

## User management

```bash
# Add a user
python -m auth.server add-user --email user@example.com --password secret

# Generate a token pair (useful for testing or static header configs)
python -m auth.server generate-token --email user@example.com
```

---

## Security notes

- **JWT_SECRET** must be the same value on both services and must stay secret.
  Rotate it by updating the env var and restarting both services (all existing
  tokens immediately become invalid — users re-auth on next request).
- Refresh tokens are rotated on every use (token rotation per OAuth 2.1).
- The auth server should be behind TLS in production. Use nginx or Caddy as a
  reverse proxy with a Let's Encrypt certificate.
- `AUTH_SERVER_URL` should be the public HTTPS URL (e.g. `https://auth.yourdomain.com`),
  not the internal `http://0.0.0.0:8001`.

---

## Project layout

```
shared/auth/
├── server.py      — FastAPI OAuth 2.1 server + CLI
├── db.py          — asyncpg connection pool, schema init
├── users.py       — user creation, lookup, password verification
├── tokens.py      — JWT access tokens + refresh token rotation
└── middleware.py  — Starlette Bearer token validator (used by transport.py)
```
