"""
Finance MCP server entry point.

Aggregates tools from all broker data grabbers and starts the server
with the configured transport.

Usage:
    # stdio (default — for Claude Desktop, Cursor, Zed, etc.)
    python transport.py

    # Streamable HTTP (for remote/networked clients)
    python transport.py --transport streamable-http
    python transport.py --transport streamable-http --host 0.0.0.0 --port 8000 --path /mcp

Environment variables:
    Tastytrade: TT_CLIENT_ID, TT_CLIENT_SECRET, TT_REFRESH_TOKEN

Dependencies:
    pip install mcp httpx pydantic websockets
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow imports from this directory when run as a script
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

from data.brokers.tastytrade import register_tools as tt_register_tools
# from data.brokers.yahoo import register_tools as yf_register_tools  # future


def build_server() -> FastMCP:
    mcp = FastMCP("finance-data")
    tt_register_tools(mcp)
    # yf_register_tools(mcp)  # uncomment when yahoo.py is ready
    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Finance MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind host for streamable-http transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port for streamable-http transport (default: 8000)",
    )
    parser.add_argument(
        "--path",
        default="/mcp",
        help="URL path for streamable-http transport (default: /mcp)",
    )
    args = parser.parse_args()

    mcp = build_server()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            path=args.path,
        )


if __name__ == "__main__":
    main()
