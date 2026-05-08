"""
OAuth 2.1 Authorization Server.

Endpoints:
  GET  /.well-known/oauth-authorization-server  — discovery metadata
  GET  /authorize                               — login form
  POST /authorize                               — process login → redirect with code
  POST /token                                   — exchange code or refresh token
  POST /revoke                                  — revoke a refresh token

Usage:
  python server.py serve [--host 0.0.0.0] [--port 8090]
  python server.py add-user --email user@example.com --password secret
  python server.py generate-token --email user@example.com

Environment variables:
  DATABASE_URL   postgresql://user:pass@10.0.0.139:5432/finance_auth
  JWT_SECRET     random secret shared with the MCP server
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

sys.path.insert(0, str(Path(__file__).parent.parent))

from auth.db import get_db, init_db, init_pool
from auth.tokens import (
    ACCESS_TOKEN_EXPIRE,
    create_refresh_token,
    make_access_token,
    rotate_refresh_token,
)
from auth.users import create_user, get_user_by_email, get_user_by_id, verify_password


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    pool = await init_pool()
    await pool.close()


app = FastAPI(title="Finance OAuth Server", lifespan=lifespan)

DbDep = Annotated[object, Depends(get_db)]


def _jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET", "")
    if not secret:
        raise HTTPException(500, detail="JWT_SECRET not configured")
    return secret


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@app.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "revocation_endpoint": f"{base}/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    }


# ---------------------------------------------------------------------------
# Authorization endpoint
# ---------------------------------------------------------------------------

_LOGIN_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Finance — Sign in</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f5f5f7;
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh;
    }}
    .card {{
      background: #fff; border-radius: 12px; padding: 2.5rem 2rem;
      width: 100%; max-width: 380px;
      box-shadow: 0 2px 12px rgba(0,0,0,.10);
    }}
    h1 {{ font-size: 1.35rem; font-weight: 600; margin-bottom: 1.75rem; color: #1d1d1f; }}
    label {{
      display: block; font-size: .8rem; font-weight: 500;
      color: #6e6e73; margin-bottom: .3rem; text-transform: uppercase;
      letter-spacing: .04em;
    }}
    input[type=email], input[type=password] {{
      width: 100%; padding: .6rem .85rem;
      border: 1px solid #d2d2d7; border-radius: 8px;
      font-size: .95rem; margin-bottom: 1.1rem; outline: none;
      transition: border-color .15s, box-shadow .15s;
    }}
    input:focus {{
      border-color: #0071e3;
      box-shadow: 0 0 0 3px rgba(0,113,227,.15);
    }}
    button {{
      width: 100%; padding: .7rem;
      background: #0071e3; color: #fff;
      border: none; border-radius: 8px;
      font-size: .95rem; font-weight: 500; cursor: pointer;
      transition: background .15s;
    }}
    button:hover {{ background: #0077ed; }}
    .error {{
      background: #fff0f0; border: 1px solid #ffd0d0;
      color: #c00; border-radius: 8px;
      padding: .6rem .85rem; margin-bottom: 1.1rem; font-size: .875rem;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Sign in to Finance</h1>
    {error_block}
    <form method="post">
      <input type="hidden" name="client_id"             value="{client_id}">
      <input type="hidden" name="redirect_uri"          value="{redirect_uri}">
      <input type="hidden" name="state"                 value="{state}">
      <input type="hidden" name="scope"                 value="{scope}">
      <input type="hidden" name="code_challenge"        value="{code_challenge}">
      <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
      <label for="email">Email</label>
      <input type="email" id="email" name="email" required autofocus>
      <label for="password">Password</label>
      <input type="password" id="password" name="password" required>
      <button type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>
"""


def _login_page(*, error: str = "", **fields) -> HTMLResponse:
    error_block = f'<div class="error">{error}</div>' if error else ""
    return HTMLResponse(_LOGIN_HTML.format(error_block=error_block, **fields))


def _login_fields(
    client_id: str,
    redirect_uri: str,
    state: str,
    scope: str,
    code_challenge: str,
    code_challenge_method: str,
) -> dict:
    return dict(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )


@app.get("/authorize", response_class=HTMLResponse)
async def authorize_get(
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query(...),
    state: str = Query(default=""),
    scope: str = Query(default="mcp"),
):
    if response_type != "code":
        raise HTTPException(400, "unsupported_response_type")
    if code_challenge_method != "S256":
        raise HTTPException(400, "unsupported_code_challenge_method — only S256 is supported")
    return _login_page(
        **_login_fields(
            client_id, redirect_uri, state, scope,
            code_challenge, code_challenge_method,
        )
    )


@app.post("/authorize")
async def authorize_post(
    db: DbDep,
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_challenge: str = Form(...),
    code_challenge_method: str = Form(...),
    state: str = Form(default=""),
    scope: str = Form(default="mcp"),
    email: str = Form(...),
    password: str = Form(...),
):
    fields = _login_fields(
        client_id, redirect_uri, state, scope,
        code_challenge, code_challenge_method,
    )

    user = await get_user_by_email(db, email)
    if user is None or not verify_password(password, user["password_hash"]):
        return _login_page(error="Invalid email or password.", **fields)

    code = secrets.token_urlsafe(32)
    await db.execute(
        """
        INSERT INTO auth_codes
          (code, client_id, user_id, redirect_uri, scope,
           code_challenge, code_challenge_method, expires_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """,
        code, client_id, user["id"], redirect_uri, scope,
        code_challenge, code_challenge_method, int(time.time()) + 600,
    )

    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}"
    if state:
        location += f"&state={state}"
    return RedirectResponse(location, status_code=302)


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------

@app.post("/token")
async def token_endpoint(
    db: DbDep,
    grant_type: str = Form(...),
    # authorization_code
    code: str | None = Form(default=None),
    redirect_uri: str | None = Form(default=None),
    client_id: str | None = Form(default=None),
    code_verifier: str | None = Form(default=None),
    # refresh_token
    refresh_token: str | None = Form(default=None),
):
    secret = _jwt_secret()

    if grant_type == "authorization_code":
        return await _token_from_code(db, secret, code, redirect_uri, client_id, code_verifier)
    if grant_type == "refresh_token":
        return await _token_from_refresh(db, secret, refresh_token)
    raise HTTPException(400, detail={"error": "unsupported_grant_type"})


async def _token_from_code(db, secret, code, redirect_uri, client_id, code_verifier):
    if not all([code, redirect_uri, client_id, code_verifier]):
        raise HTTPException(400, detail={"error": "invalid_request", "error_description": "Missing required parameters"})

    now = int(time.time())
    async with db.transaction():
        row = await db.fetchrow(
            "SELECT * FROM auth_codes WHERE code = $1 AND used = FALSE AND expires_at > $2",
            code, now,
        )
        if row is None:
            raise HTTPException(400, detail={"error": "invalid_grant"})

        row = dict(row)
        if row["redirect_uri"] != redirect_uri or row["client_id"] != client_id:
            raise HTTPException(400, detail={"error": "invalid_grant"})

        # Verify PKCE S256
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if digest != row["code_challenge"]:
            raise HTTPException(400, detail={"error": "invalid_grant", "error_description": "PKCE verification failed"})

        await db.execute("UPDATE auth_codes SET used = TRUE WHERE code = $1", code)

        user = await get_user_by_id(db, row["user_id"])
        if user is None:
            raise HTTPException(400, detail={"error": "invalid_grant"})

        refresh = await create_refresh_token(db, client_id, user["id"], row["scope"])

    return {
        "access_token": make_access_token(user["id"], user["email"], row["scope"], secret),
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE,
        "refresh_token": refresh,
        "scope": row["scope"],
    }


async def _token_from_refresh(db, secret, refresh_token):
    if not refresh_token:
        raise HTTPException(400, detail={"error": "invalid_request"})

    result = await rotate_refresh_token(db, refresh_token)
    if result is None:
        raise HTTPException(400, detail={"error": "invalid_grant"})

    old_row, new_refresh = result
    user = await get_user_by_id(db, old_row["user_id"])
    if user is None:
        raise HTTPException(400, detail={"error": "invalid_grant"})

    return {
        "access_token": make_access_token(user["id"], user["email"], old_row["scope"], secret),
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE,
        "refresh_token": new_refresh,
        "scope": old_row["scope"],
    }


# ---------------------------------------------------------------------------
# Revocation endpoint  (RFC 7009 — always returns 200)
# ---------------------------------------------------------------------------

@app.post("/revoke")
async def revoke(db: DbDep, token: str = Form(...)):
    await db.execute(
        "UPDATE refresh_tokens SET revoked = TRUE WHERE token = $1", token
    )
    return {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def _cmd_add_user(email: str, password: str) -> None:
    await init_db()
    pool = await init_pool()
    async with pool.acquire() as db:
        try:
            user = await create_user(db, email, password)
            print(f"Created: {user['email']}  (id={user['id']})")
        except Exception as exc:
            print(f"Error: {exc}")
    await pool.close()


async def _cmd_generate_token(email: str) -> None:
    secret = os.environ.get("JWT_SECRET", "")
    if not secret:
        print("Error: JWT_SECRET not set")
        return
    await init_db()
    pool = await init_pool()
    async with pool.acquire() as db:
        user = await get_user_by_email(db, email)
        if user is None:
            print(f"Error: no active user with email {email!r}")
            await pool.close()
            return
        access_token = make_access_token(user["id"], user["email"], "mcp", secret)
        refresh_token = await create_refresh_token(db, "cli", user["id"], "mcp")
    await pool.close()
    print(f"access_token  (15 min):  {access_token}")
    print(f"refresh_token (30 days): {refresh_token}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Finance OAuth server")
    sub = parser.add_subparsers(dest="cmd")

    serve_p = sub.add_parser("serve", help="Start the OAuth server (default)")
    serve_p.add_argument("--host", default="0.0.0.0")
    serve_p.add_argument("--port", type=int, default=8090)

    user_p = sub.add_parser("add-user", help="Create a new user account")
    user_p.add_argument("--email", required=True)
    user_p.add_argument("--password", required=True)

    tok_p = sub.add_parser("generate-token", help="Issue an access + refresh token pair")
    tok_p.add_argument("--email", required=True)

    args = parser.parse_args()

    if args.cmd == "add-user":
        asyncio.run(_cmd_add_user(args.email, args.password))
    elif args.cmd == "generate-token":
        asyncio.run(_cmd_generate_token(args.email))
    else:
        uvicorn.run(
            "auth.server:app",
            host=getattr(args, "host", "0.0.0.0"),
            port=getattr(args, "port", 8090),
            reload=False,
        )


if __name__ == "__main__":
    main()
