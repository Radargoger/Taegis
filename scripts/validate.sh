#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOLUTION_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

python3 -m json.tool "$SOLUTION_DIR/workflow.json" >/dev/null
python3 -m json.tool "$SOLUTION_DIR/parameters.example.json" >/dev/null

if command -v bicep >/dev/null 2>&1; then
  bicep build "$SOLUTION_DIR/main.bicep" --outfile /tmp/socradar-taegis-main.json
elif command -v az >/dev/null 2>&1; then
  mkdir -p /tmp/socradar-taegis-azure-config
  AZURE_CONFIG_DIR=/tmp/socradar-taegis-azure-config \
    AZURE_CORE_COLLECT_TELEMETRY=no \
    az bicep build --file "$SOLUTION_DIR/main.bicep" \
    --outfile /tmp/socradar-taegis-main.json
else
  echo "bicep CLI not found" >&2
  exit 1
fi

python3 -m json.tool /tmp/socradar-taegis-main.json >/dev/null
echo "Azure template validation passed"
