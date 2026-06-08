# Legacy CLI — Pipeline graph deprecated in favour of Interactive Graph.
# Use the FastAPI server + chat UI:
#   uvicorn api:app --reload
# Then open chat.html in a browser.
#
# Interactive 4-phase flow (primary onboarding method):
#   POST /interactive/start  (SSE streaming)
#   POST /interactive/respond
#   POST /interactive/status
