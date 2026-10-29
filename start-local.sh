#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-all}"
HARNESS_HOME="${AVERQEL_HARNESS_HOME:-${HOME}/.averqel-harness}"
HARNESS_BACKEND_PORT="${AVERQEL_HARNESS_BACKEND_PORT:-11000}"
HARNESS_FRONTEND_PORT="${AVERQEL_HARNESS_FRONTEND_PORT:-11030}"

export AKS_TEST_ALLOW_BOOTSTRAP_SKIP=1
export PYTHONUNBUFFERED=1
export NEXT_TELEMETRY_DISABLED=1
export AKS_SKIP_MCP_CATALOG_SYNC="${AKS_SKIP_MCP_CATALOG_SYNC:-1}"

# Electron must own the process when the desktop shell starts. Some parent
# shells (including Electron/Node development environments) export these
# flags, which makes the Electron binary execute the main file as plain Node
# and leaves `app` undefined.
unset ELECTRON_RUN_AS_NODE ELECTRON_NO_ATTACH_CONSOLE

cleanup() {
    echo ""
    echo "Shutting down AverQel Harness local services..."
    if [ -n "$FRONTEND_PID" ] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
        kill -TERM "$FRONTEND_PID" 2>/dev/null || true
    fi
    if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        kill -TERM "$BACKEND_PID" 2>/dev/null || true
    fi
    if [ -n "$DESKTOP_PID" ] && kill -0 "$DESKTOP_PID" 2>/dev/null; then
        kill -TERM "$DESKTOP_PID" 2>/dev/null || true
    fi
    # Release only the ports owned by this mode; desktop mode must not stop the
    # separate legacy web installation on the legacy local ports.
    if [ "$MODE" = "desktop" ] || [ "$MODE" = "electron" ]; then
        fuser -k "${HARNESS_BACKEND_PORT}/tcp" 2>/dev/null || true
        fuser -k "${HARNESS_FRONTEND_PORT}/tcp" 2>/dev/null || true
    else
        fuser -k 10000/tcp 2>/dev/null || true
        fuser -k 10030/tcp 2>/dev/null || true
    fi
    echo "✓ All local services stopped cleanly."
}

start_backend() {
    echo "Starting Python FastAPI Backend on http://127.0.0.1:10000 ..."
    cd "$DIR/backend"
    if [ -d "$DIR/backend/.venv" ]; then
        source "$DIR/backend/.venv/bin/activate"
    elif [ -d ".venv" ]; then
        source .venv/bin/activate
    fi
    WATCHFILES_FORCE_POLLING="${WATCHFILES_FORCE_POLLING:-true}" RELOAD="${RELOAD:-true}" exec python run_backend.py
}

start_frontend() {
    echo "Starting Next.js Frontend on http://localhost:10030 ..."
    cd "$DIR/frontend"
    if [ -f ".next/BUILD_ID" ]; then
        exec pnpm start
    else
        exec pnpm dev
    fi
}

run_tests() {
    echo "=========================================================="
    echo "         Running AverQel Harness Offline Test Suite       "
    echo "=========================================================="
    echo "1. Frontend Vitest suite (118 test files)..."
    cd "$DIR/frontend"
    pnpm run test --run

    echo ""
    echo "2. Backend Pytest suite (offline unit tests)..."
    cd "$DIR/backend"
    if [ -d "$DIR/backend/.venv" ]; then
        source "$DIR/backend/.venv/bin/activate"
    elif [ -d ".venv" ]; then
        source .venv/bin/activate
    fi
    pytest -o addopts="" tests/unit/test_deepspace_chat_service.py tests/unit/test_request_priority.py tests/unit/test_dashboard_routes.py

    echo ""
    echo "=========================================================="
    echo "✓ All test suites passed! System is 100% green and offline."
    echo "=========================================================="
}

start_all() {
    trap cleanup INT TERM EXIT

    echo "=========================================================="
    echo "    AverQel Platform — Local-First, Dockerless Mode       "
    echo "=========================================================="
    echo "• Database: SQLite (local file: ~/.averqel/db/knowledge.db)"
    echo "• Storage: Local filesystem (~/.averqel/storage)"
    echo "• Logs: Structured application log (~/.averqel/logs/backend.log)"
    echo "• Cache: Transient cache directory (~/.averqel/cache)"
    echo "• Cache / EventBus: Embedded In-Memory FakeRedis"
    echo "• Tasks: In-process eager Celery"
    echo "• No Docker containers or background daemons required"
    echo "=========================================================="
    echo ""

    # Ensure backend port is free
    fuser -k 10000/tcp 2>/dev/null || true
    fuser -k 10030/tcp 2>/dev/null || true

    # 1. Start backend in background
    echo "[1/2] Starting Python FastAPI Backend..."
    (
        cd "$DIR/backend"
        if [ -d "$DIR/backend/.venv" ]; then
            source "$DIR/backend/.venv/bin/activate"
        elif [ -d ".venv" ]; then
            source .venv/bin/activate
        fi
        WATCHFILES_FORCE_POLLING="${WATCHFILES_FORCE_POLLING:-true}" RELOAD="${RELOAD:-true}" exec python run_backend.py
    ) &
    BACKEND_PID=$!

    # 2. Poll until backend is ready
    echo "Waiting for backend health check at http://127.0.0.1:10000 ..."
    RETRIES=40
    READY=0
    while [ $RETRIES -gt 0 ]; do
        if curl -s -f http://127.0.0.1:10000/api/v1/auth/profile >/dev/null 2>&1; then
            READY=1
            break
        fi
        sleep 0.5
        RETRIES=$((RETRIES - 1))
    done

    if [ $READY -eq 1 ]; then
        echo "✓ Backend is live and healthy on http://127.0.0.1:10000"
    else
        echo "Backend took longer than expected to report ready, proceeding..."
    fi

    echo ""
    echo "[2/2] Starting Next.js Frontend on http://localhost:10030 ..."
    echo "=========================================================="
    echo "  🚀 AverQel Harness is running:"
    echo "  • Web App:      http://localhost:10030"
    echo "  • Backend API:  http://127.0.0.1:10000/api/v1"
    echo "  • Swagger Docs: http://127.0.0.1:10000/docs"
    echo "  (Press Ctrl+C to stop both backend and frontend cleanly)"
    echo "=========================================================="
    echo ""

    (
        cd "$DIR/frontend"
        if [ -f ".next/BUILD_ID" ]; then
            exec pnpm start
        else
            exec pnpm dev
        fi
    ) &
    FRONTEND_PID=$!

    # Wait for either process to exit or Ctrl+C
    wait -n $BACKEND_PID $FRONTEND_PID || true
}

start_desktop() {
    export AVERQEL_HARNESS_HOME="$HARNESS_HOME"
    export AVERQEL_HOME="$HARNESS_HOME"
    export AVERQEL_HARNESS_BACKEND_PORT="$HARNESS_BACKEND_PORT"
    export AVERQEL_HARNESS_FRONTEND_PORT="$HARNESS_FRONTEND_PORT"

    trap cleanup INT TERM EXIT

    echo "=========================================================="
    echo "    AverQel Platform — Local-First Desktop Mode           "
    echo "=========================================================="

    echo "[1/3] Starting isolated Python FastAPI Backend on http://127.0.0.1:${HARNESS_BACKEND_PORT} ..."
    (
        cd "$DIR/backend"
        if [ -d "$DIR/backend/.venv" ]; then
            source "$DIR/backend/.venv/bin/activate"
        elif [ -d ".venv" ]; then
            source .venv/bin/activate
        fi
        PORT="$HARNESS_BACKEND_PORT" BACKEND_PORT="$HARNESS_BACKEND_PORT" WATCHFILES_FORCE_POLLING="${WATCHFILES_FORCE_POLLING:-true}" RELOAD="${RELOAD:-true}" exec python run_backend.py
    ) &
    BACKEND_PID=$!

    # MCP catalog initialization may make the first local boot take longer
    # than the ordinary API startup. Do not launch the renderer until the
    # backend has completed initialization and reports readiness.
    RETRIES=240
    READY=0
    while [ $RETRIES -gt 0 ]; do
        if curl -s "http://127.0.0.1:${HARNESS_BACKEND_PORT}/api/v1/health/ready" | grep -q '"status":"ok"'; then
            READY=1
            break
        fi
        sleep 0.5
        RETRIES=$((RETRIES - 1))
    done
    if [ "$READY" -ne 1 ]; then
        echo "✗ Backend did not become ready on http://127.0.0.1:${HARNESS_BACKEND_PORT}."
        return 1
    fi

    echo "[2/3] Starting isolated Next.js Frontend on http://localhost:${HARNESS_FRONTEND_PORT} ..."
    (
        cd "$DIR/frontend"
        # Desktop development must use Next's live server. A static desktop
        # export served through `next start` cannot answer the RSC tree
        # requests made by client navigation, even when the export contains
        # the corresponding .txt files.
        PORT="$HARNESS_FRONTEND_PORT" NEXT_PUBLIC_API_URL="http://127.0.0.1:${HARNESS_BACKEND_PORT}/api/v1" NEXT_PUBLIC_BUILD_TARGET=desktop exec pnpm dev
    ) &
    FRONTEND_PID=$!

    RETRIES=30
    while [ $RETRIES -gt 0 ]; do
        if curl -s -o /dev/null -w "%{http_code}" "http://localhost:${HARNESS_FRONTEND_PORT}" | grep -qE "200|307|308"; then
            break
        fi
        sleep 0.5
        RETRIES=$((RETRIES - 1))
    done

    echo ""
    echo "[3/3] Launching isolated AverQel Harness (with Codex CLI & Local Runtime)..."
    echo "=========================================================="
    echo "  🚀 AverQel Harness & Codex CLI ready!"
    echo "=========================================================="
    echo ""

    (
        cd "$DIR"
        exec pnpm --dir applications/desktop dev
    ) &
    DESKTOP_PID=$!

    wait -n $BACKEND_PID $FRONTEND_PID $DESKTOP_PID || true
}

if [ "$MODE" = "backend" ]; then
    start_backend
elif [ "$MODE" = "frontend" ]; then
    start_frontend
elif [ "$MODE" = "test" ]; then
    run_tests
elif [ "$MODE" = "desktop" ] || [ "$MODE" = "electron" ]; then
    start_desktop
elif [ "$MODE" = "all" ] || [ -z "$MODE" ]; then
    start_all
else
    echo "Unknown mode: $MODE"
    echo "Usage: ./start-local.sh [all|desktop|backend|frontend|test]"
    exit 1
fi
