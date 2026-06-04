import os
import json
from dotenv import load_dotenv
from google import genai
from helpers import *
from state import MappingResponse, ColumnMapping

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# ─── mapping prompt ──────────────────────────────────────────────

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

1. target_attribute must be snake_case — e.g. "product_name", "item_code", "mrp", "colour", "brand", "gender", "size", "image_url". Do NOT copy source column names as-is. Convert: "ITEM CODE" → "item_code", "ITEM NAME" → "product_name", "MRP" → "mrp", "BRAND" → "brand".

2. Use column semantics + stats to decide attribute_type:
   - If column is named "sku", "code", "id" → Textbox, varchar, mandatory=true
   - If column has few unique values relative to total rows and means brand/colour/size/gender/season/type/category → Dropdown, constraint=true
   - If column contains product name/title/description → Textbox or RichText, mandatory=true
   - If column contains price/cost/mrp → Textbox, float
   - If column contains image/photo/img → Textbox, varchar, length=2048
   - If column contains date → Date, date
   - If column contains tags/features/material → MultiSelect, constraint=true

3. constraint=true ONLY for attributes where users pick from a predefined list (brand, colour, size, gender, season, status, category, type, material).

4. mandatory=true ONLY for: sku, code, product_name, mrp (identity and pricing fields).

5. attribute_group examples:
   - "Product Identification": code, sku, gtin, hsn
   - "Pricing": mrp, price, cost
   - "Classification": category, type, gender, season, brand
   - "Technical Specs": material, fabric, weight, dimensions
   - "Media": image_url, video_url
   - "Brand & Origin": brand, manufacturer, country_of_origin
   - "Shipping": weight, length, width, height

Source columns with stats:
{profile_text}

Sample rows:
{sample_text}"""

# ─── small helpers ───────────────────────────────────────────────

def build_mapping_prompt(profiles, sample_rows):
    return MAPPING_PROMPT_TEMPLATE.format(
        profile_text=json.dumps(profiles, indent=2),
        sample_text=json.dumps(sample_rows[:3], indent=2)
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


def normalize_mapping(raw_list):
    result = []
    for m in raw_list:
        src = m.get("source_column", "")
        target = m.pop("target", m.get("target_attribute", src))
        target = target.lower().replace(" ", "_").replace("-", "_")
        m["target_attribute"] = target
        m["constraint"] = m.pop("constrained", m.get("constraint", False))
        m["attribute_type"] = m.pop("type", m.get("attribute_type", "Textbox"))
        m["attribute_data_type"] = m.pop("data_type", m.get("attribute_data_type", "varchar"))
        m["attribute_group"] = m.pop("group", m.get("attribute_group", "Basic Information"))
        result.append(m)
    return result


def validate_mapping(raw_list):
    return MappingResponse(mappings=[ColumnMapping(**m) for m in raw_list])


def cache_mapping(fingerprint, mappings):
    save_cached_mapping(fingerprint, [m.model_dump() for m in mappings])


def avg_confidence(mappings):
    if not mappings:
        return 0.0
    return sum(m.confidence for m in mappings) / len(mappings)

# ─── node functions ──────────────────────────────────────────────

def fingerprint_source(state):
    rows = read_file(state["source_path"], state.get("sheet_name"))
    headers, _ = get_headers_and_data(rows)
    fp = fingerprint_headers(headers)
    cached = load_cached_mapping(fp)
    state["fingerprint"] = fp
    state["headers"] = headers
    state["is_known_schema"] = cached is not None
    state["mapping"] = cached if cached else []
    return state


def profile_source(state):
    rows = read_file(state["source_path"], state.get("sheet_name"))
    headers, data = get_headers_and_data(rows)
    state["headers"] = headers
    state["profiles"] = profile_columns(headers, data)
    state["row_count"] = len(data)
    state["sample_rows"] = [dict(zip(headers, row)) for row in data[:5]]
    state["category_hierarchy"] = detect_category_sheets(state["source_path"])
    return state


def map_columns(state):
    if state["is_known_schema"]:
        return state

    prompt = build_mapping_prompt(state["profiles"], state.get("sample_rows", []))
    raw = call_llm(prompt)
    extracted = parse_mapping_response(raw)
    normalized = normalize_mapping(extracted)
    parsed = validate_mapping(normalized)

    state["mapping"] = parsed.mappings
    state["mapping_requires_review"] = avg_confidence(parsed.mappings) < 0.75
    cache_mapping(state["fingerprint"], parsed.mappings)
    return state


def build_attributes(state):
    defs = []
    for m in state["mapping"]:
        a_type = m.attribute_type
        constraint = m.constraint or a_type in ("Dropdown", "MultiSelect")

        if a_type == "RichText":
            length = 65536
        elif a_type == "Textarea":
            length = 16384
        elif "image" in m.target_attribute.lower():
            length = 2048
        else:
            length = m.length or 255

        ref_master = f"{m.target_attribute.replace('_', ' ').title()} Master" if constraint else ""
        ref_attr = m.target_attribute.lower().replace(" ", "_") if constraint else ""

        defs.append({
            "attribute_name": m.target_attribute.lower().replace(" ", "_"),
            "short_name": m.target_attribute.lower().replace(" ", "_"),
            "display_name": m.target_attribute.replace("_", " ").title(),
            "attribute_type": a_type,
            "attribute_data_type": m.attribute_data_type,
            "constraint": constraint,
            "length": length,
            "mandatory": m.mandatory,
            "filter": True,
            "editability": True,
            "visibility": True,
            "searchable": True,
            "auto_translate": False,
            "attribute_group": m.attribute_group,
            "reference_master": ref_master,
            "reference_attribute": ref_attr,
            "status": "Active"
        })

    state["attribute_definitions"] = defs
    return state


def collect_references(state):
    refs = {}
    for m in state["mapping"]:
        if m.attribute_type not in ("Dropdown", "MultiSelect"):
            continue
        profile = next((p for p in state["profiles"] if p["name"] == m.source_column), None)
        if profile and profile.get("unique_values"):
            master_key = f"{m.target_attribute.replace('_', ' ').title()} Master"
            refs[master_key] = sorted(profile["unique_values"])

    state["reference_values"] = refs
    return state


def fill_templates(state):
    os.makedirs("output", exist_ok=True)
    fp = state["fingerprint"]
    files = {}

    if state.get("category_hierarchy"):
        wb = render_category_xlsx(state["category_hierarchy"])
        wb.save(f"output/{fp}_category.xlsx")
        wb.close()
        files["category"] = f"output/{fp}_category.xlsx"

    wb = render_attribute_xlsx(state["attribute_definitions"])
    wb.save(f"output/{fp}_attribute.xlsx")
    wb.close()
    files["attribute"] = f"output/{fp}_attribute.xlsx"

    if state.get("reference_values"):
        wb = render_reference_xlsx(state["reference_values"])
        wb.save(f"output/{fp}_reference.xlsx")
        wb.close()
        files["reference"] = f"output/{fp}_reference.xlsx"

    rows = read_file(state["source_path"], state.get("sheet_name"))
    headers, data = get_headers_and_data(rows)
    img_cols = [h for h in headers if any(k in h.lower() for k in ("image", "img", "picture", "photo"))]
    mapping_list = [{"source_column": m.source_column, "target_attribute": m.target_attribute} for m in state["mapping"]]
    attr_names = [m.get("target_attribute", m.get("source_column")) for m in mapping_list]

    product_rows = build_product_rows(headers, data, mapping_list, img_cols)
    wb = render_product_xlsx(product_rows, attr_names)
    wb.save(f"output/{fp}_product.xlsx")
    wb.close()
    files["product"] = f"output/{fp}_product.xlsx"

    state["output_files"] = files
    return state
