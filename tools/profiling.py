import openpyxl
from langchain_core.tools import tool


@tool
def detect_data_sheet(path: str) -> dict:
    """Scan all sheets in a workbook and return the one with the most data cells.
    
    Returns the sheet name and cell count of the best candidate.
    Call this when no sheet name is provided."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    best = wb.sheetnames[0]
    best_size = 0
    for sn in wb.sheetnames:
        ws = wb[sn]
        first = next(ws.iter_rows(max_row=1, values_only=True), [])
        cols = sum(1 for c in first if c is not None)
        rows = sum(1 for _ in ws.iter_rows())
        size = cols * rows
        if size > best_size:
            best_size = size
            best = sn
    wb.close()
    return {"sheet": best, "cells": best_size}


@tool
def profile_columns(headers: list[str], rows: list[list]) -> list[dict]:
    """Scan columns and return stats: name, non_null count, unique count, sample values.
    
    Use this to understand the shape and content of source data before mapping."""
    profiles = []
    for ci, h in enumerate(headers):
        vals = []
        for row in rows:
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


@tool
def detect_category_structure(path: str, data_sheet: str) -> list[dict]:
    """Find sheets that may contain category hierarchy data.

    Returns a list of candidates. Each candidate has sheet name, column headers,
    and the first 3 data rows. The caller (LLM) decides which columns form the path."""
    candidates = []
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for sn in wb.sheetnames:
            if sn == data_sheet:
                continue
            ws = wb[sn]
            first = next(ws.iter_rows(max_row=1, values_only=True), [])
            headers = [str(c) if c is not None else "" for c in first]
            non_empty = sum(1 for h in headers if h.strip())
            if non_empty < 2:
                continue
            sample = []
            for row in ws.iter_rows(min_row=2, max_row=4, values_only=True):
                sample.append({headers[i]: str(row[i]) if i < len(row) and row[i] is not None else "" for i in range(len(headers))})
            candidates.append({
                "sheet": sn,
                "headers": headers,
                "rows": sample
            })
        wb.close()
    except Exception:
        pass
    return candidates
