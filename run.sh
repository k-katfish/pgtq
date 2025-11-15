#!/bin/bash

if [ -z "$1" ]; then
  echo "Usage: $0 <command>"
  exit 1
fi

source ./venv/bin/activate

if [ "$1" == "test" ]; then
  pytest tests/
elif [ "$1" == "worker" ]; then
  python examples/sample_worker.py
elif [ "$1" == "worker2" ]; then
  python examples/sample_worker2.py
elif [ "$1" == "controller" ]; then
  python examples/sample_controller.py
elif [ "$1" == "controller2" ]; then
    python examples/sample_controller2.py
else
  echo "Unknown command: $1"
  exit 1
fi