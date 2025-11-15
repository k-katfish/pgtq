#!/usr/bin/env bash
set -e

echo "====================================================="
echo "  ASYNC PGTQ SUPERVISED DEMO"
echo "====================================================="
echo

trap "kill 0" EXIT

# Start controller
echo "Starting async controller..."
python3 examples/sample_async_controller.py &
controller_pid=$!

sleep 2

# Worker #1 – crashes after 1 task
echo "Starting async worker 1 (will crash after 1 task)..."
CRASH_AFTER_N=3 python3 examples/sample_async_worker.py &
worker1_pid=$!

# Worker #2 – normal worker
echo "Starting async worker 2..."
python3 examples/sample_async_worker.py &
worker2_pid=$!

echo
echo "=============================================="
echo " Workers running. Worker #1 will crash soon. "
echo " Supervisor will detect stale tasks.          "
echo " Worker #2 will complete them.                "
echo "=============================================="
echo

sleep 90

echo "Stopping demo..."
kill $controller_pid $worker1_pid $worker2_pid 2>/dev/null || true

echo "Done."
