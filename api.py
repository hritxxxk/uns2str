import json
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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from graph import graph, vingpt_graph
from helpers import read_file
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


class ChatRequest(BaseModel):
    thread_id: str = Field(description="Thread ID from /vingpt/start")
    answers: dict[str, bool] = Field(description="Mapping of question keys to yes/no answers")


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


@app.on_event("startup")
async def check_tracing():
    ls_key = os.getenv("LANGSMITH_API_KEY")
    ls_tracing = os.getenv("LANGSMITH_TRACING") or os.getenv("LANGCHAIN_TRACING_V2")
    ls_project = os.getenv("LANGSMITH_PROJECT") or "pim-ingestion"
    pg_uri = os.getenv("POSTGRES_URI")

    if ls_key and ls_tracing:
        logger.info(f"LangSmith tracing enabled | project={ls_project}")
    elif ls_key and not ls_tracing:
        logger.info("LangSmith API key set but tracing disabled (set LANGSMITH_TRACING=true)")
    else:
        logger.info("LangSmith not configured — set LANGSMITH_API_KEY + LANGSMITH_TRACING=true for tracing")

    if pg_uri:
        logger.info(f"Postgres checkpointer configured | uri={pg_uri.split('@')[-1] if '@' in pg_uri else 'local'}")
    else:
        logger.info("Postgres not configured — using MemorySaver (data lost on restart)")


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


@app.post("/ingest/chat")
async def ingest_chat(req: ChatRequest):
    config = {"configurable": {"thread_id": req.thread_id}}

    try:
        current = vingpt_graph.get_state(config)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Thread '{req.thread_id}' not found")

    questions = current.values.get("pending_questions", [])
    if not questions:
        raise HTTPException(status_code=400, detail="No pending questions for this thread")

    core = dict(current.values.get("core_mappings", {}))
    custom = dict(current.values.get("custom_mappings", {}))

    from google import genai as _genai
    gclient = _genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    resolved_ids = set()
    for q in questions[:len(req.answers)]:
        user_text = list(req.answers.values())[questions.index(q)] if isinstance(req.answers, dict) else ""
        if isinstance(req.answers, list):
            idx = questions.index(q)
            user_text = req.answers[idx] if idx < len(req.answers) else ""

        intent_prompt = f"""Given this question and user response, determine intent.

Question: {q.get("text", "")}
User response: {user_text}

Return JSON: {{"intent": "approve" | "reject" | "alternative", "alternative_value": "user's suggested name or empty"}}"""
        resp = gclient.models.generate_content(
            model="gemini-2.5-flash-lite", contents=intent_prompt,
            config={"response_mime_type": "application/json"},
        )
        try:
            intent = json.loads(resp.text)
        except json.JSONDecodeError:
            intent = {"intent": "approve"}

        intent_type = intent.get("intent", "approve")
        q_type = q.get("type", "")
        q_target = q.get("target", "")
        q_column = q.get("column", "")

        if intent_type == "reject":
            if q_type == "core" and q_target in core:
                del core[q_target]
            elif q_type == "custom" and q.get("columns"):
                for c in q.get("columns", []):
                    custom.pop(c, None)
        elif intent_type == "alternative":
            alt = intent.get("alternative_value", "").strip()
            if alt and q_type == "core" and q_target in core:
                core[q_target] = alt
            elif alt and q_type == "custom" and q_column:
                custom[alt] = q_column
                custom.pop(q_column, None)

        resolved_ids.add(q["id"])

    remaining = [q for q in questions if q.get("id") not in resolved_ids]
    user_msg = f"User answered {len(resolved_ids)} questions."
    new_messages = current.values.get("messages", []) + [{"role": "user", "content": user_msg}]

    vingpt_graph.update_state(config, {
        "messages": new_messages,
        "core_mappings": core,
        "custom_mappings": custom,
        "pending_questions": remaining,
    })

    for _event in vingpt_graph.stream(None, config):
        pass

    state = vingpt_graph.get_state(config).values
    new_questions = state.get("pending_questions", [])
    core_final = state.get("core_mappings", {})
    custom_final = state.get("custom_mappings", {})
    files = state.get("generated_files", [])
    msgs = state.get("messages", [])

    if new_questions:
        return {
            "status": "pending",
            "thread_id": req.thread_id,
            "questions": new_questions,
            "messages": [m for m in msgs[-4:] if isinstance(m, dict)],
        }

    return {
        "status": "complete",
        "thread_id": req.thread_id,
        "core_mappings": core_final,
        "custom_attributes": list(custom_final.keys()),
        "generated_files": files,
        "messages": [m for m in msgs[-4:] if isinstance(m, dict)],
    }


# ─── VinGPT SSE Endpoint ────────────────────────────────────────

def _agent_state_for_triage():
    return {
        "messages": [], "source_path": "", "sheet_name": None,
        "structured_response": None, "remaining_steps": 25,
        "fingerprint": "", "is_known_schema": False, "headers": [],
        "header_row": 0, "data_start_row": 1, "metadata": [], "profiles": [],
        "sample_rows": [], "row_count": 0, "column_count": 0, "sheet_count": 0,
        "sheets": [], "category_candidates": [], "category_path_config": {},
        "category_hierarchy": [], "mapping": [], "mapping_requires_review": False,
        "core_column_detection": {}, "attribute_definitions": [],
        "reference_values": {}, "output_files": {}, "need_user_input": False,
        "validation_errors": [], "validation_message": "", "correction_cycle": 0,
        "error": None,
    }


@app.post("/vingpt/start")
async def vingpt_start(req: StartRequest):
    if not os.path.exists(req.file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {req.file_path}")

    thread_id = str(uuid.uuid4())

    async def event_stream():
        config = {"configurable": {"thread_id": thread_id}}

        yield f"data: {json.dumps({'type': 'progress', 'message': 'Opening file...'})}\n\n"

        from graph import triage_source
        ts = _agent_state_for_triage()
        ts["source_path"] = req.file_path
        ts["sheet_name"] = req.sheet_name
        ts = triage_source(ts)

        sheet = ts.get("sheet_name") or "auto-detected"
        cols = ts.get("column_count", 0)
        rows = ts.get("row_count", 0)
        yield f"data: {json.dumps({'type': 'progress', 'message': f'Detected {cols} columns on sheet \"{sheet}\" ({rows} data rows)'})}\n\n"

        yield f"data: {json.dumps({'type': 'progress', 'message': 'Analyzing column types and sample values...'})}\n\n"

        vingpt_initial = {
            "messages": [],
            "file_path": req.file_path,
            "sheet_name": ts.get("sheet_name"),
            "profile_data": {
                "headers": ts.get("headers", []),
                "sample_rows": ts.get("sample_rows", []),
                "row_count": ts.get("row_count", 0),
                "column_count": ts.get("column_count", 0),
                "profiles": ts.get("profiles", []),
            },
            "core_mappings": {},
            "custom_mappings": {},
            "mapping_confidence": {},
            "pending_questions": [],
            "generated_files": [],
        }

        for event in vingpt_graph.stream(vingpt_initial, config):
            for node_name, node_data in event.items():
                if node_name == "__interrupt__":
                    break
                if node_name == "analyze":
                    yield f"data: {json.dumps({'type': 'progress', 'message': 'Mapped core fields and identified custom attributes...'})}\n\n"
                elif node_name == "check_conf":
                    yield f"data: {json.dumps({'type': 'progress', 'message': 'Checked mapping confidence...'})}\n\n"
                elif node_name == "human_input":
                    yield f"data: {json.dumps({'type': 'progress', 'message': 'Preparing questions for you...'})}\n\n"
                elif node_name == "render":
                    yield f"data: {json.dumps({'type': 'progress', 'message': 'All checks passed. Generating templates...'})}\n\n"

        state = vingpt_graph.get_state(config).values
        questions = state.get("pending_questions", [])
        core = state.get("core_mappings", {})
        custom = state.get("custom_mappings", {})

        yield f"data: {json.dumps({'type': 'result', 'thread_id': thread_id, 'core_mappings': core, 'custom_attributes': list(custom.keys()), 'questions': questions})}\n\n"

        logger.info(f"vingpt | thread={thread_id} | file={req.file_path} | core={len(core)} | custom={len(custom)} | questions={len(questions)}")

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ─── Run (for development) ──────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
