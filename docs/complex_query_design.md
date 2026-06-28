# Financial Document Intelligence Platform — Design

> Redesigned from QA bot into a complete financial document intelligence system.
> Four task types: `extract` · `reasoning` · `output` · `qa`
> Planner auto‑detects task type from natural language — users never choose a mode.

---

## Layer Architecture

```
┌──────────────────────────────────────────────┐
│              Interface                        │
│  MCP Server  ·  REST API  ·  Chat CLI        │
└───────────────────┬──────────────────────────┘
                    │
┌───────────────────┴──────────────────────────┐
│         Intelligence                         │
│  Planner (WHAT)  +  Executor (HOW)           │
│  Task types: extract | reasoning | output | qa│
└───────────────────┬──────────────────────────┘
                    │
┌───────────────────┴──────────────────────────┐
│            Capabilities (Tools)               │
│  search · extract · verify · compute         │
└───────────────────┬──────────────────────────┘
                    │
┌───────────────────┴──────────────────────────┐
│              Data                             │
│  ingest · parse · index · cache              │
└──────────────────────────────────────────────┘
```

---

## 1. Planner (strategic — decides WHAT)

One LLM call per query. Takes user question + available document catalog.
Returns a **high‑level execution plan** — no keywords, no tool params.

**Task type is auto‑detected** from the user's natural language query. The user never specifies a mode — the Planner reads the intent and selects `extract`, `reasoning`, `output`, or `qa`.

### Plan schema

```json
{
  "task_type": "extract | reasoning | output | qa",
  "objective": "what the user wants",
  "target_scope": ["document areas to search"],
  "exclude_scope": ["areas to skip"],
  "output_shape": "list | table | paragraph | assessment_report"
}
```

### The four task types

| task_type | Meaning | Example query |
|-----------|---------|---------------|
| `extract` | Decompose a big question into target areas; extract specific facts/entities from docs | "List all construction cities" |
| `reasoning` | Analyze, compare, compute, draw conclusions | "Compare R&D efficiency of BYD vs CATL" |
| `output` | Synthesize/summarize into free text | "Summarize key risks in this bond" |
| `qa` | Quality assurance — assess documents for quality, completeness, accuracy, compliance | "Is this annual report fully compliant with CSRC disclosure requirements?" |

### Planner output examples

**Extraction query** — "中国建筑在哪些城市有施工项目？"

*Planner decomposes the big question into focused target areas, then Executor extracts entities from each:*

```json
{
  "task_type": "extract",
  "objective": "Find all cities where the company has construction projects",
  "target_scope": ["项目相关章节", "工程披露章节", "业务分部章节"],
  "exclude_scope": ["风险因素章节", "会计政策章节"],
  "output_shape": "list"
}
```

**Reasoning query** — "Compare R&D spending of BYD and CATL 2022-2024"

```json
{
  "task_type": "reasoning",
  "objective": "Compare R&D efficiency (R&D/revenue ratio) between two companies",
  "target_scope": ["BYD年报 研发投入章节", "CATL年报 研发投入章节"],
  "output_shape": "table"
}
```

**Output query** — "What are the key risk factors in this bond?"

```json
{
  "task_type": "output",
  "objective": "Summarize all risk factors mentioned in the bond document",
  "target_scope": ["风险因素章节", "重大事项提示"],
  "output_shape": "paragraph"
}
```

**QA query** — "Is this annual report compliant with CSRC disclosure requirements for risk factors?"

```json
{
  "task_type": "qa",
  "objective": "Assess whether the risk factor disclosures meet CSRC standards",
  "target_scope": ["风险因素章节", "重大事项提示", "管理层讨论与分析"],
  "exclude_scope": ["财务报表附注", "独立审计报告"],
  "output_shape": "assessment_report"
}
```

---

## 2. Executor (tactical — decides HOW)

Generic loop. Takes a plan, executes it, calls Planner again if stuck.

### Core loop

```
load plan

for each task_type:
  ├── extract:
  │     Planner first decomposes big question → target areas
  │     for each target area:
  │       generate keywords → search → extract → verify → dedup
  │       if saturation (2 rounds no new) → done with this area
  │
  ├── reasoning:
  │     extract per‑entity data → compute ratios/differences
  │     → compare → format table
  │
  ├── output:
  │     broad retrieval across target areas → rerank
  │     → LLM synthesize with inline citations
  │
  └── qa (quality assurance):
        for each quality dimension (completeness, accuracy, compliance):
          retrieve relevant sections from target areas
          → LLM assess against criteria/standards
          → score + evidence + recommendations
        → aggregate into assessment report

if stuck or low confidence:
    → call Planner: "here's what we found, suggest new target areas"
    → new plan replaces old, continue
```

### Keyword generation (Executor's job)

The Executor takes a target area like `"项目相关章节"` and the entity type from the Planner. It then **generates keywords dynamically** using LLM:

```
"Given target area '项目相关章节' and entity type '城市/地点',
generate 2-3 keyword queries to find relevant sections."
→ "项目|工程|施工", "位于|地址|坐落"
```

The Planner never touches this. Executor owns search strategy.

### Saturation (when to stop)

After each round, count new entities found. If 2 consecutive rounds produce 0 new entities, the area is saturated — move to next target area.

### Rewrite loop

If saturation happens but confidence is low (found only 2 entities from a construction company that likely has 30+), the Executor calls Planner:

```
"We searched '项目相关章节' and '工程披露章节' and found only 2 cities.
Confidence is low. Suggest additional target areas to search."
```

Planner returns new target areas. Executor continues.

---

## 3. Tools (Capability Layer)

All tools are reused from the existing `agent/tools/` module.

| Tool | Purpose |
|------|---------|
| `search_text` | Keyword retrieval across document body text |
| `search_headings` | Search section headings by keyword with fuzzy matching |
| `search_tables` | Unified table search — keyword, by table_id, or all tables under a heading |
| `get_section` | Read full section content + all tables under a heading path |
| `compute` | Arithmetic with unit handling (亿/万/%) |
| `get_doc_info` | Document metadata (domain, report period, company name, etc.) |
| `expand_query` | Query expansion using financial synonyms |

---

## 4. Quality Control

| Layer | Mechanism |
|-------|-----------|
| Quote requirement | Every extracted entity must include exact source text |
| Python verification | Quote must exist verbatim in original document |
| Saturation check | Stop only when no new results appear |
| Confidence scoring | LLM self‑rates each extraction |
| Rewrite loop | If stuck, Planner suggests new search angles |
| Assessment scoring | QA assessments include compliance scores with evidence citations |

---

## 5. Interfaces

All interfaces accept natural language only — no mode selection or task_type flags. The Planner auto‑detects the user's intent.

### MCP Server — for LLM‑powered IDEs

Exposes Planner + Executor as tools that Cline/Cursor/etc. can call.
Users ask questions in natural language directly in their editor.

### REST API — for web apps

All endpoints accept `{"query": "natural language question"}` — Planner auto‑routes.

`POST /query` — full Planner → Executor pipeline
`POST /extract` — extraction only
`POST /analyze` — reasoning only
`POST /summarize` — synthesis only
`POST /assess` — quality assurance only

### Chat CLI — for development

```
python3 chat.py "中国建筑有哪些施工项目城市？"
python3 chat.py "Is this bond prospectus fully compliant with PBOC rules?"
```

---

## 6. What stays unchanged

- `agent/tools/` — current search + compute + expand_query tools
- `agent/llm_reasoner.py` — LLM API
- `indexer.py` — document index building
- `retriever.py` — document retrieval
- `run_mineru_extraction.py` — MinerU PDF/HTML extraction pipeline
- `analyze_vocab.py` — vocabulary analysis
- `build_synonyms.py` — synonym building
- `config/` — financial terms, synonyms, common words
- `indices/` — doc registry, heading index, table index

## New files

```
agent/
  planner.py          # LLM → high‑level plan (WHAT), auto task-type detection
  executor.py         # Generic loop (HOW), all four task types
server/
  mcp_server.py       # MCP tool exposure
  api.py              # REST endpoints
chat.py               # Dev CLI
```

## Data

```
public_dataset_upload/
  raw/                # PDF/HTML inputs for MinerU extraction
  extracted/          # Markdown outputs from MinerU
```

## Implementation effort: ~25 hours