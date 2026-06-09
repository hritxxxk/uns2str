# PIM Ingestion Agent — Current State

## Overview

An agentic system that processes messy eCommerce source files (CSV, xlsx, xls) and produces 4 standardized PIM template files through a **cylic, tool-calling agent** built on LangGraph + Google GenAI.

The Pipeline Graph, VinGPT Graph, and hardcoded 6-node DAG are **deprecated** — all active development is on the **Cyclic Agent Graph** (`interactive_graph.py`).

---

## File Structure

```
root/
├── api.py                   # FastAPI server — SSE streaming via astream_events
├── interactive_graph.py     # ★ Primary: cyclic agent graph (3 nodes, 6 tools)
├── interactive_state.py     # InteractiveIngestionState + PhaseOutput schemas
├── agents.py                # Category strategies + heuristic helpers
├── helpers.py               # File I/O, encoding detection, template download
├── state.py                 # Legacy AgentState + Pydantic models (deprecated)
├── graph.py                 # Legacy Pipeline/VinGPT (commented out)
├── main.py                  # Legacy CLI (commented out)
├── learning.py              # LangSmith ContextHub + log_corrections
├── chat.html                # Light-mode chat frontend
├── CURRENT_STATE.md         # ← You are here
├── STATE_1.md               # Legacy reference
├── STATE_2.md               # Phase 5/6 plan
├── STATE_3.md               # Multi-source ZIP consolidation blueprint
├── tests/
│   └── test_system.py       # 37 integration tests (graph, tools, API, output)
├── tools/
│   ├── mapping.py           # build_attribute_definitions, normalize/validate
│   ├── profiling.py         # profile_columns (used by agent tools)
│   ├── references.py        # extract_reference_values
│   └── rendering.py         # render_*_xlsx (category/attribute/reference/product)
├── blank-templates/         # Downloaded PIM blank templates
├── output/                  # Generated xlsx files (by fingerprint)
├── cache/                   # Cached column mappings (by fingerprint)
├── uploads/                 # Uploaded files + merged sheets
└── client-data/             # Source data files
```

---

## Cyclic Agent Graph — Primary Architecture

3 nodes, conditional routing, same-thread multi-turn conversation.

### Graph Topology

```
                                  START
                                    │
                          [route_start: profile_data?]
                                 /          \
                          (No) /            \ (Yes)
                              ▼              ▼
                           triage          agent (bypass)
                              │              │
                              ▼              │
                           agent ◄───────────┘
                              │
                    [route_agent_action: tool_calls?]
                         /                      \
                  (Yes) /                        \ (No)
                      ▼                            ▼
                execute_tools                    END
                (ToolNode)                    (wait for user)
                      │
                      └──→ agent (loop, max 2 iterations)
```

**Key properties:**
- **Single thread, same thread** — every `/interactive/respond` re-invokes the same LangGraph thread
- **Conditional entry** — `route_start()` checks `profile_data`: triage runs only on turn 1
- **Delta-Only Returns** — every node returns only the fields it changes; `messages` uses `operator.add` reducer
- **2-iteration budget** — `remaining_steps` caps tool calls per turn, reset on each user message
- **No interrupts** — agent routes itself to END when it responds conversationally

### Nodes

| Node | Function | Behavior |
|---|---|---|
| `triage` | `triage_interactive` | Opens file, LLM-based header detection, collects multi-sheet metadata. Returns only deltas (greeting + profile_data + empty phase outputs). Runs once. |
| `agent` | `agent_reason_node` | Calls `ChatGoogleGenerativeAI("gemini-2.5-flash")` with tools bound via `.bind_tools()`. Converts messages → LC format → SystemMessage prepended → LLM invoke → returns `{"messages": [response]}`. Skips if last message is from assistant (no pending user input). |
| `execute_tools` | `ToolNode(agent_tools)` | LangGraph's built-in ToolNode. Executes requested tools via `InjectedState`. Returns ToolMessages appended via `operator.add` reducer. |

### Router: `route_agent_action`

```python
def route_agent_action(state):
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        budget = state.get("remaining_steps", 0)
        if budget > 0:
            state["remaining_steps"] -= 1
            return "execute_tools"
    return END  # conversational response or budget exhausted
```

---

## 6 State-Aware Agent Tools

All tools use `@tool` from `langchain_core.tools` and receive state via `Annotated[dict, InjectedState()]`.

| Tool | LLM Args | Reads from State | Writes to State |
|---|---|---|---|
| `profile_file` | `file_path, sheet_name?` | — | `profile_data`, `sheet_name`, `all_sheets`, `completed_phases += ["triage"]` |
| `extract_categories` | `file_path, sheet_name?, specified_columns?` | `profile_data`, `file_path` | `profile_data.category_hierarchy`, `categories`, `completed_phases += ["categories"]` |
| `map_attributes` | `file_path, sheet_name?, feedback?` | `profile_data`, `file_path` | `core_mappings`, `custom_mappings`, `mapping_confidence`, `attributes`, `completed_phases += ["attributes"]` |
| `extract_references` | *(none)* | `core_mappings`, `custom_mappings`, `profile_data` | `references`, `completed_phases += ["references"]` |
| `build_products` | *(none)* | `core_mappings`, `custom_mappings`, `file_path`, `profile_data` | `products`, `product_rows`, `completed_phases += ["products"]` |
| `render_templates` | *(none)* | all state | `generated_files`, `completed_phases += ["render"]` |

**Path resolution fallback** — tools that receive `file_path` from the LLM resolve bare filenames via:
1. `state["file_path"]` (from checkpoint)
2. `os.path.join("uploads", basename)` prefix

---

## State Schema: `InteractiveIngestionState`

### Fields

| Field | Type | Reducer | Purpose |
|---|---|---|---|
| `messages` | `Annotated[list, operator.add]` | ✅ concatenates | Chat history (HumanMessage, AIMessage, ToolMessage) |
| `file_path` | `str` | — | Source file path |
| `sheet_name` | `str \| None` | — | Selected sheet |
| `profile_data` | `dict \| None` | — | File metadata: headers, row_count, header_row, data_start_row, profiles |
| `completed_phases` | `list[str]` | — | Milestone tracker: `["triage", "categories", ...]` |
| `remaining_steps` | `int` | — | Tool call budget (reset to 2 per turn) |
| `core_mappings` | `dict[str, str]` | — | Core PIM fields: `{target: source_column}` |
| `custom_mappings` | `dict[str, str]` | — | Dynamic attributes: `{source_column: target}` |
| `mapping_confidence` | `dict[str, int]` | — | 0-100 confidence per mapping |
| `product_rows` | `list[dict]` | — | Built product rows for rendering |
| `generated_files` | `list[str]` | — | Output xlsx paths |
| `jwt_token` | `str` | — | PIM API bearer token |
| `all_sheets` | `list` | — | Multi-sheet metadata |
| `sheet_merge` | `dict` | — | Merge detection result |
| `categories` | `PhaseOutput` | — | Phase output (populated by tools) |
| `attributes` | `PhaseOutput` | — | Phase output (populated by tools) |
| `references` | `PhaseOutput` | — | Phase output (populated by tools) |
| `products` | `PhaseOutput` | — | Phase output (populated by tools) |
| `current_phase` | `str` | — | Legacy, maintained for backward compat |
| `phases_completed` | `list` | — | Legacy, maintained for backward compat |

### PhaseOutput (TypedDict)

| Field | Type | Purpose |
|---|---|---|
| `explanation` | `str` | LLM explanation shown in chat |
| `reasoning` | `str` | Technical rationale |
| `suggestions` | `list[dict]` | Structured items for user review |
| `approved` | `bool` | User confirmation |
| `user_feedback` | `str` | Freeform edits/corrections |

---

## Agent System Prompt (Operational Playbook)

~139 tokens. Guides tool selection in strict milestone order, error handling, jargon avoidance, and the 2-call budget.

```
## Milestones (strict order — check completed_phases before acting)
1. triage (auto): file loaded & profiled
2. categories → extract_categories  | 3. attributes → map_attributes
4. references → extract_references  | 5. products → build_products
6. render → render_templates

Do NOT skip ahead. If a milestone isn't in completed_phases, run it next.

## Rules
- Explain before calling a tool. Present results clearly and ask confirmation.
- If a tool returns empty/fails: do NOT retry the same turn.
- No jargon: use "missing values" not "null".
- Off-topic user? Redirect politely.
- Max 2 tool calls per turn.
```

---

## API Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Serves `chat.html` frontend |
| `/upload` | POST | File upload (1MB chunked, returns `{path, filename}`) |
| `/interactive/start` | POST (SSE) | Start session — triage → agent greeting → `complete` |
| `/interactive/respond` | POST (SSE) | Send user message → agent processes → streams events → `complete` |
| `/interactive/status` | POST | Check session state |
| `/output/{file}` | GET | Download generated xlsx files |

### SSE Event Types

| Event | When | Fields |
|---|---|---|
| `tool_start` | Agent calls a tool | `tool`, `input` |
| `tool_end` | Tool completes | `tool`, `output_preview` |
| `progress` | LLM streaming text | `message` (token chunks) |
| `complete` | Turn finished | `thread_id`, `message`, `generated_files` |

### `/interactive/respond` Request

```json
{"thread_id": "uuid", "message": "Find categories using CATEGORY1, CATEGORY2"}
```

No `approved` or `feedback` fields — the agent handles everything conversationally.

---

## Category Resolution (`agents.py`) — 5-Strategy Fallback

```
1. Declarative Recipe (AI-generated)  ← PRIMARY
   → LLM profiles columns → writes JSON recipe → Python executes on 100% rows → self-heals

2. Hierarchy Sheet
   → Scans other sheets for explicit hierarchy data

3. Level Columns (CATEGORY1-4)
   → Profiles unique counts, LLM picks hierarchy cols, programmatic path building

4. Single Category Column
   → Finds "Category" column, parses path separators (> /)

5. Inferred from Attributes
   → LLM guesses from column names + one sample row
```

Called internally by the `extract_categories` tool.

---

## Guardrails

| Layer | Mechanism |
|---|---|
| Off-topic chat | Agent's system prompt: "Off-topic user? Redirect politely." |
| Tool ordering | `completed_phases` list — agent checks before calling next tool |
| Loop budget | `remaining_steps` decremented per tool call, reset to 2 per user turn |
| Cache fingerprint | Attribute mappings cached by header SHA-256 — skips LLM on repeat runs |
| Path resolution | Tools fall back to `state["file_path"]` or `uploads/` prefix when LLM provides bare filenames |
| Profile_data safety | All 9 read sites use `.get("profile_data", {}) or {}` to handle `None` |

---

## File Reading — Lazy Generator Pattern

`helpers.py:read_file()` returns a **generator**, never materializes the full file in memory.

| Enhancement | Status |
|---|---|
| CSV encoding detection | ✅ `charset_normalizer` — tries utf-8-sig, cp1252, latin-1, then detected encoding |
| xlsx (openpyxl `read_only=True`) | ✅ Streams from disk |
| xls (xlrd) | ✅ Loads fully — legacy format limitation |
| `errors="replace"` safety net | ✅ Invalid characters substituted instead of raising `UnicodeDecodeError` |

---

## Output Files

Generated by `render_templates` tool, saved to `output/`:

| File | Schema |
|---|---|
| `{fingerprint}_category.xlsx` | Column: Category Path (one unique path per row, `>` separator) |
| `{fingerprint}_attribute.xlsx` | 17 columns: Attribute Name, Short Name, Display Name, Attribute Type, Attribute Data Type, Constraint, Length, Mandatory, Filter, Editability, Visibility, Searchable, Auto Translate, Attribute Group, Reference Master, Reference Attribute, Status |
| `{fingerprint}_reference.xlsx` | One column per Reference Master (Brand Master, Size Master, etc.) with allowed values |
| `{fingerprint}_product.xlsx` | 6 fixed cols (Category Path, Variant Attributes, Parent SKU, Code, sku_name, mrp) + N dynamic attribute cols + 9 image cols |

---

## Bug Fix History (Recent Session)

| Bug | Root Cause | Fix |
|---|---|---|
| `contents are required` | Skip check missed plain dicts from triage | Added `isinstance(last, dict) and last.get("role") == "assistant"` |
| ToolNode wipes message history | `messages: list` had no reducer → replace behavior | Changed to `messages: Annotated[list, operator.add]` |
| State duplication on re-entry | Triage returned full state including messages | Changed to delta-only return pattern |
| `can only concatenate list to int` | Multi-sheet loop overwrote `header_row` with a list | Renamed loop variable to `sheet_headers` |
| `NoneType has no .get()` | `state.get("profile_data", {})` returns `None` when value is `None` | Added `or {}` fallback on all 9 read sites |
| LLM passes bare filenames | Tools need full path to open files | Path resolution fallback: state path → `uploads/` prefix |

---

## Test Suite

`tests/test_system.py` — 37 tests, run with:

```bash
# Local graph tests (fast, no API key needed for structure tests)
python3 tests/test_system.py --quick

# Full suite against live server
python3 tests/test_system.py --api http://localhost:8000
```

| Group | Tests | Coverage |
|---|---|---|
| Graph Structure | 7 | Nodes, conditional edges, reducer, route_start |
| Tool Unit Tests | 14 | All 6 tools, state mutations, completed_phases |
| API Endpoints | 6 | Health, upload, start SSE, respond SSE, status |
| Output Validation | 6 | Product/attribute xlsx format, column headers |
| Edge Cases | 3 | Bare filename resolution, missing file error handling |

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

# 3. Upload + Start + Respond via curl
UPLOAD=$(curl -s -X POST http://localhost:8000/upload \
  -F "file=@client-data/client_data/apparel_clean_sample.xlsx" | python3 -c "import sys,json; print(json.load(sys.stdin)['path'])")

THREAD=$(curl -s -N -X POST http://localhost:8000/interactive/start \
  -H "Content-Type: application/json" \
  -d "{\"file_path\": \"$UPLOAD\"}" | grep -o '"thread_id":"[^"]*"' | cut -d'"' -f4)

curl -s -N -X POST http://localhost:8000/interactive/respond \
  -H "Content-Type: application/json" \
  -d "{\"thread_id\": \"$THREAD\", \"message\": \"Find categories\"}"
```

---

## Legacy Code Status

| Component | Status | Notes |
|---|---|---|
| `graph.py` — Pipeline graph | ❌ Commented out | Replaced by agent graph |
| `graph.py` — VinGPT graph | ❌ Commented out | Replaced by agent graph |
| `main.py` — CLI | ❌ Commented out | Use FastAPI server |
| `api.py` — `/ingest/*` | ❌ Commented out | Use `/interactive/*` |
| `api.py` — `/vingpt/*` | ❌ Commented out | Use `/interactive/*` |
| `agents.py` — legacy node fns | ❌ Dead code | Replaced by 6 agent tools |
| `agents.py` — category strategies | ✅ Active | Used by `extract_categories` tool |
| `state.py` — `AgentState` | ❌ Legacy | Use `InteractiveIngestionState` |
| `state.py` — `ColumnMapping` | ✅ Shared | Used by render logic |
| `interactive_graph.py` — old phase nodes | ❌ Dead code | `categories_phase`, `attributes_phase`, etc. remain in file but unreachable |

---

## Known Gaps / TODOs

| Gap | Impact | Status |
|---|---|---|
| **Product template uses scratch fallback** | `render_product_xlsx` doesn't use PIM's product template | Phase 6 |
| **No Selenium blueprint** | PIM has no API for some uploads | Phase 6 |
| **No CI pipeline** | Tests must be run manually | Phase 7 |
| **ZIP pre-processor** | Cannot handle multi-file ZIP uploads >2GB | STATE_3.md |
| **Celery async workers** | Long-running tasks block SSE stream | Phase 8 |
| **LLM hallucinates file paths** | Agent passes `"/data/pim.xlsx"` or bare filenames | Tool path resolution mitigates, but not eliminated |
| **chat.html needs update** | Frontend still expects old `approved`/`feedback` API format | Chat.html update |
