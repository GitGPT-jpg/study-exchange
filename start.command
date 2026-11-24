#!/bin/bash
cd "$(dirname "$0")" || exit 1
command -v python3 >/dev/null || { echo '请先安装 Python 3.10+'; exit 1; }
( for i in {1..25}; do curl -fsS "http://127.0.0.1:${STUDY_PORT:-8765}/api/state" >/dev/null 2>&1 && { open "http://127.0.0.1:${STUDY_PORT:-8765}"; exit; }; sleep 1; done ) &
exec python3 server.py
