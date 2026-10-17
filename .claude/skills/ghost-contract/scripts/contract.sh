#!/usr/bin/env bash
set -euo pipefail
: "${1:?usage: $0 <api-slug>}"
"${GHOST_BIN:-ghost}" contract --api "$1"
