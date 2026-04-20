# Services

FastAPI services for the semantic photo search feature.

| Service       | Port  | Node  | Purpose                                        |
|---------------|-------|-------|------------------------------------------------|
| ingest-api    | 8004  | CPU   | Receives PhotoPrism webhook, enqueues jobs     |
| clip-api      | 8001  | CPU   | CLIP image + text embeddings                   |
| search-api    | 8010  | CPU   | Text query → Qdrant → (optional) rerank        |
| reranker-api  | 8020  | GPU   | Qwen3-VL-Reranker, enabled via lease           |

Shared logging, auth, and request-id middleware lives in `_shared/`.