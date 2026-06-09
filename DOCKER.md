# Docker Guide — PIM Ingestion Agent

## 1. Build the image

```bash
docker build -t pim-agent .
```

This reads the `Dockerfile`, installs dependencies from `requirements.txt`, copies the code, and produces a tagged image called `pim-agent`.

## 2. Run the container

```bash
docker run -p 8000:8000 \
  -e GEMINI_API_KEY="your-key-here" \
  -e LANGSMITH_API_KEY="optional" \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/cache:/app/cache \
  -v $(pwd)/uploads:/app/uploads \
  pim-agent
```

| Flag | Purpose |
|---|---|
| `-p 8000:8000` | Maps host port 8000 to container port 8000 |
| `-e GEMINI_API_KEY=...` | Sets env variable inside the container |
| `-v host:container` | Mounts directories so output/cache/uploads persist on your host |

Without the `-v` mounts, data written inside the container is lost when it stops.

## 3. Verify it's running

```bash
curl http://localhost:8000/           # → HTML (chat frontend)
curl http://localhost:8000/interactive/status -X POST \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "test"}'           # → {"status":"not_found",...}
```

## 4. Useful commands

| Command | What it does |
|---|---|
| `docker ps` | List running containers |
| `docker stop pim-agent` | Stop the container (by name) |
| `docker logs pim-agent` | View logs (replace with container ID from `docker ps`) |
| `docker images` | List built images |
| `docker rmi pim-agent` | Remove the image |
| `docker system prune` | Clean up unused images/containers/cache |

## Docker concepts (short version)

**Image** — a snapshot of the filesystem + metadata. Read-only. Built from a `Dockerfile`.

**Container** — a running instance of an image. Has its own filesystem, network, and process space. Writes to the container filesystem are lost when the container is removed — unless you use a **volume** (`-v`) to mount a host directory.

**Multi-stage build** — the `Dockerfile` uses two stages: `base` installs Python packages, `runtime` copies only the compiled packages + app code. Final image is smaller (~200MB instead of ~1GB).

**Port mapping (`-p`)** — containers have their own network namespace. `-p 8000:8000` says "forward host port 8000 to container port 8000."

## Why use Docker for this project

- Eliminates "works on my machine" — the exact Python version, system libs, and package versions are baked into the image
- Single command to deploy anywhere (your laptop, a VPS, Cloud Run, etc.)
- No need to manage a Python venv or worry about conflicting package versions
