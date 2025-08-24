#!/usr/bin/env bash
set -e
python keep_alive.py &
python advisor.py
