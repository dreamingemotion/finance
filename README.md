# finance

Foundation library for the Finance MCP stack.

`shared/` is a library — top-level MCP servers import from it and define their own tools.
Claude connects to the top-level MCP servers, not to anything in `shared/` directly.

---

## Architecture

```
finance/
├── shared/             ← library imported by all MCP servers
│   ├── transport.py    ← build_clients() + with_fallback() utility
│   ├── auth/           ← OAuth 2.1 server (port 8090) and JWT middleware
│   ├── knowledge/      ← knowledge base: db, embedder, retriever
│   └── data/
│       └── brokers/    ← pure data clients (no MCP coupling)
│
├── knowledge/          ← knowledge MCP server (port 8092)
├── signals/            ← (future) MCP server — imports shared/
├── portfolio/          ← (future) MCP server — imports shared/
└── ...                 ← future purpose-built MCP servers
```

Each top-level MCP server imports `build_clients()` and `with_fallback()` from
`shared/transport.py`, then defines its own tools using `@mcp.tool()`.

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

```bash
# macOS / Linux
python -m venv .venv
source .venv/bin/activate

# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
```

```bash
pip install -r requirements.txt
```

---

## Tastytrade credentials

1. Log in to [tastytrade.com](https://tastytrade.com) → **Settings → API / OAuth Applications**
2. Note your **Client ID** and **Client Secret**
3. Under **Manage**, create a grant to obtain a **Refresh Token**

Add to `/etc/finance.env`:
```
TT_CLIENT_ID=tt_oauth_aBcD1234EfGh
TT_CLIENT_SECRET=tt_secret_xYz9876WvUt
TT_REFRESH_TOKEN=tt_refresh_mNpQ5432RsTu
```

---

## MCP client configuration

Configs will be documented here as top-level MCP servers are built.

---

## Available broker capabilities

These are the data methods available on the broker clients in `shared/data/brokers/`.
Top-level MCP servers expose whichever of these they need as `@mcp.tool()` definitions.

### Shared (Tastytrade primary, Yahoo Finance fallback)

| Method | Description |
|--------|-------------|
| `get_quote` | Snapshot quote for a single symbol |
| `get_quotes` | Snapshot quotes for multiple symbols across instrument types |
| `get_candles` | OHLCV candles — live DXLink stream with Yahoo Finance fallback |
| `get_metrics` | IV rank, IV percentile, HV (30/60/90-day), beta, earnings, dividends |
| `get_dividends` | Historical dividend events for a symbol |
| `get_earnings` | Earnings history for a symbol |
| `get_option_chain` | Full equity option chain keyed by expiration date |

### Tastytrade-only

| Method | Description |
|--------|-------------|
| `get_equity` | Single equity instrument details |
| `get_equities` | Multiple equity instruments |
| `get_nested_option_chain` | Option chain in nested format (expirations → strikes → calls/puts) |
| `get_futures` | Futures contracts, filterable by symbol or product code |
| `get_future_option_chain` | Option chain for a futures underlying |
| `get_risk_free_rate` | Current risk-free rate used for margin/options pricing |
| `symbol_search` | Search for symbols by name or ticker |
| `stream_quotes` | Live bid/ask quotes |
| `stream_trades` | Live trade prints |
| `stream_candles` | OHLCV candles — historical backfill + live |
| `stream_greeks` | Live Greeks (delta, gamma, theta, vega, rho, IV) for options |
| `stream_summaries` | Day OHLC, previous close, open interest |
| `stream_profiles` | 52-week high/low, trading status, halt status |
| `stream_theo_prices` | Theoretical price and Greeks for options |
| `stream_time_and_sales` | Tick-level trade data |
| `stream_underlyings` | IV, put/call volumes, put-call ratio for underlyings |

Candle periods: `1m 2m 3m 5m 10m 15m 30m 1h 2h 4h 1d 1w 1mo`

### Yahoo Finance-only

| Method | Description |
|--------|-------------|
| `get_info` | Full company profile and fundamentals (PE, EPS, margins, analyst ratings, etc.) |
| `get_history` | OHLCV price history |
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
├── knowledge/                    # Knowledge MCP server (port 8092)
│   ├── server.py                 # FastMCP — ingest + search tools
│   ├── ingest.py                 # Claude-powered chunking + categorization
│   └── README.md                 # Setup and deployment guide
│
└── shared/                       # Foundation library
    ├── transport.py              # build_clients() + with_fallback() utility
    ├── auth/
    │   ├── server.py             # OAuth 2.1 server (port 8090)
    │   ├── db.py                 # asyncpg connection pool + schema init
    │   ├── users.py              # User management + password verification
    │   ├── tokens.py             # JWT access tokens + refresh token rotation
    │   ├── middleware.py         # Bearer token validator for MCP servers
    │   └── README.md             # Setup and deployment guide
    ├── knowledge/
    │   ├── db.py                 # asyncpg pool, knowledge schema, pgvector init
    │   ├── embedder.py           # OpenRouter embedder (text-embedding-3-large)
    │   └── retriever.py          # KnowledgeRetriever — importable by any MCP server
    └── data/
        └── brokers/
            ├── tastytrade.py     # TastytradeClient — pure data client
            └── yahoo.py          # YahooClient — pure data client
```
