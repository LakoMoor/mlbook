#!/usr/bin/env bash
# Запускается cron-ом. Активирует venv и проверяет обновления.
# Пример cron (каждый день в 8:00):
#   0 8 * * * /Users/lakomoor/mlbook/run_check.sh >> /Users/lakomoor/mlbook/logs/updates.log 2>&1

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

export DYLD_LIBRARY_PATH=/opt/homebrew/lib
source "$DIR/.venv/bin/activate"

python "$DIR/check_updates.py" "$@"
