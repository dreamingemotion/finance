# Finance Auth

Standalone OAuth 2.1 Authorization Server for the Finance MCP stack.

## Why it exists

Each MCP server in this stack (market data, knowledge, signals, etc.) runs as a
networked service in the cloud. Without authentication, anyone who knows the URL
can use them. The auth server solves this by acting as a central login service —
users authenticate once and receive a JWT that all MCP servers accept.

The MCP servers never call the auth server to validate requests. They verify
JWT signatures locally using a shared `JWT_SECRET`. This means the auth server
is only involved during login — it has zero impact on request latency afterward.

---

## How it works

```
MCP Client  ──(1) GET /.well-known/oauth-authorization-server──►  MCP Server
MCP Client  ◄──(2) { authorization_endpoint: "https://…/authorize", … } ──
MCP Client  ──(3) redirect browser to auth server /authorize?…──────────────►
                                                                    Auth Server
User        ──(4) submits email + password ─────────────────────────────────►
MCP Client  ◄──(5) redirect to redirect_uri?code=… ─────────────────────────
MCP Client  ──(6) POST /token  code + code_verifier ────────────────────────►
MCP Client  ◄──(7) { access_token, refresh_token } ─────────────────────────
MCP Client  ──(8) GET /sse  Authorization: Bearer <access_token> ───────────►  MCP Server
            ◄──(9) SSE stream ───────────────────────────────────────────────
```

Access tokens expire in 15 minutes. The client auto-refreshes using the
refresh token (valid 30 days, rotated on each use).

A single login works across all MCP servers — market data, knowledge, and any
future servers — because they all share the same `JWT_SECRET`.

---

## Setup

### 1. Postgres database

On your Postgres server at 10.0.0.139:

```sql
CREATE DATABASE finance_auth;
CREATE USER finance_auth WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE finance_auth TO finance_auth;
```

Then connect to `finance_auth` and run:
```sql
GRANT USAGE, CREATE ON SCHEMA public TO finance_auth;
```

The schema (users, auth_codes, refresh_tokens) is created automatically on
first startup.

### 2. Environment variables

Add to `/etc/finance.env` (see format below). No quotes needed — systemd reads
these as plain `KEY=VALUE` pairs:

```
DATABASE_URL=postgresql://finance_auth:Tr0ub4dor&3@10.0.0.139:5432/finance_auth
JWT_SECRET=a3f8c2d1e4b7f9e0c3d5a8b2f6e1c4d7a9b3f0e2c5d8a1b4f7e0c3d6a9b2f5
AUTH_SERVER_URL=https://finance.example.com:8090
TT_CLIENT_ID=tt_oauth_aBcD1234EfGh
TT_CLIENT_SECRET=tt_secret_xYz9876WvUt
TT_REFRESH_TOKEN=tt_refresh_mNpQ5432RsTu
```

`JWT_SECRET` must be the same value across all MCP servers. Generate it once:
```bash
openssl rand -hex 32
```

> **Note:** When setting env vars manually in a shell (e.g. for testing), use
> single quotes: `export DATABASE_URL='...'`. Double quotes cause bash to
> interpret `!` as history expansion.

Lock down the file:
```bash
sudo chmod 600 /etc/finance.env
sudo chown root:root /etc/finance.env
```

### 3. Install dependencies

```bash
cd /opt/agents/finance
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Create your first user

```bash
cd /opt/agents/finance/shared
python -m auth.server add-user --email you@example.com --password yourpassword
```

---

## Running

### Auth server

```bash
cd /opt/agents/finance/shared
python -m auth.server serve --host 0.0.0.0 --port 8090
```

### Systemd service

**/etc/systemd/system/finance-auth.service**
```ini
[Unit]
Description=Finance OAuth Server
After=network.target postgresql.service

[Service]
User=ubuntu
WorkingDirectory=/opt/agents/finance/shared
ExecStart=/opt/agents/finance/.venv/bin/python -m auth.server serve
EnvironmentFile=/etc/finance.env
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable finance-auth
sudo systemctl start finance-auth
```

### Nginx reverse proxy

Add this location block to your nginx server config:

```nginx
location /servers/finance/auth/ {
    proxy_pass http://127.0.0.1:8090/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_buffering off;
}
```

Then update `AUTH_SERVER_URL` in `/etc/finance.env`:
```
AUTH_SERVER_URL=https://mcp.unfolding.in/servers/finance/auth
```

Reload nginx:
```bash
sudo nginx -t && sudo systemctl reload nginx
```

Each MCP server has its own systemd service documented in its own README.

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

- **JWT_SECRET** must be identical across all services. Rotating it invalidates
  all existing tokens immediately — users re-auth on next request.
- Refresh tokens are rotated on every use (OAuth 2.1 token rotation).
- The auth server should be behind TLS in production. Use nginx or Caddy as a
  reverse proxy with a Let's Encrypt certificate.
- `AUTH_SERVER_URL` must be the public HTTPS URL — not `http://0.0.0.0:8090`.

---

## Project layout

```
shared/auth/
├── server.py      — FastAPI OAuth 2.1 server + CLI
├── db.py          — asyncpg connection pool, schema init
├── users.py       — user creation, lookup, password verification
├── tokens.py      — JWT access tokens + refresh token rotation
└── middleware.py  — Starlette Bearer token validator (used by all MCP servers)
```
