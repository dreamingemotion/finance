# finance-research MCP

Research MCP server. Currently provides structure-first SEC filing analysis;
additional tool modules will be added here over time.

## Architecture

```
research/
├── server.py            # FastMCP + Starlette, same auth pattern as finance-knowledge
├── edgar.py             # EDGAR REST API: ticker → CIK, find filing, download PDF
├── pageindex_client.py  # Async wrapper around the PageIndex SDK
└── tools/
    └── sec_filings.py   # Six SEC filing tools (first tool module)
```

## Tools

| Tool | Description |
|---|---|
| `submit_filing(ticker, form_type, year)` | Download PDF from EDGAR and index with PageIndex. Returns `doc_id`. Cached after first call. |
| `get_filing_status(doc_id)` | Check PageIndex processing status for a filing. |
| `get_filing_structure(doc_id)` | Return the full hierarchical section tree (node_ids, titles, page ranges, summaries). |
| `get_section(doc_id, node_id)` | Fetch full text of a section by node_id. |
| `search_filing(query, doc_id)` | Navigate structure, identify relevant sections, return cited passages. |
| `batch_query(query, doc_ids)` | Run `search_filing` in parallel across multiple filings. |

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
| `KNOWLEDGE_DATABASE_URL` | Yes (knowledge tools) | PostgreSQL DSN shared with finance-knowledge, e.g. `postgresql://user:pass@10.0.0.139:5432/finance` |
| `OPENROUTER_API_KEY` | Yes | Used for search reasoning (section relevance) and knowledge query embeddings |
| `OPENROUTER_BASE_URL` | No | Default: `https://openrouter.ai/api/v1` |
| `GENERATION_MODEL` | No | Default: `anthropic/claude-sonnet-4-6` |
| `RESEARCH_WORKSPACE` | No | PageIndex workspace dir. Default: `./workspace` |
| `RESEARCH_HOST` | No | Bind host. Default: `0.0.0.0` |
| `RESEARCH_PORT` | No | Bind port. Default: `8093` |
| `RESEARCH_URL` | Auth only | Public base URL of this server |
| `JWT_SECRET` | Auth only | Shared with auth server |
| `AUTH_SERVER_URL` | Auth only | Public URL of auth server |

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
# 1. Ingest a filing (slow first time, instant on repeat)
submit_filing("BLK", "10-K", 2024)
→ { doc_id: "abc123", ticker: "BLK", filing_date: "2024-02-23", page_count: 212 }

# 2. Explore the structure
get_filing_structure("abc123")
→ { structure: [{ title: "Item 1A: Risk Factors", node_id: "0012", ... }] }

# 3. Read a specific section
get_section("abc123", "0012")
→ { section_title: "Item 1A: Risk Factors", full_text: "...", word_count: 4250, pages: "23-45" }

# 4. Search within a filing
search_filing("liquidity risk", "abc123")
→ { passages: [{ text: "...", section: "Item 1A", pages: "23-25" }] }

# 5. Compare across companies
batch_query("liquidity risk", ["abc123", "def456"])
→ { results: { "abc123": { passages: [...] }, "def456": { passages: [...] } } }
```

## Adding new tool modules

1. Create `research/tools/your_module.py` with plain async functions
2. Import and register them as `@mcp.tool()` in `server.py`
