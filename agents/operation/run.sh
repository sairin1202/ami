#!/bin/zsh
# 在终端里运行 Ami：./run.sh（交互）、./run.sh "指令"（只做这一条）、./run.sh serve（只跑后台巡检）
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  # 代码要 Python 3.10+（系统自带的 python3 可能是 3.9）
  PY=$(for p in python3.14 python3.13 python3.12 python3.11 python3.10; do command -v $p && break; done)
  [ -n "$PY" ] || { echo "需要 Python 3.10 以上（brew install python）"; exit 1; }
  $PY -m venv .venv && .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python cli.py "$@"
