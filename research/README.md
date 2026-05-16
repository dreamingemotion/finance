# finance-research MCP

Research MCP server providing stock analysis, SEC filing research, market
data, valuation history, and knowledge base queries.

## Architecture

```
research/
├── server.py              # FastMCP + Starlette, same auth pattern as finance-knowledge
├── edgar.py               # EDGAR REST API: ticker → CIK, find filing, download HTML
├── html_to_markdown.py    # HTML → Markdown conversion for filings
├── indexer.py             # Builds hierarchical tree index from filing markdown
├── tree_store.py          # JSON-on-disk store for indexed filing trees
├── tree_search.py         # Keyword + LLM reasoning search over filing trees
└── tools/
    ├── analysis.py        # analyze() — full and partial stock analysis aggregator
    ├── valuation.py       # get_valuation_ratios() — 10-year P/E and P/B via EDGAR XBRL
    ├── market_data.py     # get_quote, get_snapshot, get_bars, get_full_timeframe
    ├── sec_filings.py     # submit_filing, search_filing, get_section, batch_query, etc.
    └── knowledge.py       # search_knowledge, list_knowledge_categories, etc.
```

## Tools

### Analysis

| Tool | Description |
|---|---|
| `analyze(symbol, full=True)` | **Start here.** Full or partial stock analysis — aggregates snapshot, charts, SEC filing (risks/moat/cashflow), valuation ratios, and knowledge base context in one call. |
| `get_valuation_ratios(symbol)` | 10-year P/E and P/B history from EDGAR XBRL + current sector ETF benchmark. |

**Full analysis** (`full=True`, default) includes:
- Real-time snapshot (price, P/E, P/B, IV rank, HV, beta, market cap, dividend yield)
- 2×2 multi-timeframe candlestick chart grid (2M daily, 2Y weekly, 3D 60-min, 3Y monthly)
- Most recent 10-K from EDGAR, searched for risk factors, economic moat, and cash flow
- 10-year P/E and P/B history with averages, current values, and sector ETF benchmark
- Relevant knowledge base context

**Partial analysis** (`full=False`) includes:
- Real-time snapshot
- 1-year weekly candlestick chart
- 10-year P/E history and sector benchmark (no P/B, no filing)
- Knowledge base context

### Market data

| Tool | Description |
|---|---|
| `get_quote(symbol)` | Real-time price, bid/ask/mark, day OHLCV, volume. |
| `get_snapshot(symbol)` | Quote + full metrics: P/E, P/B, IV rank, HV 30/60-day, beta, market cap, dividend yield, borrow rate. |
| `get_bars(symbol, period, interval)` | OHLCV bars for a single timeframe. period: 1d–10y. interval: 1m–1mo. |
| `get_full_timeframe(symbol, charts?)` | Multi-timeframe chart data (2×2 grid by default). Use instead of multiple get_bars calls. |

### SEC filings

| Tool | Description |
|---|---|
| `submit_filing(ticker, form_type, year)` | Download filing HTML from EDGAR and index it locally. Returns `doc_id`. Cached after first call. |
| `get_filing_structure(doc_id)` | Full hierarchical section tree (node_ids, titles, summaries). |
| `get_section(doc_id, node_id)` | Full text of a section by node_id. |
| `search_filing(query, doc_id)` | Search a filing for relevant sections. Returns cited passages. |
| `batch_query(query, doc_ids)` | Run `search_filing` in parallel across multiple filings. |
| `list_filings()` | List all filings indexed in the local workspace. |
| `delete_filing(doc_id)` | Remove an indexed filing from the workspace. |

### Knowledge base (read-only)

| Tool | Description |
|---|---|
| `search_knowledge(query, categories?, limit?)` | Semantic search over the finance knowledge base. Returns ranked chunks with similarity scores. |
| `list_knowledge_categories()` | List all categories (risk, macro, strategy, etc.) with chunk counts. |
| `list_knowledge_documents()` | List all ingested documents with chunk counts. |
| `get_knowledge_document(document_id)` | Retrieve all chunks for a document. |

## Setup

### Install dependencies

```bash
cd /opt/agents/finance
source .venv/bin/activate
pip install -r requirements.txt
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `EDGAR_USER_AGENT` | Yes | SEC requires this, e.g. `"Name email@example.com"` |
| `KNOWLEDGE_DATABASE_URL` | Yes (knowledge + analysis tools) | PostgreSQL DSN shared with finance-knowledge, e.g. `postgresql://user:pass@10.0.0.139:5432/finance` |
| `OPENROUTER_API_KEY` | Yes | Used for filing search reasoning and knowledge query embeddings |
| `OPENROUTER_BASE_URL` | No | Default: `https://openrouter.ai/api/v1` |
| `GENERATION_MODEL` | No | Default: `anthropic/claude-sonnet-4-6` |
| `RESEARCH_WORKSPACE` | No | Filing index workspace dir. Default: `./workspace` |
| `RESEARCH_HOST` | No | Bind host. Default: `0.0.0.0` |
| `RESEARCH_PORT` | No | Bind port. Default: `8093` |
| `RESEARCH_URL` | Auth only | Public base URL of this server |
| `JWT_SECRET` | Auth only | Shared with auth server |
| `AUTH_SERVER_URL` | Auth only | Public URL of auth server |
| `TT_CLIENT_ID` | No | Tastytrade OAuth client ID (market data primary source) |
| `TT_CLIENT_SECRET` | No | Tastytrade OAuth client secret |
| `TT_REFRESH_TOKEN` | No | Tastytrade OAuth refresh token |

PageIndex uses LiteLLM internally. Route it through OpenRouter by also setting:

```bash
OPENAI_API_KEY=<same value as OPENROUTER_API_KEY>
OPENAI_BASE_URL=https://openrouter.ai/api/v1
```

### Running

```bash
# stdio (for local Claude Desktop)
python -m research.server

# HTTP, no auth
python -m research.server --transport streamable-http

# HTTP with JWT auth (production)
python -m research.server --transport streamable-http --require-auth
```

---

## Deploying as a remote MCP server

### 1. Environment variables

Add these to `/etc/finance.env` alongside the existing entries:

```
EDGAR_USER_AGENT=YourName your@email.com
KNOWLEDGE_DATABASE_URL=<same value as finance-knowledge server>
RESEARCH_WORKSPACE=/opt/agents/finance/research/workspace
RESEARCH_HOST=0.0.0.0
RESEARCH_PORT=8093
RESEARCH_URL=https://mcp.unfolding.in/servers/finance/research

# PageIndex LiteLLM — route through OpenRouter
OPENAI_API_KEY=<same value as OPENROUTER_API_KEY>
OPENAI_BASE_URL=https://openrouter.ai/api/v1

# Required — shared with auth server
JWT_SECRET=<same value as knowledge server>
AUTH_SERVER_URL=https://mcp.unfolding.in/servers/finance/auth
```

### 2. Systemd service

**/etc/systemd/system/finance-research.service**
```ini
[Unit]
Description=Finance Research MCP Server
After=network.target finance-auth.service

[Service]
User=ubuntu
WorkingDirectory=/opt/agents/finance
ExecStart=/opt/agents/finance/.venv/bin/python -m research.server --transport streamable-http --require-auth
EnvironmentFile=/etc/finance.env
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable finance-research
sudo systemctl start finance-research
```

### 3. Nginx reverse proxy

Add to your nginx server config:

```nginx
location /servers/finance/research/ {
    proxy_pass http://127.0.0.1:8093/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Connection '';
    proxy_buffering off;
    proxy_read_timeout 86400;
    chunked_transfer_encoding on;
}
```

Reload nginx:
```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 4. Connect to Claude web

Add it as a remote MCP server in Claude:

**URL:** `https://mcp.unfolding.in/servers/finance/research/mcp`

Claude will prompt you to authenticate via OAuth on first connection.

---

## Usage

```
# Full stock analysis (default)
analyze("PLTR")
→ { snapshot, price_structure, valuation, filing, knowledge }

# Partial analysis (no filing, 1Y weekly chart only)
analyze("PLTR", full=False)
→ { snapshot, price_structure (1Y weekly), valuation (P/E only), knowledge }

# Standalone valuation history
get_valuation_ratios("AAPL")
→ { pe_history (10y), pe_average, pe_current, pb_history (10y), pb_average,
    pb_current, sector_benchmark: { etf, sector_pe, sector_pb } }

# Ingest and search a filing
submit_filing("BLK", "10-K", 2024)
→ { doc_id: "abc123", ticker: "BLK", filing_date: "2024-02-23" }

get_filing_structure("abc123")
→ { overview: [{ title: "Item 1A: Risk Factors", node_id: "0012", ... }] }

search_filing("liquidity risk", "abc123")
→ { passages: [{ text: "...", section: "Item 1A", node_id: "0012" }] }

# Compare across companies
batch_query("liquidity risk", ["abc123", "def456"])
→ { results: { "abc123": { passages: [...] }, "def456": { passages: [...] } } }
```

## Adding new tool modules

1. Create `research/tools/your_module.py` with plain async functions
2. Import and register them as `@mcp.tool()` in `server.py`
