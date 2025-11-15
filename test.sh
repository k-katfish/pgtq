#!/bin/bash

ENVIRONMENT="venv"
source ./$ENVIRONMENT/bin/activate

pip install -e .
pip install -e .[test]

pytest tests

coverage run -m pytest tests

coverage report -m
coverage html
