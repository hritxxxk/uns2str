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
    """Lazy generator yielding one row at a time. Use take_rows() for partial reads.

    xlsx uses openpyxl read_only (streams from disk, low memory).
    csv uses csv.reader (lazy by nature).
    xls (xlrd) loads fully — legacy format limitation.
    """
    import itertools
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        with open(path, "rb") as _f:
            raw = _f.read(8192)
        _enc = "cp1252"
        for _candidate in ["utf-8-sig", "cp1252", "latin-1"]:
            try:
                raw.decode(_candidate)
                _enc = _candidate
                break
            except (UnicodeDecodeError, LookupError):
                continue
        # Prefer detected encoding over default cp1252 if confident
        if _enc == "cp1252":
            try:
                from charset_normalizer import detect as _cd
                _r = _cd(raw)
                if _r.get("encoding") and _r.get("confidence", 0) >= 0.9:
                    try:
                        raw.decode(_r["encoding"])
                        _enc = _r["encoding"]
                    except (UnicodeDecodeError, LookupError):
                        pass
            except Exception:
                pass
        with open(path, encoding=_enc, errors="replace") as f:
            yield from csv.reader(f)
        return
    if ext in (".xlsx", ".xls"):
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            ws = wb[sheet_name] if sheet_name else wb.active
            try:
                for row in ws.iter_rows(values_only=True):
                    yield list(row)
            finally:
                wb.close()
            return
        except Exception:
            pass
        import xlrd
        wb = xlrd.open_workbook(path)
        ws = wb.sheet_by_name(sheet_name) if sheet_name else wb.sheet_by_index(0)
        for r in range(ws.nrows):
            yield [ws.cell_value(r, c) for c in range(ws.ncols)]
        return
    raise ValueError(f"Unsupported file extension: {ext}")


def take_rows(rows, n):
    """Return first n rows from a generator as a list. Stops early if exhausted."""
    import itertools
    return list(itertools.islice(rows, n))


def get_headers_and_data(rows, header_row=0):
    headers = [str(c) if c is not None else "" for c in rows[header_row]]
    data = rows[header_row + 1:]
    return headers, data


def build_product_rows_streaming(headers, data, mapping, image_cols, core_cols=None):
    """Generator version — yields one record at a time, never holds all rows in memory."""
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
            if tgt in ("code", "sku_name", "mrp", "category_path"):
                continue
            if src and src in headers:
                idx = headers.index(src)
                if idx < len(row):
                    record[tgt] = row[idx]

        for ii, ic in enumerate(image_cols[:9]):
            if ic in headers:
                idx = headers.index(ic)
                if idx < len(row):
                    record[f"image_{ii + 1}"] = row[idx]

        yield record


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
            if tgt in ("code", "sku_name", "mrp", "category_path"):
                continue
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


BLANK_TEMPLATES_DIR = "blank-templates"


def download_blank_template(auth_header: str, template_type="category", lang_code="en", timezone="Asia/Calcutta"):
    import urllib.request
    configs = {
        "category": {
            "api_url": "https://uat-api.vinpim.com/api/pie/v1/download/download-template",
            "module": "master",
            "submodule": "product-master",
            "category_id": [None],
            "route": "https://uat.vinpim.com/pim/master/product-master?is_enabled=true",
            "filename": "Category_template.xlsx",
        },
        "attribute": {
            "api_url": "https://uat-api.vinpim.com/api/pie/v1/download/download-template",
            "module": "attribute",
            "submodule": "product-attribute",
            "category_id": [],
            "route": "https://uat.vinpim.com/pim/attribute/product-attribute?is_enabled=true",
            "filename": "Attribute_template.xlsx",
        },
        "product": {
            "api_url": "https://uat-api.vinpim.com/api/pdatg/v1/product/generate-master-template",
            "module": "",
            "submodule": "",
            "category_id": [],
            "route": "",
            "filename": "Product_template.xlsx",
        },
    }
    cfg = configs.get(template_type, configs["category"])
    os.makedirs(BLANK_TEMPLATES_DIR, exist_ok=True)
    if template_type == "product":
        body = json.dumps({}).encode()
    else:
        body = json.dumps({
            "lang_code": lang_code,
            "timezone": timezone,
            "product": "pim",
            "module": cfg["module"],
            "submodule": cfg["submodule"],
            "reference_master_id": "",
            "category_id": cfg["category_id"],
            "route": cfg["route"],
        }).encode()
    req = urllib.request.Request(
        cfg["api_url"],
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": auth_header,
            "Origin": "https://uat.vinpim.com",
            "Referer": "https://uat.vinpim.com/",
        },
    )
    resp = json.loads(urllib.request.urlopen(req).read())
    urls = resp.get("data", {}).get("url", [])
    url = urls[0] if isinstance(urls, list) and urls else urls
    url = url.replace("\\u0026", "&")
    local_path = os.path.join(BLANK_TEMPLATES_DIR, cfg["filename"])
    urllib.request.urlretrieve(url, local_path)
    return local_path


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
