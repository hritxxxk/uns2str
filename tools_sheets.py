import os
import logging
from typing import Annotated
from langchain_core.tools import tool
from langgraph.types import Command
from langgraph.prebuilt import InjectedState
import polars as pl
from helpers_data_plane import get_lazy_frame, detect_header_row_and_headers

logger = logging.getLogger("pim_sheets")


@tool
def merge_sheets_programmatically(
    state: Annotated[dict, InjectedState],
    sheet_a: str,
    sheet_b: str,
    join_column: str,
) -> Command:
    """
    Programmatically joins two distinct sheets in the workbook using a shared relational column (e.g., SKU).
    Consolidates them into a single master file.

    Args:
        sheet_a: The name of the primary sheet (e.g., 'Product Details').
        sheet_b: The name of the secondary sheet to merge (e.g., 'Pricing').
        join_column: The column name present in both sheets used to align the rows.
    """
    file_path = state["file_path"]

    try:
        lf_a = get_lazy_frame(file_path, sheet_name=sheet_a)
        lf_b = get_lazy_frame(file_path, sheet_name=sheet_b)

        lf_joined = lf_a.join(lf_b, on=join_column, how="left")

        output_path = file_path.replace(".xlsx", "_merged_master.xlsx")
        df_result = lf_joined.collect(streaming=True)
        df_result.write_excel(output_path)

        completed = list(state.get("completed_phases", []))
        if "multi_sheet_join" not in completed:
            completed.append("multi_sheet_join")

        return Command(
            update={
                "file_path": output_path,
                "sheet_name": None,
                "completed_phases": completed,
            },
            value=(
                f"Successfully joined sheet '{sheet_a}' and '{sheet_b}' "
                f"using column '{join_column}'."
            ),
        )

    except Exception as e:
        return Command(value=f"Failed to programmatically join sheets: {str(e)}")
