# Shared middleware

Files in this directory are **copied** (not imported) into each service's image at
build time, keeping each service deployable independently while sharing the
template.

Currently shared:

| File | Used by |
| --- | --- |
| `metrics.py` | `search-api`, `clip-api`, `ingest-api`, `feature-worker`, `reranker-api` |

To wire it into a service:

1. Add `prometheus-client` to the service's `requirements.txt`.
2. Copy the file in the Dockerfile: `COPY _shared/metrics.py /app/metrics.py`.
   (Build context must include `services/`; see existing Dockerfiles.)
3. In FastAPI services: `from metrics import install_fastapi; install_fastapi(app, "<svc>")`.
4. In workers: `from metrics import install_worker; install_worker("<svc>", 9100)`.
