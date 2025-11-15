#!/usr/bin/env bash

set -e

echo "Starting supervised pgtq demo..."

# Stop on Ctrl+C
trap "kill 0" EXIT

# Start controller (supervisor)
echo "Launching controller..."
python3 examples/sample_supervised_controller.py &
controller_pid=$!

# Give controller time to enqueue tasks & start supervisor
sleep 2

echo "Launching worker 1 (will crash after 3 tasks)..."
CRASH_AFTER_N=3 python3 examples/sample_supervised_worker.py &
worker1_pid=$!

echo "Launching worker 2 (normal)..."
python3 examples/sample_supervised_worker.py &
worker2_pid=$!

echo
echo "=============================================="
echo " Workers and Controller Running                "
echo " Worker #1 will crash after first task.        "
echo " Supervisor will detect stale tasks.           "
echo " Worker #2 will finish them.                   "
echo "=============================================="
echo

# Let everything run for a bit
sleep 90

echo "Demo complete — killing processes..."
kill $controller_pid $worker1_pid $worker2_pid 2>/dev/null || true
