#!/usr/bin/env bash
# setup.sh - Clone reference repositories into the reference/ directory

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REFERENCE_DIR="$SCRIPT_DIR/reference"

repos=(
  "visa/visa-vulnerability-agentic-harness"
  "langchain-ai/open-swe"
  "langchain-ai/deepagents"
  "karpathy/autoresearch"
  "NousResearch/hermes-agent"
  "aws-samples/bedrock-engineer"
  "openai/codex"
  "aaif-goose/goose"
)

mkdir -p "$REFERENCE_DIR"

for repo in "${repos[@]}"; do
  name="${repo##*/}"
  dest="$REFERENCE_DIR/$name"
  if [ -d "$dest" ]; then
    echo "Updating $repo..."
    git -C "$dest" pull --ff-only
  else
    echo "Cloning $repo..."
    gh repo clone "$repo" "$dest"
  fi
done

echo "Done. All reference repositories are in $REFERENCE_DIR"
