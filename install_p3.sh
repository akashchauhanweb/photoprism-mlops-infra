#!/usr/bin/env bash
# install_p3.sh — apply reranker optimization patch.
set -euo pipefail
cd "$(dirname "$0")"
[[ -f scripts/bring_up.sh ]] || { echo "run from ~/photoprism-mlops-infra"; exit 1; }

# Bump search-api tag to 0.4.1
sed -i 's|SEARCH_API_TAG:-0.4.0|SEARCH_API_TAG:-0.4.1|' scripts/lib/22_build_images.sh
sed -i 's|akashweb/search-api:0.4.0|akashweb/search-api:0.4.1|' k8s/platform/services/search-api.yaml
# Bump reranker Makefile to 0.1.6
sed -i 's|^TAG\s*?=.*|TAG   ?= 0.1.6|' services/reranker-api/Makefile
echo "tag bumps applied"
