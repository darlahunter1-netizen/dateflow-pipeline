#!/bin/bash
set -e
# Setup Buffer CLI config and credentials
mkdir -p ~/.config/buffer

# BUFFER_API_KEY comes from GitHub secrets as env var
# If not present (e.g. local dev), pipeline.py passes it directly
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
    echo "⚠️ BUFFER_API_KEY not set in this step (will use env var from next step)"
fi

# Run doctor but don't fail if it reports issues (the main publish step passes BUFFER_API_KEY directly)
if command -v buffer &> /dev/null; then
    buffer doctor 2>&1 || echo "(doctor warnings ignored — key will be passed at publish time)"
fi
