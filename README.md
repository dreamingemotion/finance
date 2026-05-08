# finance

MCP server for market data. Tastytrade is the primary source (REST snapshots + DXLink live streaming); Yahoo Finance is the automatic fallback. When the live feed is unavailable, responses include a `_note` field indicating delayed data.

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
```

Create and activate a virtual environment:

```bash
# macOS / Linux
python -m venv .venv
source .venv/bin/activate

# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
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

### SSE (remote/networked clients)

```bash
python shared/transport.py --transport sse
python shared/transport.py --transport sse --host 0.0.0.0 --port 8000

# With OAuth 2.1 authentication (recommended for cloud deployments):
python shared/transport.py --transport sse --require-auth
```

See [`shared/auth/README.md`](shared/auth/README.md) for the full cloud deployment guide including the OAuth server, Postgres setup, and systemd services.

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

Point to the same command in your MCP settings, or use the SSE transport if running the server separately and configure the URL (e.g. `http://localhost:8000/sse`).

---

## Available tools

### Shared (Tastytrade primary, Yahoo Finance fallback)

When Tastytrade is unavailable, these tools automatically fall back to Yahoo Finance and include `"_note": "Note: this analysis uses delayed data because the live feed is down."` in the response.

| Tool | Description |
|------|-------------|
| `get_quote` | Snapshot quote for a single symbol |
| `get_quotes` | Snapshot quotes for multiple symbols across instrument types (limit: 100) |
| `get_candles` | OHLCV candles — live DXLink stream with Yahoo Finance fallback |
| `get_metrics` | IV rank, IV percentile, HV (30/60/90-day), beta, earnings, dividends |
| `get_dividends` | Historical dividend events for a symbol |
| `get_earnings` | Earnings history for a symbol |
| `get_option_chain` | Full equity option chain keyed by expiration date |

### Tastytrade-only

| Tool | Description |
|------|-------------|
| `get_equity` | Single equity instrument details |
| `get_equities` | Multiple equity instruments |
| `get_nested_option_chain` | Option chain in nested format (expirations → strikes → calls/puts) |
| `get_futures` | Futures contracts, filterable by symbol or product code |
| `get_future_option_chain` | Option chain for a futures underlying |
| `get_risk_free_rate` | Current risk-free rate used for margin/options pricing |
| `symbol_search` | Search for symbols by name or ticker |

### DXLink streaming (Tastytrade-only)

Streaming tools open a WebSocket to DXLink, subscribe, collect events for `duration_seconds`, then return the full list.

| Tool | Description |
|------|-------------|
| `stream_quotes` | Live bid/ask quotes |
| `stream_trades` | Live trade prints |
| `stream_candles` | OHLCV candles — historical backfill + live; configurable period and start date |
| `stream_greeks` | Live Greeks (delta, gamma, theta, vega, rho, IV) for options |
| `stream_summaries` | Day OHLC, previous close, open interest |
| `stream_profiles` | 52-week high/low, trading status, halt status |
| `stream_theo_prices` | Theoretical price and Greeks for options |
| `stream_time_and_sales` | Tick-level trade data |
| `stream_underlyings` | IV, put/call volumes, put-call ratio for underlyings |

Candle periods: `1m 2m 3m 5m 10m 15m 30m 1h 2h 4h 1d 1w 1mo`

### Yahoo Finance-only

| Tool | Description |
|------|-------------|
| `get_info` | Full company profile and fundamentals (PE, EPS, margins, analyst ratings, etc.) |
| `get_history` | OHLCV price history with yfinance period/interval syntax |
| `get_financials` | Annual income statement |
| `get_balance_sheet` | Annual balance sheet |
| `get_cashflow` | Annual cash flow statement |
| `get_splits` | Historical stock splits |
| `get_recommendations` | Analyst buy/sell/hold recommendations |
| `get_news` | Recent news articles |
| `get_option_expirations` | Available options expiration dates |

---

## Project structure

```
finance/
└── shared/
    ├── transport.py              # MCP server entry point — owns all tool definitions
    ├── auth/
    │   ├── server.py             # Standalone OAuth 2.1 server (port 8001)
    │   ├── db.py                 # asyncpg connection pool + schema init
    │   ├── users.py              # User management + password verification
    │   ├── tokens.py             # JWT access tokens + refresh token rotation
    │   ├── middleware.py         # Bearer token validator for transport.py
    │   └── README.md             # Cloud deployment guide
    └── data/
        └── brokers/
            ├── tastytrade.py     # TastytradeClient — pure data client, no MCP coupling
            └── yahoo.py          # YahooClient — pure data client, no MCP coupling
```

Each broker file is a self-contained data client with no MCP dependencies. `transport.py` instantiates both clients, defines all tools, and wires up the `_with_fallback` helper for shared tools.
