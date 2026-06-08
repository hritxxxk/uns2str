# PIM Ingestion Agent — Current State

## Overview

An agentic system that processes messy eCommerce source files (CSV, xlsx, xls) and produces 4 standardized PIM template files through a **conversational 4-phase interactive graph**.

The Pipeline Graph and VinGPT Graph are **deprecated** — all active development is on the **Interactive Graph** (`interactive_graph.py`).

---

## File Structure

```
root/
├── api.py                 # FastAPI server — all active endpoints
├── interactive_graph.py   # ★ Primary: 4-phase interactive onboarding graph
├── interactive_state.py   # InteractiveIngestionState + PhaseOutput schemas
├── agents.py              # Category strategies + heuristic helpers
├── helpers.py             # File I/O, encoding detection, template download
├── state.py               # Legacy AgentState + Pydantic models
├── graph.py               # Legacy Pipeline/VinGPT (commented out)
├── main.py                # Legacy CLI (commented out)
├── learning.py            # LangSmith ContextHub + log_corrections
├── chat.html              # Light-mode chat frontend
├── CURRENT_STATE.md       # ← You are here
├── STATE_1.md             # Legacy reference
├── STATE_2.md             # Phase 5/6 plan
├── STATE_3.md             # Multi-source ZIP consolidation blueprint
├── tools/
│   ├── mapping.py         # build_attribute_definitions, normalize/validate
│   ├── profiling.py       # profile_columns (legacy, partially used)
│   ├── references.py      # extract_reference_values
│   └── rendering.py       # render_*_xlsx (category/attribute/reference/product)
├── blank-templates/       # Downloaded PIM blank templates
├── output/                # Generated xlsx files (by fingerprint)
├── cache/                 # Cached column mappings (by fingerprint)
├── uploads/               # Uploaded files + merged sheets
└── client-data/           # Source data files
```

---

## Interactive Graph (`interactive_graph.py`) — Primary

6 nodes, 4 interrupts, conversational chat interface.

```
START → triage → categories ──[INTERRUPT]──
                  attributes ──[INTERRUPT]──
                  references ──[INTERRUPT]──
                  products   ──[INTERRUPT]──
                  render → END
```

| Node | Function | What it does |
|---|---|---|
| `triage` | `triage_interactive` | Opens file, LLM-based header detection, collects multi-sheet metadata. |
| `categories` | `categories_phase` | 5-strategy fallback (declarative recipe primary) + bypass + merge detection + conversational fallback |
| `attributes` | `attributes_phase` | 2-call: screening (50→500 rows, adaptive) + mapping (3-bucket grouping). Validation + 3 auto-retry |
| `references` | `references_phase` | Programmatic `extract_reference_values` + LLM education + messy value detection + bypass |
| `products` | `products_phase` | Programmatic `build_product_rows` + image URL validation + LLM explanation |
| `render` | `render_interactive` | Generates 4 xlsx templates (blank PIM templates if JWT available) |

### State Schema: `InteractiveIngestionState`

17 fields:

| Field | Type | Purpose |
|---|---|---|
| `messages` | `list` | Chat history |
| `file_path` | `str` | Source file |
| `sheet_name` | `str \| None` | Selected sheet |
| `profile_data` | `dict \| None` | Collapsed file metadata |
| `current_phase` | `str` | categories/attributes/references/products/complete |
| `phases_completed` | `list` | Phases the user has confirmed |
| `categories` | `PhaseOutput` | Output for categories phase |
| `attributes` | `PhaseOutput` | Output for attributes phase |
| `references` | `PhaseOutput` | Output for references phase |
| `products` | `PhaseOutput` | Output for products phase |
| `all_sheets` | `list` | Multi-sheet metadata for merge detection |
| `sheet_merge` | `dict` | Merge detection result + user response |
| `core_mappings` | `dict[str, str]` | SKU/code/mrp → source column |
| `custom_mappings` | `dict[str, str]` | Dynamic attributes preserved as-is |
| `mapping_confidence` | `dict[str, int]` | 0-100 confidence per mapping |
| `generated_files` | `list[str]` | Output xlsx paths |
| `jwt_token` | `str` | Bearer token for PIM API calls |

### PhaseOutput (TypedDict)

| Field | Type | Purpose |
|---|---|---|
| `explanation` | `str` | Glass-clear narrative shown in chat |
| `reasoning` | `str` | Deeper technical rationale |
| `suggestions` | `list[dict]` | Structured items (groups/items) |
| `approved` | `bool` | User confirmed this phase |
| `user_feedback` | `str` | Freeform edits/corrections |

---

## Phase Details

### 1. Categories Phase

**Standard flow:** 5-strategy fallback chain in `agents.py:resolve_category_paths()`:
1. Declarative recipe (primary) — LLM writes JSON config → Python executes on 100% rows
2. Hierarchy sheet — scans other sheets
3. Level columns — CATEGORY1-4 profiles
4. Single column — parses path separators
5. Infer from attributes — LLM fallback

**Bypass flow (ReAct):**
- `parse_category_feedback()` — lightweight LLM call (~200 tokens)
- If `is_direct_override` with `specified_columns` → `build_paths_from_generator()` → `approved=True`
- If `is_merge_approval` → `execute_sheet_merge()` joins two sheets by key column
- If `is_off_topic` → polite redirect, no state change
- If conversational (none of the above matched) → `answer_category_query()` from state

**Self-healing:** `_heal_category_paths()` fuzzy-merges near-duplicates (≥85% token overlap)

**Tree truncation:** >10 paths → first 5 shown, rest in `<details>` block

### 2. Attributes Phase

**Two sequential LLM calls:**

| Call | Prompt | What it does |
|---|---|---|
| 1. Screening | `SCREENING_PROMPT` | Adaptive sampling (50→500 rows, 3 rounds). Detects format (columns/EAV/hybrid). Screens which columns are attributes vs noise |
| 2. Mapping | `MAPPING_PROMPT` | Maps only screened columns into 3 buckets (High-Confidence Core, Custom Preserved, Low-Confidence Ambiguous) with `attribute_type`, `attribute_data_type`, and `attribute_group` |

**Validation + Retry:**
- `_validate_mappings()` — checks type compatibility (int/float/boolean/date regex, ≥20% threshold) + mandatory PIM_DEFAULTS (`sku_name`, `code`, `mrp`)
- 3 auto-retry cycles with error context fed back into LLM prompt

**Caching:** Fingerprint-based (`cache/{sha256[:16]}.json`) — skips both LLM calls on cache hit

**Few-shot learning:** `fetch_similar_examples()` from LangSmith ContextHub injects up to 5 historical corrections

### 3. References Phase

**Standard flow:**
- `extract_reference_values()` — programmatic, reads unique values from column profiles
- LLM call — generates educational explanation + detects messy values (`MED` → `M`)
- Values capped at 20 in suggestions

**Bypass flow:**
- `parse_reference_feedback()` — detects approval or value overrides
- If `keep_values={"SML": "keep as SML"}` → removes from `messy_values` + `normalizations`

### 4. Products Phase

**Standard flow:**
- `build_product_rows()` — programmatic, reads 100% of rows, maps by `core_mappings` + `custom_mappings`
- `extract_image_columns()` — detects image URL columns
- Image URL validation — checks if URLs start with `http`, flags >30% failure rate
- LLM call — explains preview (no data decisions)

**Bypass flow:**
- `parse_product_feedback()` — detects approval or column exclusion
- If `is_approval` → auto-advance to render
- If `exclude_columns` → filters row mappings before building

### 5. Render

Fully programmatic — generates 4 xlsx files:
- `{fingerprint}_category.xlsx` — one path per row
- `{fingerprint}_attribute.xlsx` — 17-column PIM attribute schema
- `{fingerprint}_reference.xlsx` — unique values per dropdown/multiselect master
- `{fingerprint}_product.xlsx` — 6 fixed + N dynamic + 9 image columns

Downloads PIM blank templates via JWT if available; creates from scratch otherwise.

---

## Category Resolution (`agents.py`) — 5-Strategy Fallback

```
1. Declarative Recipe (AI-generated)  ← PRIMARY
   → LLM profiles columns → writes JSON recipe → Python executes on 100% of rows → self-heals

2. Hierarchy Sheet
   → Scans other sheets for explicit hierarchy data

3. Level Columns (CATEGORY1-4)
   → Profiles unique counts, LLM picks hierarchy cols, programmatic path building

4. Single Category Column
   → Finds "Category" column, parses path separators (> /)

5. Inferred from Attributes
   → LLM guesses from column names + one sample row
```

---

## API Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Serves chat.html frontend |
| `/upload` | POST | File upload (1MB chunks, returns server path) |
| `/interactive/start` | POST (SSE) | Start 4-phase onboarding with streaming progress + phase events |
| `/interactive/respond` | POST | Submit user feedback, resume graph, auto-advance through approved phases |
| `/interactive/status` | POST | Check session state |
| `/output/{file}` | GET | Download generated xlsx files |

---

## Auto-Advance Cascade

**SSE handler** (`/interactive/start`):
```python
while phases_run < max_phases:
    graph.stream(initial or None, config)
    phase = state["current_phase"]
    if state[phase]["approved"]:
        phases_run += 1; continue   # silent advance
    yield SSE phase data; break     # pause for user
```

**Respond handler** (`/interactive/respond`):
```python
while auto_advance_count < 4:
    if not is_empty and not is_approved:
        break
    # Phase is empty (uncomputed) or auto-approved → stream next
    advance phase, stream again
```

Both loops are fully generic — check `approved` on whatever phase the graph reports, no hardcoded phase names.

---

## Frontend (`chat.html`)

Single-file light-mode HTML, purple accents, ChatGPT-style interface:

- SSE streaming with animated progress cards
- Background processing indicator for large files (>10000 rows)
- Phase messages rendered as plain text with collapsible "Show sample data" + "Why I chose this"
- Category path truncation at >10 (first 5 + `+N more` toggle)
- Attribute group display: `Heel Height → heel_height [Sizing & Fit]`
- Download links for final xlsx files
- No sidebar, no structured cards, no action buttons — pure chat

---

## Guardrails

| Layer | What it catches | Response |
|---|---|---|
| `parse_*_feedback` | Off-topic chat (jokes, weather, coding) | `is_off_topic=True` → polite redirect, no state change |
| `parse_*_feedback` | PIM intent | `is_direct_override/has_override/is_approval` → bypass + auto-advance |
| `user_feedback=""` after bypass | Prompt leakage prevention | Next phase's prompt doesn't contain previous phase's feedback |
| Cache fingerprint | Duplicate header schemas | Skips LLM entirely, loads previous mappings |
| Auto-retry (3 cycles) | Type validation failures | Re-runs LLM with error context, capped at 3 |

---

## File Reading — Lazy Generator Pattern

`helpers.py:read_file()` returns a **generator**, not a list. Never materializes the full file in memory.

| Enhancement | Status |
|---|---|
| CSV encoding detection | ✅ `charset_normalizer` — tries utf-8-sig, cp1252, latin-1, then detected encoding |
| xlsx (openpyxl `read_only=True`) | ✅ Streams from disk |
| xls (xlrd) | ✅ Loads fully — legacy format limitation |
| `errors="replace"` safety net | ✅ Invalid characters substituted instead of raising `UnicodeDecodeError` |

---

## Multi-Sheet Merge Detection

When a file has ≥2 sheets with common key columns (e.g., `ITEM CODE` appearing in both sheets):

1. LLM detects the relationship and generates a user-facing question
2. Question is prepended to the categories explanation in SSE stream
3. User says "yes merge them" → `execute_sheet_merge()` joins sheets by key, writes to `uploads/merged_*.xlsx`
4. User says "no keep separate" → proceeds with primary sheet only

---

## Image URL Validation

- After `build_product_rows`, scans all `image_1` through `image_9` values
- Flags URLs not starting with `http` (relative paths, internal references)
- If >30% broken → warning appended to products explanation
- Non-blocking — files still generate, user can proceed via approval

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
# 1. Start the server
uvicorn api:app --reload

# 2. Open the frontend
open http://localhost:8000

# 3. Or use curl for SSE streaming
curl -X POST http://localhost:8000/interactive/start \
  -H "Content-Type: application/json" \
  -d '{"file_path": "client-data/client_data/apparel_clean_sample.xlsx"}'

# 4. Submit feedback
curl -X POST http://localhost:8000/interactive/respond \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "<id>", "approved": false, "feedback": "combine CATEGORY1 and CATEGORY2"}'

# 5. Upload a file
curl -X POST http://localhost:8000/upload \
  -F "file=@/path/to/file.xlsx"

# 6. Check session status
curl -X POST http://localhost:8000/interactive/status \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "<id>"}'
```

---

## Legacy Code Status

| Component | Status | Notes |
|---|---|---|
| `graph.py` — Pipeline graph | ❌ Commented out | Replaced by `interactive_graph.py` |
| `graph.py` — VinGPT graph | ❌ Commented out | Replaced by `interactive_graph.py` |
| `main.py` — CLI | ❌ Commented out | Use FastAPI server instead |
| `api.py` — `/ingest/*` endpoints | ❌ Commented out | Use `/interactive/*` instead |
| `api.py` — `/vingpt/start` | ❌ Commented out | Use `/interactive/start` instead |
| `agents.py` — graph node functions | ❌ Dead code | `fingerprint_source`, `profile_source`, `map_columns`, etc. |
| `agents.py` — category strategies | ✅ Active | Used as fallback chain by interactive graph |
| `state.py` — `AgentState` | ❌ Legacy | `InteractiveIngestionState` is the primary state |
| `state.py` — `ColumnMapping` | ✅ Shared | Used by both old and new graphs |

---

## Known Gaps / TODOs

| Gap | Impact | Status |
|---|---|---|
| **Product template uses scratch fallback** | `render_product_xlsx` creates from scratch, doesn't use PIM's product template | Needs Phase 6 |
| **No Selenium blueprint** | PIM has no API for some uploads | Phase 6 |
| **No regression tests** | No CI, no test suite | Phase 7 |
| **ZIP pre-processor** | Cannot handle multi-file ZIP uploads >2GB | STATE_3.md — Tasks 7-10 |
| **Celery async workers** | Long-running tasks block SSE stream | Phase 8 |
