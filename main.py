import os
from state import AgentState
from graph import graph


def run(path, sheet=None):
    initial: AgentState = {
        "messages": [],
        "source_path": path,
        "sheet_name": sheet,
        "fingerprint": "",
        "is_known_schema": False,
        "headers": [],
        "header_row": 0,
        "data_start_row": 1,
        "metadata": [],
        "sample_rows": [],
        "profiles": [],
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
    print(f"Fingerprint: {result['fingerprint']}")
    print(f"Rows: {result['row_count']}, Columns: {len(result['headers'])}")
    print(f"Attributes defined: {len(result['attribute_definitions'])}")

    if result.get("reference_values"):
        total = sum(len(v) for v in result["reference_values"].values())
        print(f"Reference values: {total} across {len(result['reference_values'])} masters")

    if result.get("category_hierarchy"):
        print(f"Category paths: {len(result['category_hierarchy'])}")

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
