# Knowledge MCP Server

Ingest research articles and surface relevant insights during analysis via semantic search.

Upload an article in the Claude app, tell Claude to ingest it, and the server extracts
discrete insight units using Claude, embeds them with `text-embedding-3-large`, and stores
everything in Postgres with pgvector. Future analysis sessions query the knowledge base
via semantic similarity and category filters.

Runs on port 8092. Shares the same OAuth infrastructure as the market data server (port 8091).

---

## How ingestion works

```
User uploads article in Claude app
Claude reads the file and calls ingest_document(title, content)
  └─► Claude API (via OpenRouter) extracts discrete insight chunks + assigns categories
  └─► OpenRouter embeddings API embeds all chunks in one batch
  └─► Stored atomically in Postgres: documents + chunks + chunk_categories
```

Each chunk is a self-contained claim that stands alone — specific numbers, thresholds,
and named entities are preserved. Example from a VIX article:

> "When VIX reaches 30, JP Morgan research shows S&P 500 returns are positive
>  70–83% of the time over subsequent periods, making it a historically reliable
>  buy signal."

That chunk is tagged `["strategy", "options", "technical"]` and embedded for semantic search.

---

## Seeded categories

| Category | Description |
|----------|-------------|
| `risk` | General risk factors and warnings |
| `market_risk` | Market-specific risks: concentration, liquidity, breadth, fragility |
| `macro` | Macroeconomic factors: Fed policy, rates, inflation, GDP |
| `strategy` | Trading and investment strategies |
| `technical` | Technical analysis, chart patterns, indicators |
| `sentiment` | Market sentiment, fear/greed, investor positioning |
| `earnings` | Earnings reports, guidance, analyst estimates |
| `sector` | Sector rotation and sector-specific analysis |
| `valuation` | Valuations: PE ratios, multiples, fair value, spreads |
| `options` | Options-specific: VIX, implied volatility, skew, positioning |

Claude can add new categories during ingestion when none of the above fit.

---

## Setup

### 1. Postgres database

On your Postgres server at 10.0.0.139:

```sql
CREATE DATABASE finance;
CREATE USER finance_knowledge WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE finance TO finance_knowledge;
\c finance
GRANT USAGE, CREATE ON SCHEMA public TO finance_knowledge;
```

Enable the pgvector extension (requires superuser, one-time per database):

```sql
\c finance
CREATE EXTENSION IF NOT EXISTS vector;
```

The knowledge schema (`documents`, `chunks`, `categories`, `chunk_categories`) and
the vector index are created automatically on first startup.

### 2. Environment variables

Add these to `/etc/finance.env` alongside the existing entries (see `shared/auth/README.md`). No quotes needed — systemd reads these as plain `KEY=VALUE` pairs:

```
# Required
KNOWLEDGE_DATABASE_URL=postgresql://finance_knowledge:Tr0ub4dor&3@10.0.0.139:5432/finance
OPENROUTER_API_KEY=sk-or-v1-aBcD1234EfGhIjKl5678MnOpQrStUvWx

# Optional — these are the defaults
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
GENERATION_MODEL=anthropic/claude-sonnet-4-6
EMBEDDING_MODEL=openai/text-embedding-3-large

# Required only if running with --require-auth (same value as auth + market data servers)
JWT_SECRET=a3f8c2d1e4b7f9e0c3d5a8b2f6e1c4d7a9b3f0e2c5d8a1b4f7e0c3d6a9b2f5
AUTH_SERVER_URL=https://finance.example.com:8090
```

### 3. Install dependencies

```bash
cd /path/to/finance
source .venv/bin/activate    # or create venv first — see shared/auth/README.md
pip install -r requirements.txt
```

---

## Running

### Locally (stdio — for Claude Desktop)

```bash
cd /path/to/finance
python -m knowledge.server
```

### SSE without auth (development)

```bash
python -m knowledge.server --transport sse --port 8092
```

### SSE with OAuth (recommended for cloud)

```bash
python -m knowledge.server --transport sse --port 8092 --require-auth
```

### Systemd service

**/etc/systemd/system/finance-knowledge.service**
```ini
[Unit]
Description=Finance Knowledge MCP Server
After=network.target postgresql.service finance-auth.service

[Service]
User=ubuntu
WorkingDirectory=/path/to/finance
ExecStart=/path/to/finance/.venv/bin/python -m knowledge.server --transport sse --port 8092 --require-auth
EnvironmentFile=/etc/finance.env
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable finance-knowledge
sudo systemctl start finance-knowledge
```

---

## Authentication

The knowledge server uses the same OAuth 2.1 infrastructure as the market data server.
Both share `JWT_SECRET` — a valid token from the auth server works on both.

Without `--require-auth`: no authentication, suitable for local/stdio use.
With `--require-auth`: Bearer JWT required on every request. The auth server at port 8001
issues tokens via the same login flow.

See [`shared/auth/README.md`](../shared/auth/README.md) for the full OAuth setup guide.

---

## MCP client configuration

Client configs will be documented here once top-level MCP servers are built.
For local stdio use, point your client at `knowledge/server.py` with the required env vars.

---

## Available tools

| Tool | Description |
|------|-------------|
| `ingest_document` | Extract chunks from an article, embed, and store with category tags |
| `search_knowledge` | Semantic search — returns most relevant chunks for a query |
| `get_chunks_by_category` | All chunks tagged with a category, most recent first |
| `list_categories` | All categories (seeded + discovered) with chunk counts |
| `list_documents` | All ingested documents with titles and chunk counts |
| `get_document` | Full document + all its chunks by document ID |
| `delete_document` | Permanently remove a document and its chunks |

### Usage pattern

When you upload an article in the Claude app:
```
"Ingest this article — title it 'JPM VIX Study May 2026'"
```
Claude reads the file and calls `ingest_document` with the full text.

During analysis:
```
"What do we know about VIX as a buy signal?"
"Show me everything tagged market_risk"
```
Claude calls `search_knowledge` or `get_chunks_by_category` to surface relevant insights.

---

## Project layout

```
finance/
├── knowledge/
│   ├── server.py     — FastMCP server + all tool definitions
│   └── ingest.py     — Claude-powered chunk extraction + Postgres storage
└── shared/
    └── knowledge/
        ├── db.py         — asyncpg pool, schema init, seeded categories
        ├── embedder.py   — OpenRouter embedder (text-embedding-3-large)
        └── retriever.py  — KnowledgeRetriever (importable by any MCP server)
```

`shared/knowledge/retriever.py` is the read layer — any future MCP server (signals,
portfolio, etc.) can import `KnowledgeRetriever` to query the knowledge base during
its own analysis without going through this server.
