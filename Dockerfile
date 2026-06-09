FROM python:3.13-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM python:3.13-slim AS runtime

WORKDIR /app

COPY --from=base /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=base /usr/local/bin /usr/local/bin

COPY api.py interactive_graph.py interactive_state.py state.py agents.py helpers.py helpers_zip.py learning.py merger.py meta_exporter.py graph.py main.py ./
COPY tools/ ./tools/
COPY chat.html ./

RUN mkdir -p output cache uploads

ENV GEMINI_API_KEY=""
ENV PORT=8000

EXPOSE 8000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
