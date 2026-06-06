import logging
import os
import uuid
from typing import Optional

logger = logging.getLogger("pim_api")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
logger.addHandler(handler)

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from graph import graph
from learning import log_corrections
from state import ColumnMapping

# ─── Request / Response Schemas ─────────────────────────────────

class StartRequest(BaseModel):
    file_path: str = Field(description="Path to the source file (CSV, xlsx, xls)")
    sheet_name: Optional[str] = Field(None, description="Optional sheet name within workbook")


class MappingOverride(BaseModel):
    source_column: str = Field(description="Original column name from source file")
    target_attribute: str = Field(description="Target PIM attribute name (snake_case)")
    attribute_type: str = Field(default="Textbox", description="Textbox, Dropdown, RichText, Textarea, MultiSelect, Date, Time")
    attribute_data_type: str = Field(default="varchar", description="varchar, varchar[], int, float, boolean, date")
    constraint: bool = Field(default=False, description="True if dropdown/multiselect")
    length: Optional[int] = Field(None, description="Max field length")
    mandatory: bool = Field(default=False, description="True if required")
    attribute_group: str = Field(default="Basic Information", description="Logical group name")
    confidence: float = Field(default=1.0, description="Human override confidence")


class ApproveRequest(BaseModel):
    thread_id: str = Field(description="Thread ID from /ingest/start")
    mapping: list[MappingOverride] = Field(description="Corrected column mappings")
    core_column_detection: Optional[dict[str, str]] = Field(None, description="Override core column detection")
    category_hierarchy: Optional[list[str]] = Field(None, description="Override category paths")


class StatusRequest(BaseModel):
    thread_id: str = Field(description="Thread ID to query")


class StatusResponse(BaseModel):
    thread_id: str
    status: str = Field(description="pending_review, completed, or not_found")
    next_nodes: list[str] = Field(default=[], description="Nodes still pending")
    has_output_files: bool = False
    output_files: dict[str, str] = {}
    summary: Optional[str] = None


class StartResponse(BaseModel):
    thread_id: str
    status: str = "pending_review"
    file_path: str
    sheet_name: Optional[str] = None
    header_row: int = 0
    data_start_row: int = 0
    row_count: int = 0
    column_count: int = 0
    mapping: list[dict] = []
    core_column_detection: dict[str, str] = {}
    category_hierarchy: list[str] = []
    validation_errors: list[dict] = []
    needs_human_input: bool = False
    correction_cycle: int = 0


class ApproveResponse(BaseModel):
    thread_id: str
    status: str = Field(description="success or failed")
    output_files: dict[str, str] = {}
    attribute_count: int = 0
    reference_count: int = 0
    category_count: int = 0
    summary: str = ""


# ─── FastAPI Application ────────────────────────────────────────

app = FastAPI(
    title="PIM Ingestion API",
    description="Human-in-the-loop approval workflow for PIM data ingestion",
    version="1.0.0",
)


def _build_initial_state(file_path: str, sheet_name: Optional[str] = None) -> dict:

    return {
        "messages": [
            {
                "role": "user",
                "content": f"Profile and map this file: {file_path}"
                + (f" (sheet: {sheet_name})" if sheet_name else ""),
            }
        ],
        "source_path": file_path,
        "sheet_name": sheet_name,
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
        "human_approved": False,
        "validation_errors": [],
        "validation_message": "",
        "correction_cycle": 0,
        "error": None,
    }


def _serialize_mapping(mapping) -> list[dict]:

    return [
        {
            "source_column": m.source_column,
            "target_attribute": m.target_attribute,
            "attribute_type": m.attribute_type,
            "attribute_data_type": m.attribute_data_type,
            "constraint": m.constraint,
            "length": m.length,
            "mandatory": m.mandatory,
            "attribute_group": m.attribute_group,
            "confidence": m.confidence,
        }
        for m in mapping
    ]


# ─── Endpoints ──────────────────────────────────────────────────

@app.post("/ingest/start", response_model=StartResponse)
def ingest_start(req: StartRequest):

    if not os.path.exists(req.file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {req.file_path}")

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    initial = _build_initial_state(req.file_path, req.sheet_name)

    logger.info(f"start  | thread={thread_id} | file={req.file_path}")

    for _event in graph.stream(initial, config):
        pass

    state = graph.get_state(config)
    vals = state.values
    mapping_raw = vals.get("mapping", [])
    needs_human = vals.get("need_user_input", False)

    err_count = len(vals.get("validation_errors", []))
    logger.info(f"start  | thread={thread_id} | mappings={len(mapping_raw)} | errors={err_count} | human={needs_human} | status=pending_review")

    return StartResponse(
        thread_id=thread_id,
        status="pending_review",
        file_path=req.file_path,
        sheet_name=vals.get("sheet_name"),
        header_row=vals.get("header_row", 0),
        data_start_row=vals.get("data_start_row", 0),
        row_count=vals.get("row_count", 0),
        column_count=vals.get("column_count", 0),
        mapping=_serialize_mapping(mapping_raw),
        core_column_detection=vals.get("core_column_detection", {}),
        category_hierarchy=vals.get("category_hierarchy", []),
        validation_errors=vals.get("validation_errors", []),
        needs_human_input=needs_human,
        correction_cycle=vals.get("correction_cycle", 0),
    )


@app.post("/ingest/approve", response_model=ApproveResponse)
def ingest_approve(req: ApproveRequest):

    config = {"configurable": {"thread_id": req.thread_id}}

    try:
        current_state = graph.get_state(config)
    except Exception:
        logger.warning(f"approve | thread={req.thread_id} | not found")
        raise HTTPException(
            status_code=404,
            detail=f"Thread '{req.thread_id}' not found. Start a new ingestion first.",
        )

    logger.info(f"approve | thread={req.thread_id} | overrides={len(req.mapping)} mappings")

    corrected_mapping = [
        ColumnMapping(
            source_column=m.source_column,
            target_attribute=m.target_attribute,
            attribute_type=m.attribute_type,
            attribute_data_type=m.attribute_data_type,
            constraint=m.constraint,
            length=m.length,
            mandatory=m.mandatory,
            attribute_group=m.attribute_group,
            confidence=m.confidence,
        )
        for m in req.mapping
    ]

    updates = {
        "mapping": corrected_mapping,
        "core_column_detection": req.core_column_detection
        or current_state.values.get("core_column_detection", {}),
        "category_hierarchy": req.category_hierarchy
        or current_state.values.get("category_hierarchy", []),
        "validation_errors": [],
        "validation_message": "",
        "correction_cycle": 0,
        "need_user_input": False,
        "human_approved": True,
    }

    graph.update_state(config, updates, as_node="mapper")

    logger.info(f"approve | thread={req.thread_id} | resuming graph")

    for _ in range(5):
        for _event in graph.stream(None, config):
            pass
        s = graph.get_state(config)
        if not s.next:
            break

    final_state = graph.get_state(config)
    vals = final_state.values
    output = vals.get("structured_response")
    files = vals.get("output_files", {})

    if output and output.status == "success":
        log_corrections(
            req.mapping,
            vals.get("profiles", []),
            fingerprint=vals.get("fingerprint"),
        )
        logger.info(f"approve | thread={req.thread_id} | success | files={list(files.values())}")
        return ApproveResponse(
            thread_id=req.thread_id,
            status="success",
            output_files=files,
            attribute_count=output.attribute_count,
            reference_count=output.reference_count,
            category_count=output.category_count,
            summary=output.message,
        )

    errors = vals.get("validation_errors", [])
    error_summary = "; ".join(
        f"{e.get('field', '')}: {e.get('issue', '')}" for e in errors[:3]
    )
    logger.warning(f"approve | thread={req.thread_id} | failed | {error_summary}")
    return ApproveResponse(
        thread_id=req.thread_id,
        status="failed",
        output_files=files,
        summary=f"Approval applied but rendering failed: {error_summary or 'unknown error'}",
    )


@app.post("/ingest/status", response_model=StatusResponse)
def ingest_status(req: StatusRequest):

    thread_id = req.thread_id
    config = {"configurable": {"thread_id": thread_id}}

    try:
        state = graph.get_state(config)
    except Exception:
        logger.info(f"status  | thread={thread_id} | not_found")
        return StatusResponse(
            thread_id=thread_id,
            status="not_found",
        )

    vals = state.values
    if not vals.get("source_path"):
        logger.info(f"status  | thread={thread_id} | not_found (empty state)")
        return StatusResponse(
            thread_id=thread_id,
            status="not_found",
        )
    output = vals.get("structured_response")
    files = vals.get("output_files", {})

    if output and output.status == "success":
        return StatusResponse(
            thread_id=thread_id,
            status="completed",
            next_nodes=list(state.next),
            has_output_files=True,
            output_files=files,
            summary=output.message,
        )

    if state.next:
        return StatusResponse(
            thread_id=thread_id,
            status="pending_review",
            next_nodes=list(state.next),
            has_output_files=bool(files),
            output_files=files,
        )

    return StatusResponse(
        thread_id=thread_id,
        status="completed",
        next_nodes=[],
        has_output_files=bool(files),
        output_files=files,
        summary=vals.get("validation_message") or "Run completed without success output",
    )


# ─── Run (for development) ──────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
