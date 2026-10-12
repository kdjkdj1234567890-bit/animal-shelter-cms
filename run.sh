#!/bin/bash
# 동물구조단체 데모 실행 스크립트
cd "$(dirname "$0")"
python3 -c "import flask" 2>/dev/null || pip3 install --break-system-packages flask
python3 app.py
