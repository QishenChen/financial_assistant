# Financial Document Intelligence Platform

A natural-language interface for querying, analyzing, and assessing financial documents. It uses a **Planner + ReACT Executor** architecture with automatic task-type detection, and exposes a web UI, REST API, MCP server, and a CLI.

**Four task types** — detected automatically from the user's question:

| Type | Purpose | Example |
|------|---------|---------|
| `extract` | Pull specific facts, entities, or data points | "List all construction project cities" |
| `reasoning` | Analyze, compare, compute ratios, draw conclusions | "Compare R&D spending of BYD vs CATL 2022-2024" |
| `output` | Synthesize or summarize | "Summarize key risks in this bond" |
| `qa` | Quality assurance / compliance assessment | "Is this annual report CSRC-compliant?" |

---

## Table of Contents

- [Quick Start](#quick-start)
- [Interfaces](#interfaces)
  - [Web App](#web-app)
  - [REST API](#rest-api)
  - [Chat CLI](#chat-cli)
  - [MCP Server](#mcp-server)
- [Project Structure](#project-structure)
- [Architecture](#architecture)
- [Data Pipeline](#data-pipeline)
- [PDF Viewer & Reference Links](#pdf-viewer--reference-links)
- [Configuration](#configuration)
- [Utility Scripts](#utility-scripts)

---

## Quick Start

### 1. Create Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install requests jieba pypdf PyPDF2

# For the REST API / web app
pip install fastapi uvicorn python-multipart
```

### 2. Configure API Key

```bash
cp .env.example .env
# Edit .env with your DashScope / OpenAI-compatible key
```

Example `.env`:

```bash
LLM_API_KEY=sk-your-api-key-here
LLM_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus-latest
```

### 3. Build Indices

```bash
python3 indexer.py          # headings, tables, doc registry
python3 build_page_map.py   # page-level text snippets for reference links
```

### 4. Run

```bash
# Web app + API
python3 server/api.py

# Or chat in the terminal
python3 chat.py "中国建筑有哪些施工项目城市？"
```

Open http://localhost:8000 in Chrome for the web UI.

---

## Interfaces

### Upload Page

`http://localhost:8000/upload`

- Drag-and-drop or browse to select PDF / HTML files.
- Click **Submit** to upload; the app redirects to the dialogue page.
- Uploaded files are saved to `uploads/raw/`.
- MinerU extraction and index rebuilding run in the background.
- While extraction is in progress, files appear under `[uploaded]` in the left panel.
- After extraction completes, click the refresh button (↻) to see them as indexed documents.

### Web App

`server/api.py` serves a three-pane web UI at `http://localhost:8000`:

- **Left**: document catalog
- **Center**: chat history with clickable source badges
- **Right**: PDF.js viewer

Click a source badge like `📎 annual_cmb_2025_report p.263` to open the PDF at that page.

> The PDF viewer uses PDF.js loaded from jsDelivr and needs CORS headers for cross-origin Range requests. The FastAPI server already adds `CORSMiddleware` for `/raw/*` static files.

### REST API

```bash
python3 server/api.py
```

Endpoints:

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/query` | Full Planner → Executor pipeline |
| `GET`  | `/health` | Health + LLM config check |
| `GET`  | `/catalog` | Document catalog |
| `GET`  | `/page-map` | Page mapping for reference resolution |

Example:

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Compare R&D spending of BYD vs CATL 2022-2024"}'
```

### Chat CLI

Interactive:

```bash
python3 chat.py
```

Single query:

```bash
python3 chat.py "中国建筑有哪些施工项目城市？"
python3 chat.py "Compare R&D spending of BYD vs CATL 2022-2024"
python3 chat.py "Is this annual report compliant with CSRC disclosure requirements?"
```

### MCP Server

Expose the platform as an MCP tool for compatible IDEs.

```bash
python3 server/mcp_server.py
```

Tool name: `query_financial_docs`

Example JSON-RPC call:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "query_financial_docs",
    "arguments": {
      "query": "Summarize key risks in the bond prospectus"
    }
  }
}
```

---

## Project Structure

```
├── chat.py                          # Dev CLI (single query or interactive)
├── server/
│   ├── api.py                       # FastAPI REST API + web UI
│   └── mcp_server.py                # MCP stdio server
├── frontend/                        # Web UI assets
│   ├── index.html
│   ├── app.js                       # PDF.js viewer + chat logic
│   └── style.css
├── agent/
│   ├── planner.py                   # Auto task-type detection + plan
│   ├── executor.py                  # ReACT loop orchestration
│   ├── react_loop.py                # Generic Think→Act→Observe loop
│   ├── llm_reasoner.py              # LLM API client
│   ├── page_resolver.py             # Text snippet → PDF page number
│   ├── ref_mapper.py                # Reference IDs + background page resolution
│   └── tools/                       # Search / compute tools
│       ├── search_headings.py
│       ├── search_tables.py
│       ├── search_text.py
│       ├── get_section.py
│       ├── compute.py
│       ├── get_doc_info.py
│       ├── expand_query.py
│       └── _loader.py
├── config/
│   ├── financial_terms.json         # Financial term synonyms
│   ├── financial_synonyms.json      # 500+ grouped synonyms
│   └── common_words.json            # jieba IDF weights
├── indices/
│   ├── doc_registry.json
│   ├── heading_index.json
│   ├── table_index.json
│   └── page_map.json                # Page snippets for reference links
├── utils/
│   └── text_utils.py                # Fuzzy matching, normalization
├── public_dataset_upload/           # legacy bundled dataset (no longer indexed)
│   ├── raw/                         # Original PDF/HTML files
│   └── extracted/                   # MinerU-extracted markdown + layout JSON
├── uploads/
│   ├── raw/                         # user-uploaded PDFs/HTMLs
│   ├── extracted/                   # extraction output for uploads
│   └── metadata/                    # LLM-generated names + summaries
├── indexer.py                       # Build heading/table/doc indices
├── build_page_map.py                # Build page_map.json from MinerU output
├── upload_namer.py                  # LLM filename + summary generation
├── upload_processor.py              # background extraction + indexing for uploads
├── retriever.py                     # Backward-compat retrieval facade
├── run_mineru_extraction.py         # MinerU cloud batch extraction
├── analyze_vocab.py                 # jieba vocabulary / IDF analysis
├── build_synonyms.py                # LLM-generated synonym dictionary
└── docs/
    └── complex_query_design.md      # Full architecture design
└── .venv/                           # Python virtual environment
```

---

## Architecture

```
User Query
    ↓
Planner (WHAT)  →  task_type, objective, target_scope, output_shape
    ↓
Executor (HOW)
    ↓
ReACT Loop  →  Think → Act (tools) → Observe → repeat until saturated
    ↓
Synthesized Answer + clickable source references
```

### Planner

One LLM call per query. Returns a high-level plan without tool parameters. The plan is passed to the Executor.

### Executor / ReACT Loop

The Executor runs a multi-round ReACT loop:

1. **Think**: LLM decides which tools to call.
2. **Act**: Execute tools (`search_headings`, `search_tables`, `search_text`, `get_section`, `compute`, etc.).
3. **Observe**: Incorporate results and track newly found entities.
4. **Saturation**: Stop when two consecutive rounds yield no new results.

### Tools

| Tool | Purpose |
|------|---------|
| `search_headings` | Find section headings by keyword |
| `search_tables` | Find tables by keyword, ID, or surrounding heading |
| `search_text` | Search raw text in a document |
| `get_section` | Read full section content + tables |
| `compute` | Unit-aware arithmetic (亿 / 万 / % / ratios) |
| `get_doc_info` | Document metadata and domain |
| `expand_query` | Expand query with financial synonyms |

---

## Data Pipeline

```
uploads/raw/                         # user-uploaded PDFs & HTMLs
        ↓
run_mineru_extraction.py             # MinerU v4 cloud API
        ↓
uploads/extracted/                   # markdown + *_middle.json / *_layout.json
        ↓
upload_processor.py                  # stage indexed outputs under uploads/extracted/uploaded/
        ↓
indexer.py                           # → indices/{heading,table,doc_registry}.json
build_page_map.py                    # → indices/page_map.json
        ↓
agent/tools/                         # search, retrieve, compute
```

> **Note:** `public_dataset_upload/` is no longer scanned or displayed. Only documents uploaded through the UI are indexed and queryable.

### Adding New Documents

1. Open `http://localhost:8000/upload`.
2. Drag and drop PDFs / HTMLs and click **Submit**.
3. The server automatically extracts text and rebuilds indices in the background.
4. Refresh the dialogue page after a minute to see the new document under `[uploaded]`.

---

## PDF Viewer & Reference Links

Source references in answers look like:

```markdown
[来源: annual_cmb_2025_report p.263](ref:annual_cmb_2025_report:263)
```

The web UI renders them as clickable badges. Clicking a badge:

1. Resolves the document ID to a raw PDF path via `/page-map`.
2. Probes both `.pdf` and `.PDF` extensions (case-sensitive Linux filesystem).
3. Loads the PDF in the right-side PDF.js viewer.
4. Jumps to the referenced page number.

Page numbers are resolved by `agent/page_resolver.py` using `indices/page_map.json`, which stores the full text of each PDF page extracted from MinerU's `middle.json` / `layout.json`.

---

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `LLM_API_KEY` | (required) | API key |
| `LLM_API_BASE` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | API endpoint |
| `LLM_MODEL` | `qwen-plus-latest` | Model name |
| `LLM_TEMPERATURE` | `0.0` | Sampling temperature |
| `LLM_MAX_TOKENS` | `4096` | Max completion tokens |

---

## Utility Scripts

```bash
# Analyze document vocabulary and generate IDF weights
python3 analyze_vocab.py

# Generate / refresh financial synonym dictionary
python3 build_synonyms.py

# Extract PDFs/HTMLs using MinerU cloud API
python3 run_mineru_extraction.py

# Build page-level text index
python3 build_page_map.py

# Run page-number smoke test
python3 test_page_numbers.py
```

---

## License

MIT
