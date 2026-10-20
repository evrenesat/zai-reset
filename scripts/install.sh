#!/bin/sh
# Install zai-reset skills. Set ENGINE_DIR=<delegation engine directory> to
# also install the agentic-delegation skill from the engine (agent-exec side).
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
for root in "$HOME/.zcode/skills" "$HOME/.agents/skills"; do
  mkdir -p "$root"
  ln -sfn "$REPO/zai-quota" "$root/zai-quota"
done
if [ -n "$ENGINE_DIR" ] && [ -d "$ENGINE_DIR" ]; then
  for root in "$HOME/.zcode/skills" "$HOME/.agents/skills"; do
    ln -sfn "$ENGINE_DIR" "$root/agentic-delegation"
  done
  echo "installed: zai-quota + agentic-delegation"
else
  echo "installed: zai-quota (set ENGINE_DIR to also install agentic-delegation)"
fi
