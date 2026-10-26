#!/bin/bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"
unset PYTHONHOME PYTHONPATH

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)" /usr/bin/python3; do
  if [ -n "$candidate" ] && [ -x "$candidate" ] \
    && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [ -z "$PYTHON_BIN" ] && command -v brew >/dev/null 2>&1; then
  echo "缺少 Python 3.10+，正在用 Homebrew 安装…"
  brew install python || true
  PYTHON_BIN="$(brew --prefix)/bin/python3"
fi

if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "需要 Python 3.10 或更高版本。请先安装 Homebrew（https://brew.sh）后重新双击本文件。"
  read -r -p "按回车键关闭…"
  exit 1
fi

set +e
"$PYTHON_BIN" launcher.py "$@"
EXIT_CODE=$?
set -e
if [ "$EXIT_CODE" -ne 0 ]; then
  echo ""
  read -r -p "启动中止，请查看上面的信息，然后按回车键关闭…"
fi
exit "$EXIT_CODE"
