import openpyxl
import hashlib
import json
import os
import csv


def fingerprint_headers(headers):
    raw = "|".join(sorted(headers))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_cached_mapping(fp):
    path = f"cache/{fp}.json"
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def save_cached_mapping(fp, data):
    os.makedirs("cache", exist_ok=True)
    with open(f"cache/{fp}.json", "w") as f:
        json.dump(data, f, indent=2)


def read_file(path, sheet_name=None):
    # CSV is plain text — read with csv module directly
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = [row for row in reader]
        return rows

    # Modern .xlsx is a zip file — openpyxl handles it
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet_name] if sheet_name else wb.active
        rows = [list(row) for row in ws.iter_rows(values_only=True)]
        wb.close()
        return rows
    except Exception:
        pass

    # Old .xls is an OLE container — xlrd handles it
    import xlrd
    wb = xlrd.open_workbook(path)
    ws = wb.sheet_by_name(sheet_name) if sheet_name else wb.sheet_by_index(0)
    rows = [[ws.cell_value(r, c) for c in range(ws.ncols)] for r in range(ws.nrows)]
    return rows


def get_headers_and_data(rows, header_row=0):
    headers = [str(c) if c is not None else "" for c in rows[header_row]]
    data = rows[header_row + 1:]
    return headers, data


def build_product_rows(headers, data, mapping, image_cols, core_cols=None):
    attr_names = [m.get("target_attribute", m.get("source_column")) for m in mapping]

    if core_cols:
        code_col = core_cols.get("code")
        name_col = core_cols.get("sku")
        mrp_col = core_cols.get("mrp")
        cat_col = core_cols.get("category")
    else:
        code_col = next((h for h in headers if h.lower() in ("code", "item code", "sku")), None)
        name_col = next((h for h in headers if h.lower() in ("product name", "item name", "sku name", "name", "title")), None)
        mrp_col = next((h for h in headers if h.lower() in ("mrp", "price", "retail price", "price retail")), None)
        cat_col = next((h for h in headers if "category" in h.lower()), None)

    rows = []
    for row in data:
        record = {}
        if cat_col and cat_col in headers and headers.index(cat_col) < len(row):
            record["category_path"] = row[headers.index(cat_col)]
        if code_col and code_col in headers and headers.index(code_col) < len(row):
            record["code"] = row[headers.index(code_col)]
        if name_col and name_col in headers and headers.index(name_col) < len(row):
            record["sku_name"] = row[headers.index(name_col)]
        if mrp_col and mrp_col in headers and headers.index(mrp_col) < len(row):
            record["mrp"] = row[headers.index(mrp_col)]

        for m in mapping:
            src = m.get("source_column")
            tgt = m.get("target_attribute", src)
            if src and src in headers:
                idx = headers.index(src)
                if idx < len(row):
                    record[tgt] = row[idx]

        for ii, ic in enumerate(image_cols[:9]):
            if ic in headers:
                idx = headers.index(ic)
                if idx < len(row):
                    record[f"image_{ii + 1}"] = row[idx]

        rows.append(record)
    return rows


def extract_image_columns(headers, mapping=None):
    keyword_cols = [h for h in headers if any(k in h.lower() for k in ("image", "img", "picture", "photo"))]

    if not mapping:
        return keyword_cols

    mapped_image_cols = []
    for m in mapping:
        tgt = m.get("target_attribute", "")
        src = m.get("source_column", "")
        if src and src in headers and ("image" in tgt.lower() or "img" in tgt.lower() or "photo" in tgt.lower() or "picture" in tgt.lower()):
            if src not in mapped_image_cols:
                mapped_image_cols.append(src)

    # Merge: prefer mapped order, append any keyword-only matches not already included
    seen = set(mapped_image_cols)
    for col in keyword_cols:
        if col not in seen:
            mapped_image_cols.append(col)
            seen.add(col)

    return mapped_image_cols
