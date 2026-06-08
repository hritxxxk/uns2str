import csv
import os
import json
import logging

from helpers import read_file

logger = logging.getLogger("pim_merger")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)


def read_chunks(file_path: str, chunk_size: int = 5000):
    """Lazily yield chunks of rows from a CSV or XLSX file.

    Each chunk is a list of dicts keyed by header.
    """
    ext = os.path.splitext(file_path)[1].lower()
    gen = read_file(file_path)
    try:
        raw_headers = next(gen)
    except StopIteration:
        return
    headers = [str(c).strip() if c else "" for c in raw_headers]
    chunk = []
    for row in gen:
        row_dict = {}
        for i, h in enumerate(headers):
            if i < len(row) and row[i] is not None:
                row_dict[h] = str(row[i]).strip()
            else:
                row_dict[h] = ""
        chunk.append(row_dict)
        if len(chunk) >= chunk_size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _transform_value(val: str, transform: str | None) -> str:
    """Apply a single transformation to a string value."""
    if not val:
        return val
    if not transform:
        return val
    t = transform.lower().strip()
    if t == "strip_and_uppercase":
        return val.strip().upper()
    if t == "strip_and_lowercase":
        return val.strip().lower()
    if t == "to_float":
        cleaned = val.replace(",", "").replace("$", "").replace("€", "").replace("£", "").replace("₹", "").strip()
        try:
            return str(float(cleaned))
        except ValueError:
            return val
    if t == "strip_html":
        import re
        return re.sub(r"<[^>]+>", "", val).strip()
    return val.strip()


def merge_sources(recipe: dict, source_dir: str, output_path: str) -> str:
    """Read each source file in chunks, rename columns per recipe, deduplicate, write unified CSV.

    recipe format (from build_union_recipe):
    {
        "target_mappings": {
            "code": {
                "sources": {"Shopify.csv": "Variant SKU", ...},
                "transformation": "strip_and_uppercase"
            },
            ...
        },
        "unified_headers": ["code", "sku_name", "mrp", ...]
    }

    Returns output_path.
    """
    target_mappings = recipe.get("target_mappings", {})
    unified_headers = recipe.get("unified_headers", list(target_mappings.keys()))

    seen_codes = set()
    total_written = 0
    source_file_names = set()
    for field, mapping in target_mappings.items():
        for src_file in mapping.get("sources", {}):
            source_file_names.add(src_file)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as out_f:
        writer = csv.writer(out_f)
        writer.writerow(unified_headers)

        for fname in sorted(source_file_names):
            fpath = os.path.join(source_dir, fname)
            if not os.path.exists(fpath):
                logger.warning(f"source file not found: {fpath}")
                continue

            for chunk in read_chunks(fpath, chunk_size=5000):
                for row in chunk:
                    mapped = {}
                    for target_field, mapping_info in target_mappings.items():
                        source_col = mapping_info.get("sources", {}).get(fname, "")
                        transform = mapping_info.get("transformation")
                        raw_val = row.get(source_col, "")
                        mapped[target_field] = _transform_value(raw_val, transform)

                    code_val = mapped.get("code", "")
                    if code_val:
                        code_key = code_val.strip().upper()
                        if code_key in seen_codes:
                            continue
                        seen_codes.add(code_key)

                    out_row = [mapped.get(h, "") for h in unified_headers]
                    writer.writerow(out_row)
                    total_written += 1

    logger.info(f"merged {total_written} rows (unique) to {output_path}")
    return output_path


def deduplicate_fuzzy(unified_path: str, threshold: float = 0.92) -> str:
    """Post-process: fuzzy-deduplicate near-duplicate codes using character overlap.

    Reads the unified CSV, groups rows whose code field has a high character overlap,
    keeps the first occurrence of each group, rewrites the file.
    Returns the same path.
    """
    import tempfile, shutil

    def overlap(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        a = a.upper()
        b = b.upper()
        common = sum(1 for c in a if c in b)
        return common / max(len(a), len(b))

    with open(unified_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = next(reader)
        rows = list(reader)

    code_idx = headers.index("code") if "code" in headers else 0
    kept = []
    seen_groups = []

    for row in rows:
        code = row[code_idx].strip().upper() if code_idx < len(row) else ""
        if not code:
            kept.append(row)
            continue
        is_dup = False
        for group_code in seen_groups:
            if overlap(code, group_code) >= threshold:
                is_dup = True
                break
        if not is_dup:
            seen_groups.append(code)
            kept.append(row)

    if len(kept) < len(rows):
        tmp_path = unified_path + ".tmp"
        with open(tmp_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(kept)
        shutil.move(tmp_path, unified_path)
        logger.info(f"fuzzy dedup: {len(rows)} -> {len(kept)} rows")

    return unified_path
