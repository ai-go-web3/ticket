#!/bin/bash
# 影片定时同步 wrapper —— 供 crontab 调用
# 每天 00:30 与 13:00 各执行一次，从麻花拉取热映+待映并入库。
# 手动执行： /Users/huanglong/myData/movie/ticket/scripts/sync_movies_cron.sh

set -u

PROJ="/Users/huanglong/myData/movie/ticket"
PY="$PROJ/venv/bin/python"
SETTINGS="config.settings_dev"
CITY="8"          # 成都 cityId
LOG_DIR="$PROJ/logs"
LOG="$LOG_DIR/sync_cron.log"

# cron 环境 PATH 极简，补上常见路径
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

mkdir -p "$LOG_DIR"
cd "$PROJ" || { echo "cd failed" >> "$LOG"; exit 1; }

{
  echo "==================================================="
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] sync_movies start (city=$CITY)"
  "$PY" manage.py sync_movies --settings="$SETTINGS" --city "$CITY"
  code=$?
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] sync_movies exit=$code"
  exit $code
} >> "$LOG" 2>&1

# 日志清理：只保留最近 30 天
find "$LOG_DIR" -name 'sync_cron.log' -mtime +30 -exec sh -c \
  'tail -n 2000 "$1" > "$1.tmp" && mv "$1.tmp" "$1"' _ {} \;
