#!/usr/bin/env bash
# Fetch the pinned, checksum-verified embedding model the Dockerfile's
# `embedding-model` stage bakes into the image, so CI test jobs (which run on
# the host, not in the image) can import apps that fail fast without it.
# Keep repo/revision in sync with that Dockerfile stage.
set -euo pipefail
dest="${1:-/app/models/all-MiniLM-L6-v2}"
repo="Qdrant/all-MiniLM-L6-v2-onnx"
revision="5f1b8cd78bc4fb444dd171e59b18f3a3af89a079"
root="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$dest"
cd "$dest"
for f in config.json model.onnx special_tokens_map.json tokenizer.json tokenizer_config.json vocab.txt; do
    curl --proto "=https" -fsSL -o "$f" "https://huggingface.co/${repo}/resolve/${revision}/${f}"
done
sha256sum -c "$root/adapter/embedding/model.sha256"
