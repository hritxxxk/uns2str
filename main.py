import os

from graph import graph


def run(path, sheet=None):
    initial = {
        "messages": [{"role": "user", "content": f"Profile and map this file: {path}" + (f" (sheet: {sheet})" if sheet else "")}],
        "source_path": path,
        "sheet_name": sheet,
        "structured_response": None,
        "remaining_steps": 25,
        "fingerprint": "",
        "is_known_schema": False,
        "headers": [],
        "header_row": 0,
        "data_start_row": 1,
        "metadata": [],
        "profiles": [],
        "sample_rows": [],
        "row_count": 0,
        "column_count": 0,
        "sheet_count": 0,
        "sheets": [],
        "category_candidates": [],
        "category_path_config": {},
        "category_hierarchy": [],
        "mapping": [],
        "mapping_requires_review": False,
        "core_column_detection": {},
        "attribute_definitions": [],
        "reference_values": {},
        "output_files": {},
        "need_user_input": False,
        "validation_errors": [],
        "validation_message": "",
        "correction_cycle": 0,
        "error": None,
    }

    config = {"configurable": {"thread_id": "pim-ingestion"}}

    for _ in graph.stream(initial, config):
        pass
    for _ in range(5):
        for _ in graph.stream(None, config):
            pass
        s = graph.get_state(config)
        if not s.next:
            break

    result = graph.get_state(config).values
    output = result.get("structured_response")
    files = result.get("output_files", {})
    errors = result.get("validation_errors", [])
    cycle = result.get("correction_cycle", 0)
    mapping = result.get("mapping", [])

    print(f"\nSource: {path}")
    if output and output.status == "success":
        print(f"Status: success")
        print(f"Attributes: {len(result.get('attribute_definitions', []))}")
        print(f"Categories: {len(result.get('category_hierarchy', []))}")
        print(f"References: {len(result.get('reference_values', {}))}")
    else:
        print(f"Status: validation failed ({cycle}/3 retries)")
        print(f"Mappings produced: {len(mapping)}")
        print(f"Categories: {len(result.get('category_hierarchy', []))}")

    if errors:
        for err in errors:
            print(f"  ⚠ {err['field']}: {err['issue'][:80]}")

    if files:
        print(f"Output:")
        for f in files.values():
            print(f"  {f}")
    else:
        print(f"No output files — use the API to review and approve:")
        print(f"  curl -X POST http://localhost:8000/ingest/start -H 'Content-Type: application/json' -d '{{\"file_path\": \"{path}\"}}'")
        print(f"  # Then approve with corrected mappings via /ingest/approve")

    if output and output.message:
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
