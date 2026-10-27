#!/bin/bash
set -e
# Setup Buffer CLI config and credentials
mkdir -p ~/.config/buffer

if [ -n "$BUFFER_API_KEY" ]; then
    cat > ~/.config/buffer/config.json <<EOF
{
  "apiKey": "${BUFFER_API_KEY}",
  "organizationId": "${BUFFER_ORG_ID:-6ad678fa27469e130f893e13}",
  "outputFormat": "json"
}
EOF
echo "✅ Buffer config written"
else
    echo "⚠️ BUFFER_API_KEY not set (will pass directly at publish step)"
fi

if command -v buffer &> /dev/null; then
    buffer doctor 2>&1 || echo "(doctor warnings ignored)"
fi
