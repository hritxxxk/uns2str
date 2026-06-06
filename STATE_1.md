# STATE_1 — PIM Ingestion Agent

## Session Context

This document captures the project state at the end of Phase 1 (Agentic Pipeline).The system is an agentic LangGraph pipeline that takes messy eCommerce source files (CSV, xlsx, old xls) and produces 4 standardized PIM template files ready for upload.

The agent uses `create_react_agent` with one tool (`profile_file`). It profiles the source, maps columns to PIM attributes via LLM, returns structured `IngestionOutput`. Deterministic post-processing builds attribute definitions, extracts reference values, and renders xlsx files.

Next session starts here. Below is every code location needed for the upcoming phases.

## 1. What This Project Does

Given a messy source file (CSV, xlsx, old xls) from any eCommerce platform (Shopify, Magento, WooCommerce, Flipkart, Amazon, custom ERP), this system produces 4 standardized PIM template files ready for upload.

The system uses an LLM (Gemini 2.5 Flash Lite) to understand the source file's structure and map columns to PIM attributes. LangGraph orchestrates the agent loop. Deterministic code handles template generation.

---

## 2. Quick Start

```bash
# Set API key
echo 'GEMINI_API_KEY="your-key-here"' > .env

# Run on a file
python3 main.py "client-data/exports/Eretail_Export_14_Nov.xlsx"

# With specific sheet
python3 main.py "client-data/exports/Eretail_Export_14_Nov.xlsx" "SKU Export"
```

Output goes to `output/{fingerprint}_*.xlsx` (4 files: category, attribute, reference, product).

---

## 3. File Structure

```
pim_agent/
├── main.py              # CLI entry point — builds state, invokes graph, post-processes
├── graph.py             # LangGraph agent — create_react_agent with profile_file tool
├── state.py             # All Pydantic models + AgentState schema
├── helpers.py           # Pure utility functions (file I/O, caching, product row builder)
├── agents.py            # Category resolution strategies + old pipeline nodes (legacy/fallback)
│
├── tools/
│   ├── profiling.py     # profile_file tool — reads file, detects header, profiles columns
│   ├── mapping.py       # build_attribute_definitions — converts mapping to 17-col attribute list
│   ├── references.py    # extract_reference_values — unique values for dropdowns
│   └── rendering.py     # render_all_templates — generates 4 xlsx files
│
├── blank-templates/     # PIM template files (Category_template.xlsx)
├── cache/               # Cached column mappings per file fingerprint
└── output/              # Generated xlsx files
```

---

## 4. Architecture — Agentic Flow

```
User runs: python3 main.py file.xlsx
                │
                ▼
    ┌─────────────────────────────────────┐
    │         main.py                     │
    │                                     │
    │  1. Build initial AgentState        │
    │  2. graph.invoke(state)             │
    │  3. Extract structured_response     │
    │  4. Post-process (deterministic)    │
    └─────────────────────────────────────┘
                │
                ▼
    ┌──────────────────────────────────────────────────────┐
    │              AGENT LOOP (create_react_agent)         │
    │                                                      │
    │  ┌──────────┐    ┌──────────────┐    ┌───────────┐  │
    │  │  LLM     │───→│ profile_file │───→│   LLM     │  │
    │  │ decides  │    │   (tool)     │    │  returns  │  │
    │  │ to call  │    │              │    │Ingestion  │  │
    │  │ profile  │    │1. Detect     │    │ Output    │  │
    │  │          │    │   sheet      │    │           │  │
    │  │          │    │2. Detect     │    │mapping[]  │  │
    │  │          │    │   header row │    │profiles[] │  │
    │  │          │    │3. Profile    │    │categories │  │
    │  │          │    │   columns    │    │header_row │  │
    │  │          │    │4. Detect     │    │data_start │  │
    │  │          │    │   categories │    └───────────┘  │
    │  └──────────┘    └──────────────┘                   │
    └──────────────────────────────────────────────────────┘
                │
                ▼
    ┌──────────────────────────────────────────────────────┐
    │          POST-PROCESS (Deterministic)                │
    │                                                      │
    │  mapping[] ──→ build_attribute_definitions()         │
    │                  → 17-column attribute master list    │
    │                                                      │
    │  mapping[] + profiles[] ──→ extract_reference_values()│
    │                  → dropdown value lists               │
    │                                                      │
    │  source file + mapping ──→ build_product_rows()      │
    │                  → product data dicts                 │
    │                                                      │
    │  all data ──→ render_all_templates()                 │
    │                  → 4 xlsx files                      │
    └──────────────────────────────────────────────────────┘
                │
                ▼
    ┌──────────────────────────────────────┐
    │  OUTPUT: 4 xlsx files               │
    │  - {fp}_category.xlsx               │
    │  - {fp}_attribute.xlsx              │
    │  - {fp}_reference.xlsx              │
    │  - {fp}_product.xlsx                │
    └──────────────────────────────────────┘
```

---

## 5. Key Files Explained

### `graph.py` — The Agent

Uses `create_react_agent` from LangGraph (v1.2.4). The agent has one tool: `profile_file`. The system prompt tells it to:
1. Call `profile_file` to understand the file
2. Map columns to PIM attributes itself
3. Return structured `IngestionOutput` with mapping data

`response_format=IngestionOutput` forces the agent to return structured JSON (not free-form conversation).

```python
graph = create_react_agent(
    model, 
    tools=[profile_file], 
    prompt=SYSTEM_PROMPT, 
    response_format=IngestionOutput, 
    state_schema=AgentState
)
```

### `tools/profiling.py` — The `profile_file` Tool

This is the only tool the agent calls. It does everything needed to understand the source file:

1. **Sheet detection** (`detect_data_sheet`): Scans all sheets, picks the one with `cols × rows` biggest
2. **Header detection** (LLM call): Reads first 15 rows, asks LLM which row is the header and where data starts. Handles files with metadata rows (MS files have row 0 = numbers, row 1 = headers, rows 2-4 = metadata)
3. **Column profiling** (`profile_columns`): For each column: non-null count, unique count, sample values, unique values (if ≤100)
4. **Category detection** (`detect_category_structure`): Finds non-data sheets that might have category hierarchy data
5. **Metadata extraction**: Rows above the header (data type hints, constraints, descriptions)

Returns a single dict with everything: `{fingerprint, sheet_name, header_row, data_start_row, headers, profiles, row_count, sample_rows, metadata, category_candidates, cached_mapping_exists}`

### `tools/mapping.py` — Build Attribute Definitions

Takes the agent's column mappings and converts them to the 17-column PIM attribute format.

Key rules:
- `constraint=True` for Dropdown, MultiSelect, MultiSelectDropdown, MultiTextBox
- Length defaults: RichText=65536, Textarea=16384, image=2048, else 255
- Reference master = `{target_attribute} Master` if constrained
- Reference attribute = `{target_attribute}` if constrained

### `tools/references.py` — Extract Reference Values

For every Dropdown/MultiSelect attribute, reads unique values directly from the column profiles (no extra file scan). Returns `{"Brand Master": ["Nike", "Adidas"], ...}`.

### `tools/rendering.py` — Generate XLSX

Contains `render_all_templates` which calls individual renderers:
- `render_category_xlsx`: One column, paths with `>` separator
- `render_attribute_xlsx`: 17-column header + data rows
- `render_reference_xlsx`: One column per master (not stacked)
- `render_product_xlsx`: 6 fixed cols + dynamic attribute cols + 9 image url cols

### `agents.py` — Category Resolution & Legacy

Contains category resolution strategies (fallback chain):
1. Hierarchy sheet — separate sheet with category columns
2. Level columns — CATEGORY1-4, L1-L4 in data sheet (sends column profiles to LLM)
3. Single column — one "Categories" column (LLM determines separators)
4. Infer from attributes — DIVISION, GENDER, COMMODITY, etc. (sends column profiles to LLM)
5. User input — sets `need_user_input=True` when all fail

Each strategy is validated by an LLM that checks for duplicate levels and garbage data.

Also contains the old pipeline node functions (fingerprint_source, profile_source, map_columns, etc.) — no longer used by the main flow but kept for reference.

### `helpers.py` — Pure Utilities

- `read_file(path, sheet_name)`: Tries openpyxl first (for .xlsx), falls back to xlrd (for old .xls format with macros)
- `fingerprint_headers(headers)`: SHA-256 hash of sorted column names → 16-char cache key
- `load/save_cached_mapping`: JSON file cache in `cache/` directory
- `get_headers_and_data(rows, header_row)`: Splits rows into headers + data at given index
- `build_product_rows(headers, data, mapping, image_cols)`: Transforms source data to product dicts. Detects which columns are code/name/mrp/category by name matching
- `extract_image_columns(headers)`: Finds columns with "image", "img", "picture", "photo" in name

### `state.py` — All Models

**Pydantic models for structured data:**
- `ColumnMapping`: One source→target mapping (source_column, target_attribute, attribute_type, data_type, constraint, length, mandatory, group, confidence)
- `MappingResponse`: {mappings: [ColumnMapping]} — used by structured LLM calls
- `IngestionOutput`: Agent's final output (status, mapping[], profiles[], category_hierarchy[], header_row, etc.)
- `AgentState(MessagesState)`: Full state schema for the LangGraph agent

**Constants:**
- `PIM_DEFAULTS = ["sku_name", "code", "description", "mrp", "brand"]` — attributes the PIM already has, should not be recreated

---

## 6. Supported File Formats

| Format | Library | Notes |
|---|---|---|
| `.csv` | Python csv module | UTF-8 BOM handled |
| `.xlsx` | openpyxl | Modern Excel |
| `.xls` (old) | xlrd (fallback) | Supports macros, cell colors, old format |

---

## 7. Output Templates

| Template | Columns | Description |
|---|---|---|
| **Category** | Category Path | One column, `>` separator between levels |
| **Attribute** | 17 columns | Attribute Name, Short Name, Display Name, Attribute Type, Attribute Data Type, Constraint, Length, Mandatory, Filter, Editability, Visibility, Searchable, Auto Translate, Attribute Group, Reference Master, Reference Attribute, Status |
| **Reference** | One per master | Column header = master name, values below |
| **Product** | 6 fixed + N dynamic + 9 image | Fixed: Category Path, Variant Attributes, Parent SKU, Code, sku_name, mrp. Dynamic: one column per attribute. Image: image_1 through image_9 |

Supported attribute types: Textbox, Dropdown, RichText, Textarea, MultiSelect, MultiSelectDropdown, MultiTextBox, Date, Time.

Supported data types: varchar, varchar[], int, float, boolean, date.

---

## 8. Tested Files

| File | Source | Cols | Rows | Status |
|---|---|---|---|---|
| `apparel_clean_sample.xlsx` | ASICS/retail | 51 | 10 | ✅ |
| `Eretail_Export_14_Nov.xlsx` | ERP/eRetail | 101 | 2,226 | ✅ All 4 templates |
| `MS_Product_Bottoms_20251009.xlsx` | Marks & Spencer | 407 | 3,197 | ✅ |
| `wc-product-export-13-11-2025.xlsx` | WooCommerce | 447 | 3,427 | ✅ 401 category paths |
| `gamepad.xlsx` (old .xls) | Flipkart template | 51 | 141 | ✅ xlrd fallback |

---

## 9. Key Design Decisions

**Why create_react_agent instead of a custom graph?**
The agent pattern lets the LLM decide the flow. If profiling fails, the agent can retry. If it needs more info, it can ask. A fixed DAG would break on unexpected file formats.

**Why only one tool (profile_file)?**
The mapping is done by the LLM itself — it's the most intelligent step. Building attributes and extracting references are deterministic and don't need agent orchestration. Keeping only one tool simplifies the agent loop.

**Why keyword lists were removed?**
Every keyword list was brittle — it would miss some format and cause false positives on others. Now the LLM receives actual column profiles (unique counts, sample values) and decides semantically. Zero hardcoded platform detection.

**Why two-pass mapping was replaced?**
The agent handles this internally. It maps confident columns first, then fills gaps with generic mappings. No need for separate pass1/pass2 logic.

**Why reference values come from profiles?**
`profile_columns` already scans every cell. Unique values are collected during profiling. `extract_reference_values` reads from the profile — zero extra file I/O.

---

## 10. Known Gaps

| Gap | Impact | Notes |
|---|---|---|
| **Categories in separate file** | ASICS has categories in a separate Master Template file | Pipeline processes one file at a time |
| **Multi-file ingestion** | Client may have products, prices, images in different files | Future feature |
| **Cache TTL/eviction** | Cache grows unbounded in `cache/` directory | Low priority |
| **Product fixed column detection** | Code/name/mrp use keyword lookup — misses "Seller SKU ID" | Should use LLM-mapped attributes instead |
| **Human-in-the-loop API** | Agent sets `needs_human_input=True` but no API to receive input | Needs FastAPI layer |
| **xlrd cell colors** | xlrd can read cell background colors but we don't extract them | Marketplace templates use color for validation status |

---

## 11. Dependencies

```
langgraph>=1.2.4
langchain-core>=1.4.0
langchain-google-genai>=4.2.4
google-genai>=2.8.0
openpyxl>=3.1.0
xlrd>=2.0.0
pandas>=2.0.0
python-dotenv>=1.0.0
pydantic>=2.0.0
```

---

---

## Phase 2 — State & Memory Refactoring (DeltaChannel)

**Goal:** Apply `DeltaChannel` reducer to state memory to prevent storage bloat and enable lightweight checkpointing on large datasets.

### What to change

| File | Location | Current | Target |
|---|---|---|---|
| `state.py` | `class AgentState(MessagesState)` | Plain TypedDict — all fields stored on every checkpoint | `messages: Annotated[list, DeltaChannel(add_messages, snapshot_frequency=100)]` to store only deltas |
| `state.py` | `mapping: list[ColumnMapping]` | Full mapping in every checkpoint | Consider `DeltaChannel` or split into separate keys |
| `graph.py` | `create_react_agent(...)` | Uses default `MemorySaver` | Switch to `PostgresSaver` or `SqliteSaver` |

### Key import

```python
from langgraph.channels.delta import DeltaChannel
from langgraph.graph.message import add_messages
```

---

## Phase 3 — State Machine Deconstruction

**Goal:** Replace `create_react_agent` with custom `StateGraph` that separates profiling, mapping, validation, and self-correction.

### What to change

| File | Location | Current | Target |
|---|---|---|---|
| `graph.py` | Lines 40-47 | `create_react_agent(model, tools=[profile_file], prompt=..., response_format=IngestionOutput, state_schema=AgentState)` | Custom `StateGraph(AgentState)` with nodes: `step_profile` → `step_map` → `step_validate` → `step_correct` (loop back) |
| `agents.py` | Functions `fingerprint_source`, `profile_source`, `map_columns`, `build_attributes`, `collect_references` | Legacy — unused by current flow | Reuse as templates for new node functions |
| `graph.py` | Conditional edge | Always runs once | Add retry logic: if validation fails, loop back to `step_map` (max 3 retries) |

### Test files

- `apparel_clean_sample.xlsx` (fast iteration)
- `Eretail_Export_14_Nov.xlsx` (medium)
- `wc-product-export-13-11-2025.xlsx` (large)

---

## Phase 4 — HITL & API Integration

**Goal:** FastAPI service with PostgreSQL checkpointer. Pause, review, modify, resume.

### What to build

| File | Status | What it does |
|---|---|---|
| `api.py` | **New** | `POST /api/ingest`, `GET /api/runs/{id}`, `GET /api/runs/{id}/files/{name}`, `POST /api/runs/{id}/resume` |
| `main.py` | Modify | Extract `run_agent(path, thread_id)` for background use |
| `graph.py` | Modify | Add `checkpointer=PostgresSaver(...)` to `create_react_agent` |
| `requirements.txt` | Add | `fastapi`, `uvicorn`, `psycopg2-binary` or `asyncpg` |

### Key imports

```python
from langgraph.types import Command, interrupt
from langgraph.checkpoint.postgres import PostgresSaver
```

---

## Phase 5 — Post-Processing Refactoring

**Goal:** Replace keyword matchers in `helpers.py` with LLM-resolved mappings.

### What to change

| File | Lines | Current (brittle) | Target (use mapping) |
|---|---|---|---|
| `helpers.py` — `build_product_rows()` | code_col lookup | `h.lower() in ("code", "item code", "sku")` | Read `target_attribute == "code"` from `output.mapping` |
| `helpers.py` — `build_product_rows()` | name_col lookup | `h.lower() in ("product name", "item name", ...)` | Read `target_attribute == "sku_name"` from mapping |
| `helpers.py` — `build_product_rows()` | mrp_col lookup | `h.lower() in ("mrp", "price", ...)` | Read `target_attribute == "mrp"` from mapping |
| `helpers.py` — `extract_image_columns()` | Keyword: `"image", "img", ...` | Match by name | Read `attribute_type == "image"` or column mapped to image from mapping |
| `main.py` | Lines 50-65 | Re-reads file, re-builds rows | Pass profile data from `IngestionOutput` directly |

---

## Phase 6 — Continuous Learning (LangSmith ContextHub)

**Goal:** Register corrections as reference examples. Retrieve them on future runs.

### What to build

| File | What it does |
|---|---|
| `learning.py` (new) | On success: save `fingerprint → mapping` to ContextHub. On run: check ContextHub for similar fingerprints, inject as few-shot examples |
| `tools/profiling.py` — `profile_file()` | Before LLM call, check ContextHub for matching fingerprint |
| `.env` | Add `LANGSMITH_API_KEY` |

### Key import

```python
from langsmith import Client
```

---

## Phase 7 — Verification & Testing

**Goal:** Regression tests on all client files. Monitor in LangSmith.

### What to build

| File | What it tests |
|---|---|
| `tests/test_regression.py` | Run on all 6 test files. Verify output files exist |
| `tests/test_attribute_checks.py` | PIM sanity rules: no special chars, constraint→reference, valid types |
| `tests/test_category_strategies.py` | Each strategy independently on known files |

### PIM sanity checklist (from PIM error enum)

- `BlankString`: no empty/whitespace attribute names
- `SpecialCharacter`: no invalid chars in names
- `TypeError`: data_type compatible with attribute_type
- `LengthInvalid`: no negative lengths
- `AttrGroupMand`: every attribute has a group
- `RefAttrNot`: constraint=true → reference_master + reference_attribute exist

The project has 17 commits. Key milestones:

1. Initial LangGraph StateGraph with 6 nodes (deterministic pipeline)
2. LLM-based header detection (MS file format support)
3. xlrd fallback for old .xls files
4. Category fallback chain with validation (4 strategies + user input)
5. Two-pass LLM mapping (structured output + review pass)
6. Removed all keyword lists — LLM uses column stats
7. Agentic pipeline with create_react_agent (current)
