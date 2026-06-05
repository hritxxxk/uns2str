from langchain_core.tools import tool
from openpyxl import Workbook


@tool
def render_category_xlsx(paths: list[str]) -> Workbook:
    """Generate category master workbook. One column: Category Path."""
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "Category Path"
    for i, p in enumerate(paths, 2):
        ws.cell(row=i, column=1, value=p)
    return wb


@tool
def render_attribute_xlsx(defs: list[dict]) -> Workbook:
    """Generate attribute master workbook. 17 columns matching PIM template."""
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


@tool
def render_reference_xlsx(refs: dict[str, list[str]]) -> Workbook:
    """Generate reference master workbook. One column per attribute master."""
    wb = Workbook()
    ws = wb.active
    all_values = list(refs.values())
    max_rows = max(len(v) for v in all_values) if all_values else 0
    for ci, (master, values) in enumerate(refs.items(), 1):
        ws.cell(row=1, column=ci, value=master)
        for ri, v in enumerate(values, 2):
            ws.cell(row=ri, column=ci, value=v)
    return wb


@tool
def render_product_xlsx(rows: list[dict], attr_names: list[str]) -> Workbook:
    """Generate product master workbook.
    
    6 fixed columns (Category Path, Variant Attributes, Parent SKU, Code, sku_name, mrp)
    + dynamic attribute columns + 9 image columns."""
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
