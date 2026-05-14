#!/usr/bin/env bash
# Start the PM Trading Brain server on DGX Spark
# Usage: bash start_brain.sh [--install]

set -euo pipefail

BRAIN_DIR="/home/sankerkr/brain"
DATA_DIR="/home/sankerkr/brain_data"
VENV="/home/sankerkr/brain-env"
PORT=8100

# Install dependencies if --install flag
if [[ "${1:-}" == "--install" ]]; then
    echo "Installing brain dependencies..."
    pip install --user chromadb sentence-transformers fastapi 'uvicorn[standard]' httpx
    echo "Dependencies installed."
fi

# Ensure data directory exists
mkdir -p "$DATA_DIR"

# Kill any existing brain server
if lsof -ti :$PORT >/dev/null 2>&1; then
    echo "Killing existing brain server on port $PORT..."
    lsof -ti :$PORT | xargs kill 2>/dev/null || true
    sleep 1
fi

cd "$BRAIN_DIR"

echo "Starting PM Trading Brain on port $PORT..."
echo "  ChromaDB: $DATA_DIR/chromadb"
echo "  Skills DB: $DATA_DIR/skills.sqlite3"
echo "  Embedding: sentence-transformers/all-MiniLM-L6-v2"
echo "  Qwen: http://localhost:8000"

export BRAIN_CHROMA_PATH="$DATA_DIR/chromadb"
export BRAIN_SQLITE_PATH="$DATA_DIR/skills.sqlite3"
export BRAIN_EMBEDDING_MODEL="sentence-transformers/all-MiniLM-L6-v2"
export BRAIN_EMBEDDING_DEVICE="cpu"
export BRAIN_QWEN_URL="http://localhost:8000/v1/chat/completions"
export BRAIN_QWEN_MODEL="Qwen/Qwen3-8B"

nohup "$VENV/bin/python" -m uvicorn brain_server:app --host 0.0.0.0 --port $PORT \
    >> "$DATA_DIR/brain_server.log" 2>&1 &

echo $! > "$DATA_DIR/brain_server.pid"
echo "Brain server started (PID: $(cat $DATA_DIR/brain_server.pid))"
echo "Log: $DATA_DIR/brain_server.log"
echo "Health check: curl http://localhost:$PORT/brain/health"
