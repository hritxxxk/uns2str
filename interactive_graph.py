"""Interactive 4-phase VinGPT onboarding graph.

Each phase (categories → attributes → references → products) runs its own
LLM node, populates a structured PhaseOutput with explanations + suggestions,
then interrupts via interrupt_after to await user feedback.

The API layer advances current_phase before resuming, so the route_by_phase
conditional edge sends the graph to the correct next node.
"""

import json
import os
import logging

from dotenv import load_dotenv
from google import genai as google_genai
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from interactive_state import InteractiveIngestionState, PhaseOutput, IngestionPhase
from helpers import (
    read_file, take_rows, fingerprint_headers, extract_image_columns,
    build_product_rows, download_blank_template,
)
from tools.mapping import build_attribute_definitions
from tools.references import extract_reference_values
from tools.rendering import render_all_templates

load_dotenv()
logger = logging.getLogger("pim_interactive")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)

api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
client = google_genai.Client(api_key=api_key)


# ─── Helpers ──────────────────────────────────────────────────────

def _llm_json(prompt: str, temperature: float = 1.0) -> dict:
    """Call Gemini with JSON mode and return parsed dict."""
    resp = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "temperature": temperature,
        },
    )
    try:
        return json.loads(resp.text)
    except json.JSONDecodeError:
        logger.warning(f"LLM JSON parse failed, raw:\n{resp.text[:500]}")
        return {}


def _make_empty_phase() -> PhaseOutput:
    return PhaseOutput(
        explanation="",
        reasoning="",
        suggestions=[],
        approved=False,
        user_feedback="",
    )


# ─── Triage Node ─────────────────────────────────────────────────

def triage_interactive(state: InteractiveIngestionState) -> dict:
    """Open file, detect sheets/headers, seed profile_data and greeting."""
    path = state["file_path"]
    ext = os.path.splitext(path)[1].lower()
    sheet_name = state.get("sheet_name")

    sheets = []
    sheet_count = 0
    first_rows = []
    total_row_count = 0

    if ext == ".csv":
        sheet_count = 1
        gen = read_file(path)
        first_rows = take_rows(gen, 20)
        total_row_count = 1 + sum(1 for _ in gen)
    elif ext in (".xlsx", ".xls"):
        import openpyxl
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            sheets = wb.sheetnames
            sheet_count = len(sheets)
            if sheet_name and sheet_name in sheets:
                ws = wb[sheet_name]
            else:
                sheet_name = sheets[0]
                ws = wb[sheet_name]
            total_row_count = ws.max_row or 0
            wb.close()
            gen = read_file(path, sheet_name)
            first_rows = take_rows(gen, 20)
        except Exception:
            import xlrd
            xl = xlrd.open_workbook(path)
            sheets = xl.sheet_names()
            sheet_count = len(sheets)
            if not sheet_name or sheet_name not in sheets:
                sheet_name = sheets[0]
            ws = xl.sheet_by_index(sheets.index(sheet_name))
            total_row_count = ws.nrows
            gen = read_file(path, sheet_name)
            first_rows = take_rows(gen, 20)
    else:
        gen = read_file(path, sheet_name or None)
        first_rows = take_rows(gen, 20)
        sheet_count = 1
        total_row_count = 1 + sum(1 for _ in gen)

    # Detect header row
    header_row = 0
    for i, row in enumerate(first_rows):
        cleaned = [str(c).strip() for c in row if c is not None and str(c).strip()]
        if cleaned:
            header_row = i
            break

    headers = [str(c) if c is not None else "" for c in first_rows[header_row]]
    data_start_row = header_row + 1
    row_count = total_row_count - data_start_row
    if row_count < 0:
        row_count = 0
    column_count = len(headers)

    state["sheet_name"] = sheet_name
    state["profile_data"] = {
        "headers": headers,
        "sample_rows": [],
        "row_count": row_count,
        "column_count": column_count,
        "header_row": header_row,
        "data_start_row": data_start_row,
    }
    state["phases_completed"] = []
    state["current_phase"] = "categories"
    state["categories"] = _make_empty_phase()
    state["attributes"] = _make_empty_phase()
    state["references"] = _make_empty_phase()
    state["products"] = _make_empty_phase()
    state["core_mappings"] = {}
    state["custom_mappings"] = {}
    state["mapping_confidence"] = {}
    state["generated_files"] = []

    # Build a greeting message
    greeting = (
        f"👋 I've loaded **{os.path.basename(path)}** "
        f"({row_count} rows, {column_count} columns on sheet "
        f"'{sheet_name}').\n\n"
        f"Here's the plan — we'll work through **4 steps** together:\n\n"
        f"1️⃣ **Categories** — I'll discover your product hierarchy\n"
        f"2️⃣ **Attributes** — I'll map your columns to PIM fields\n"
        f"3️⃣ **Reference Masters** — I'll extract dropdown values\n"
        f"4️⃣ **Products** — I'll compile the final template\n\n"
        f"Ready to start with **Categories**?"
    )
    state.setdefault("messages", []).append({
        "role": "assistant", "content": greeting,
    })

    logger.info(f"triage | file={path} | rows={row_count} | cols={column_count}")
    return state


# ─── Categories Phase Node ───────────────────────────────────────

# ─── Categories Phase Node ───────────────────────────────────────

# The actual category logic lives in agents.py (resolve_category_paths).
# This node delegates to it, then wraps the result in a PhaseOutput.


def categories_phase(state: InteractiveIngestionState) -> dict:
    """Resolve category taxonomy via declarative recipe strategy from agents.py.

    Uses the new _strategy_declarative_recipe (primary) which:
    1. Profiles columns with unique counts
    2. Asks LLM to write a declarative parsing recipe
    3. Executes the recipe on 100% of rows (deterministic)
    4. Self-heals near-duplicates

    Falls back to the existing 4-strategy chain if the recipe approach
    doesn't yield valid paths.
    """
    profile = state.get("profile_data", {})
    headers = profile.get("headers", [])
    feedback = state.get("categories", {}).get("user_feedback", "")

    # Build a temporary state dict for agents.resolve_category_paths
    # It expects: source_path, sheet_name, headers, header_row, data_start_row
    cat_state = {
        "source_path": state["file_path"],
        "sheet_name": state.get("sheet_name"),
        "headers": headers,
        "header_row": profile.get("header_row", 0),
        "data_start_row": profile.get("data_start_row", 1),
        "category_candidates": [],
        "category_path_config": {},
        "category_hierarchy": [],
        "need_user_input": False,
        "is_known_schema": False,
        "mapping": [],
        "sample_rows": [],
        "row_count": profile.get("row_count", 0),
    }

    from agents import resolve_category_paths
    resolve_category_paths(cat_state)

    paths = cat_state.get("category_hierarchy", [])
    explanation = cat_state.get("category_reasoning", "")
    needs_input = cat_state.get("need_user_input", False)

    # Build suggestions in the format the frontend expects
    suggestions = [
        {
            "type": "item",
            "label": p,
            "confidence": 95,
            "reasoning": "Part of the product category hierarchy",
        }
        for p in paths
    ]

    if not explanation:
        if paths:
            explanation = (
                f"I discovered **{len(paths)}** category paths from your data. "
                f"The hierarchy was built by analysing columns that form parent→child relationships."
            )
        else:
            explanation = (
                "I wasn't able to automatically detect a clear category hierarchy. "
                "Could you describe how your products are categorised? "
                "For example: *'We sell Footwear > Shoes > Sneakers'*"
            )

    reasoning = (
        f"Strategy used: declarative recipe execution on {len(paths)} paths. "
        f"Empty levels collapsed, near-duplicates merged."
    )

    state["categories"] = PhaseOutput(
        explanation=explanation,
        reasoning=reasoning,
        suggestions=suggestions,
        approved=False,
        user_feedback=feedback,
    )

    state["profile_data"]["category_hierarchy"] = paths
    state["profile_data"]["category_candidates"] = suggestions

    if needs_input:
        msg = (
            f"📂 **Category Discovery**\n\n{explanation}\n\n"
            f"Could you tell me what categories you use? "
            f"Type something like: *'We sell Mens > Shoes and Womens > Dresses'*"
        )
    else:
        msg = (
            f"📂 **Category Discovery**\n\n{explanation}\n\n"
            f"I found **{len(paths)}** category paths. Do these look right?\n\n"
            f"If something's off, just tell me — e.g. *\"Remove that path\"* "
            f"or *\"These don't match my hierarchy\"*."
        )
    state.setdefault("messages", []).append({"role": "assistant", "content": msg})

    logger.info(f"categories | paths={len(paths)} | need_input={needs_input}")
    return state


# ─── Attributes Phase Node ───────────────────────────────────────

ATTRIBUTES_PROMPT = """You are a VinAI PIM onboarding assistant. The user has confirmed their category hierarchy.

Current phase: Attribute Mapping

File: {filename}
Headers: {headers}
Sample data (first 3 rows):
{samples}

The user's feedback from the previous attempt (if any):
{feedback}

Map each source column to a PIM attribute. Group your results into three buckets:

1. **High-Confidence Core Mappings** — system-critical fields (sku_name, code, mrp)
   that are clearly identified. Use simple language — e.g. "The 'Price' column has
   decimals like 49.99, so I'll store it as a standard decimal number."

2. **Custom Dynamic Attributes** — proprietary columns (e.g. "Heel Height",
   "Upper Material") that should be preserved as-is with their source name.

3. **Low-Confidence / Ambiguous Fields** — columns where you're < 80% sure.
   Explain WHY you're unsure and offer alternatives.

PIM defaults (map TO these, don't recreate): sku_name, code, mrp

attribute_type rules:
- Brand, colour, size, gender, season, type, category → Dropdown (constraint=true)
- Description/notes → RichText (length=65536)
- Codes, names, numbers, prices → Textbox
- Multi-value tags/features → MultiSelect (constraint=true)
- Date fields → Date
- Image URLs → Textbox, length=2048

Return JSON:
{{
  "explanation": "A plain-English summary of what you found — avoid jargon like 'float64' or 'nullable'.",
  "reasoning": "Technical breakdown for users who want details.",
  "suggestions": [
    {{
      "type": "group",
      "label": "High-Confidence Core Mappings",
      "items": [
        {{
          "type": "item",
          "column": "Product Name",
          "mapped_to": "sku_name",
          "confidence": 100,
          "reasoning": "Standard product title field"
        }}
      ]
    }},
    {{
      "type": "group",
      "label": "Custom Dynamic Attributes",
      "items": [...]
    }},
    {{
      "type": "group",
      "label": "Low-Confidence / Needs Review",
      "items": [
        {{
          "type": "item",
          "column": "Manufacturer Tag",
          "mapped_to": null,
          "confidence": 55,
          "reasoning": "Could be brand, manufacturer, or internal code",
          "options": ["brand", "manufacturer", "skip"]
        }}
      ]
    }}
  ]
}}
"""


def attributes_phase(state: InteractiveIngestionState) -> dict:
    profile = state.get("profile_data", {})
    headers = profile.get("headers", [])
    feedback = state.get("attributes", {}).get("user_feedback", "")

    gen = read_file(state["file_path"], state.get("sheet_name"))
    hr = profile.get("header_row", 0)
    dr = profile.get("data_start_row", hr + 1)
    for _ in range(dr):
        try:
            next(gen)
        except StopIteration:
            break
    sample_rows = take_rows(gen, 5)
    samples = []
    for row in sample_rows:
        s = {}
        for i, h in enumerate(headers):
            if i < len(row) and row[i] is not None and str(row[i]).strip():
                s[h] = str(row[i]).strip()[:80]
        samples.append(s)

    prompt = ATTRIBUTES_PROMPT.format(
        filename=os.path.basename(state["file_path"]),
        headers=", ".join(headers[:30]),
        samples=json.dumps(samples, indent=2),
        feedback=feedback or "(none — first attempt)",
    )

    result = _llm_json(prompt)

    state["attributes"] = PhaseOutput(
        explanation=result.get("explanation", ""),
        reasoning=result.get("reasoning", ""),
        suggestions=result.get("suggestions", []),
        approved=False,
        user_feedback=feedback,
    )

    # Extract core mappings for downstream use
    core_group = {}
    custom_group = {}
    for group in result.get("suggestions", []):
        if group.get("type") == "group":
            label = group.get("label", "")
            for item in group.get("items", []):
                if item.get("type") == "item":
                    col = item.get("column", "")
                    mapped = item.get("mapped_to", "")
                    if label == "High-Confidence Core Mappings" and mapped:
                        core_group[mapped] = col
                    elif label == "Custom Dynamic Attributes":
                        custom_group[col] = col

    state["core_mappings"] = core_group
    state["custom_mappings"] = custom_group

    msg = (
        f"📋 **Attribute Mapping**\n\n{result.get('explanation', '')}\n\n"
        f"I've grouped the mappings below. You can accept all, or tell me "
        f"about specific ones you'd like to change."
    )
    state.setdefault("messages", []).append({"role": "assistant", "content": msg})

    logger.info(f"attributes | core={len(core_group)} custom={len(custom_group)}")
    return state


# ─── References Phase Node ──────────────────────────────────────

REFERENCES_PROMPT = """You are a VinAI PIM onboarding assistant. The user has confirmed their attribute mappings.

Current phase: Reference Masters

File: {filename}
Headers: {headers}
Column profiles (unique counts + samples):
{profiles}

The user's feedback from the previous attempt (if any):
{feedback}

Attributes marked as Dropdown or MultiSelect in a PIM need a strict,
predefined list of allowed options — called Reference Masters. This
prevents data-entry mistakes and typos.

From the column profiles, identify which columns are candidates for
Reference Masters (have low-moderate unique value counts, look like
brands/colours/sizes/materials/etc.).

For each candidate:
1. List the unique values you found
2. Flag any messy/inconsistent values (e.g. "MED" vs "M", "Blk" vs "Black")
3. Suggest normalizations

Return JSON:
{{
  "explanation": "An educational paragraph explaining what Reference Masters are and why they matter.",
  "reasoning": "Technical details about values found and normalizations proposed.",
  "suggestions": [
    {{
      "type": "item",
      "label": "Brand",
      "column": "Brand Name",
      "unique_count": 15,
      "values": ["Sony", "Bose", "Samsung", ...],
      "messy_values": [],
      "normalizations": [],
      "confidence": 100
    }},
    {{
      "type": "item",
      "label": "Size",
      "column": "Size",
      "unique_count": 6,
      "values": ["S", "M", "L", "XL", "MED", "LRG"],
      "messy_values": ["MED", "LRG"],
      "normalizations": ["MED → M", "LRG → L"],
      "confidence": 85
    }}
  ]
}}
"""


def references_phase(state: InteractiveIngestionState) -> dict:
    profile = state.get("profile_data", {})
    headers = profile.get("headers", [])
    feedback = state.get("references", {}).get("user_feedback", "")

    # Build column profiles from data
    gen = read_file(state["file_path"], state.get("sheet_name"))
    hr = profile.get("header_row", 0)
    dr = profile.get("data_start_row", hr + 1)
    for _ in range(dr):
        try:
            next(gen)
        except StopIteration:
            break
    rows = list(gen)

    col_profiles = []
    for i, h in enumerate(headers):
        vals = []
        for row in rows:
            if i < len(row) and row[i] is not None and str(row[i]).strip():
                vals.append(str(row[i]).strip())
        if vals:
            unique = sorted(set(vals))
            col_profiles.append({
                "name": h,
                "unique_count": len(unique),
                "samples": unique[:8],
            })

    prompt = REFERENCES_PROMPT.format(
        filename=os.path.basename(state["file_path"]),
        headers=", ".join(headers[:30]),
        profiles=json.dumps(col_profiles[:30], indent=2),
        feedback=feedback or "(none — first attempt)",
    )

    result = _llm_json(prompt)

    state["references"] = PhaseOutput(
        explanation=result.get("explanation", ""),
        reasoning=result.get("reasoning", ""),
        suggestions=result.get("suggestions", []),
        approved=False,
        user_feedback=feedback,
    )

    msg = (
        f"📚 **Reference Masters**\n\n{result.get('explanation', '')}\n\n"
        f"Here's what I found. Let me know if any values need cleaning up!"
    )
    state.setdefault("messages", []).append({"role": "assistant", "content": msg})

    ref_count = len(result.get("suggestions", []))
    logger.info(f"references | masters={ref_count}")
    return state


# ─── Products Phase Node ────────────────────────────────────────

PRODUCTS_PROMPT = """You are a VinAI PIM onboarding assistant. The user has confirmed their
Reference Masters.

Current phase: Product Preview

File: {filename}
Headers: {headers}
Sample product rows (first 5):
{samples}

The user's feedback from the previous attempt (if any):
{feedback}

Now we compile the final Product Master template. Explain to the user:

1. The product sheet will have:
   - Fixed columns: Category Path, Variant Attributes, Parent SKU, Code, sku_name, mrp
   - Dynamic columns: one per attribute (from the attribute mapping phase)
   - Image URL columns: image_1 through image_9

2. How many products will be in the output

3. What the next step is (downloading / uploading)

Return JSON:
{{
  "explanation": "A friendly summary of what the product sheet will look like.",
  "reasoning": "Technical breakdown of row count, columns, and image handling.",
  "suggestions": [
    {{
      "type": "item",
      "label": "Total products",
      "value": "{row_count}",
      "reasoning": "Based on data rows in the source file"
    }},
    {{
      "type": "item",
      "label": "Total columns in output",
      "value": "{col_count}",
      "reasoning": "Fixed columns + dynamic attributes + image columns"
    }}
  ]
}}

IMPORTANT: Use the actual row_count and col_count values, not placeholders.
"""


def products_phase(state: InteractiveIngestionState) -> dict:
    profile = state.get("profile_data", {})
    headers = profile.get("headers", [])
    row_count = profile.get("row_count", 0)
    column_count = profile.get("column_count", 0)
    feedback = state.get("products", {}).get("user_feedback", "")

    gen = read_file(state["file_path"], state.get("sheet_name"))
    hr = profile.get("header_row", 0)
    dr = profile.get("data_start_row", hr + 1)
    for _ in range(dr):
        try:
            next(gen)
        except StopIteration:
            break
    sample_rows = take_rows(gen, 5)
    samples = []
    for row in sample_rows:
        s = {}
        for i, h in enumerate(headers):
            if i < len(row) and row[i] is not None and str(row[i]).strip():
                s[h] = str(row[i]).strip()[:60]
        samples.append(s)

    prompt = PRODUCTS_PROMPT.format(
        filename=os.path.basename(state["file_path"]),
        headers=", ".join(headers[:20]),
        samples=json.dumps(samples, indent=2),
        feedback=feedback or "(none — first attempt)",
        row_count=row_count,
        col_count=column_count + 9,  # base cols + up to 9 image cols
    )

    result = _llm_json(prompt)

    # Calculate dynamic column count for accuracy
    attr_count = len(state.get("core_mappings", {})) + len(state.get("custom_mappings", {}))
    img_col_count = min(9, sum(1 for h in headers if any(k in h.lower() for k in ("image", "img", "photo"))))
    total_cols = 6 + attr_count + img_col_count  # 6 fixed + attrs + images

    # Override suggestions with accurate numbers
    suggestions = [
        {
            "type": "item",
            "label": "Total products",
            "value": str(row_count),
            "reasoning": f"Found {row_count} data rows in the source file",
        },
        {
            "type": "item",
            "label": "Total columns in output",
            "value": str(total_cols),
            "reasoning": f"6 fixed columns + {attr_count} attributes + {img_col_count} image columns",
        },
        {
            "type": "item",
            "label": "Image columns detected",
            "value": str(img_col_count),
            "reasoning": "Up to 9 images per product supported",
        },
    ]

    state["products"] = PhaseOutput(
        explanation=result.get("explanation", ""),
        reasoning=result.get("reasoning", ""),
        suggestions=suggestions,
        approved=False,
        user_feedback=feedback,
    )

    msg = (
        f"📦 **Product Compilation**\n\n{result.get('explanation', '')}\n\n"
        f"Ready to generate **{row_count} products** across **{total_cols} columns**.\n\n"
        f"Shall I proceed with generating the final PIM templates?"
    )
    state.setdefault("messages", []).append({"role": "assistant", "content": msg})

    logger.info(f"products | rows={row_count} | cols={total_cols}")
    return state


# ─── Render Node ────────────────────────────────────────────────

def render_interactive(state: InteractiveIngestionState) -> dict:
    """Generate the 4 output xlsx files after all phases are approved."""
    fp = fingerprint_headers(state.get("profile_data", {}).get("headers", []))
    headers = state.get("profile_data", {}).get("headers", [])
    cats = state.get("profile_data", {}).get("category_hierarchy", [])

    # Build mapping objects from phase outputs
    from state import ColumnMapping

    mapping_objs = []
    for target, col in state.get("core_mappings", {}).items():
        if col:
            mapping_objs.append(ColumnMapping(
                source_column=col, target_attribute=target, confidence=1.0,
            ))
    for col, preserved in state.get("custom_mappings", {}).items():
        mapping_objs.append(ColumnMapping(
            source_column=col, target_attribute=preserved, confidence=1.0,
        ))

    # Build attribute definitions
    attr_defs = build_attribute_definitions.invoke({"mappings": mapping_objs})

    # Extract reference values
    mapping_dicts = [
        {"source_column": m.source_column, "target_attribute": m.target_attribute,
         "attribute_type": m.attribute_type}
        for m in mapping_objs
    ]
    profiles_list = state.get("profile_data", {}).get("profiles", [])
    refs = extract_reference_values.invoke({
        "mappings": mapping_dicts,
        "profiles": profiles_list,
    })

    # Read all data rows
    rows = read_file(state["file_path"], state.get("sheet_name"))
    hr = state.get("profile_data", {}).get("header_row", 0)
    dr = max(state.get("profile_data", {}).get("data_start_row", hr + 1), hr + 1)
    for _ in range(dr):
        try:
            next(rows)
        except StopIteration:
            break

    # Build product rows
    img_cols = extract_image_columns(headers, mapping_dicts)
    row_mappings = [{"source_column": m.source_column, "target_attribute": m.target_attribute} for m in mapping_objs]
    product_rows = build_product_rows(
        headers, rows, row_mappings, img_cols, state.get("core_mappings"),
    )

    # Download blank templates if JWT available
    jwt = state.get("jwt_token", "")
    blank_cat = download_blank_template(jwt, "category") if jwt else ""
    blank_attr = download_blank_template(jwt, "attribute") if jwt else ""

    # Render
    attr_names = [m.target_attribute for m in mapping_objs]
    files = render_all_templates.invoke({
        "fingerprint": fp,
        "category_hierarchy": cats,
        "attribute_definitions": attr_defs,
        "reference_values": refs,
        "headers": headers,
        "product_rows": product_rows,
        "attr_names": attr_names,
        "blank_category_path": blank_cat,
        "blank_attribute_path": blank_attr,
    })

    state["generated_files"] = list(files.values())
    state["current_phase"] = "complete"

    msg = (
        f"✅ **All done!** I've generated **{len(files)}** PIM template files:\n\n"
        + "\n".join(f"- `{v.split('/')[-1]}`" for v in files.values())
        + "\n\nYou can download them now. They're ready for upload to your PIM."
    )
    state.setdefault("messages", []).append({"role": "assistant", "content": msg})

    logger.info(f"render | files={list(files.values())}")
    return state


# ─── Router ──────────────────────────────────────────────────────

def route_by_phase(state: InteractiveIngestionState) -> str:
    """Return the next phase name, or 'complete' to trigger render."""
    phase = state.get("current_phase", "categories")
    completed = state.get("phases_completed", [])

    if phase == "complete":
        return "render"
    if phase in ("categories", "attributes", "references", "products"):
        return phase
    return "categories"


# ─── Graph Assembly ──────────────────────────────────────────────

builder = StateGraph(InteractiveIngestionState)

builder.add_node("triage", triage_interactive)
builder.add_node("categories", categories_phase)
builder.add_node("attributes", attributes_phase)
builder.add_node("references", references_phase)
builder.add_node("products", products_phase)
builder.add_node("render", render_interactive)

builder.add_edge(START, "triage")
builder.add_edge("triage", "categories")
builder.add_edge("render", END)

builder.add_conditional_edges(
    "categories",
    lambda s: route_by_phase(s),
    {"categories": "categories", "attributes": "attributes",
     "references": "references", "products": "products", "render": "render"},
)
builder.add_conditional_edges(
    "attributes",
    lambda s: route_by_phase(s),
    {"categories": "categories", "attributes": "attributes",
     "references": "references", "products": "products", "render": "render"},
)
builder.add_conditional_edges(
    "references",
    lambda s: route_by_phase(s),
    {"categories": "categories", "attributes": "attributes",
     "references": "references", "products": "products", "render": "render"},
)
builder.add_conditional_edges(
    "products",
    lambda s: route_by_phase(s),
    {"categories": "categories", "attributes": "attributes",
     "references": "references", "products": "products", "render": "render"},
)

# Use MemorySaver (Postgres can be swapped in)
checkpointer = MemorySaver()

interactive_graph = builder.compile(
    checkpointer=checkpointer,
    interrupt_after=["categories", "attributes", "references", "products"],
)
