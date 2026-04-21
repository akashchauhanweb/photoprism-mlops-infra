#!/usr/bin/env bash
# PhotoPrism MLOps — cluster bring-up orchestrator.
# Runs on node1. Assumes K8s cluster already exists (provisioned via Terraform + kubespray).

set -euo pipefail

# ---- Colors & helpers ----
readonly GREEN=$'\e[32m' YELLOW=$'\e[33m' RED=$'\e[31m' RESET=$'\e[0m'
log()  { echo -e "${GREEN}[bring_up]${RESET} $*" | tee -a "$LOG_FILE"; }
warn() { echo -e "${YELLOW}[bring_up]${RESET} $*" | tee -a "$LOG_FILE"; }
die()  { echo -e "${RED}[bring_up]${RESET} $*" | tee -a "$LOG_FILE" >&2; exit 1; }
export -f log warn die
export GREEN YELLOW RED RESET

# ---- Paths & logging ----
readonly REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
readonly LIB_DIR="$REPO_ROOT/scripts/lib"
readonly LOG_FILE="/tmp/bring_up_$(date +%Y%m%d_%H%M%S).log"
export REPO_ROOT LIB_DIR LOG_FILE
touch "$LOG_FILE"

# ---- Parse args ----
SINGLE_STEP=""
usage() {
    cat <<EOF
Usage: $0 [--step <name>]

Options:
  --step <name>   Run only one module (e.g. 40_services). Default: run all.
  -h, --help      Show this help.

Available modules:
  00_prereqs   10_security_groups   20_sealed_secrets
  30_databases 40_services          50_photoprism
  60_network_policies 70_reranker   75_feedback_trainer   80_grafana 85_dashboard 99_smoke
EOF
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        --step) SINGLE_STEP="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done

# ---- Load config ----
[[ -f "$REPO_ROOT/scripts/config.env" ]] \
    || die "scripts/config.env not found. Copy scripts/config.env.example and fill it in."
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/config.env"
export OS_CLOUD RERANKER_IP RERANKER_SSH_USER RERANKER_SSH_KEY
export DOCKER_HUB_USER SEALED_SECRETS_KEY_BACKUP
export PHOTOPRISM_INGEST_TAG INGEST_API_TAG CLIP_API_TAG
export SEARCH_API_TAG FEATURE_WORKER_TAG RERANKER_API_TAG

# Derive node1 floating IP at runtime
NODE1_FLOATING_IP="$(curl -s --max-time 5 https://api.ipify.org || echo '')"
[[ -n "$NODE1_FLOATING_IP" ]] || warn "could not auto-detect floating IP"
export NODE1_FLOATING_IP

log "log file: $LOG_FILE"
log "node1 floating IP: ${NODE1_FLOATING_IP:-unknown}"

# ---- Module list ----
readonly ALL_STEPS=(00_prereqs 10_security_groups 20_sealed_secrets
                    30_databases 40_services 50_photoprism
                    60_network_policies 70_reranker 75_feedback_trainer 80_grafana 85_dashboard 99_smoke)

run_step() {
    local step="$1"
    local module="$LIB_DIR/${step}.sh"
    [[ -f "$module" ]] || die "missing module: $module"
    log "=========================================="
    log "RUNNING: $step"
    log "=========================================="
    # shellcheck disable=SC1090
    source "$module"
}

if [[ -n "$SINGLE_STEP" ]]; then
    run_step "$SINGLE_STEP"
else
    for step in "${ALL_STEPS[@]}"; do run_step "$step"; done
fi

log ""
log "============================================"
log "  bring-up complete"
log "============================================"
# ---- Credentials (best-effort fetch; blanks if module wasn't run) ----
GRAFANA_PASS=$(kubectl -n monitoring get secret loki-stack-grafana \
    -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d || true)
DASH_TOKEN=$(kubectl -n kubernetes-dashboard get secret dashboard-admin-token \
    -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)

log ""
log "--- Services ---"
log "PhotoPrism   : http://${NODE1_FLOATING_IP}:30234"
log "               user: admin  pass: photoprism-admin"
log "Search API   : http://${NODE1_FLOATING_IP}:30810/search"
log "Qdrant       : http://${NODE1_FLOATING_IP}:30633/dashboard/"
log "Reranker API : http://${RERANKER_IP}:8000 (internal only)"
log "Feedback API : http://${RERANKER_IP}:8002/health (internal only)"
log ""
log "--- Platform ---"
log "Grafana      : http://${NODE1_FLOATING_IP}:30300"
log "               user: admin  pass: ${GRAFANA_PASS:-<not deployed>}"
log "K8s Dashboard: https://${NODE1_FLOATING_IP}:30443"
log "               token: ${DASH_TOKEN:-<not deployed>}"
log ""
log "log file     : $LOG_FILE"
