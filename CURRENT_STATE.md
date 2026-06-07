# PIM Ingestion Agent — Current State

## Overview

An agentic system that processes messy eCommerce source files (CSV, xlsx, xls) and produces 4 standardized PIM template files. Two graphs exist:

1. **Pipeline Graph** (`graph` in `graph.py`) — FastAPI-based HITL approval workflow
2. **VinGPT Graph** (`vingpt_graph` in `graph.py`) — Conversational SSE-based chat interface

Both share the same underlying tools (profiling, mapping, rendering) but differ in how they handle user interaction.

---

## File Structure

```
pim_agent/
├── main.py              # CLI entry point (uses pipeline graph)
├── api.py               # FastAPI server (both graphs)
├── graph.py             # Both graph assemblies + all node functions
├── state.py             # Pydantic models + AgentState + IngestionState
├── helpers.py           # File I/O, utility, column mapping, blank template download
├── agents.py            # Category strategies + VinGPT node functions
├── learning.py          # LangSmith ContextHub integration
├── STATE_1.md           # Phase boundary handover (legacy reference)
├── STATE_2.md           # Phase 5/6 PIM API integration plan
├── CURRENT_STATE.md     # ← You are here
├── tools/
│   ├── mapping.py       # build_attribute_definitions
│   ├── profiling.py     # profile_file tool (legacy, unused)
│   ├── references.py    # extract_reference_values
│   └── rendering.py     # render_category/attribute/reference/product xlsx
├── blank-templates/     # Downloaded PIM blank templates
│   ├── Category_template.xlsx
│   ├── Attribute_template.xlsx
│   └── Product_template.xlsx
├── output/              # Generated xlsx files (by fingerprint)
└── cache/               # Cached column mappings (by fingerprint)
```

---

## State Schemas

### `AgentState` (`state.py`) — Used by pipeline graph
25+ fields. Includes everything: file metadata, profiles, mapping, validation, output paths. Uses `DeltaChannel` for messages.

### `IngestionState` (`state.py`) — Used by VinGPT graph
9 fields, clean and minimal:
| Field | Type | Purpose |
|---|---|---|
| `messages` | `list` | Chat history |
| `file_path` | `str` | Source file |
| `sheet_name` | `str \| None` | Selected sheet |
| `profile_data` | `dict \| None` | Collapsed file metadata |
| `core_mappings` | `dict[str, str]` | SKU/code/mrp → source column |
| `custom_mappings` | `dict[str, str]` | Dynamic attributes preserved as-is |
| `mapping_confidence` | `dict[str, int]` | 0-100 confidence per mapping |
| `pending_questions` | `list` | Structured question dicts for chat |
| `generated_files` | `list[str]` | Output xlsx paths |
| `jwt_token` | `str` | Bearer token for PIM API calls |

---

## Pipeline Graph (`graph`)

```
START → triage → categories → mapper → evaluate → router → render → END
                                         ↑                │
                                         └──── retry ─────┘
```

| Node | Function | What it does |
|---|---|---|
| `triage` | `triage_source` | Opens file, detects sheets/headers, counts rows/cols. Lazy reads (first 20 rows only). |
| `categories` | `resolve_categories` | Runs 4-strategy fallback chain from `agents.py` to discover category paths. |
| `mapper` | `map_columns_specialist` | Calls Gemini with structured output `MappingLLMResponse`. Maps columns to PIM attributes. |
| `evaluate` | `evaluate_mappings` | Programmatic type + mandatory checks. 3 retry cycles. |
| `render` | `render_agent` | Builds attribute defs, references, product rows, renders 4 xlsx files. |

**Router signals:**
| Signal | Condition |
|---|---|
| `retry` | Validation errors + cycle < 3 |
| `fail` | Validation errors + cycle >= 3 |
| `halt` | `need_user_input == True` |
| `render` | No errors, no halt |

**Checkpointer:** `interrupt_after=["evaluate"]` — pauses for human review.

---

## VinGPT Graph (`vingpt_graph`)

```
START → analyze → check_conf ──[questions]──→ human_input → check_conf (loop)
                              └─[clear]──→ render → END
```

| Node | Function | What it does |
|---|---|---|
| `analyze` | `analyze_and_ask` | Calls Gemini with column names + sample values. Returns core_mappings + custom_mappings + structured questions. |
| `check_conf` | `check_confidence` | Scans mapping_confidence. Adds questions for scores < 85. |
| `human_input` | `_human_input_node` | Passthrough — `interrupt_after` pauses here. User answers via `/ingest/chat`. |
| `render` | `_render_vingpt` | Downloads PIM blank templates, writes data into them, sets `generated_files`. |

**Question format** (structured dicts):
```python
{
    "id": "core_sku",
    "type": "core",          # core | custom | missing
    "target": "sku",
    "column": "SKU",
    "confidence": 98,
    "text": "I found 'SKU' — looks like it could be the **sku** field. Is that right?"
}
```

---

## API Endpoints (`api.py`)

| Endpoint | Method | Graph | Purpose |
|---|---|---|---|
| `/ingest/start` | POST | Pipeline | Start ingestion, returns suggestions |
| `/ingest/approve` | POST | Pipeline | Accept corrections, resume to render |
| `/ingest/status` | POST | Both | Check thread state |
| `/ingest/chat` | POST | VinGPT | Submit answers, Gemini parses intent, resumes graph |
| `/vingpt/start` | POST | VinGPT | SSE streaming start with real-time progress events |

### `/ingest/chat` — Intent Parsing

When a user answers questions, Gemini determines intent:
- `approve` — Keep the mapping
- `reject` — Remove the mapping
- `alternative` — Update with user's suggestion

Decisions are applied to `core_mappings` / `custom_mappings` before graph resume.

---

## File Reading — Lazy Generator Pattern

`helpers.py:read_file()` returns a **generator**, not a list. Never materializes the full file in memory.

| Node | Rows read |
|---|---|
| `triage_source` | 20 |
| `map_columns_specialist` | 5 |
| `evaluate_mappings` | 10 |
| `render_agent` / `_render_vingpt` | All (single legitimate pass) |

CSV and xlsx (openpyxl `read_only=True`) stream from disk. xls (xlrd) loads fully — legacy format limitation.

---

## PIM Blank Template Integration

### `download_blank_template(auth_header, template_type)`

| Type | API Endpoint | Body |
|---|---|---|
| `category` | `/api/pie/v1/download/download-template` | `{module:"master", submodule:"product-master"}` |
| `attribute` | `/api/pie/v1/download/download-template` | `{module:"attribute", submodule:"product-attribute"}` |
| `product` | `/api/pdatg/v1/product/generate-master-template` | `{}` |

The function:
1. Calls PIM API with the user's Authorization header
2. Fixes `\u0026` → `&` in the S3 URL
3. Downloads the blank xlsx to `blank-templates/`
4. Returns the local path

**Important:** The product template is dynamic — it depends on which categories and attributes exist in the PIM at the time of generation. The category and attribute templates should be uploaded FIRST before generating the product template (not yet implemented — see Phase 6 in STATE_2.md).

---

## Known Gaps / TODOs

| Gap | Impact | Status |
|---|---|---|
| **Render uses scratch fallback** | `render_product_xlsx` and `render_reference_xlsx` create from scratch, don't use downloaded blanks | Needs implementation |
| **Product template depends on category + attribute upload** | Product template columns are determined by what's in the PIM | Blocked on Phase 6 upload |
| **Nothing processes user answers** | `human_input` node just pauses — answers are parsed in `/ingest/chat` but not persisted beyond that turn | Needs resume → re-evaluate loop |
| **No Selenium blueprint** | PIM has no API for some uploads | Phase 6 |
| **Profile data stores no raw values** | `profiles` in triage is just `[{name, col_index}]` — no unique counts or samples | Intentional, but LLM has less context |
| **Old AgentState has 25+ fields** | Used by pipeline graph — not cleaned up | Low priority |
| **agents.py has legacy code** | Old pipeline functions (`fingerprint_source`, `profile_source`, etc.) coexist with active code | Low priority |
| **No regression tests** | No CI, no test suite | Phase 7 |

---

## Environment Variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GEMINI_API_KEY` | Yes | — | Gemini LLM access |
| `POSTGRES_URI` | No | — | Postgres checkpointer (MemorySaver fallback) |
| `LANGSMITH_API_KEY` | No | — | LangSmith tracing + ContextHub |
| `LANGSMITH_TRACING` | No | — | Set to `true` to enable tracing |
| `LANGSMITH_PROJECT` | No | `pim-ingestion` | LangSmith project name |

---

## How to Run

```bash
# CLI pipeline
python3 main.py "client-data/exports/Eretail_Export_14_Nov.xlsx"

# FastAPI server
uvicorn api:app --reload

# VinGPT SSE start (with JWT for template download)
curl -X POST http://localhost:8000/vingpt/start \
  -H "Authorization: Bearer <jwt>" \
  -H "Content-Type: application/json" \
  -d '{"file_path": "client-data/client_data/apparel_clean_sample.xlsx"}'

# Answer questions
curl -X POST http://localhost:8000/ingest/chat \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "<id>", "answers": {"core_sku": "yes", "core_sku_name": "yes"}}'
```
