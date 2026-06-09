# PIM Ingestion Agent

Agentic workflow that converts messy eCommerce spreadsheets into 4 standardized PIM templates (categories, attributes, references, products).

## Quick Start

```bash
pip install -r requirements.txt
uvicorn api:app --reload
```

Requires `GEMINI_API_KEY` in environment.

## How it works

A cyclic LangGraph agent with 6 tools (profile, categories, attributes, references, products, render) processes files through conversational turns via SSE streaming. See `CURRENT_STATE.md` for architecture details.

## Endpoints

| POST /upload | Upload file |
|---|---|
| POST /interactive/start | Start session (SSE) |
| POST /interactive/respond | Send message (SSE) |
| POST /interactive/status | Check session |
