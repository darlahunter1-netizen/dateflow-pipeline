#!/bin/bash
set -e
# Setup Buffer CLI and credentials
mkdir -p ~/.config/buffer
cat > ~/.config/buffer/config.json <<EOF
{
  "apiKey": "${BUFFER_API_KEY}",
  "organizationId": "${BUFFER_ORG_ID:-6ad678fa27469e130f893e13}",
  "outputFormat": "json"
}
EOF
echo "✅ Buffer config written"
buffer doctor
