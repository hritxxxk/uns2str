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

import re

from interactive_state import InteractiveIngestionState, PhaseOutput, IngestionPhase
from helpers import (
    read_file, take_rows, fingerprint_headers, extract_image_columns,
    build_product_rows, download_blank_template,
    load_cached_mapping, save_cached_mapping,
)
from tools.mapping import build_attribute_definitions
from tools.references import extract_reference_values
from tools.rendering import render_all_templates
from tools.profiling import profile_columns
from learning import fetch_similar_examples
from state import PIM_DEFAULTS, ColumnMapping

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


def _check_type_compatibility(samples: list[str], declared_type: str) -> list[str]:
    if declared_type in ("varchar", "varchar[]"):
        return []
    non_matching = []
    for v in samples:
        v = v.strip()
        if not v:
            continue
        if declared_type == "int":
            try:
                int(v)
            except ValueError:
                non_matching.append(v)
        elif declared_type == "float":
            cleaned = v.replace(",", "").replace("$", "").replace("€", "").replace("£", "").replace("₹", "")
            try:
                float(cleaned)
            except ValueError:
                non_matching.append(v)
        elif declared_type == "boolean":
            if v.lower() not in ("true", "false", "yes", "no", "0", "1", "y", "n", "t", "f"):
                non_matching.append(v)
        elif declared_type == "date":
            date_patterns = [
                r"^\d{4}-\d{2}-\d{2}$",
                r"^\d{2}/\d{2}/\d{4}$",
                r"^\d{2}-\d{2}-\d{4}$",
                r"^\d{4}/\d{2}/\d{2}$",
                r"^\d{2}\.\d{2}\.\d{4}$",
            ]
            if not any(re.match(p, v) for p in date_patterns):
                non_matching.append(v)
    return non_matching


def _validate_mappings(headers: list[str], mapping_data: list[dict], sample_rows_data: list[list]) -> list[dict]:
    errors = []
    col_index = {h: i for i, h in enumerate(headers)}
    for item in mapping_data:
        src = item.get("column", "")
        declared_type = item.get("data_type", "varchar")
        target = item.get("mapped_to", "")
        if not src or not target:
            continue
        if src not in col_index:
            errors.append({"field": target, "issue": f"source_column '{src}' not found in headers", "samples": []})
            continue
        idx = col_index[src]
        samples = []
        for row in sample_rows_data:
            if idx < len(row) and row[idx] is not None and str(row[idx]).strip():
                samples.append(str(row[idx]).strip())
        if not samples:
            continue
        bad = _check_type_compatibility(samples, declared_type)
        if bad and len(bad) / max(len(samples), 1) >= 0.2:
            errors.append({"field": target, "issue": f"Type mismatch: declared '{declared_type}' but samples don't conform", "samples": bad[:5]})
    mapped_targets = {m.get("mapped_to", "") for m in mapping_data}
    for default in PIM_DEFAULTS:
        if default not in mapped_targets:
            errors.append({"field": default, "issue": f"Missing mandatory PIM default: '{default}' has no mapping", "samples": []})
    return errors


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

Column profiles (unique counts + samples):
{profiles}

Sample data (first {sample_count} rows):
{samples}

Metadata rows found above the header (constraints, descriptions, types):
{metadata_context}

Entity-Attribute-Value format detection:
{eav_info}

Historical corrections for similar columns:
{few_shots}

The user's feedback from the previous attempt (if any):
{feedback}

Previous validation errors (fix these):
{validation_errors_text}

Map each source column to a PIM attribute. Attributes may be:
- In columns (each column is one attribute — standard format)
- In rows (EAV format — one column has attribute names, another has values)
- Described in metadata rows above the header

If the data is in EAV format, suggest pivoting the attribute names into columns.

Include BOTH `attribute_type` AND `attribute_data_type` for every mapping. Group your results into three buckets:

1. **High-Confidence Core Mappings** — system-critical fields (sku_name, code, mrp)
   that are clearly identified. Use simple language.

2. **Custom Dynamic Attributes** — proprietary columns that should be preserved.

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

attribute_data_type rules:
- Prices, decimals → float
- Counts, quantities → int
- Yes/No fields → boolean
- Dates → date
- Everything else → varchar

Also report your sampling confidence. If you don't have enough rows to be confident
(e.g. too few unique values per column, suspicious patterns), set needs_more_samples
to true and I'll send more data.

Return JSON:
{{
  "sampling_confidence": 90,
  "needs_more_samples": false,
  "explanation": "A plain-English summary of what you found.",
  "reasoning": "Technical breakdown for users who want details.",
  "detected_format": "columns",
  "suggestions": [
    {{
      "type": "group",
      "label": "High-Confidence Core Mappings",
      "items": [
        {{
          "type": "item",
          "column": "Product Name",
          "mapped_to": "sku_name",
          "attribute_type": "Textbox",
          "attribute_data_type": "varchar",
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
          "attribute_type": "Textbox",
          "attribute_data_type": "varchar",
          "confidence": 55,
          "reasoning": "Could be brand, manufacturer, or internal code",
          "options": ["brand", "manufacturer", "skip"]
        }}
      ]
    }}
  ]
}}

If sampling_confidence < 70 and needs_more_samples is true, I'll send more rows.
"""


def _detect_eav_format(headers: list, rows: list) -> dict:
    if not rows or len(rows) < 3 or len(headers) < 2:
        return {"is_eav": False, "attr_column": None, "val_column": None}
    col_value_counts = {}
    for ci in range(min(3, len(headers))):
        vals = [str(r[ci]).strip() for r in rows[:50] if ci < len(r) and r[ci] is not None and str(r[ci]).strip()]
        unique = set(vals)
        if len(vals) >= 5 and 3 <= len(unique) <= 100 and (len(unique) / len(vals)) < 0.7:
            col_value_counts[ci] = len(unique)
    if not col_value_counts:
        return {"is_eav": False, "attr_column": None, "val_column": None}
    likely_attr_col = max(col_value_counts, key=col_value_counts.get)
    likely_val_col = 1 if likely_attr_col == 0 else 0
    attr_samples = list(set(str(r[likely_attr_col]).strip() for r in rows[:50] if likely_attr_col < len(r) and r[likely_attr_col] is not None))
    return {
        "is_eav": True,
        "attr_column": headers[likely_attr_col] if likely_attr_col < len(headers) else "",
        "val_column": headers[likely_val_col] if likely_val_col < len(headers) else "",
        "sample_attributes": sorted(attr_samples)[:15],
    }


def _read_metadata_rows(state) -> str:
    profile = state.get("profile_data", {})
    hr = profile.get("header_row", 0)
    dr = profile.get("data_start_row", hr + 1)
    if dr <= hr + 1:
        return "(no metadata rows — headers are immediately above data)"
    headers = profile.get("headers", [])
    gen = read_file(state["file_path"], state.get("sheet_name"))
    all_rows = list(gen)
    metadata_rows = []
    for mr in range(hr + 1, min(dr, len(all_rows))):
        row_data = {}
        for ci, h in enumerate(headers):
            if ci < len(all_rows[mr]) and all_rows[mr][ci] is not None and str(all_rows[mr][ci]).strip():
                row_data[h] = str(all_rows[mr][ci]).strip()[:80]
        if row_data:
            metadata_rows.append(row_data)
    if metadata_rows:
        return json.dumps(metadata_rows[:5], indent=2)
    return "(no metadata rows found)"


def attributes_phase(state: InteractiveIngestionState) -> dict:
    profile = state.get("profile_data", {})
    headers = profile.get("headers", [])
    feedback = state.get("attributes", {}).get("user_feedback", "")
    cycle = state.get("attributes", {}).get("correction_cycle", 0)

    gen = read_file(state["file_path"], state.get("sheet_name"))
    hr = profile.get("header_row", 0)
    dr = profile.get("data_start_row", hr + 1)
    for _ in range(dr):
        try:
            next(gen)
        except StopIteration:
            break
    all_data_rows = list(gen)

    fp = fingerprint_headers(headers)
    cached = load_cached_mapping(fp)
    if cached and not feedback and cycle == 0:
        return _apply_cached_mappings(state, cached, headers, fp)

    # Adaptive sampling: start with 50 rows, ask LLM if it needs more
    max_sample = min(len(all_data_rows), 500)
    sample_size = min(50, max_sample)
    sampling_round = 0
    max_sampling_rounds = 3
    needs_more = True
    result = {}

    while needs_more and sampling_round < max_sampling_rounds and sample_size <= max_sample:
        sampling_round += 1
        current_rows = all_data_rows[:sample_size]

        sample_rows = current_rows[:5]
        samples = []
        for row in sample_rows:
            s = {}
            for i, h in enumerate(headers):
                if i < len(row) and row[i] is not None and str(row[i]).strip():
                    s[h] = str(row[i]).strip()[:80]
            samples.append(s)

        cols = profile_columns.invoke({"headers": headers, "rows": current_rows})
        col_profiles = []
        for c in cols[:40]:
            col_profiles.append({
                "name": c["name"],
                "unique": c["unique"],
                "non_null": c["non_null"],
                "sample": c["sample"][:3],
            })

        # EAV detection
        eav_info = _detect_eav_format(headers, current_rows)
        eav_text = json.dumps(eav_info, indent=2) if eav_info["is_eav"] else "(data appears to be in standard columnar format)"

        # Metadata scanning
        metadata_context = _read_metadata_rows(state)

        few_shots = []
        seen_targets = set()
        for h in headers[:10]:
            vals = []
            for row in current_rows:
                idx = headers.index(h)
                if idx < len(row) and row[idx] is not None and str(row[idx]).strip():
                    vals.append(str(row[idx]).strip()[:40])
            if not vals:
                continue
            matches = fetch_similar_examples(h, vals, k=2)
            for m in matches:
                tgt = m["target_attribute"]
                if tgt and tgt not in seen_targets:
                    few_shots.append(m)
                    seen_targets.add(tgt)
                    if len(few_shots) >= 5:
                        break
            if len(few_shots) >= 5:
                break

        few_shots_text = "\n".join(
            f'- "{fs["column_name"]}" → {fs["target_attribute"]} ({fs["attribute_type"]}, {fs["attribute_data_type"]}, mandatory={str(fs["mandatory"]).lower()})'
            for fs in few_shots
        ) if few_shots else "(no historical corrections available)"

        previous_errors = state.get("attributes", {}).get("validation_errors", [])
        validation_errors_text = "\n".join(
            f'- {e["field"]}: {e["issue"]}'
            for e in previous_errors
        ) if previous_errors else "(none)"

        prompt = ATTRIBUTES_PROMPT.format(
            filename=os.path.basename(state["file_path"]),
            profiles=json.dumps(col_profiles, indent=2),
            samples=json.dumps(samples, indent=2),
            sample_count=sample_size,
            metadata_context=metadata_context,
            eav_info=eav_text,
            few_shots=few_shots_text,
            feedback=feedback or "(none — first attempt)",
            validation_errors_text=validation_errors_text,
        )

        result = _llm_json(prompt)
        needs_more = result.get("needs_more_samples", False) and result.get("sampling_confidence", 100) < 70

        if needs_more:
            sample_size = min(sample_size * 2, max_sample)
            logger.info(f"attributes | sampling round {sampling_round}: increased to {sample_size} rows")

    all_items = []
    for group in result.get("suggestions", []):
        if group.get("type") == "group":
            for item in group.get("items", []):
                if item.get("type") == "item" and item.get("column") and item.get("mapped_to"):
                    all_items.append(item)

    validation_errors = _validate_mappings(headers, all_items, all_data_rows[:10])

    max_retries = 3
    if validation_errors and cycle < max_retries and not feedback:
        new_cycle = cycle + 1
        state.setdefault("attributes", {})["correction_cycle"] = new_cycle
        state.setdefault("attributes", {})["validation_errors"] = validation_errors
        logger.info(f"attributes | auto-retry {new_cycle}/{max_retries} | errors={len(validation_errors)}")
        return attributes_phase(state)

    cache_data = [{"source_column": it["column"], "target_attribute": it["mapped_to"],
                    "attribute_type": it.get("attribute_type", "Textbox"),
                    "attribute_data_type": it.get("attribute_data_type", "varchar")}
                   for it in all_items]
    save_cached_mapping(fp, cache_data)

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

    explanation = result.get("explanation", "")
    if validation_errors:
        error_text = "; ".join(f'{e["field"]}: {e["issue"]}' for e in validation_errors[:3])
        explanation += f"\n\n\u26a0\ufe0f **{len(validation_errors)} validation issue(s)**: {error_text}"

    state["attributes"] = PhaseOutput(
        explanation=explanation,
        reasoning=result.get("reasoning", ""),
        suggestions=result.get("suggestions", []),
        approved=False,
        user_feedback=feedback,
    )
    if validation_errors:
        state["attributes"]["validation_errors"] = validation_errors

    msg = (
        f"\ud83d\udccb **Attribute Mapping**\n\n{explanation}\n\n"
        f"I've grouped the mappings below. You can accept all, or tell me "
        f"about specific ones you'd like to change."
    )
    state.setdefault("messages", []).append({"role": "assistant", "content": msg})

    logger.info(f"attributes | core={len(core_group)} custom={len(custom_group)} errors={len(validation_errors)}")
    return state


def _apply_cached_mappings(state, cached, headers, fp):
    core_group = {}
    custom_group = {}
    suggestions = []
    core_items = []
    custom_items = []
    for m in cached:
        tgt = m.get("target_attribute", "")
        src = m.get("source_column", "")
        if tgt in ("sku_name", "code", "mrp"):
            core_group[tgt] = src
            core_items.append({"type": "item", "column": src, "mapped_to": tgt,
                               "attribute_type": m.get("attribute_type", "Textbox"),
                               "attribute_data_type": m.get("attribute_data_type", "varchar"),
                               "confidence": 100, "reasoning": "Cached from previous session"})
        else:
            custom_group[src] = src
            custom_items.append({"type": "item", "column": src, "mapped_to": tgt,
                                 "attribute_type": m.get("attribute_type", "Textbox"),
                                 "attribute_data_type": m.get("attribute_data_type", "varchar"),
                                 "confidence": 100, "reasoning": "Cached from previous session"})
    if core_items:
        suggestions.append({"type": "group", "label": "High-Confidence Core Mappings", "items": core_items})
    if custom_items:
        suggestions.append({"type": "group", "label": "Custom Dynamic Attributes", "items": custom_items})
    state["core_mappings"] = core_group
    state["custom_mappings"] = custom_group
    state["attributes"] = PhaseOutput(
        explanation=f"Loaded {len(cached)} mappings from cache (fingerprint: {fp}).",
        reasoning="",
        suggestions=suggestions,
        approved=False,
        user_feedback="",
    )
    msg = (
        f"\ud83d\udccb **Attribute Mapping**\n\nI recognized this file structure — "
        f"I've loaded **{len(cached)}** saved mappings from a previous session.\n\n"
        f"You can accept all, or tell me about specific ones you'd like to change."
    )
    state.setdefault("messages", []).append({"role": "assistant", "content": msg})
    logger.info(f"attributes | cache hit | fp={fp} | mappings={len(cached)}")
    return state


# ─── References Phase Node ──────────────────────────────────────

REFERENCES_PROMPT = """You are a VinAI PIM onboarding assistant. The user has confirmed their attribute mappings.

Current phase: Reference Masters

Here are the reference values I extracted from the data:
{refs_summary}

The user's feedback from the previous attempt (if any):
{feedback}

Attributes marked as Dropdown or MultiSelect in a PIM need a strict,
predefined list of allowed options — called Reference Masters. This
prevents data-entry mistakes and typos.

Review the extracted reference masters above and:
1. Generate an educational explanation of what Reference Masters are and why they matter
2. Flag any messy/inconsistent values (e.g. "MED" vs "M", "Blk" vs "Black")
3. Suggest normalizations for messy values

Return JSON:
{{
  "explanation": "An educational paragraph explaining what Reference Masters are and why they matter.",
  "reasoning": "Details about normalizations proposed.",
  "suggestions": [
    {{
      "type": "item",
      "label": "Brand",
      "column": "Brand Name",
      "unique_count": 15,
      "values": ["Sony", "Bose", "Samsung"],
      "messy_values": [],
      "normalizations": [],
      "confidence": 100
    }}
  ]
}}
"""


def references_phase(state: InteractiveIngestionState) -> dict:
    profile = state.get("profile_data", {})
    headers = profile.get("headers", [])
    feedback = state.get("references", {}).get("user_feedback", "")

    gen = read_file(state["file_path"], state.get("sheet_name"))
    hr = profile.get("header_row", 0)
    dr = profile.get("data_start_row", hr + 1)
    for _ in range(dr):
        try:
            next(gen)
        except StopIteration:
            break
    all_data_rows = list(gen)

    # Profile all columns (gets unique_values for reference extraction)
    cols = profile_columns.invoke({"headers": headers, "rows": all_data_rows})

    # Use programmatic extract_reference_values with the mappings we have
    mapping_dicts = []
    for target, col in state.get("core_mappings", {}).items():
        if col:
            mapping_dicts.append({"source_column": col, "target_attribute": target, "attribute_type": "Textbox"})
    for col, preserved in state.get("custom_mappings", {}).items():
        mapping_dicts.append({"source_column": col, "target_attribute": preserved, "attribute_type": "Dropdown"})

    refs = extract_reference_values.invoke({"mappings": mapping_dicts, "profiles": cols})

    # Build suggestions from programmatic refs
    suggestions = []
    for master_name, values in refs.items():
        suggestions.append({
            "type": "item",
            "label": master_name.replace(" Master", ""),
            "column": master_name,
            "unique_count": len(values),
            "values": values[:20],
            "messy_values": [],
            "normalizations": [],
            "confidence": 100,
        })

    # Use LLM to add educational explanation + detect messy values
    refs_summary = "\n".join(
        f'- {k}: {len(v)} values — {v[:5]}'
        for k, v in refs.items()
    ) if refs else "(no reference masters detected)"

    prompt = REFERENCES_PROMPT.format(
        refs_summary=refs_summary,
        feedback=feedback or "(none — first attempt)",
    )
    result = _llm_json(prompt)

    # Merge LLM's messy value detection into programmatic suggestions
    llm_suggestions = result.get("suggestions", [])
    for llm_s in llm_suggestions:
        llm_label = llm_s.get("label", "")
        for s in suggestions:
            if s["label"] == llm_label or s["column"] == llm_s.get("column", ""):
                if llm_s.get("messy_values"):
                    s["messy_values"] = llm_s["messy_values"]
                if llm_s.get("normalizations"):
                    s["normalizations"] = llm_s["normalizations"]
                break

    state["references"] = PhaseOutput(
        explanation=result.get("explanation", ""),
        reasoning=result.get("reasoning", ""),
        suggestions=suggestions,
        approved=False,
        user_feedback=feedback,
    )

    msg = (
        f"\ud83d\udcda **Reference Masters**\n\n{result.get('explanation', '')}\n\n"
        f"I extracted **{len(suggestions)}** reference lists from your data. "
        f"Let me know if any values need cleaning up!"
    )
    state.setdefault("messages", []).append({"role": "assistant", "content": msg})

    logger.info(f"references | masters={len(suggestions)}")
    return state


# ─── Products Phase Node ────────────────────────────────────────

PRODUCTS_PROMPT = """You are a VinAI PIM onboarding assistant. The user has confirmed their
Reference Masters.

Current phase: Product Preview

Here is a preview of the first 3 mapped products:
{preview}

The user's feedback from the previous attempt (if any):
{feedback}

The product sheet will have:
- Fixed columns: Category Path, Variant Attributes, Parent SKU, Code, sku_name, mrp
- Dynamic columns: one per attribute from the mapping phase
- Image URL columns: image_1 through image_9

Explain to the user what they're seeing and confirm they're happy to proceed.

Return JSON:
{{
  "explanation": "A friendly summary of what the product sheet will look like, referencing the preview.",
  "reasoning": "Technical breakdown of row count, columns, and image handling.",
  "suggestions": []
}}
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
    all_data_rows = list(gen)

    # Build product rows programmatically (same as render does)
    row_mappings = []
    for target, col in state.get("core_mappings", {}).items():
        if col:
            row_mappings.append({"source_column": col, "target_attribute": target})
    for col, preserved in state.get("custom_mappings", {}).items():
        row_mappings.append({"source_column": col, "target_attribute": preserved})

    img_cols = extract_image_columns(headers, row_mappings)
    product_rows = build_product_rows(
        headers, all_data_rows, row_mappings, img_cols, state.get("core_mappings"),
    )

    # Build a preview of the first 3 mapped products
    preview_rows = []
    for pr in product_rows[:3]:
        row_preview = {k: str(v)[:40] for k, v in list(pr.items())[:8]}
        preview_rows.append(row_preview)

    attr_count = len(state.get("core_mappings", {})) + len(state.get("custom_mappings", {}))
    img_col_count = len(img_cols[:9])
    total_cols = 6 + attr_count + img_col_count

    preview_text = json.dumps(preview_rows, indent=2) if preview_rows else "(no product rows)"

    prompt = PRODUCTS_PROMPT.format(
        preview=preview_text,
        feedback=feedback or "(none — first attempt)",
    )
    result = _llm_json(prompt)

    suggestions = [
        {"type": "item", "label": "Total products", "value": str(row_count),
         "reasoning": f"Found {row_count} data rows in the source file"},
        {"type": "item", "label": "Total columns in output", "value": str(total_cols),
         "reasoning": f"6 fixed columns + {attr_count} attributes + {img_col_count} image columns"},
        {"type": "item", "label": "Image columns detected", "value": str(img_col_count),
         "reasoning": "Up to 9 images per product supported"},
    ]
    if preview_rows:
        suggestions.insert(0, {
            "type": "group",
            "label": "Product Preview (first 3 rows)",
            "items": [
                {"type": "item", "column": k, "mapped_to": v, "confidence": 100,
                 "reasoning": ""}
                for pr in preview_rows for k, v in pr.items()
            ][:12],
        })

    state["products"] = PhaseOutput(
        explanation=result.get("explanation", ""),
        reasoning=result.get("reasoning", ""),
        suggestions=suggestions,
        approved=False,
        user_feedback=feedback,
    )

    msg = (
        f"\ud83d\udce6 **Product Compilation**\n\n{result.get('explanation', '')}\n\n"
        f"**{row_count}** products across **{total_cols}** columns ready.\n\n"
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
