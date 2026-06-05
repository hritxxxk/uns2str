import os
import json
import openpyxl
from dotenv import load_dotenv
from google import genai
from helpers import *
from state import MappingResponse, ColumnMapping, CategoryValidation
from tools.profiling import detect_data_sheet, profile_columns, detect_category_structure
from tools.mapping import normalize_mapping, validate_mapping, build_attribute_definitions
from tools.references import extract_reference_values
from tools.rendering import render_category_xlsx, render_attribute_xlsx, render_reference_xlsx, render_product_xlsx

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

PIM_DEFAULTS = ["sku_name", "code", "description", "mrp", "brand"]

MAPPING_PROMPT_TEMPLATE = """You are a PIM data mapping expert. Map each source column to a PIM attribute.

Return a JSON object with key "mappings" containing an array of objects.
Each object has these fields:
  source_column (str): original column name
  target_attribute (str): snake_case PIM attribute name
  attribute_type (str): Textbox | Dropdown | RichText | Textarea | MultiSelect | Date | Time
  attribute_data_type (str): varchar | int | float | boolean | date
  constraint (bool): true only if dropdown or multiselect
  length (int): max characters
  mandatory (bool): true for identity or legal fields
  attribute_group (str): e.g. "Product Identification", "Pricing", "Classification", "Media", "Technical Specs", "Brand & Origin"
  confidence (float): 0.0 to 1.0

RULES:

1. target_attribute must be snake_case — e.g. "product_name", "item_code", "mrp", "colour", "brand", "gender", "size", "image_url". Do NOT copy source column names as-is.

2. Use column semantics + stats to decide attribute_type:
   - SKU, code, id fields → Textbox, varchar, mandatory=true
   - Brand, colour, size, gender, season, type, category → Dropdown, constraint=true
   - Product name / title → Textbox, mandatory=true
   - Description → RichText, varchar, length=65536
   - Price, MRP, cost → Textbox, float
   - Image/photo/img URLs → Textbox, varchar, length=2048
   - Tags, features, materials → MultiSelect, constraint=true
   - Date fields → Date, date

3. The PIM already has these default attributes: sku_name, code, description, mrp, brand. Do NOT recreate them — map source columns TO them instead (e.g. "Product Name" → "sku_name", not "product_name").

4. constraint=true ONLY for attributes with predefined selectable values.

5. mandatory=true ONLY for: sku, code, product_name, mrp.

Source columns with stats:
{profile_text}

Column metadata notes (data types, constraints, defaults above the header):
{metadata_text}

Sample rows:
{sample_text}"""

MAX_PROFILE_COLS = 150


def build_mapping_prompt(profiles, sample_rows, metadata=None):
    capped = profiles[:MAX_PROFILE_COLS]
    trimmed = [{k: v for k, v in p.items() if k != "unique_values"} for p in capped]
    for p in trimmed:
        if len(p.get("sample", [])) > 2:
            p["sample"] = p["sample"][:2]
    return MAPPING_PROMPT_TEMPLATE.format(
        profile_text=json.dumps(trimmed, indent=2),
        sample_text=json.dumps(sample_rows[:3], indent=2),
        metadata_text=json.dumps(metadata, indent=2) if metadata else "None"
    )


def call_llm(prompt):
    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)


def parse_mapping_response(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("mappings", "attributes", "columns"):
            if key in raw and isinstance(raw[key], list):
                return raw[key]
    raise ValueError(f"Cannot extract mappings from: {type(raw)}")


def cache_mapping(fingerprint, mappings):
    save_cached_mapping(fingerprint, [m.model_dump() for m in mappings])


def avg_confidence(mappings):
    if not mappings:
        return 0.0
    return sum(m.confidence for m in mappings) / len(mappings)


# ─── Graph nodes (thin orchestrators) ────────────────────────────

def fingerprint_source(state):
    rows = read_file(state["source_path"], state.get("sheet_name"))
    headers, _ = get_headers_and_data(rows)
    fp = fingerprint_headers(headers)
    cached = load_cached_mapping(fp)
    state["fingerprint"] = fp
    state["headers"] = headers
    state["is_known_schema"] = cached is not None
    if cached:
        state["mapping"] = [ColumnMapping(**m) for m in cached]
    else:
        state["mapping"] = []
    return state


def detect_header_via_llm(rows):
    preview = json.dumps([{f"col_{j}": str(c)[:40] for j, c in enumerate(row[:20]) if c is not None and str(c).strip()} for row in rows[:15]], indent=2)
    prompt = f"""Given the first 15 rows of a spreadsheet, identify:
1. Which row index (0-based) contains the column headers
2. Which row index (0-based) does the actual product data start at (skipping header and any metadata/description/constraint rows between the header and data).

Rows:
{preview}
Return JSON: {{"header_row": int, "data_start_row": int}}"""
    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )
    result = json.loads(response.text)
    if isinstance(result, list):
        result = result[0] if result else {}
    return result.get("header_row", 0), result.get("data_start_row", len(rows))


def profile_source(state):
    if not state.get("sheet_name"):
        result = detect_data_sheet.invoke({"path": state["source_path"]})
        state["sheet_name"] = result["sheet"]
        print(f"  Auto-detected sheet: '{result['sheet']}' ({result['cells']} cells)")

    rows = read_file(state["source_path"], state.get("sheet_name"))
    header_row, data_start_row = detect_header_via_llm(rows)
    headers, data = get_headers_and_data(rows, header_row)
    if data_start_row < header_row + 1:
        data_start_row = header_row + 1
    data = rows[data_start_row:]

    state["headers"] = headers
    state["header_row"] = header_row
    state["data_start_row"] = data_start_row
    state["metadata"] = [{headers[j]: str(rows[mr][j])[:60] for j in range(min(len(headers), len(rows[mr]))) if rows[mr][j] is not None and str(rows[mr][j]).strip()} for mr in range(header_row)]
    state["profiles"] = profile_columns.invoke({"headers": headers, "rows": data})
    state["row_count"] = len(data)
    state["sample_rows"] = [dict(zip(headers, row)) for row in data[:5]]
    state["category_candidates"] = detect_category_structure.invoke({"path": state["source_path"], "data_sheet": state["sheet_name"]})
    state["category_path_config"] = {}
    state["category_hierarchy"] = []
    return state


def map_columns(state):
    if state["is_known_schema"]:
        return state

    prompt = build_mapping_prompt(state["profiles"], state.get("sample_rows", []), state.get("metadata"))
    raw = call_llm(prompt)
    extracted = parse_mapping_response(raw)
    normalized = normalize_mapping.invoke({"raw_list": extracted})
    parsed = validate_mapping.invoke({"raw_list": normalized})

    state["mapping"] = parsed
    state["mapping_requires_review"] = avg_confidence(parsed) < 0.75
    cache_mapping(state["fingerprint"], parsed)
    return state


def _validate_paths(paths):
    if not paths or len(paths) < 2:
        return False, "Less than 2 paths found"
    prompt = f"""Validate these category paths. They are VALID if they form a parent→child hierarchy.

ACCEPT paths that have:
- Prefix codes like "P_Product", "N_Item", "C_Code" (these are labels, not IDs)
- Mixed naming conventions

REJECT paths that have:
- True duplicate levels like "A > A > A" where same name repeats
- Garbage data like "t1 > temp > temp"
- Single values that aren't hierarchical

Sample paths:
{json.dumps(list(paths)[:10], indent=2)}

Return JSON: {{"is_valid": bool, "reason": "short explanation"}}"""
    resp = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )
    r = json.loads(resp.text)
    if isinstance(r, list):
        r = r[0] if r else {}
    return r.get("is_valid", False), r.get("reason", "")


def _build_paths_from_columns(headers, rows, col_indices, sep=" > "):
    paths = set()
    for row in rows:
        parts = [str(row[i]).strip() for i in col_indices if i < len(row) and row[i] is not None and str(row[i]).strip()]
        if len(parts) >= 2:
            paths.add(sep.join(parts))
    return paths


def _strategy_hierarchy_sheet(state):
    candidates = state.get("category_candidates", [])
    if not candidates:
        return None
    trimmed = [{k: v for k, v in c.items()} for c in candidates[:3]]
    for t in trimmed:
        if len(t.get("headers", [])) > 20:
            t["headers"] = t["headers"][:20]
        if len(t.get("rows", [])) > 2:
            t["rows"] = t["rows"][:2]
    prompt = f"""Given sheets with potential hierarchy data, decide which columns form a category path.
Return JSON: {{"sheet": str, "columns": [str], "skip_code_columns": bool}}
Rules: Pick columns forming parent→child chain. Skip code/id/num columns.
Candidates:
{json.dumps(trimmed, indent=2)}"""
    resp = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )
    r = json.loads(resp.text)
    if isinstance(r, list):
        r = r[0] if r else {}
    sheet_name = r.get("sheet", "")
    columns = r.get("columns", [])
    skip_codes = r.get("skip_code_columns", True)
    candidate = next((c for c in candidates if c["sheet"] == sheet_name), None)
    if not candidate or not columns:
        return None
    all_h = candidate["headers"]
    indices = [all_h.index(c) for c in columns if c in all_h]
    if skip_codes:
        kw = ["code", "id", "key", "num", "no"]
        indices = [i for i in indices if not any(k in all_h[i].lower() for k in kw)]
        if len(indices) < 2:
            indices = [all_h.index(c) for c in columns if c in all_h]
    if len(indices) < 2:
        return None
    wb = openpyxl.load_workbook(state["source_path"], read_only=True, data_only=True)
    ws = wb[sheet_name]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    wb.close()
    return _build_paths_from_columns(all_h, rows, indices)


def _strategy_level_columns(state):
    headers = state.get("headers", [])
    level_cols = [i for i, h in enumerate(headers)
                  if any(k in h.lower() for k in ("categor", "sub", "product type", "l1", "l2", "l3", "l4", "level", "silo", "commodity", "division", "department", "gender"))
                  and "code" not in h.lower() and "id" not in h.lower()]
    if len(level_cols) < 2:
        return None
    prompt = f"""Pick columns that form a category path from this list. Skip duplicates.
Return JSON: {{"columns": ["col1", "col2", ...]}}
Columns: {json.dumps([headers[i] for i in level_cols])}
Sample values: {json.dumps({headers[i]: state.get("sample_rows", [{}])[0].get(headers[i], "") for i in level_cols})}"""
    resp = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )
    r = json.loads(resp.text)
    if isinstance(r, list):
        r = r[0] if r else {}
    chosen = r.get("columns", [])
    indices = [headers.index(c) for c in chosen if c in headers]
    if len(indices) < 2:
        return None
    rows = read_file(state["source_path"], state.get("sheet_name"))
    _, data = get_headers_and_data(rows, state.get("header_row", 0))
    dr = state.get("data_start_row", state.get("header_row", 0) + 1)
    if dr < state.get("header_row", 0) + 1:
        dr = state.get("header_row", 0) + 1
    data = rows[dr:]
    return _build_paths_from_columns(headers, data, indices)


def _strategy_single_column(state):
    headers = state.get("headers", [])
    cat_col = next((i for i, h in enumerate(headers) if h.lower() in ("category", "categories", "product type")), None)
    if cat_col is None:
        return None
    rows = read_file(state["source_path"], state.get("sheet_name"))
    _, data = get_headers_and_data(rows, state.get("header_row", 0))
    dr = state.get("data_start_row", state.get("header_row", 0) + 1)
    if dr < state.get("header_row", 0) + 1:
        dr = state.get("header_row", 0) + 1
    data = rows[dr:]
    samples = list(set(str(row[cat_col]).strip() for row in data[:50] if row[cat_col] is not None and str(row[cat_col]).strip()))[:10]
    prompt = f"""Column '{headers[cat_col]}' contains category data. Sample values:
{json.dumps(samples, indent=2)}
Determine: multi_value_separator (if one cell has multiple categories), path_separator (between levels), split_cells (true/false).
Return JSON: {{"multi_value_separator": str or null, "path_separator": str, "split_cells": bool}}"""
    resp = client.models.generate_content(model="gemini-2.5-flash-lite", contents=prompt, config={"response_mime_type": "application/json"})
    r = json.loads(resp.text)
    if isinstance(r, list):
        r = r[0] if r else {}
    multi_sep = r.get("multi_value_separator")
    path_sep = r.get("path_separator", ">")
    split_cells = r.get("split_cells", False)
    paths = set()
    for row in data:
        val = str(row[cat_col]).strip() if cat_col < len(row) and row[cat_col] is not None else ""
        if not val:
            continue
        if split_cells and multi_sep and multi_sep in val:
            for chunk in val.split(multi_sep):
                chunk = chunk.strip()
                if not chunk:
                    continue
                if path_sep in chunk:
                    parts = [p.strip() for p in chunk.split(path_sep) if p.strip()]
                    if len(parts) >= 2:
                        paths.add(" > ".join(parts))
        elif path_sep in val:
            parts = [p.strip() for p in val.split(path_sep) if p.strip()]
            if len(parts) >= 2:
                paths.add(" > ".join(parts))
    return paths if len(paths) >= 2 else None
    return paths if len(paths) >= 2 else None


def _strategy_infer_from_attributes(state):
    headers = state.get("headers", [])
    attr_keywords = ["product type", "commodity", "silo", "pillar", "division", "department", "gender", "category"]
    attr_cols = [i for i, h in enumerate(headers) if any(k in h.lower() for k in attr_keywords)]
    if len(attr_cols) < 2:
        return None
    prompt = f"""These columns might form a product hierarchy. Pick columns in order from broadest to most specific.
Return JSON: {{"columns": ["col1", "col2", ...]}}
Available columns: {json.dumps([headers[i] for i in attr_cols])}
Sample row: {json.dumps({headers[i]: state.get("sample_rows", [{}])[0].get(headers[i], "") for i in attr_cols})}"""
    resp = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )
    r = json.loads(resp.text)
    if isinstance(r, list):
        r = r[0] if r else {}
    chosen = r.get("columns", [])
    indices = [headers.index(c) for c in chosen if c in headers]
    if len(indices) < 2:
        return None
    rows = read_file(state["source_path"], state.get("sheet_name"))
    _, data = get_headers_and_data(rows, state.get("header_row", 0))
    dr = state.get("data_start_row", state.get("header_row", 0) + 1)
    if dr < state.get("header_row", 0) + 1:
        dr = state.get("header_row", 0) + 1
    data = rows[dr:]
    return _build_paths_from_columns(headers, data, indices)


def resolve_category_paths(state):
    strategies = [
        ("Hierarchy sheet", _strategy_hierarchy_sheet),
        ("Level columns (CATEGORY1-4)", _strategy_level_columns),
        ("Single category column", _strategy_single_column),
        ("Inferred from product attributes", _strategy_infer_from_attributes),
    ]

    for name, strategy in strategies:
        result = strategy(state)
        if result is None:
            continue
        valid, reason = _validate_paths(result)
        print(f"  Category strategy '{name}': {'✅' if valid else '❌'} {reason}")
        if valid:
            state["category_hierarchy"] = sorted(result)
            return state

    state["need_user_input"] = True
    print("  ⚠ Could not determine category paths. Set need_user_input=True")
    return state


def build_attributes(state):
    defs = build_attribute_definitions.invoke({"mappings": state["mapping"]})
    state["attribute_definitions"] = defs
    return state


def collect_references(state):
    raw_mappings = [{"source_column": m.source_column, "target_attribute": m.target_attribute, "attribute_type": m.attribute_type} for m in state["mapping"]]
    refs = extract_reference_values.invoke({"mappings": raw_mappings, "profiles": state["profiles"]})
    state["reference_values"] = refs
    return state


def fill_templates(state):
    os.makedirs("output", exist_ok=True)
    fp = state["fingerprint"]
    files = {}

    if state.get("category_hierarchy"):
        wb = render_category_xlsx.invoke({"paths": state["category_hierarchy"]})
        wb.save(f"output/{fp}_category.xlsx")
        wb.close()
        files["category"] = f"output/{fp}_category.xlsx"

    wb = render_attribute_xlsx.invoke({"defs": state["attribute_definitions"]})
    wb.save(f"output/{fp}_attribute.xlsx")
    wb.close()
    files["attribute"] = f"output/{fp}_attribute.xlsx"

    if state.get("reference_values"):
        wb = render_reference_xlsx.invoke({"refs": state["reference_values"]})
        wb.save(f"output/{fp}_reference.xlsx")
        wb.close()
        files["reference"] = f"output/{fp}_reference.xlsx"

    rows = read_file(state["source_path"], state.get("sheet_name"))
    hr = state.get("header_row", 0)
    headers, data = get_headers_and_data(rows, hr)
    dr = state.get("data_start_row", hr + 1)
    if dr < hr + 1:
        dr = hr + 1
    data = rows[dr:]
    img_cols = extract_image_columns(headers)
    mapping_list = [{"source_column": m.source_column, "target_attribute": m.target_attribute} for m in state["mapping"]]
    attr_names = [m.get("target_attribute", m.get("source_column")) for m in mapping_list]

    product_rows = build_product_rows(headers, data, mapping_list, img_cols)
    wb = render_product_xlsx.invoke({"rows": product_rows, "attr_names": attr_names})
    wb.save(f"output/{fp}_product.xlsx")
    wb.close()
    files["product"] = f"output/{fp}_product.xlsx"

    state["output_files"] = files
    return state
