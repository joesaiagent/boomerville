#!/bin/bash
# Boomerville — start the FastAPI server
# Usage: ./start.sh
# Requires ANTHROPIC_API_KEY to be set in your environment (for Claude endpoints).

set -e

cd "$(dirname "$0")"

if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "⚠️  WARNING: ANTHROPIC_API_KEY is not set."
  echo "   /api/draft-letter and /api/search will return errors until you set it:"
  echo "   export ANTHROPIC_API_KEY=sk-ant-..."
  echo ""
fi

echo "Starting Boomerville on http://localhost:8000"
echo "Frontend: http://localhost:8000/"
echo ""

.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
