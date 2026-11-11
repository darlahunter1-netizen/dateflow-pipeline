#!/usr/bin/env bash
# Buffer CLI preflight.
#
# This script deliberately does NOT write the API key to disk. The official
# Buffer CLI reads BUFFER_API_KEY from the environment (it takes precedence
# over any config file), which is the recommended mode for CI.
set -euo pipefail

if ! command -v buffer >/dev/null 2>&1; then
    echo "Buffer CLI is not installed. Run: npm install -g @bufferapp/cli@2.3.0" >&2
    exit 1
fi

buffer --version

if [ -z "${BUFFER_API_KEY:-}" ]; then
    echo "BUFFER_API_KEY is not set; this is fine for a dry run, but production requires it." >&2
    exit 0
fi

# Do not print the key, the account email, or channel identifiers.
echo "Buffer CLI found. The API key will be read from BUFFER_API_KEY at runtime."
