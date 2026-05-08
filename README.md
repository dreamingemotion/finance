# finance

MCP server for market data. Currently supports Tastytrade (REST snapshots + DXLink live streaming). Yahoo Finance coming soon.

Compatible with any MCP client: Claude Desktop, Cursor, Zed, Continue, or any host that speaks the [Model Context Protocol](https://modelcontextprotocol.io).

---

## Requirements

- Python 3.11+
- A Tastytrade account with an OAuth application configured

---

## Installation

```bash
git clone https://github.com/dreamingemotion/finance.git
cd finance
pip install mcp httpx pydantic websockets
```

---

## Tastytrade credentials

This server uses OAuth2 refresh-token authentication. You need three values from the Tastytrade developer portal:

1. Log in to [tastytrade.com](https://tastytrade.com) and go to **Settings → API / OAuth Applications**
2. Create an application (or open an existing one) and note your **Client ID** and **Client Secret**
3. Under **Manage**, create a grant to obtain a **Refresh Token**

Set these as environment variables before running the server:

```bash
export TT_CLIENT_ID=your_client_id
export TT_CLIENT_SECRET=your_client_secret
export TT_REFRESH_TOKEN=your_refresh_token
```

On Windows:
```powershell
$env:TT_CLIENT_ID     = "your_client_id"
$env:TT_CLIENT_SECRET = "your_client_secret"
$env:TT_REFRESH_TOKEN = "your_refresh_token"
```

Access tokens are short-lived (~15 min) and are refreshed automatically. The refresh token never expires.

---

## Running the server

### stdio (local clients — Claude Desktop, Cursor, Zed, etc.)

```bash
python shared/transport.py
```

### Streamable HTTP (remote/networked clients)

```bash
python shared/transport.py --transport streamable-http
python shared/transport.py --transport streamable-http --host 0.0.0.0 --port 8000 --path /mcp
```

---

## MCP client configuration

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "finance": {
      "command": "python",
      "args": ["C:/path/to/finance/shared/transport.py"],
      "env": {
        "TT_CLIENT_ID": "your_client_id",
        "TT_CLIENT_SECRET": "your_client_secret",
        "TT_REFRESH_TOKEN": "your_refresh_token"
      }
    }
  }
}
```

### Cursor / Zed / Continue

Point to the same command in your MCP settings, or use the streamable-http transport if running the server separately and configure the URL (e.g. `http://localhost:8000/mcp`).

---

## Available tools

All Tastytrade tools are prefixed `tt_`.

### Market data (REST snapshots)

| Tool | Description |
|------|-------------|
| `tt_get_quote` | Snapshot quote for a single symbol |
| `tt_get_quotes_by_type` | Snapshot quotes for multiple symbols across instrument types (limit: 100) |

### Market metrics

| Tool | Description |
|------|-------------|
| `tt_get_market_metrics` | IV rank, IV percentile, HV (30/60/90-day), beta, earnings, dividends |
| `tt_get_dividends` | Historical dividend events for a symbol |
| `tt_get_earnings` | Historical earnings reports for a symbol |
| `tt_get_risk_free_rate` | Current risk-free rate used for margin/options pricing |

### Instruments

| Tool | Description |
|------|-------------|
| `tt_get_equity` | Single equity instrument details |
| `tt_get_equities` | Multiple equity instruments |
| `tt_get_option_chain` | Full equity option chain, keyed by expiration date |
| `tt_get_nested_option_chain` | Option chain in nested format (expirations → strikes → calls/puts) |
| `tt_get_futures` | Futures contracts, filterable by symbol or product code |
| `tt_get_future_option_chain` | Option chain for a futures underlying |
| `tt_symbol_search` | Search for symbols by name or ticker |

### DXLink streaming

Streaming tools open a WebSocket to DXLink, subscribe, collect events for `duration_seconds`, then return the full list.

| Tool | Description |
|------|-------------|
| `tt_stream_quotes` | Live bid/ask quotes |
| `tt_stream_trades` | Live trade prints |
| `tt_stream_candles` | OHLCV candles — historical backfill + live; configurable period and start date |
| `tt_stream_greeks` | Live Greeks (delta, gamma, theta, vega, rho, IV) for options |
| `tt_stream_summaries` | Day OHLC, previous close, open interest |
| `tt_stream_profiles` | 52-week high/low, trading status, halt status |
| `tt_stream_theo_prices` | Theoretical price and Greeks for options |
| `tt_stream_time_and_sales` | Tick-level trade data |
| `tt_stream_underlyings` | IV, put/call volumes, put-call ratio for underlyings |

Candle periods: `1m 2m 3m 5m 10m 15m 30m 1h 2h 4h 1d 1w 1mo`

---

## Project structure

```
finance/
└── shared/
    ├── transport.py              # MCP server entry point (stdio or streamable-http)
    └── data/
        └── brokers/
            ├── tastytrade.py     # Self-contained Tastytrade data grabber + MCP tools
            └── yahoo.py          # Coming soon
```

Each broker file is self-contained (no shared dependencies between them) and exposes a `register_tools(mcp)` function that `transport.py` calls at startup.
