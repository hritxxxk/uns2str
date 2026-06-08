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

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from helpers import read_file
from learning import log_corrections
from state import ColumnMapping

# ─── Request / Response Schemas ─────────────────────────────────

class StatusRequest(BaseModel):
    thread_id: str = Field(description="Thread ID to query")


# ─── FastAPI Application ────────────────────────────────────────

app = FastAPI(
    title="PIM Ingestion API",
    description="Human-in-the-loop approval workflow for PIM data ingestion",
    version="1.0.0",
)

# Serve the chat frontend at root
@app.get("/")
async def serve_chat():
    return FileResponse("chat.html", media_type="text/html")

# Serve static files (output xlsx downloads, etc.)
os.makedirs("output", exist_ok=True)
app.mount("/output", StaticFiles(directory="output"), name="output")

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    import shutil
    safe_name = f"{uuid.uuid4().hex}_{file.filename}"
    dest = os.path.join(UPLOAD_DIR, safe_name)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    logger.info(f"upload | file={dest} | original={file.filename}")

    # If ZIP, run pre-processor pipeline
    if file.filename and file.filename.lower().endswith(".zip"):
        logger.info(f"upload | zip detected | running pre-processor")
        try:
            from helpers_zip import extract_zip, cleanup_temp, profile_files
            from agents import build_union_recipe
            from merger import merge_sources, deduplicate_fuzzy

            temp_dir, extracted = extract_zip(dest)
            profiles = profile_files(temp_dir, extracted)

            if not profiles:
                cleanup_temp(temp_dir)
                return {"path": dest, "filename": safe_name, "warning": "No supported files found in ZIP."}

            recipe = build_union_recipe(profiles)

            master_name = f"{uuid.uuid4().hex}_master.csv"
            master_path = os.path.join(UPLOAD_DIR, master_name)
            merge_sources(recipe, temp_dir, master_path)
            deduplicate_fuzzy(master_path)

            cleanup_temp(temp_dir)
            logger.info(f"upload | zip processed | master={master_path}")
            return {
                "path": master_path,
                "filename": master_name,
                "original_zip": safe_name,
                "files_found": list(profiles.keys()),
                "summary": recipe.get("summary", ""),
            }
        except Exception as exc:
            logger.warning(f"upload | zip processing failed: {exc}")
            import traceback
            traceback.print_exc()
            return {"path": dest, "filename": safe_name, "warning": f"ZIP processing failed: {exc}"}

    return {"path": dest, "filename": safe_name}


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


# def _build_initial_state(file_path: str, sheet_name: Optional[str] = None) -> dict:

#     return {
#         "messages": [
#             {
#                 "role": "user",
#                 "content": f"Profile and map this file: {file_path}"
#                 + (f" (sheet: {sheet_name})" if sheet_name else ""),
#             }
#         ],
#         "source_path": file_path,
#         "sheet_name": sheet_name,
#         "structured_response": None,
#         "remaining_steps": 25,
#         "fingerprint": "",
#         "is_known_schema": False,
#         "headers": [],
#         "header_row": 0,
#         "data_start_row": 1,
#         "metadata": [],
#         "profiles": [],
#         "sample_rows": [],
#         "row_count": 0,
#         "column_count": 0,
#         "sheet_count": 0,
#         "sheets": [],
#         "category_candidates": [],
#         "category_path_config": {},
#         "category_hierarchy": [],
#         "mapping": [],
#         "mapping_requires_review": False,
#         "core_column_detection": {},
#         "attribute_definitions": [],
#         "reference_values": {},
#         "output_files": {},
#         "need_user_input": False,
#         "human_approved": False,
#         "validation_errors": [],
#         "validation_message": "",
#         "correction_cycle": 0,
#         "error": None,
#     }


# def _serialize_mapping(mapping) -> list[dict]:

#     return [
#         {
#             "source_column": m.source_column,
#             "target_attribute": m.target_attribute,
#             "attribute_type": m.attribute_type,
#             "attribute_data_type": m.attribute_data_type,
#             "constraint": m.constraint,
#             "length": m.length,
#             "mandatory": m.mandatory,
#             "attribute_group": m.attribute_group,
#             "confidence": m.confidence,
#         }
#         for m in mapping
#     ]


# ─── Endpoints ──────────────────────────────────────────────────

# ── Legacy endpoints removed ────────────────────────────
# Pipeline (/ingest/start, /ingest/approve, /ingest/status)
# and VinGPT (/ingest/chat, /vingpt/start) are deprecated.
# Use /interactive/start, /interactive/respond, /interactive/status.
#
# @app.post("/ingest/start", response_model=StartResponse)
# def ingest_start(req: StartRequest):

#     if not os.path.exists(req.file_path):
#         raise HTTPException(status_code=404, detail=f"File not found: {req.file_path}")

#     thread_id = str(uuid.uuid4())
#     config = {"configurable": {"thread_id": thread_id}}
#     initial = _build_initial_state(req.file_path, req.sheet_name)

#     logger.info(f"start  | thread={thread_id} | file={req.file_path}")

#     for _event in graph.stream(initial, config):
#         pass

#     state = graph.get_state(config)
#     vals = state.values
#     mapping_raw = vals.get("mapping", [])
#     needs_human = vals.get("need_user_input", False)

#     err_count = len(vals.get("validation_errors", []))
#     logger.info(f"start  | thread={thread_id} | mappings={len(mapping_raw)} | errors={err_count} | human={needs_human} | status=pending_review")

#     return StartResponse(
#         thread_id=thread_id,
#         status="pending_review",
#         file_path=req.file_path,
#         sheet_name=vals.get("sheet_name"),
#         header_row=vals.get("header_row", 0),
#         data_start_row=vals.get("data_start_row", 0),
#         row_count=vals.get("row_count", 0),
#         column_count=vals.get("column_count", 0),
#         mapping=_serialize_mapping(mapping_raw),
#         core_column_detection=vals.get("core_column_detection", {}),
#         category_hierarchy=vals.get("category_hierarchy", []),
#         validation_errors=vals.get("validation_errors", []),
#         needs_human_input=needs_human,
#         correction_cycle=vals.get("correction_cycle", 0),
#     )


# @app.post("/ingest/approve", response_model=ApproveResponse)
# def ingest_approve(req: ApproveRequest):

#     config = {"configurable": {"thread_id": req.thread_id}}

#     try:
#         current_state = graph.get_state(config)
#     except Exception:
#         logger.warning(f"approve | thread={req.thread_id} | not found")
#         raise HTTPException(
#             status_code=404,
#             detail=f"Thread '{req.thread_id}' not found. Start a new ingestion first.",
#         )

#     logger.info(f"approve | thread={req.thread_id} | overrides={len(req.mapping)} mappings")

#     corrected_mapping = [
#         ColumnMapping(
#             source_column=m.source_column,
#             target_attribute=m.target_attribute,
#             attribute_type=m.attribute_type,
#             attribute_data_type=m.attribute_data_type,
#             constraint=m.constraint,
#             length=m.length,
#             mandatory=m.mandatory,
#             attribute_group=m.attribute_group,
#             confidence=m.confidence,
#         )
#         for m in req.mapping
#     ]

#     updates = {
#         "mapping": corrected_mapping,
#         "core_column_detection": req.core_column_detection
#         or current_state.values.get("core_column_detection", {}),
#         "category_hierarchy": req.category_hierarchy
#         or current_state.values.get("category_hierarchy", []),
#         "validation_errors": [],
#         "validation_message": "",
#         "correction_cycle": 0,
#         "need_user_input": False,
#         "human_approved": True,
#     }

#     graph.update_state(config, updates, as_node="mapper")

#     logger.info(f"approve | thread={req.thread_id} | resuming graph")

#     for _ in range(5):
#         for _event in graph.stream(None, config):
#             pass
#         s = graph.get_state(config)
#         if not s.next:
#             break

#     final_state = graph.get_state(config)
#     vals = final_state.values
#     output = vals.get("structured_response")
#     files = vals.get("output_files", {})

#     if output and output.status == "success":
#         log_corrections(
#             req.mapping,
#             vals.get("profiles", []),
#             fingerprint=vals.get("fingerprint"),
#         )
#         logger.info(f"approve | thread={req.thread_id} | success | files={list(files.values())}")
#         return ApproveResponse(
#             thread_id=req.thread_id,
#             status="success",
#             output_files=files,
#             attribute_count=output.attribute_count,
#             reference_count=output.reference_count,
#             category_count=output.category_count,
#             summary=output.message,
#         )

#     errors = vals.get("validation_errors", [])
#     error_summary = "; ".join(
#         f"{e.get('field', '')}: {e.get('issue', '')}" for e in errors[:3]
#     )
#     logger.warning(f"approve | thread={req.thread_id} | failed | {error_summary}")
#     return ApproveResponse(
#         thread_id=req.thread_id,
#         status="failed",
#         output_files=files,
#         summary=f"Approval applied but rendering failed: {error_summary or 'unknown error'}",
#     )


# @app.post("/ingest/status", response_model=StatusResponse)
# def ingest_status(req: StatusRequest):

#     thread_id = req.thread_id
#     config = {"configurable": {"thread_id": thread_id}}

#     try:
#         state = graph.get_state(config)
#     except Exception:
#         logger.info(f"status  | thread={thread_id} | not_found")
#         return StatusResponse(
#             thread_id=thread_id,
#             status="not_found",
#         )

#     vals = state.values
#     if not vals.get("source_path"):
#         logger.info(f"status  | thread={thread_id} | not_found (empty state)")
#         return StatusResponse(
#             thread_id=thread_id,
#             status="not_found",
#         )
#     output = vals.get("structured_response")
#     files = vals.get("output_files", {})

#     if output and output.status == "success":
#         return StatusResponse(
#             thread_id=thread_id,
#             status="completed",
#             next_nodes=list(state.next),
#             has_output_files=True,
#             output_files=files,
#             summary=output.message,
#         )

#     if state.next:
#         return StatusResponse(
#             thread_id=thread_id,
#             status="pending_review",
#             next_nodes=list(state.next),
#             has_output_files=bool(files),
#             output_files=files,
#         )

#     return StatusResponse(
#         thread_id=thread_id,
#         status="completed",
#         next_nodes=[],
#         has_output_files=bool(files),
#         output_files=files,
#         summary=vals.get("validation_message") or "Run completed without success output",
#     )


# @app.post("/ingest/chat")
# async def ingest_chat(req: ChatRequest):
#     config = {"configurable": {"thread_id": req.thread_id}}

#     try:
#         current = vingpt_graph.get_state(config)
#     except Exception:
#         raise HTTPException(status_code=404, detail=f"Thread '{req.thread_id}' not found")

#     questions = current.values.get("pending_questions", [])
#     if not questions:
#         raise HTTPException(status_code=400, detail="No pending questions for this thread")

#     core = dict(current.values.get("core_mappings", {}))
#     custom = dict(current.values.get("custom_mappings", {}))

#     from google import genai as _genai
#     gclient = _genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

#     resolved_ids = set()
#     for q in questions[:len(req.answers)]:
#         user_text = list(req.answers.values())[questions.index(q)] if isinstance(req.answers, dict) else ""
#         if isinstance(req.answers, list):
#             idx = questions.index(q)
#             user_text = req.answers[idx] if idx < len(req.answers) else ""

#         intent_prompt = f"""Given this question and user response, determine intent.

# Question: {q.get("text", "")}
# User response: {user_text}

# Return JSON: {{"intent": "approve" | "reject" | "alternative", "alternative_value": "user's suggested name or empty"}}"""
#         resp = gclient.models.generate_content(
#             model="gemini-2.5-flash-lite", contents=intent_prompt,
#             config={"response_mime_type": "application/json"},
#         )
#         try:
#             intent = json.loads(resp.text)
#         except json.JSONDecodeError:
#             intent = {"intent": "approve"}

#         intent_type = intent.get("intent", "approve")
#         q_type = q.get("type", "")
#         q_target = q.get("target", "")
#         q_column = q.get("column", "")

#         if intent_type == "reject":
#             if q_type == "core" and q_target in core:
#                 del core[q_target]
#             elif q_type == "custom" and q.get("columns"):
#                 for c in q.get("columns", []):
#                     custom.pop(c, None)
#         elif intent_type == "alternative":
#             alt = intent.get("alternative_value", "").strip()
#             if alt and q_type == "core" and q_target in core:
#                 core[q_target] = alt
#             elif alt and q_type == "custom" and q_column:
#                 custom[alt] = q_column
#                 custom.pop(q_column, None)

#         resolved_ids.add(q["id"])

#     remaining = [q for q in questions if q.get("id") not in resolved_ids]
#     user_msg = f"User answered {len(resolved_ids)} questions."
#     new_messages = current.values.get("messages", []) + [{"role": "user", "content": user_msg}]

#     vingpt_graph.update_state(config, {
#         "messages": new_messages,
#         "core_mappings": core,
#         "custom_mappings": custom,
#         "pending_questions": remaining,
#     })

#     for _event in vingpt_graph.stream(None, config):
#         pass

#     state = vingpt_graph.get_state(config).values
#     new_questions = state.get("pending_questions", [])
#     core_final = state.get("core_mappings", {})
#     custom_final = state.get("custom_mappings", {})
#     files = state.get("generated_files", [])
#     msgs = state.get("messages", [])

#     if new_questions:
#         return {
#             "status": "pending",
#             "thread_id": req.thread_id,
#             "questions": new_questions,
#             "messages": [m for m in msgs[-4:] if isinstance(m, dict)],
#         }

#     return {
#         "status": "complete",
#         "thread_id": req.thread_id,
#         "core_mappings": core_final,
#         "custom_attributes": list(custom_final.keys()),
#         "generated_files": files,
#         "messages": [m for m in msgs[-4:] if isinstance(m, dict)],
#     }


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


# @app.post("/vingpt/start")
# async def vingpt_start(req: StartRequest, request: Request):
#     if not os.path.exists(req.file_path):
#         raise HTTPException(status_code=404, detail=f"File not found: {req.file_path}")

#     auth_header = request.headers.get("authorization", "")
#     thread_id = str(uuid.uuid4())

#     async def event_stream():
#         config = {"configurable": {"thread_id": thread_id}}

#         yield f"data: {json.dumps({'type': 'progress', 'message': 'Opening file...'})}\n\n"

#         from graph import triage_source
#         ts = _agent_state_for_triage()
#         ts["source_path"] = req.file_path
#         ts["sheet_name"] = req.sheet_name
#         ts = triage_source(ts)

#         sample_rows = []
#         try:
#             from helpers import read_file, take_rows
#             gen = read_file(req.file_path, ts.get("sheet_name"))
#             skip = ts.get("data_start_row", 1)
#             for _ in range(skip):
#                 try: next(gen)
#                 except StopIteration: break
#             sample_rows = take_rows(gen, 5)
#         except Exception:
#             pass

#         sheet = ts.get("sheet_name") or "auto-detected"
#         cols = ts.get("column_count", 0)
#         rows = ts.get("row_count", 0)
#         yield f"data: {json.dumps({'type': 'progress', 'message': f'Detected {cols} columns on sheet \"{sheet}\" ({rows} data rows)'})}\n\n"

#         yield f"data: {json.dumps({'type': 'progress', 'message': 'Analyzing column types and sample values...'})}\n\n"

#         vingpt_initial = {
#             "messages": [],
#             "file_path": req.file_path,
#             "sheet_name": ts.get("sheet_name"),
#             "profile_data": {
#                 "headers": ts.get("headers", []),
#                 "sample_rows": sample_rows,
#                 "row_count": ts.get("row_count", 0),
#                 "column_count": ts.get("column_count", 0),
#                 "profiles": ts.get("profiles", []),
#             },
#             "core_mappings": {},
#             "custom_mappings": {},
#             "mapping_confidence": {},
#             "pending_questions": [],
#             "generated_files": [],
#             "jwt_token": auth_header,
#         }

#         for event in vingpt_graph.stream(vingpt_initial, config):
#             for node_name, node_data in event.items():
#                 if node_name == "__interrupt__":
#                     break
#                 if node_name == "analyze":
#                     yield f"data: {json.dumps({'type': 'progress', 'message': 'Mapped core fields and identified custom attributes...'})}\n\n"
#                 elif node_name == "check_conf":
#                     yield f"data: {json.dumps({'type': 'progress', 'message': 'Checked mapping confidence...'})}\n\n"
#                 elif node_name == "human_input":
#                     yield f"data: {json.dumps({'type': 'progress', 'message': 'Preparing questions for you...'})}\n\n"
#                 elif node_name == "render":
#                     yield f"data: {json.dumps({'type': 'progress', 'message': 'All checks passed. Generating templates...'})}\n\n"

#         state = vingpt_graph.get_state(config).values
#         questions = state.get("pending_questions", [])
#         core = state.get("core_mappings", {})
#         custom = state.get("custom_mappings", {})

#         yield f"data: {json.dumps({'type': 'result', 'thread_id': thread_id, 'core_mappings': core, 'custom_attributes': list(custom.keys()), 'questions': questions})}\n\n"

#         logger.info(f"vingpt | thread={thread_id} | file={req.file_path} | core={len(core)} | custom={len(custom)} | questions={len(questions)}")

#     return StreamingResponse(event_stream(), media_type="text/event-stream")


# ─── Interactive Graph Endpoints ────────────────────────────────

from interactive_graph import interactive_graph
from interactive_state import PhaseOutput


class InteractiveStartRequest(BaseModel):
    file_path: str = Field(description="Path to the source file (CSV, xlsx, xls)")
    sheet_name: Optional[str] = Field(None, description="Optional sheet name within workbook")


class InteractiveRespondRequest(BaseModel):
    thread_id: str = Field(description="Thread ID from /interactive/start")
    approved: bool = Field(description="User confirmed this phase's suggestions")
    feedback: str = Field(default="", description="Optional freeform edits or corrections")


def extract_tenant_from_jwt(jwt_token: str) -> str:
    """Extract tenant_id from JWT payload without verification.

    Base64-decodes the payload section of a JWT (header.payload.signature)
    and returns the tenant_id claim if present. Returns empty string on failure.
    """
    import base64
    try:
        parts = jwt_token.split(".")
        if len(parts) != 3:
            return ""
        payload = parts[1]
        padded = payload + "=" * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        claims = json.loads(decoded)
        return claims.get("tenant_id", claims.get("tenant", claims.get("org_id", "")))
    except Exception:
        return ""


def _build_interactive_initial(file_path: str, sheet_name: str | None, jwt: str) -> dict:
    return {
        "messages": [],
        "file_path": file_path,
        "sheet_name": sheet_name,
        "profile_data": None,
        "current_phase": "categories",
        "phases_completed": [],
        "categories": PhaseOutput(explanation="", reasoning="", suggestions=[], approved=False, user_feedback=""),
        "attributes": PhaseOutput(explanation="", reasoning="", suggestions=[], approved=False, user_feedback=""),
        "references": PhaseOutput(explanation="", reasoning="", suggestions=[], approved=False, user_feedback=""),
        "custom_mappings": {},
        "mapping_confidence": {},
        "generated_files": [],
        "jwt_token": jwt,
        "products": PhaseOutput(explanation="", reasoning="", suggestions=[], approved=False, user_feedback=""),
        "all_sheets": [],
        "sheet_merge": {},
        "core_mappings": {},
    }


@app.post("/interactive/start")
async def interactive_start(req: InteractiveStartRequest, request: Request):
    """Start a new interactive 4-phase onboarding session.

    SSE stream that:
    1. Runs triage (file profiling) and streams progress events
    2. Runs the first phase (categories) and streams the phase data
    3. Closes the stream — client then uses /interactive/respond
    """
    if not os.path.exists(req.file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {req.file_path}")

    auth_header = request.headers.get("authorization", "")
    thread_id = str(uuid.uuid4())
    tenant_id = extract_tenant_from_jwt(auth_header)
    config = {"configurable": {"thread_id": thread_id, "tenant_id": tenant_id}}

    async def event_stream():
        initial = _build_interactive_initial(req.file_path, req.sheet_name, auth_header)

        # Phase 0: triage — stream file profiling progress
        yield f"data: {json.dumps({'type': 'progress', 'message': 'Opening file...'})}\n\n"

        from graph import triage_source

        ts_state = triage_source({
            "source_path": req.file_path,
            "sheet_name": req.sheet_name,
            "messages": [{"role": "user", "content": "Profile file"}],
        })

        headers = ts_state.get("headers", [])
        row_count = ts_state.get("row_count", 0)
        column_count = ts_state.get("column_count", 0)
        sheet_name = ts_state.get("sheet_name") or "auto-detected"
        header_row = ts_state.get("header_row", 0)

        if row_count > 10000:
            yield f"data: {json.dumps({
                'type': 'background',
                'rows': row_count,
                'message': f'Your file has {row_count} rows. Background processing started — you will be notified when ready.',
            })}\n\n"

        yield f"data: {json.dumps({
            'type': 'progress',
            'message': f'I detected {column_count} columns on sheet \"{sheet_name}\" and verified Row {header_row + 1} contains your headers.',
            'details': {'columns': column_count, 'rows': row_count, 'sheet': sheet_name, 'header_row': header_row},
        })}\n\n"

        # Seed profile_data into interactive state
        sample_rows = []
        try:
            gen = read_file(req.file_path, sheet_name)
            dr = ts_state.get("data_start_row", header_row + 1)
            for _ in range(dr):
                next(gen, None)
            sample_rows = take_rows(gen, 5)
            sample_rows = [
                {headers[i]: str(row[i])[:60] for i in range(min(len(headers), len(row))) if row[i] is not None and str(row[i]).strip()}
                for row in sample_rows
            ]
        except Exception:
            pass

        initial["profile_data"] = {
            "headers": headers,
            "sample_rows": sample_rows,
            "row_count": row_count,
            "column_count": column_count,
            "header_row": header_row,
            "data_start_row": ts_state.get("data_start_row", header_row + 1),
        }
        initial["profile_data"]["category_hierarchy"] = []

        # Stream the graph — runs triage then hits interrupt on categories
        yield f"data: {json.dumps({'type': 'progress', 'message': 'Analysing column structure and values...'})}\n\n"
         # Auto-advance loop: after each interrupt, check if phase was auto-approved                                                                                                          
        max_phases = 4                                                                                                                                                                       
        phases_run = 0                                                                                                                                                                       
        while phases_run < max_phases:                                                                                                                                                       
            for event in interactive_graph.stream(None if phases_run > 0 else initial, config): 
                for node_name, node_data in event.items(): 
                    if node_name == "__interrupt__": 
                        break 

            state_vals = interactive_graph.get_state(config).values 
            current_phase = state_vals.get("current_phase", "categories") 
            phase_output = state_vals.get(current_phase, {}) 
            is_approved = phase_output.get("approved", False) 

            if is_approved: 
                phases_run += 1 
                logger.info(f"interactive | auto-advance | phase={current_phase} | approved=True") 
                continue 

            # Not approved — pause and send to frontend 
            yield f"data: {json.dumps({ 
                'type': 'phase', 
                'phase': current_phase, 
                'thread_id': thread_id, 
                'explanation': phase_output.get('explanation', ''), 
                'reasoning': phase_output.get('reasoning', ''), 
                'suggestions': phase_output.get('suggestions', []), 
                'message': state_vals.get('messages', [{}])[-1].get('content', '') if state_vals.get('messages') else '', 
            })}\n\n" 
            logger.info(f"interactive | thread={thread_id} | phase={current_phase} | paused") 
            break 


    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/interactive/respond")
async def interactive_respond(req: InteractiveRespondRequest, request: Request):
    """Resume an interactive session with user feedback.

    Accepts the user's approval + optional feedback text, updates the
    current phase's PhaseOutput, advances to the next phase, and returns
    the new phase's data (or completion result).
    """
    tenant_id = ""
    config = {"configurable": {"thread_id": req.thread_id}}

    try:
        current = interactive_graph.get_state(config)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Thread '{req.thread_id}' not found")

    vals = dict(current.values)
    tenant_id = extract_tenant_from_jwt(vals.get("jwt_token", ""))
    config["configurable"]["tenant_id"] = tenant_id

    phase = vals.get("current_phase", "categories")

    if phase == "complete":
        return {
            "status": "complete",
            "thread_id": req.thread_id,
            "generated_files": vals.get("generated_files", []),
        }

    # Update the current phase output with user feedback
    phase_output = dict(vals.get(phase, PhaseOutput(
        explanation="", reasoning="", suggestions=[], approved=False, user_feedback="",
    )))
    phase_output["approved"] = req.approved
    phase_output["user_feedback"] = req.feedback
    vals[phase] = PhaseOutput(**phase_output)

    # If approved, advance to next phase. If rejected, stay on same phase with feedback.
    if req.approved:
        completed = list(vals.get("phases_completed", []))
        completed.append(phase)
        vals["phases_completed"] = completed

        phase_order = ["categories", "attributes", "references", "products", "complete"]
        idx = phase_order.index(phase)
        next_phase = phase_order[idx + 1] if idx + 1 < len(phase_order) else "complete"
        vals["current_phase"] = next_phase
        logger.info(f"interactive | thread={req.thread_id} | tenant={tenant_id} | phase={phase} -> {next_phase} | approved")
    else:
        logger.info(f"interactive | thread={req.thread_id} | tenant={tenant_id} | phase={phase} | re-run with feedback")

    # Update state and resume
    interactive_graph.update_state(config, vals)

    # Stream until next interrupt or completion
    for event in interactive_graph.stream(None, config):
        pass

    new_vals = interactive_graph.get_state(config).values 
    new_phase = new_vals.get("current_phase", "complete") 

    # Check if the phase was auto-approved by the graph (bypass) 
    phase_output = new_vals.get(new_phase, {}) 
    if isinstance(phase_output, dict) and phase_output.get("approved", False): 
        phase_order = ["categories", "attributes", "references", "products", "complete"] 
        if new_phase in phase_order: 
            idx = phase_order.index(new_phase) 
            new_phase = phase_order[idx + 1] if idx + 1 < len(phase_order) else "complete" 
            logger.info(f"interactive | auto-advanced | phase={new_phase} | bypass detected") 

    if new_phase == "complete": 

        files = new_vals.get("generated_files", [])
        # Log approved mappings to LangSmith for future learning
        try:
            from helpers import fingerprint_headers
            profile = new_vals.get("profile_data", {})
            headers = profile.get("headers", [])
            if headers:
                fp = fingerprint_headers(headers)
                core = new_vals.get("core_mappings", {})
                custom = new_vals.get("custom_mappings", {})
                mapping_list = [
                    {"source_column": col, "target_attribute": tgt}
                    for tgt, col in {**core, **{v: k for k, v in custom.items()}}.items()
                ]
                log_corrections(mapping_list, profile.get("profiles", []), fingerprint=fp)
        except Exception as exc:
            logger.warning(f"interactive | log_corrections failed: {exc}")
        return {
            "status": "complete",
            "thread_id": req.thread_id,
            "phase": "complete",
            "generated_files": files,
            "message": new_vals.get("messages", [{}])[-1].get("content", "") if new_vals.get("messages") else "",
        }

    # Auto-advance loop: if we bypassed, keep streaming through subsequent phases 
    auto_advance_count = 0 
    while auto_advance_count < 4:  
        next_output = new_vals.get(new_phase, {})  
        is_empty = not next_output.get("explanation") and new_phase not in ("complete",) 
        is_approved = next_output.get("approved", False) 
        if not is_empty and not is_approved: 
            break 
        # Phase was also auto-approved — stream again to run the next phase 
        auto_advance_count += 1 
        vals["current_phase"] = new_phase 
        interactive_graph.update_state(config, vals) 
        for event in interactive_graph.stream(None, config): 
            pass 
        new_vals = interactive_graph.get_state(config).values 
        new_phase = new_vals.get("current_phase", "complete") 
        if new_phase == "complete": 
            break 

    if new_phase == "complete":                                                                                                                                                              
        files = new_vals.get("generated_files", [])                                                                                                                                          
        try:                                                                                                                                                                                 
            from helpers import fingerprint_headers                                                                                                                                          
            profile = new_vals.get("profile_data", {})                                                                                                                                       
            headers = profile.get("headers", [])                                                                                                                                             
            if headers:                                                                                                                                                                      
                fp = fingerprint_headers(headers) 
                core = new_vals.get("core_mappings", {}) 
                custom = new_vals.get("custom_mappings", {}) 
                mapping_list = [ 
                    {"source_column": col, "target_attribute": tgt} 
                    for tgt, col in {**core, **{v: k for k, v in custom.items()}}.items() 
                ] 
                log_corrections(mapping_list, profile.get("profiles", []), fingerprint=fp) 
        except Exception as exc: 
            logger.warning(f"interactive | log_corrections failed: {exc}") 
        return { 
            "status": "complete", 
            "thread_id": req.thread_id, 
            "phase": "complete", 
            "generated_files": files, 
            "message": new_vals.get("messages", [{}])[-1].get("content", "") if new_vals.get("messages") else "", 
        } 


    next_output = new_vals.get(new_phase, {}) 
    return { 
        "status": "continue", 
        "thread_id": req.thread_id, 
        "phase": new_phase, 
        "explanation": next_output.get("explanation", ""), 
        "reasoning": next_output.get("reasoning", ""), 
        "suggestions": next_output.get("suggestions", []), 
        "message": new_vals.get("messages", [{}])[-1].get("content", "") if new_vals.get("messages") else "",
    } 


@app.post("/interactive/status")
async def interactive_status(req: StatusRequest):
    """Check the status of an interactive session."""
    config = {"configurable": {"thread_id": req.thread_id}}

    try:
        state = interactive_graph.get_state(config)
    except Exception:
        return {
            "status": "not_found",
            "thread_id": req.thread_id,
        }

    vals = state.values
    tenant_id = extract_tenant_from_jwt(vals.get("jwt_token", ""))
    config["configurable"]["tenant_id"] = tenant_id

    vals = state.values
    phase = vals.get("current_phase", "unknown")
    files = vals.get("generated_files", [])

    return {
        "status": "complete" if phase == "complete" else "in_progress",
        "thread_id": req.thread_id,
        "phase": phase,
        "phases_completed": vals.get("phases_completed", []),
        "has_output_files": bool(files),
        "generated_files": files,
    }


# ─── Run (for development) ──────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
