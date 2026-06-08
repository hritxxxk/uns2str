# STATE_3.md — Multi-Source ZIP Consolidation Blueprint

## Objective

Ingest a **2GB ZIP file** containing chaotic marketplace exports (Shopify, Magento, WooCommerce, Amazon, Flipkart, Myntra, Meesho) and consolidate them into a unified, deduplicated PIM master list without modifying the existing `interactive_graph.py` flow.

---

## Three Critical Technical Boundaries

### 1. The Memory Boundary (OOM Prevention)

A compressed 2GB ZIP expands to 5–10GB of raw CSV/XLSX text on disk. Loading this into memory at once in our FastAPI process will trigger an immediate **Out-Of-Memory (OOM) crash**, bringing down the entire server.

### 2. The Timeout Boundary (Async Handshake)

Extracting, parsing, aligning, and deduplicating millions of data points takes several minutes. It cannot run inside a standard synchronous HTTP request or a standard fast-response SSE thread.

### 3. The Schema Alignment Boundary

Different platforms use totally different headers for the same attributes (e.g., Shopify uses `Variant SKU` while Flipkart uses `Style Code`). We must align these dynamically before performing any entity resolution or deduplication.

---

## The Non-Disruptive "Pre-Processor" Architecture

We will **not** modify our existing modular `interactive_graph.py` logic. Instead, we introduce a decoupled, asynchronous **Pre-Processor Pipeline** upstream of our graph.

This pre-processor takes the massive multi-file ZIP, standardizes the schemas, performs the deduplication, and outputs **one clean, consolidated master file**. It then hands this single master file directly to our validated `interactive_graph.py` to trigger the standard conversational VinGPT flow.

```
                    Uploaded ZIP Archive (up to 2GB)
                                 │
                                 ▼
                     ┌───────────────────────┐
                     │ Stream-to-Disk Upload │  ◄── (Chunked streaming, OOM safe)
                     └───────────┬───────────┘
                                 │
                                 ▼
                     ┌───────────────────────┐
                     │ Celery Worker / Async │  ◄── (Headless decompression & profiling)
                     └───────────┬───────────┘
                                 │
                                 ▼
                     ┌───────────────────────┐
                     │ LLM Union Planner     │  ◄── (Generates the Consolidation Recipe)
                     └───────────┬───────────┘
                                 │
                                 ▼
                     ┌───────────────────────┐
                     │ Chunked Stream Merger │  ◄── (Lazy evaluation Pandas/Iterators)
                     └───────────┬───────────┘
                                 │
                                 ▼
                    Standardized Master File
                                 │
                                 ▼
                   ┌──────────────────────────┐
                   │  api.py /interactive     │  ◄── (Leverages existing v1 graph)
                   └──────────────────────────┘
```

---

## Current System State (Pre-Processor)

### What exists now

| Component | File | Status |
|---|---|---|
| File upload (small files) | `api.py` — `POST /upload` | ✅ Streams to `uploads/` via `shutil.copyfileobj` |
| File profiling / triage | `interactive_graph.py` — `triage_interactive` | ✅ LLM-based header detection |
| 4-phase interactive graph | `interactive_graph.py` | ✅ Categories → Attributes → References → Products |
| Render | `interactive_graph.py` — `render_interactive` | ✅ 4 xlsx templates |
| SSE streaming | `api.py` — `/interactive/start` | ✅ Progress + phase events |
| Auto-advance cascade | `api.py` — SSE + respond loops | ✅ Phase-name agnostic |
| Off-topic guardrails | `interactive_graph.py` — All 4 phases | ✅ |
| Bypass / ReAct handling | `interactive_graph.py` — All 4 phases | ✅ |
| Image URL validation | `interactive_graph.py` — Products phase | ✅ |
| Encoding detection | `helpers.py` — `read_file` | ✅ `charset_normalizer` |
| Background processing indicator | `api.py` + `chat.html` | ✅ For files >10000 rows |
| Multi-sheet merge | `interactive_graph.py` — Categories phase | ✅ |
| State fields | `interactive_state.py` | ✅ 17 fields |

### What needs to be built

| # | Task | File | Description |
|---|---|---|---|
| 7 | ZIP Unpacker & Profiler | `helpers_zip.py` | Async ZIP extraction to temp dir, returns per-file headers |
| 8 | LLM Union Recipe | `agents.py` | Gemini generates `ConsolidationRecipe` JSON mapping source columns → target PIM attributes |
| 9 | Chunked File Merger | `merger.py` | Reads each file in chunks (5,000 rows), renames columns per recipe, deduplicates on `code`, writes unified CSV |
| 10 | Wire into `api.py` | `api.py` | If file is `.zip`, run pre-processor pipeline, pass unified file to `interactive_graph.py` |

---

## Step-by-Step Technical Execution Path

### Step A: Chunked Streaming Upload (OOM-Safe)

The FastAPI upload endpoint must not read the file bytes in-memory. We use `shutil.copyfileobj` to stream the uploaded binary data block-by-block directly onto server disk.

```python
# api.py
import shutil
from fastapi import UploadFile

async def upload_large_zip(file: UploadFile, target_path: str):
    with open(target_path, "wb") as buffer:
        while chunk := await file.read(1024 * 1024):
            buffer.write(chunk)
```

**Current state:** The existing `POST /upload` endpoint already does this — 1MB chunks, OOM-safe. The ZIP case follows the same pattern.

### Step B: Headless Extraction & Column Profiling

An asynchronous background task (Celery worker or FastAPI `BackgroundTask`) decompresses the ZIP into a sandboxed temp directory. It then lazily reads **only the first 50 rows** of each discovered file to extract headers and sample values, minimizing processing overhead.

**Output:**
```python
{
    "Shopify_Export.csv": {"headers": ["Variant SKU", "Variant Price", ...], "row_count": 12400, "samples": [...]},
    "Flipkart_Export.xlsx": {"headers": ["Style Code", "MRP", ...], "row_count": 8700, "samples": [...]},
    "Amazon_Export.txt": {"headers": ["seller-sku", "price", ...], "row_count": 4500, "samples": [...]},
}
```

### Step C: The Union Recipe (Model-Driven Schema Consolidation)

Pass the headers and sample profiles of all extracted files to `gemini-3.5-flash` in a single pass. The model analyzes the semantic relationships and writes a **Declarative Consolidation Recipe** (JSON configuration):

```json
{
  "target_mappings": {
    "code": {
      "sources": {
        "Shopify_Export.csv": "Variant SKU",
        "Flipkart_Export.xlsx": "Style Code",
        "Amazon_Export.txt": "seller-sku"
      },
      "transformation": "strip_and_uppercase"
    },
    "mrp": {
      "sources": {
        "Shopify_Export.csv": "Variant Price",
        "Flipkart_Export.xlsx": "MRP",
        "Amazon_Export.txt": "price"
      },
      "transformation": "to_float"
    },
    "sku_name": {
      "sources": {
        "Shopify_Export.csv": "Title",
        "Flipkart_Export.xlsx": "Product Name",
        "Amazon_Export.txt": "item-name"
      },
      "transformation": "strip_html"
    }
  }
}
```

### Step D: Chunked Stream Merger (Lazy Evaluation)

Using the AI-generated recipe, the Python engine reads each massive source file **in chunks** (e.g., using `pd.read_csv(chunksize=10000)` or custom sheet generator iterators), renames the columns to match our target standard attributes, and writes them incrementally to a single, unified CSV file on disk. This step uses negligible memory, regardless of the file size.

```python
# merger.py
def merge_sources(recipe: dict, source_dir: str, output_path: str):
    with open(output_path, "w", newline="") as out_f:
        writer = csv.writer(out_f)
        writer.writerow(["code", "mrp", "sku_name", ...])  # unified header
        
        for filename, mapping in recipe["target_mappings"].items():
            file_path = os.path.join(source_dir, filename)
            for chunk in read_chunks(file_path, chunk_size=5000):
                for row in chunk:
                    mapped = {}
                    for target, src_info in mapping.items():
                        col_name = src_info["sources"].get(filename)
                        mapped[target] = transform(row[col_name], src_info["transformation"])
                    writer.writerow([mapped.get(h, "") for h in unified_headers])
```

### Step E: SKU Deduplication (Entity Resolution)

To handle duplication of SKUs across marketplaces:

1.  **Exact match:** Run a fast, deterministic hash indexing pass on our standardized `code` column to identify exact duplicate items. Keep the first occurrence, discard subsequent ones.
2.  **Fuzzy match:** For near-duplicate SKUs (e.g., `"ASICS-101-BLU"` and `"ASICS-101-BLUE"`), run a localized Jaro-Winkler string similarity check on the title.
3.  **Golden Record:** If any high-probability duplicates are found, they are merged into a single "Golden Record" row, preserving the highest-quality metadata available across all sources.

---

## The VinGPT User Journey (How it Looks to the Client)

We completely shield the non-technical user from the underlying database complexity, keeping the dialogue simple and conversational:

> **VinGPT:**
> *"Hi! I've received your ASICS archive. I detected 4 marketplace files inside: Shopify, Amazon, Myntra, and Flipkart exports (totaling 2,226 product rows).*
>
> *I have mapped their schemas into a unified format. For example, I matched Amazon's 'seller-sku' and Flipkart's 'Style Code' directly to our standard 'code' identifier.*
>
> *During this process, I detected that 'Gel-Nimbus 25' on Amazon and 'Gel-Nimbus_25' on Myntra are actually the same product. I have consolidated them to prevent duplicate products in your PIM.*
>
> *I'm ready to proceed with your taxonomy categorization. Shall we start? (Yes/No)"*

---

## Tasks for Implementation

- [ ] **Task 7:** Write the ZIP Unpacker & Profiler Helper (`helpers_zip.py`)
  - Async file-unzipping using Python's standard `zipfile` module
  - Decompress to temp directory
  - Return dict of extracted filenames + detected column headers

- [ ] **Task 8:** Implement the `build_union_recipe` LLM Node (`agents.py`)
  - Focused LLM prompt where `gemini-3.5-flash` analyzes mapped sheet headers
  - Returns structured JSON `ConsolidationRecipe`

- [ ] **Task 9:** Build the Chunked File Merger (`merger.py`)
  - Read each extracted CSV/XLSX file in chunks of 5,000 rows
  - Rename columns per `ConsolidationRecipe`
  - Drop exact duplicates across the `code` column
  - Write sequentially into a single `master_onboarding_file.csv`

- [ ] **Task 10:** Wire the Pre-Processor into `api.py`
  - In `POST /upload`, if file ends with `.zip`, call ZIP unpacker → run merger
  - Save resulting unified file to `uploads/`
  - Pass this clean single master file path to `interactive_graph.py` via SSE start
