#!/usr/bin/env bash
set -euo pipefail
: "${1:?usage: $0 <api-slug>}"
ghost discover --api "$1"
