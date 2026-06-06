import os
import json
from graph import graph
from state import IngestionOutput, ColumnMapping
from helpers import read_file, get_headers_and_data, build_product_rows, extract_image_columns, fingerprint_headers, load_cached_mapping, save_cached_mapping
from tools.mapping import build_attribute_definitions
from tools.references import extract_reference_values
from tools.rendering import render_all_templates


def run(path, sheet=None):
    initial = {
        "messages": [{"role": "user", "content": f"Profile and map this file: {path}" + (f" (sheet: {sheet})" if sheet else "")}],
        "structured_response": None,
        "remaining_steps": 25,
        "source_path": path,
        "sheet_name": sheet,
        "fingerprint": "",
        "is_known_schema": False,
        "headers": [],
        "header_row": 0,
        "data_start_row": 1,
        "metadata": [],
        "profiles": [],
        "sample_rows": [],
        "row_count": 0,
        "category_candidates": [],
        "category_path_config": {},
        "category_hierarchy": [],
        "mapping": [],
        "mapping_requires_review": False,
        "attribute_definitions": [],
        "reference_values": {},
        "output_files": {},
        "need_user_input": False,
        "error": None
    }

    result = graph.invoke(initial)
    output = result.get("structured_response") or IngestionOutput(status="partial", message="No structured output")

    if output.status != "success":
        print(f"\nSource: {path}")
        print(f"Status: {output.status}")
        print(f"Message: {output.message}")
        return result

    # Build attribute definitions from the agent's mapping
    fp = output.fingerprint or fingerprint_headers([])
    sheet_name = sheet or result.get("sheet_name") or output.sheet_name
    
    # Re-read file for row data
    rows = read_file(path, sheet_name)
    headers, _ = get_headers_and_data(rows, output.header_row)
    
    # Map attribute outputs to input format for build_attribute_definitions
    raw_mappings = output.mapping
    column_mappings = [ColumnMapping(**m) if isinstance(m, dict) else m for m in raw_mappings]
    
    attr_defs = build_attribute_definitions.invoke({"mappings": column_mappings})
    refs = extract_reference_values.invoke({"mappings": [m.dict() if hasattr(m, 'dict') else m for m in column_mappings], "profiles": output.profiles})
    cats = output.category_hierarchy

    # Build product rows
    hr = output.header_row
    dr = max(output.data_start_row, hr + 1)
    h2, prod_data = get_headers_and_data(rows, hr)
    prod_data = rows[dr:]
    img_cols = extract_image_columns(h2)
    mapping_dicts = [{"source_column": m.source_column, "target_attribute": m.target_attribute} for m in column_mappings]
    attr_names = [m.get("target_attribute", m.get("source_column")) for m in mapping_dicts]
    product_rows = build_product_rows(h2, prod_data, mapping_dicts, img_cols)

    # Render templates
    files = render_all_templates.invoke({
        "fingerprint": fp,
        "category_hierarchy": cats,
        "attribute_definitions": attr_defs,
        "reference_values": refs,
        "headers": h2,
        "product_rows": product_rows,
        "attr_names": attr_names
    })

    print(f"\nSource: {path}")
    print(f"Status: {output.status}")
    print(f"Attributes: {len(attr_defs)}")
    if output.reference_count:
        print(f"References: {output.reference_count}")
    if output.category_count:
        print(f"Categories: {output.category_count}")
    print(f"Output:")
    for f in files.values():
        print(f"  {f}")
    print(f"\n{output.message}")

    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python main.py <file> [sheet]")
        sys.exit(1)
    path = sys.argv[1]
    sheet = sys.argv[2] if len(sys.argv) > 2 else None
    if not os.path.exists(path):
        print(f"File not found: {path}")
        sys.exit(1)
    run(path, sheet)
