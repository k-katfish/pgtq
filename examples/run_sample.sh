#!/usr/bin/env bash

set -e

echo "Starting pgtq demo..."

# Stop on Ctrl+C
trap "kill 0" EXIT

# Start controller (supervisor)
echo "Launching controller..."
python3 examples/sample_controller2.py &
controller_pid=$!

# Give controller time to enqueue tasks & start supervisor
sleep 2

echo "Launching worker 1..."
python3 examples/sample_worker.py &
worker1_pid=$!

echo "Launching worker 2..."
python3 examples/sample_worker2.py &
worker2_pid=$!

echo "Launching worker 3..."
python3 examples/sample_worker.py &
worker3_pid=$!

echo "Launching worker 4..."
python3 examples/sample_worker.py &
worker4_pid=$!

echo
echo "=============================================="
echo " Workers and Controller Running                "
echo "=============================================="
echo

# Let everything run for a bit
sleep 90

echo "Demo complete — killing processes..."
kill $controller_pid $worker1_pid $worker3_pid $worker4_pid $worker2_pid 2>/dev/null || true
