# Monitoring stack patch — install notes

This patch wires the cluster onto **kube-prometheus-stack + Loki + Promtail** and
ships two Grafana dashboards (apps + infra). It is reproducible across teardowns.

## What's in here

```
scripts/
├── bring_up.sh                        (modified — registers 15_monitoring_stack)
├── teardown.sh                        (modified — uninstalls helm releases)
└── lib/
    ├── 10_security_groups.sh          (modified — adds Prometheus 30900 + MLflow 30500)
    ├── 15_monitoring_stack.sh         (NEW)
    ├── 22_build_images.sh             (modified — supports per-image Dockerfile path; adds 4 service builds)
    ├── 65_gpu_vm_prereqs.sh           (modified — also installs DCGM exporter container)
    ├── 80_grafana.sh                  (rewrite — handles Prom + Loki UIDs, imports all JSON in dashboards/)
    └── 99_smoke.sh                    (modified — adds monitoring smoke checks)

monitoring/
├── helm-values/
│   ├── kube-prometheus-stack.yaml     (NEW)
│   ├── loki.yaml                      (NEW)
│   └── promtail.yaml                  (NEW)
└── dashboards/
    ├── photoprism-services.json       (rewritten — Prom-based RED + ML signals)
    └── infra.json                     (NEW)

k8s/platform/
├── jobs/
│   └── kaniko-build-job.yaml.tpl      (modified — adds __DOCKERFILE__ placeholder)
├── monitoring/
│   ├── grafana-loki-datasource.yaml   (NEW)
│   ├── servicemonitors.yaml           (NEW)
│   └── dcgm-tunnel.yaml.tpl           (NEW — autossh tunnel for GPU VM /metrics)
├── network-policies.yaml              (modified — adds allow-monitoring-scrape)
└── services/
    ├── search-api.yaml                (modified — adds metrics port; image bumped to 0.4.0)
    ├── clip-api.yaml                  (modified — adds metrics port; image bumped to 0.2.0)
    ├── ingest-api.yaml                (modified — adds metrics port; image bumped to 0.2.0)
    └── feature-worker.yaml            (modified — adds Service for /metrics; image bumped to 0.4.0)

services/
├── _shared/
│   ├── README.md                      (modified)
│   └── metrics.py                     (NEW — shared instrumentation lib)
├── search-api/                        (Dockerfile + app.py + requirements.txt)
├── clip-api/                          (Dockerfile + app.py + requirements.txt)
├── ingest-api/                        (Dockerfile + app.py + requirements.txt)
├── feature-worker/                    (Dockerfile + worker.py + requirements.txt)
└── reranker-api/                      (Dockerfile + app.py + requirements.txt)
```

## How to apply

```bash
cd ~/photoprism-mlops-infra
tar xf /path/to/monitoring-patch.tar
git status        # eyeball the diff
git add -A && git commit -m "monitoring: kube-prometheus-stack + Loki + 2 dashboards + service instrumentation"
git push origin main
```

## Build context note (read me)

The 4 instrumented services now use `services/` as their build context (so the
Dockerfile can `COPY _shared/metrics.py`). The kaniko template was updated with a
`__DOCKERFILE__` placeholder; `22_build_images.sh` now supports image entries of
the form `<repo>:<tag>:<context-subpath>:<dockerfile-relative-to-context>`.

The existing 3-field form is still accepted (defaulting `Dockerfile` for the 4th
field), so `pg-backup` keeps working unchanged.

## Run order

Reranker-api lives on the GPU VM (built locally from `feedback_train.py`, not
via kaniko). When you next run `bring_up.sh` the orchestration becomes:

1. `10_security_groups` opens 30900 (Prometheus) on node1.
2. `15_monitoring_stack` (NEW) installs kube-prom, Loki, promtail; also detects &
   removes the legacy `loki-stack` helm release if present.
3. `22_build_images` builds the 4 service images (kaniko picks them up from
   `main`, so push the patch BEFORE running bring-up).
4. `40_services` deploys with the new manifests (metrics ports + bumped tags).
5. `65_gpu_vm_prereqs` installs DCGM exporter container on GPU VM.
6. `80_grafana` imports both dashboards and patches Prom + Loki UIDs.
7. `99_smoke` validates Prometheus targets, Grafana datasources, dashboards.

## Reranker

The reranker's docker image on the GPU VM needs to be rebuilt once for metrics
to flow. Easiest path: bump `RERANKER_API_TAG` in `scripts/config.env` (or the
default in `feedback_train.py`'s `_docker_build_push`) and push. The autossh
tunnel container in the cluster will scrape it via the in-cluster DNS name
`reranker-tunnel.monitoring:8000`.

Until that rebuild happens, the reranker panels will be empty and the
`reranker-tunnel` Prometheus target will show as down. Everything else still
works.

## URLs after bring-up

- Grafana       http://<NODE1_FLOATING_IP>:30300         (admin / fetched secret)
- Prometheus    http://<NODE1_FLOATING_IP>:30900
- existing      ... (PhotoPrism, Search, MLflow, etc.)

## Rollback

Everything new is additive. To roll back:

1. `helm -n monitoring uninstall kube-prom loki promtail`
2. `git revert <patch-commit>`
3. `./scripts/bring_up.sh --step 22_build_images` to rebuild the images at their
   previous tags. Then `--step 40_services` to redeploy.
