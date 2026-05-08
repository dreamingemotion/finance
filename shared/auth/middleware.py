"""
Starlette middleware that validates Bearer JWT tokens on MCP server requests.

Attach to the MCP Starlette app before running uvicorn:

    app.add_middleware(BearerTokenMiddleware, jwt_secret=os.environ["JWT_SECRET"])
"""

from __future__ import annotations

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# These paths are served without a token so MCP clients can discover the auth server.
_OPEN_PATHS = frozenset({
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
})


class BearerTokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, jwt_secret: str) -> None:
        super().__init__(app)
        self._secret = jwt_secret

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _OPEN_PATHS:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(
                {"error": "unauthorized", "error_description": "Bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="Finance MCP"'},
            )

        token = auth[7:]
        try:
            jwt.decode(token, self._secret, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return JSONResponse(
                {"error": "invalid_token", "error_description": "Token expired"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        except jwt.InvalidTokenError:
            return JSONResponse(
                {"error": "invalid_token", "error_description": "Invalid token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )

        return await call_next(request)
