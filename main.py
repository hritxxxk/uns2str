import os
import json
from graph import graph
from helpers import build_product_rows, extract_image_columns, get_headers_and_data, read_file
from tools.rendering import render_all_templates


def run(path, sheet=None):
    initial = {
        "messages": [],
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
        "error": None
    }

    result = graph.invoke(initial)

    print(f"\nSource: {path}")
    print(f"Fingerprint: {result.get('fingerprint', '?')}")
    print(f"Rows: {result.get('row_count', 0)}, Columns: {len(result.get('headers', []))}")
    print(f"Attributes defined: {len(result.get('attribute_definitions', []))}")

    refs = result.get("reference_values", {})
    if refs:
        total = sum(len(v) for v in refs.values())
        print(f"Reference values: {total} across {len(refs)} masters")

    cats = result.get("category_hierarchy", [])
    if cats:
        print(f"Category paths: {len(cats)}")

    print(f"\nOutput:")
    for kind, p in result.get("output_files", {}).items():
        print(f"  {kind}: {p}")

    if result.get("mapping_requires_review"):
        print(f"\n⚠ Low confidence mapping — check cache/{result['fingerprint']}.json")

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
