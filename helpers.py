import openpyxl
import hashlib
import json
import os
import csv
from openpyxl import Workbook


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
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = [row for row in reader]
        return rows
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet_name] if sheet_name else wb.active
    rows = [list(row) for row in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


def get_headers_and_data(rows):
    headers = [str(c) if c is not None else "" for c in rows[0]]
    data = rows[1:]
    return headers, data


def profile_columns(headers, data):
    profiles = []
    for ci, h in enumerate(headers):
        vals = []
        for row in data:
            if ci < len(row) and row[ci] is not None and str(row[ci]).strip():
                vals.append(str(row[ci]))
        uniq = list(set(vals))
        if len(uniq) > 5:
            sample = uniq[:3] + uniq[-2:]
        else:
            sample = uniq
        profiles.append({
            "name": h,
            "non_null": len(vals),
            "unique": len(uniq),
            "sample": sample,
            "unique_values": uniq if len(uniq) <= 100 else []
        })
    return profiles


def detect_category_sheets(path):
    hierarchy = []
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for sn in wb.sheetnames:
            low = sn.lower()
            if any(k in low for k in ("hierarchy", "category", "mapping")):
                ws = wb[sn]
                for row in ws.iter_rows(min_row=2, values_only=True):
                    parts = [str(c).strip() for c in row if c is not None and str(c).strip()]
                    if len(parts) >= 2:
                        hierarchy.append(" > ".join(parts))
                break
        wb.close()
    except:
        pass
    return sorted(set(hierarchy))


def build_product_rows(headers, data, mapping, image_cols):
    attr_names = [m.get("target_attribute", m.get("source_column")) for m in mapping]

    code_col = next((h for h in headers if h.lower() in ("code", "item code", "sku")), None)
    name_col = next((h for h in headers if h.lower() in ("product name", "item name", "sku name", "name", "title")), None)
    mrp_col = next((h for h in headers if h.lower() in ("mrp", "price", "retail price", "price retail")), None)
    cat_col = next((h for h in headers if "category" in h.lower()), None)

    rows = []
    for row in data:
        record = {}
        if cat_col and headers.index(cat_col) < len(row):
            record["category_path"] = row[headers.index(cat_col)]
        if code_col and headers.index(code_col) < len(row):
            record["code"] = row[headers.index(code_col)]
        if name_col and headers.index(name_col) < len(row):
            record["sku_name"] = row[headers.index(name_col)]
        if mrp_col and headers.index(mrp_col) < len(row):
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


def render_category_xlsx(paths):
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "Category Path"
    for i, p in enumerate(paths, 2):
        ws.cell(row=i, column=1, value=p)
    return wb


def render_attribute_xlsx(defs):
    wb = Workbook()
    ws = wb.active
    cols = [
        "Attribute Name", "Short Name", "Display Name", "Attribute Type",
        "Attribute Data Type", "Constraint", "Length", "Mandatory",
        "Filter", "Editability", "Visibility", "Searchable", "Auto Translate",
        "Attribute Group", "Reference Master", "Reference Attribute", "Status"
    ]
    for i, c in enumerate(cols, 1):
        ws.cell(row=1, column=i, value=c)
    for ri, d in enumerate(defs, 2):
        for ci, c in enumerate(cols, 1):
            key = c.lower().replace(" ", "_")
            val = d.get(key)
            if val is not None:
                ws.cell(row=ri, column=ci, value=val)
    return wb


def render_reference_xlsx(refs):
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "Reference Master"
    row = 2
    for master, values in refs.items():
        ws.cell(row=row, column=1, value=master)
        row += 1
        for v in values:
            ws.cell(row=row, column=1, value=v)
            row += 1
    return wb


def render_product_xlsx(rows, attr_names):
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "Category Path"
    ws["B1"] = "Variant Attributes"
    ws["C1"] = "Parent SKU"
    ws["D1"] = "Code"
    ws["E1"] = "sku_name"
    ws["F1"] = "mrp"

    for i, name in enumerate(attr_names, 7):
        ws.cell(row=1, column=i, value=name)

    for i in range(1, 10):
        ws.cell(row=1, column=6 + len(attr_names) + i, value=f"image_{i}")

    for ri, record in enumerate(rows, 2):
        ws.cell(row=ri, column=1, value=record.get("category_path"))
        ws.cell(row=ri, column=4, value=record.get("code"))
        ws.cell(row=ri, column=5, value=record.get("sku_name"))
        ws.cell(row=ri, column=6, value=record.get("mrp"))

        for ci, name in enumerate(attr_names, 7):
            ws.cell(row=ri, column=ci, value=record.get(name))

        for ii in range(1, 10):
            key = f"image_{ii}"
            if key in record:
                ws.cell(row=ri, column=6 + len(attr_names) + ii, value=record[key])

    return wb
