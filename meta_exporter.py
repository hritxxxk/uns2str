"""Meta-exporter: generates marketplace-compliant output files from unified PIM data.

Uses JSON Schema Definition (JSD) files to map canonical PIM fields
to each target platform's expected column names and format.
"""

import json
import os
import csv
import logging

from openpyxl import Workbook

logger = logging.getLogger("pim_export")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)


def generate_target_export(pim_product_rows: list[dict], schema_path: str, output_path: str):
    """Map unified PIM product rows into a target marketplace format using a JSD file.

    Args:
        pim_product_rows: list of dicts keyed by canonical PIM field names
                         (code, sku_name, mrp, description, image_1..image_9, etc.)
        schema_path: path to the JSON Schema Definition file (e.g. target_schemas/shopify_schema.json)
        output_path: where to write the output file (e.g. output/export_shopify.csv)

    Returns:
        output_path
    """
    if not os.path.exists(schema_path):
        raise FileNotFoundError(f"Target schema configuration not found: {schema_path}")

    with open(schema_path, "r") as f:
        schema = json.load(f)

    target_mappings = schema.get("mappings", {})
    static_defaults = schema.get("static_defaults", {})
    file_format = schema.get("file_format", "csv")

    # Build ordered target headers
    target_headers = list(target_mappings.keys())
    for key in static_defaults:
        if key not in target_headers:
            target_headers.append(key)

    # Build output rows
    compiled_rows = []
    for pim_row in pim_product_rows:
        compiled = {}
        # Map canonical PIM fields to target headers
        for target_header, pim_key in target_mappings.items():
            if pim_key:
                compiled[target_header] = pim_row.get(pim_key, "")
            else:
                compiled[target_header] = ""
        # Apply static defaults (overwrites any mapped value)
        for target_header, default_val in static_defaults.items():
            compiled[target_header] = default_val
        compiled_rows.append(compiled)

    # Write output
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if file_format == "csv":
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=target_headers)
            writer.writeheader()
            writer.writerows(compiled_rows)
    else:
        wb = Workbook()
        ws = wb.active
        ws.append(target_headers)
        for row in compiled_rows:
            ws.append([row.get(h, "") for h in target_headers])
        wb.save(output_path)
        wb.close()

    logger.info(f"export | {schema['target_platform']} | {len(compiled_rows)} rows | {output_path}")
    return output_path
