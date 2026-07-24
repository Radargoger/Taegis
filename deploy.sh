#!/usr/bin/env sh
set -eu

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "usage: $0 <resource-group> <parameters-file> [--apply]" >&2
  exit 2
fi

RESOURCE_GROUP=$1
PARAMETERS_FILE=$2
MODE=${3:-what-if}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TEMPLATE="$SCRIPT_DIR/../main.bicep"

if [ ! -f "$PARAMETERS_FILE" ]; then
  echo "parameters file does not exist" >&2
  exit 2
fi

if [ "$MODE" = "--apply" ]; then
  az deployment group create \
    --resource-group "$RESOURCE_GROUP" \
    --template-file "$TEMPLATE" \
    --parameters "@$PARAMETERS_FILE"
elif [ "$MODE" = "what-if" ]; then
  az deployment group what-if \
    --resource-group "$RESOURCE_GROUP" \
    --template-file "$TEMPLATE" \
    --parameters "@$PARAMETERS_FILE"
else
  echo "third argument must be --apply when a real deployment is intended" >&2
  exit 2
fi

