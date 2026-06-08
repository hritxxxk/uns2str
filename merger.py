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


def _jaro_winkler(s1: str, s2: str) -> float:
    """Jaro-Winkler string similarity."""
    if not s1 or not s2:
        return 0.0
    s1, s2 = s1.upper(), s2.upper()
    if s1 == s2:
        return 1.0
    match_distance = max(len(s1), len(s2)) // 2 - 1
    match_distance = max(match_distance, 0)
    s1_matches = [False] * len(s1)
    s2_matches = [False] * len(s2)
    matches = 0
    transpositions = 0
    for i in range(len(s1)):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len(s2))
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    k = 0
    for i in range(len(s1)):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1
    jaro = (matches / len(s1) + matches / len(s2) + (matches - transpositions / 2) / matches) / 3
    prefix = 0
    for i in range(min(4, len(s1), len(s2))):
        if s1[i] == s2[i]:
            prefix += 1
        else:
            break
    return jaro + prefix * 0.1 * (1 - jaro)


def _row_quality(row: list[str], skip_indices: set = frozenset()) -> int:
    """Count non-empty fields in a row as a measure of data quality."""
    return sum(1 for i, val in enumerate(row) if val.strip() and i not in skip_indices)


def deduplicate_fuzzy(unified_path: str, threshold: float = 0.92) -> str:
    """Fuzzy-deduplicate near-duplicate codes using Jaro-Winkler similarity.

    Near-duplicate pairs are merged into a single "Golden Record" row:
    the row with the most non-empty fields is kept, combining fields
    from both rows (duplicate row fills missing fields in the golden record).

    Returns the same path.
    """
    import tempfile, shutil

    with open(unified_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = next(reader)
        rows = list(reader)

    code_idx = headers.index("code") if "code" in headers else 0
    code_idx = max(code_idx, 0)
    kept = []
    groups = []  # list of {"code": str, "row": list, "quality": int}

    for row in rows:
        code = row[code_idx].strip().upper() if code_idx < len(row) else ""
        if not code:
            kept.append(row)
            continue

        qty = _row_quality(row, {code_idx})
        matched = None

        for group in groups:
            if _jaro_winkler(code, group["code"]) >= threshold:
                matched = group
                break

        if matched is None:
            groups.append({"code": code, "row": row, "quality": qty})
        elif qty > matched["quality"]:
            # New row has better quality — merge into golden record
            for i in range(len(row)):
                if i == code_idx:
                    continue
                if row[i].strip() and (not matched["row"][i].strip() or i >= len(matched["row"])):
                    if i < len(matched["row"]):
                        matched["row"][i] = row[i]
            matched["quality"] = _row_quality(matched["row"], {code_idx})
        else:
            # Existing golden record has better quality — fill missing fields
            for i in range(len(matched["row"])):
                if i == code_idx:
                    continue
                if not matched["row"][i].strip() and i < len(row) and row[i].strip():
                    matched["row"][i] = row[i]
            matched["quality"] = _row_quality(matched["row"], {code_idx})

    deduped_count = len(rows) - len(groups) - (len(rows) - len([r for r in rows if not r[code_idx].strip()]))
    kept = [g["row"] for g in groups] + [r for r in rows if not r[code_idx].strip()]

    if len(kept) < len(rows):
        tmp_path = unified_path + ".tmp"
        with open(tmp_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(kept)
        shutil.move(tmp_path, unified_path)
        logger.info(f"fuzzy dedup: {len(rows)} -> {len(kept)} rows | jaro-winkler threshold={threshold}")

    return unified_path
