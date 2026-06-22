"""
Research MCP server.

Provides structure-first SEC filing analysis via EDGAR + PageIndex.
Additional tool modules will be registered here as the server grows.

Usage:
    python -m research.server                                      # stdio
    python -m research.server --transport streamable-http          # no auth
    python -m research.server --transport streamable-http --require-auth

Environment variables:
  EDGAR_USER_AGENT          required by SEC, e.g. "MyApp contact@example.com"
  OPENROUTER_API_KEY        your OpenRouter API key
  OPENROUTER_BASE_URL       https://openrouter.ai/api/v1 (default)
  GENERATION_MODEL          anthropic/claude-sonnet-4-6 (default)
  RESEARCH_WORKSPACE        workspace directory (default: ./workspace)
  RESEARCH_HOST             bind host (default 0.0.0.0)
  RESEARCH_PORT             bind port (default 8093)
  RESEARCH_URL              public base URL for this server
  JWT_SECRET                shared with auth server (--require-auth only)
  AUTH_SERVER_URL           public URL of auth server (--require-auth only)
  TT_CLIENT_ID              tastytrade OAuth client ID
  TT_CLIENT_SECRET          tastytrade OAuth client secret
  TT_REFRESH_TOKEN          tastytrade OAuth refresh token

PageIndex uses LiteLLM internally. To route it through OpenRouter set:
  OPENAI_API_KEY  → same value as OPENROUTER_API_KEY
  OPENAI_BASE_URL → https://openrouter.ai/api/v1
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)

from mcp.server.fastmcp import FastMCP
from research.tools.sec_filings import (
    batch_query          as _batch_query,
    delete_filing        as _delete_filing,
    get_filing_structure as _get_filing_structure,
    get_section          as _get_section,
    list_filings         as _list_filings,
    search_filing        as _search_filing,
    submit_filing        as _submit_filing,
)
from research.tools.market_data import (
    get_quote           as _get_quote,
    get_snapshot        as _get_snapshot,
    get_bars            as _get_bars,
    get_full_timeframe  as _get_full_timeframe,
)
from research.tools.knowledge import (
    search_knowledge          as _search_knowledge,
    list_knowledge_categories as _list_knowledge_categories,
    list_knowledge_documents  as _list_knowledge_documents,
    get_knowledge_document    as _get_knowledge_document,
)
from research.tools.analysis import analyze as _analyze
from research.tools.valuation import get_valuation_ratios as _get_valuation_ratios
from research.tools.market_analysis import get_market_analysis as _get_market_analysis
from research.tools.fred import get_fred_series as _get_fred_series

_host = os.getenv("RESEARCH_HOST", "0.0.0.0")
_port = int(os.getenv("RESEARCH_PORT", "8093"))

mcp = FastMCP("finance-research", host=_host, port=_port)


@mcp.tool()
async def submit_filing(ticker: str, form_type: str, year: int) -> dict:
    """
    Fetch a filing from EDGAR and index it with PageIndex.

    Downloads the PDF for the most recent filing of form_type (e.g. "10-K")
    filed in the given year and returns a doc_id for use with all other tools.

    ticker can be a ticker symbol (e.g. "BLK") or a numeric CIK (e.g. "1364742").
    If the ticker lookup fails, the value is tried as a raw CIK automatically.

    Indexing a large filing may take several minutes on first call.
    Subsequent calls for the same filing return immediately from cache.
    """
    return await _submit_filing(ticker, form_type, year)


@mcp.tool()
async def get_filing_structure(doc_id: str) -> dict:
    """
    Return the full hierarchical section tree for a filing.

    Each node has title, node_id, page range, summary, and nested children.
    Use node_ids with get_section to retrieve the full text of any section.

    Call this first to orient yourself before fetching specific sections.
    """
    return await _get_filing_structure(doc_id)


@mcp.tool()
async def get_section(doc_id: str, node_id: str) -> dict:
    """
    Retrieve the full text of a section by node_id.

    Call get_filing_structure first to find node_ids. Returns section title,
    full text, word count, and page range.
    """
    return await _get_section(doc_id, node_id)


@mcp.tool()
async def search_filing(query: str, doc_id: str) -> dict:
    """
    Search a filing for sections relevant to a query.

    Navigates the filing's hierarchical structure, fetches the relevant
    sections, and returns cited passages with section and page info.
    For cross-company search, use batch_query instead.
    """
    return await _search_filing(query, doc_id)


@mcp.tool()
async def batch_query(query: str, doc_ids: list[str]) -> dict:
    """
    Search a query across multiple filings simultaneously.

    Runs in parallel. Use this to compare risk factors, disclosures,
    or financials across companies. Results are keyed by doc_id.
    """
    return await _batch_query(query, doc_ids)


@mcp.tool()
async def list_filings() -> list[dict]:
    """List all filings currently indexed in the local workspace."""
    return await _list_filings()


@mcp.tool()
async def delete_filing(doc_id: str) -> dict:
    """
    Permanently delete an indexed filing from the workspace.

    Does not delete the cached HTML file, only the search index.
    """
    return await _delete_filing(doc_id)


@mcp.tool()
async def get_quote(symbol: str) -> dict:
    """
    Fetch a real-time quote for an equity symbol.

    Returns price, bid, ask, mark, day open/high/low/close, volume,
    data_source ("primary" = tastytrade, "secondary" = yfinance),
    and stale (true if the quote predates the most recent market close).
    """
    return await _get_quote(symbol)


@mcp.tool()
async def get_snapshot(symbol: str) -> dict:
    """
    Fetch a full market snapshot for an equity symbol.

    Combines real-time quote with extended metrics: P/E ratio (tastytrade),
    P/B ratio (yfinance), IV rank, 30/60-day historical volatility, beta,
    market cap, dividend yield, and borrow rate.

    data_source reflects whether tastytrade (primary) or yfinance
    (secondary) supplied the quote and metrics.  P/B is always from yfinance.
    """
    return await _get_snapshot(symbol)


@mcp.tool()
async def get_bars(symbol: str, period: str, interval: str) -> dict:
    """
    Fetch OHLCV bars for a single timeframe.

    For "full timeframe", "full timeframe continuity", or any multi-timeframe
    request, use get_full_timeframe instead — do NOT call this tool multiple times.

    period   — look-back window: 1d 5d 1mo 3mo 6mo 1y 2y 5y 10y
    interval — bar width:        1m 5m 15m 30m 1h 1d 1wk 1mo

    Returns bars (list of {time, open, high, low, close, volume}),
    bar_count, data_source, last_bar_stale, suppress_time_gaps, and chart_style.
    Bars are sorted oldest-first.  tastytrade DXLink is primary; yfinance is the fallback.

    Rendering rules (follow exactly — do not override):
    - Render as a candlestick chart using raw HTML Canvas 2D API only.
      Do NOT use Chart.js, chartjs-chart-financial, Plotly, D3, or any chart library.
      Draw wicks and bodies manually.
    - Use chart_style.up_color for bullish candles and chart_style.down_color for bearish.
      Never use Chart.js default colors or any other color scheme.
    - When suppress_time_gaps is true, use a categorical or sequential index x-axis
      so overnight and weekend gaps are not shown.
    - Each chart must support mouseover tooltips showing date/time, O, H, L, C, and
      % change for the hovered candle. Position tooltips within the panel bounds.
    - Before rendering, deduplicate bars: first filter out any bar where open === 0,
      then deduplicate by composite key (time + open + close).
    - Do not use line charts unless the user explicitly asks.
    """
    return await _get_bars(symbol, period, interval)


@mcp.tool()
async def get_full_timeframe(symbol: str, charts: list[dict] | None = None) -> dict:
    """
    ALWAYS call this tool — never get_bars — when the user wants a
    multi-timeframe overview, continuity analysis, or asks to see a symbol
    across multiple timeframes at once. Do NOT call get_bars multiple times
    as a substitute.

    charts: optional list of {"period": str, "interval": str} dicts to
    override the default timeframes. Omit for the four standard timeframes
    arranged in a 2×2 grid (top-left → top-right → bottom-left → bottom-right):
      top-left:     2-Month Daily   — short-term trend and recent price action
      top-right:    2-Year Weekly   — intermediate trend and swing structure
      bottom-left:  3-Day 60 Min    — intraday detail and entry/exit context
      bottom-right: 3-Year Monthly  — long-term trend and major structure

    Each entry in charts[] has label, symbol, period, interval, bar_count,
    data_source, last_bar_stale, and bars (OHLCV list, oldest-first).

    Each chart entry includes render_order (0-based integer) and grid_position
    ("top-left", "top-right", "bottom-left", "bottom-right") fields.

    Rendering rules (follow exactly — do not override):
    - Render as candlestick charts in a 2×2 grid using raw HTML Canvas 2D API only.
      Do NOT use Chart.js, chartjs-chart-financial, Plotly, D3, or any chart library.
      Draw wicks and bodies manually.
    - Sort charts by render_order and place each at its grid_position.
      Never reorder by timeframe length or any other logic.
    - Use each chart's label field verbatim as the chart title. Do not
      rename or substitute (e.g. do not replace "60 Min" with "Hourly").
    - Use chart_style.up_color for bullish candles and chart_style.down_color for bearish
      across all four panels. Never use Chart.js default colors or any other color scheme.
    - When a chart's suppress_time_gaps is true, use a categorical or sequential index
      x-axis so overnight and weekend gaps are not shown.
    - Each chart must support mouseover tooltips showing date/time, O, H, L, C, and
      % change for the hovered candle. Position tooltips within the panel bounds.
    - Before rendering each chart, deduplicate bars: first filter out any bar where
      open === 0, then deduplicate by composite key (time + open + close).
    - Do not use line charts unless the user explicitly asks.
    """
    return await _get_full_timeframe(symbol, charts)


@mcp.tool()
async def analyze(symbol: str, full: bool = True) -> dict:
    """
    ALWAYS call this tool when the user asks for a stock analysis, company
    analysis, or research on a ticker symbol.  Do NOT attempt to answer from
    memory, read files, or search the web — this tool aggregates everything.

    full=True (default): use for any analysis request unless the user
    explicitly asks for a "partial" or "quick" overview.
      - price_structure: 2×2 multi-timeframe chart grid. Each entry has
        render_order, grid_position, label, suppress_time_gaps, and bars
        (OHLCV list, oldest-first).
      - snapshot: real-time quote plus full metrics (P/E, P/B, IV rank, HV,
        beta, market cap, dividend yield, borrow rate).
      - filing: most recent 10-K from EDGAR, pre-searched for risk factors,
        competitive moat / business model, and cash flow.  Indexing a filing
        for the first time may take several minutes.
      - knowledge: semantically relevant chunks from the finance knowledge base.
      - valuation: full get_valuation_ratios result — 10-year P/E and P/B
        history, pe_average, pb_average, pe_current, pb_current,
        fcf_history (ocf_M, capex_M, fcf_M per year), debt_history
        (lt_debt_M, cash_M, net_debt_M per year), and sector_benchmark.

    full=False (partial): use when the user explicitly requests a partial
    analysis or a quick overview.
      - price_structure: single 1-year weekly chart. Has suppress_time_gaps
        and bars (OHLCV list, oldest-first).
      - snapshot: same as full.
      - knowledge: same as full.
      - valuation: P/E history and sector benchmark only (no P/B data).
      No filing is fetched for partial analysis.

    Chart rendering rules (follow exactly — do not override):
      - Full analysis ONLY: you MUST render the 2×2 candlestick grid before
        writing any analysis text. Do NOT skip or defer chart rendering. Do
        NOT call get_bars or get_full_timeframe separately — the data is
        already in price_structure.charts. Sort by render_order and place
        each chart at its grid_position. Use the label field verbatim as the
        chart title — do not rename or substitute.
      - Partial analysis: render a single candlestick chart from price_structure.
      - Use raw HTML Canvas 2D API only. Do NOT use Chart.js,
        chartjs-chart-financial, Plotly, D3, or any charting library.
        Draw wicks and bodies manually with canvas.getContext("2d").
      - When suppress_time_gaps is true, use a categorical/sequential index
        x-axis — do not use a datetime axis.
      - Before rendering, deduplicate bars: drop any bar where open === 0,
        then deduplicate by composite key (time + open + close).
      - Each chart must have mouseover tooltips showing date/time, O, H, L,
        C, and % change. Position tooltips within the panel bounds.
      - Do not render line charts unless the user explicitly asks.

    After rendering the charts, synthesise directly from the returned data.
    Do NOT search the web or look up additional data — everything needed
    is already in the response. If you need any SEC filing data beyond
    what is returned here (8-Ks, 10-Qs, proxy statements, etc.), use
    submit_filing + search_filing — never the web:
      - Historical and current P/E, P/B, FCF, and debt are in valuation.
      - Risk factors, moat, cash flow, and segment performance are in filing.
      - Real-time price and metrics are in snapshot.
      - Chart data for all timeframes is in price_structure.

    Synthesise the following sections:
      1. Price Structure — support/resistance on the higher timeframes,
         trend direction, and notable technical context.
      2. Snapshot — current valuation, volatility, and positioning metrics.
      3. Valuation — current P/E and P/B vs their 10-year averages (full),
         or current P/E vs its 10-year average (partial); compare both against
         the sector benchmark to assess relative valuation.
      4. Debt & Capital Structure — 10-year trend in long-term debt, cash,
         and net debt from valuation.debt_history (full only).
      5. Risks — from the filing risk_factors search and knowledge base.
      6. Economic Moat — competitive advantage assessment from the filing
         moat search (full only).
      7. Cash Flow — free cash flow trend from valuation.fcf_history and
         capital allocation context from the filing (full only).
      8. Segment Performance — revenue and operating income by segment
         from the filing (full only).
      9. Knowledge Context — include only if knowledge chunks are directly
         relevant to the analysis; omit the section entirely if they are not.
         Cite with Unicode superscript numbers (¹ ² ³ …), not HTML tags,
         e.g. "moat is durable.¹"
         After the final section, if any sources were cited, render a numbered
         "References" list matching the superscripts:
           [n]. Source Title. Finance Knowledge Base. [source_url if available]
    """
    return await _analyze(symbol, full=full)


@mcp.tool()
async def get_market_analysis() -> dict:
    """
    ALWAYS call this tool — and only this tool — when the user asks for a
    Market Analysis, daily market overview, or market summary.
    Do NOT call get_bars, get_quote, or get_snapshot separately to assemble
    this — all data is returned in a single parallel fetch here.

    Market DATA must come exclusively from this tool — do not use WebSearch
    or fetch any URLs to obtain prices, quotes, yields, or index levels.

    Current events are permitted via WebSearch: after rendering the charts
    and before writing the analysis text, search the web for news that
    explains the observed market behavior — sector rotations, yield swings,
    risk-on/risk-off moves, VIX spikes, or any notable divergence. Use the
    search results to provide "why" context in each analysis section.

    ── CITATION STYLE (applies to every section) ────────────────────────────
    Use Unicode superscript numbers for all citations — both knowledge base
    chunks and web search results — e.g. "yields fell sharply.¹"
    Use ¹ ² ³ ⁴ ⁵ ⁶ ⁷ ⁸ ⁹ (Unicode, not HTML <sup> tags).
    Assign numbers sequentially in the order sources are first cited.
    Collect all cited sources into a single numbered References list at the
    very end (after the market summary). Do not use inline author-date
    format anywhere.

    Returns the following sections. Follow all rendering rules exactly.

    ── TENSE / MARKET STATUS ─────────────────────────────────────────────────
    market_open (bool) indicates whether the US equity market is currently
    trading. Adjust all language accordingly:
    - market_open = true  → use present tense throughout. Say "is up", "is
      leading", "is trading at", "so far today", "as of [time] ET", etc.
      Never say "finished", "closed at", "ended", or imply the session is over.
    - market_open = false → use past tense. Say "finished", "closed at",
      "ended the session", etc.

    ── COLOR RULE (applies to every section) ────────────────────────────────
    All percentage changes and numeric deltas must be colored based solely on
    their sign — positive = green (chart_style.up_color #1d9e75), negative =
    red (chart_style.down_color #d85a30). Never override this based on what
    the move "means" for the market (e.g. a falling VIX is still negative →
    red). Always use the pre-computed *_color fields provided in the data
    rather than deriving colors yourself.
    Colors apply ONLY inside Canvas chart elements and rendered HTML tables.
    In prose text, do NOT use any HTML tags (<span>, <sup>, or otherwise) —
    they render as raw markup. For citation superscripts use Unicode
    superscript characters instead: ¹ ² ³ ⁴ ⁵ ⁶ ⁷ ⁸ ⁹

    ── HEADER ────────────────────────────────────────────────────────────────
    Display "Market Analysis for {analysis_date}" as a prominent title at the
    very top, before any charts or analysis. analysis_date is a pre-formatted
    string (e.g. "June 1st, 2026") — use it verbatim.

    ── SECTION 1: INDEX CHARTS (index_charts) ──────────────────────────────
    index_charts.charts contains four entries with render_order (0–3) and
    grid_position ("top-left", "top-right", "bottom-left", "bottom-right"):
      top-left:     SPX  — S&P 500
      top-right:    DJX  — Dow Jones
      bottom-left:  COMP — Nasdaq Composite
      bottom-right: RUT  — Russell 2000
    Each entry has today's intraday bars (period=1d, interval=5m, ~78 bars).
    suppress_time_gaps is true — use a sequential index x-axis, not datetime.

    Rendering rules:
    - Render the 2×2 grid FIRST, before any text analysis.
    - Use raw HTML Canvas 2D API only. No Chart.js, Plotly, D3, or any library.
    - Draw candlesticks (wicks + bodies) using chart_style.up_color (#1d9e75)
      and chart_style.down_color (#d85a30). Never substitute other colors.
    - Sort by render_order; place each at its grid_position.
    - Title each panel with the index label (e.g. "S&P 500") in the top-left.
    - Display chart.formatted_pct prominently in the top-right of each panel
      as large bold text. Use chart.pct_color for the text color.
      Both fields are pre-computed — do not reformat or recolor them.
    - suppress_time_gaps is true: use a sequential index x-axis so pre-market
      gaps are hidden. Bar timestamps are in UTC — convert to Eastern Time
      when formatting x-axis labels:
        const etLabel = new Date(bar.time).toLocaleTimeString('en-US', {
          timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false
        });
    - When computing the y-axis price range, include overlay prices so the
      reference line is always within bounds even on gap days:
        const allPrices = [
          ...bars.flatMap(b => [b.low, b.high]),
          ...chart.overlays.map(ov => ov.price)
        ];
        const rawMin = Math.min(...allPrices);
        const rawMax = Math.max(...allPrices);
        const pad    = (rawMax - rawMin) * 0.03;
        const minPrice = rawMin - pad;
        const maxPrice = rawMax + pad;
    - Each panel must have mouseover tooltips: time (ET, HH:MM), O, H, L, C,
      % change. Convert bar.time from UTC to America/New_York for the tooltip
      time display. Position tooltips within the panel bounds.
    - Before rendering, deduplicate bars: drop any bar where open === 0,
      then deduplicate by composite key (time + open + close).
    - REQUIRED — after drawing all candles, draw reference overlays.
      Each chart entry has an overlays array. Iterate it exactly as shown:
        for (const ov of chart.overlays) {
          if (ov.type === "hline") {
            const lineY = chartHeight - ((ov.price - minPrice) / (maxPrice - minPrice)) * chartHeight;
            ctx.save();
            ctx.setLineDash(ov.dash || [4, 4]);
            ctx.strokeStyle = ov.color;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(0, lineY);
            ctx.lineTo(chartWidth, lineY);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.restore();
          }
        }
      Draw the line only — no text label.

    ── SECTION 2: SECTOR PERFORMANCE (sector_performance) ──────────────────
    sector_performance.sectors lists all 11 GICS sectors via SPDR ETFs,
    already sorted largest-to-smallest by day_change_pct.

    Rendering rules:
    - Render as a horizontal bar chart immediately after the index grid.
    - Each bar = one sector, labeled with the sector label on the left.
    - Use sector.bar_color for each bar's fill color (pre-computed).
    - Annotate each bar with sector.formatted_pct (pre-formatted string).
    - Sectors with a null day_change_pct (data unavailable) show a grey bar.
    - Use #999999 for ALL text labels — sector names on the left AND
      percentage annotations on the right. Do not use white or black.
    - Use raw Canvas 2D API only. No chart libraries.

    After the bar chart write a 3–5 sentence analysis covering:
    - Which sectors led and lagged and what the spread implies.
    - Whether the rotation pattern suggests risk-on or risk-off positioning.
    - Any notable anomaly (e.g. a defensive sector outperforming on an up day).
    - If any knowledge.sector.results are directly relevant to what the data
      shows, incorporate them. If not, omit them entirely. Cite with a
      superscript number per the citation style rule above.

    ── SECTION 3: VIX (vix) ─────────────────────────────────────────────────
    vix contains current_level, prev_level, and day_change_pct. No chart.

    Render the VIX level and day % change before the treasury section.
    Display vix.formatted_pct colored with vix.pct_color (pre-computed).
    A falling VIX is still a negative number — color it red, not green.
    Write a short VIX commentary (2–3 sentences):
    - State the current VIX level and vix.formatted_pct day change.
    - Name the regime from vix.regime (pre-computed: complacent / normal /
      elevated / fear/crisis) — do not recompute it from the level.
    - Note whether VIX direction confirms or contradicts the equity price action.
    - Do NOT perform a new web search for this section. Infer from the index,
      sector, and treasury data plus any web search results already gathered
      for those sections.
    - If any knowledge.volatility.results are directly relevant to what the
      data shows, incorporate them. If not, omit them entirely. Cite with a
      superscript number per the citation style rule above.

    ── SECTION 4: TREASURY YIELDS (treasury_yields) ────────────────────────
    treasury_yields.yields lists five maturities in order: 3M, 2Y, 5Y, 10Y, 30Y.
    Data comes from FRED (US Treasury constant-maturity rates, authoritative,
    typically published with a 1-business-day lag). treasury_yields.as_of shows
    the date of the most recent FRED observation.
    Each entry has yield_pct (percent), change_bps (basis points vs prior day),
    and prev_yield_pct. Entries with no data have yield_pct=null.
    treasury_yields.curve_shape contains spread_2y_10y, spread_3m_10y,
    spread_10y_30y, and shape_notes for curve characterisation.

    Rendering rules:
    - Render the section header "Treasury Yields" followed immediately by a
      subtitle in small grey text:
        "As of {treasury_yields.as_of} · FRED publishes at ~4:15 PM ET on business days"
    - Render a table: Maturity | Yield (%) | Change (bps).
      Display yield.formatted_yield and yield.formatted_change — pre-formatted
      strings (e.g. "4.352%" and "+2.0 bps"). Show "N/A" as-is.
    - Color each Yield and Change cell using yield.change_color — a pre-computed
      hex string. Do not recompute colors from the numeric values.
    - Below the table render a yield curve line chart using raw Canvas 2D API.
      x-axis = maturity order (3M → 2Y → 5Y → 10Y → 30Y), y-axis = yield_pct.
      Plot each available maturity as a point; connect with straight lines.
      Use curve_shape.curve_color for the line color (pre-computed hex).
      Skip null maturities.

    After the curve write a 4–6 sentence analysis covering:
    - Whether yields rose or fell today and what that implies for risk assets.
    - Current curve shape (normal, flat, inverted, or humped) using curve_shape.
    - Whether the curve is steepening or flattening based on today's bps changes.
    - What the shape implies for the economic and credit outlook.
    - If any knowledge.yields.results are directly relevant to what the data
      shows, incorporate them. If not, omit them entirely. Cite with a
      superscript number per the citation style rule above.

    ── KNOWLEDGE CONTEXT (knowledge) ────────────────────────────────────────
    knowledge contains three keys — sector, yields, volatility — each with
    a results list of relevant knowledge base chunks (content, source, score).
    Use knowledge chunks only when they are directly relevant to what the
    market data shows — do not force them in. If a chunk does not add
    meaningful context to the observed behavior, omit it. Weave any used
    insights into the appropriate section above rather than listing them
    separately. Skip silently on error or empty results.

    After all sections, write a 2–3 sentence overall market summary tying
    together the index moves, sector rotation, yield curve, and VIX into a
    single coherent picture of the day's market character. This summary must
    be inferred purely from the data sections above — do not perform any
    additional web searches for it.

    ── REFERENCES ────────────────────────────────────────────────────────────
    At the very end, after the market summary, if any sources were cited
    render a "References" section with a numbered list matching the
    superscripts used in the text. Format each entry as:
      Knowledge base: [n]. Source Title. Finance Knowledge Base. [source_url if available]
      Web source:     [n]. Author/Outlet. (Date). Headline/Title. URL if available.
    Omit the References section entirely if nothing was cited.
    """
    return await _get_market_analysis()


@mcp.tool()
async def get_fred_series(series_id: str, period: str = "1y") -> dict:
    """
    Fetch a FRED (Federal Reserve Economic Data) economic time series.

    Use this for any macroeconomic indicator question — interest rates, inflation,
    employment, GDP, yield spreads, money supply, or anything from the St. Louis Fed.
    Do NOT use get_bars for this — FRED data is not available through tastytrade or yfinance.

    series_id: FRED series identifier. Common examples:
      Treasury rates : DGS1MO DGS3MO DGS6MO DGS1 DGS2 DGS5 DGS10 DGS20 DGS30
      Fed policy     : DFF (daily fed funds effective rate)
                       FEDFUNDS (monthly effective rate)
      Inflation      : CPIAUCSL (headline CPI, monthly)
                       CPILFESL (core CPI ex food/energy)
                       PCEPI (PCE price index)
                       PCEPILFE (core PCE — Fed's preferred measure)
      Employment     : UNRATE (unemployment rate)
                       PAYEMS (total nonfarm payrolls, monthly chg)
                       ICSA (initial jobless claims, weekly)
      Growth         : GDP (nominal, quarterly)
                       GDPC1 (real GDP, quarterly)
                       INDPRO (industrial production index)
      Spreads        : T10Y2Y (10Y-2Y Treasury spread)
                       T10Y3M (10Y-3M Treasury spread)
      Money supply   : M2SL (M2 money supply)

    period: look-back window — 1d 3d 5d 1mo 3mo 6mo 1y 2y 5y 10y.
      Note: FRED series have varying native frequencies (daily, weekly, monthly,
      quarterly). The observations returned reflect actual publication dates.
      Short periods (1d, 3d) are padded to ensure at least one observation is returned.

    Returns observations as [{date, value}, ...] oldest-first, with series
    metadata (title, units, frequency). Missing values (weekends/holidays)
    are already stripped — only real published data points are included.

    Render observations as a line chart when the user asks to visualise the series.
    Annotate the most recent value prominently. For rate/yield series express the
    value in percent (e.g. "4.35%"); for index series show the raw level.
    """
    return await _get_fred_series(series_id, period)


@mcp.tool()
async def get_valuation_ratios(symbol: str) -> dict:
    """
    10-year historical P/E and P/B ratios plus current sector benchmarks.

    Data sources:
      - EPS and book value: EDGAR XBRL company-facts API (structured, no filing
        download needed). Covers 10+ years of annual 10-K data.
      - Year-end stock prices: yfinance daily history matched to each fiscal
        year-end date.
      - Sector benchmark: SPDR sector ETF (XLK, XLF, XLV, etc.) P/E and P/B
        via yfinance — market-cap-weighted sector proxy.

    Returns:
      pe_history: list of {year, fiscal_year_end, eps, price, pe} for each
        available fiscal year. Loss years (negative EPS) have pe=null.
      pe_average: arithmetic mean of positive P/E values over the period.
      pe_current: trailing P/E from yfinance.
      pb_history: list of {year, fiscal_year_end, equity_M, shares_M, bvps,
        price, pb}. equity_M and shares_M are in millions.
      pb_average: arithmetic mean of positive P/B values over the period.
      pb_current: current P/B from yfinance.
      fcf_history: list of {year, ocf_M, capex_M, fcf_M} in millions.
      debt_history: list of {year, lt_debt_M, cash_M, net_debt_M} in millions.
        lt_debt_M prefers LongTermDebtNoncurrent+DebtCurrent; falls back to LongTermDebt.
        net_debt_M = lt_debt_M - cash_M.
      sector_benchmark: {sector, etf, sector_pe, sector_pb}.
      data_years: number of years available for P/E, P/B, FCF, and debt respectively.
      notes: list of warnings (e.g. fallback EPS type, missing data).

    Use pe_average and pb_average vs pe_current and pb_current to assess
    whether the stock is trading at a premium or discount to its historical
    range. Compare pe_current and pb_current against sector_benchmark to
    assess relative valuation within the sector.
    """
    return await _get_valuation_ratios(symbol)


@mcp.tool()
async def search_knowledge(
    query: str,
    categories: list[str] | None = None,
    limit: int = 5,
) -> dict:
    """
    Semantic search over the finance knowledge base.

    Embeds the query and returns the most similar knowledge chunks ranked by
    cosine similarity.  Use this to pull in domain knowledge — strategy notes,
    macro context, risk frameworks — before or during analysis.

    categories: optional list of category names to restrict results.
        Common values: risk, market_risk, macro, strategy, technical,
        sentiment, earnings, sector, valuation, options, inference, methodology.
        Call list_knowledge_categories to see all available categories and counts.

    Returns chunks with content, source document title, source_url, categories,
    and similarity score.
    """
    return await _search_knowledge(query, categories=categories, limit=limit)


@mcp.tool()
async def list_knowledge_categories() -> list[dict]:
    """
    List all categories in the knowledge base with chunk counts.

    Returns seeded categories (risk, macro, strategy, etc.) and any
    auto-discovered categories.  Use category names with search_knowledge
    to scope queries to a specific domain.
    """
    return await _list_knowledge_categories()


@mcp.tool()
async def list_knowledge_documents() -> list[dict]:
    """
    List all documents ingested into the knowledge base.

    Returns document id, title, source_url, creation date, and chunk count.
    Use document ids with get_knowledge_document to retrieve full chunk text.
    """
    return await _list_knowledge_documents()


@mcp.tool()
async def get_knowledge_document(document_id: int) -> dict:
    """
    Retrieve all chunks for a specific knowledge document.

    Returns the document title, source_url, and every chunk with its content
    and categories.  Use search_knowledge when you want targeted passages
    instead of an entire document.
    """
    return await _get_knowledge_document(document_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Research MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
    )
    parser.add_argument("--require-auth", action="store_true")
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    if not args.require_auth:
        mcp.run(transport="streamable-http")
        return

    # ---- Auth-protected streamable-http ------------------------------------
    import anyio
    import uvicorn
    from contextlib import asynccontextmanager
    from starlette.applications import Starlette
    from starlette.requests import Request as StarletteRequest
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    from shared.auth.middleware import BearerTokenMiddleware

    jwt_secret = os.environ["JWT_SECRET"]
    auth_url   = os.environ["AUTH_SERVER_URL"].rstrip("/")
    mcp_url    = os.environ.get("RESEARCH_URL", "").rstrip("/")
    resource_metadata_url = (
        f"{mcp_url}/.well-known/oauth-protected-resource" if mcp_url else ""
    )

    _mcp_asgi_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with _mcp_asgi_app.router.lifespan_context(_mcp_asgi_app):
            yield

    async def protected_resource_metadata(request: StarletteRequest):
        return JSONResponse({
            "resource":             f"{mcp_url}/mcp",
            "authorization_servers": [auth_url],
        })

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
            Mount("/", app=_mcp_asgi_app),
        ],
    )
    app.add_middleware(
        BearerTokenMiddleware,
        jwt_secret=jwt_secret,
        resource_metadata_url=resource_metadata_url,
    )

    config = uvicorn.Config(app, host=_host, port=_port, log_level="info")
    server = uvicorn.Server(config)
    anyio.run(server.serve)


if __name__ == "__main__":
    main()
