#!/usr/bin/env bash
set -euo pipefail
: "${1:?usage: $0 <api-slug>}"
ghost scan --api "$1"
