#!/usr/bin/env bash
# =============================================================================
# 拼拼电影票 后端 · 裸机部署脚本（gunicorn 由 systemd 托管，非 Docker）
#
# 做什么：备份 MySQL → 拉最新代码 → 装依赖 → migrate → 重启服务 → 冒烟自检
# 为什么：线上是裸机 systemd 起的 gunicorn（带 --preload），Docker 版镜像
#        ENTRYPOINT(entrypoint.sh) 里的自动 migrate 在这里根本不触发，所以
#        「备份 + 拉代码 + migrate + 重启」必须自己跑。本脚本把这套固化成一条命令，
#        以后每次发版只跑 ./deploy.sh，不必手敲 migrate。
#
# 用法：
#   1) 改下面「配置区」默认值（或用环境变量覆盖），放到服务器上
#   2) chmod +x deploy.sh
#   3) 干跑确认动作：DRY_RUN=1 ./deploy.sh
#   4) 正式执行：    ./deploy.sh
#      免交互/CI：   FORCE=1 ./deploy.sh
#
# 重启策略（脚本自动择一，优先级从高到低）：
#   A) RESTART_CMD 非空        → 直接执行它（最高优先级，逃生舱）。
#   B) SYSTEMD_SERVICE 非空    → systemctl restart <svc>（裸机 systemd 推荐）。
#        注：systemd restart 会整体重启进程，--preload 也能加载到最新代码，
#            这正是比 SIGHUP 热重载更可靠的地方（SIGHUP 在 --preload 下不重载代码）。
#   C) Gunicorn 自带 pid 进程  → 优雅停(SIGHUP)；仅未用 systemd 时兜底。
#
# 安全：migrate 前 mysqldump 全量备份（防删列等不可逆迁移）；git 仅 --ff-only；
#       migrate 后做模型/迁移漂移自检(--check)，有漂移即中止，绝不带着缺 schema 重启；
#       set -euo pipefail，任一关键步失败即停。
#
# 边界：本脚本只发【后端 ticket/】。C 端小程序 web/ 与 docs/ 不在本仓、不在 git，
#       需在微信开发者工具单独上传发布，不由本脚本处理。
# =============================================================================
set -euo pipefail

# ------------------------------- 配置区 --------------------------------------
APP_DIR="${APP_DIR:-/srv/ticket}"            # manage.py 所在目录（服务器上 = 仓库根 ticket/）
BRANCH="${BRANCH:-master}"
VENV_BIN="${VENV_BIN:-${APP_DIR}/venv/bin}"  # 虚拟环境 bin（用其下 python/pip）
# ⚠️ 裸机/生产走默认 config.settings（读 .env 的 MYSQL_ADDRESS 连 MySQL），别用 settings_dev(SQLite)
export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings}"

ENV_FILE="${ENV_FILE:-${APP_DIR}/.env}"      # 取数据库连接信息用于备份
BACKUP_DIR="${BACKUP_DIR:-${APP_DIR}/db_backups}"
KEEP_BACKUPS="${KEEP_BACKUPS:-10}"

# ---- 重启相关 ----
SYSTEMD_SERVICE="${SYSTEMD_SERVICE:-}"       # 裸机 systemd 服务名，如 movie-ticket；填了就走 systemctl restart
RESTART_CMD="${RESTART_CMD:-}"               # 完全自定义重启命令（最高优先级，非空即只用它）
GUNICORN_START_CMD="${GUNICORN_START_CMD:-}" # 未用 systemd 时的兜底拉起命令（建议带 --pid）
PID_FILE="${PID_FILE:-${APP_DIR}/gunicorn.pid}"
STOP_WAIT_SEC="${STOP_WAIT_SEC:-25}"         # 优雅停最长等待秒（应 > gunicorn --timeout）

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/api/health}"
DRY_RUN="${DRY_RUN:-0}"                      # =1 只打印不执行
FORCE="${FORCE:-0}"                          # =1 跳过人工确认
# -----------------------------------------------------------------------------

PY="${VENV_BIN}/python"
PIP="${VENV_BIN}/pip"
TS="$(date +%Y%m%d-%H%M%S)"
log()  { printf '\n\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
run()  { if [ "$DRY_RUN" = "1" ]; then printf '\033[2m  DRY-RUN: %s\033[0m\n' "$*"; else eval "$@"; fi; }

# 找 gunicorn master PID（仅模式 C 用）：优先 pidfile，否则取「父进程不是 gunicorn」的那个
find_gunicorn_master() {
  if [ -f "$PID_FILE" ]; then
    local p; p="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then echo "$p"; return 0; fi
  fi
  local pids pid ppid
  pids="$(pgrep -f 'gunicorn' || true)"
  [ -z "$pids" ] && return 1
  for pid in $pids; do
    ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if ! echo "$pids" | grep -qx "$ppid"; then echo "$pid"; return 0; fi
  done
  return 1
}

# ------------------------- 前置校验 -------------------------
cd "$APP_DIR"
[ -f manage.py ] || { echo "manage.py 不在 $APP_DIR，检查 APP_DIR"; exit 1; }
[ -x "$PY" ]     || { echo "找不到 venv python：$PY，检查 VENV_BIN"; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "$APP_DIR 不是 git 仓库"; exit 1; }

log "部署目录：$APP_DIR | 分支：$BRANCH | settings：$DJANGO_SETTINGS_MODULE"
log "当前提交：$(git rev-parse --short HEAD) — $(git log -1 --pretty=%s)"
[ -n "$(git status --porcelain)" ] && warn "工作区有未提交改动，git --ff-only pull 可能被拒（正常服务器应干净）。"

if [ "$FORCE" != "1" ] && [ "$DRY_RUN" != "1" ]; then
  printf '\n确认部署？将先备份数据库再拉代码/migrate/重启 [y/N] '
  read -r ans; [ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "已取消"; exit 1; }
fi

# ------------------------- 1. 备份 MySQL -------------------------
log "步骤1/5：备份数据库（migrate 前，防删列等不可逆迁移）"
if [ -f "$ENV_FILE" ]; then
  set -a; # shellcheck disable=SC1090
  . "$ENV_FILE" 2>/dev/null || true
  set +a
else
  warn "未找到 .env（$ENV_FILE），尝试用当前环境里的 MYSQL_*"
fi
DB_HOST="${MYSQL_HOST:-}"; DB_PORT="${MYSQL_PORT:-3306}"
if [ -n "${MYSQL_ADDRESS:-}" ]; then
  DB_HOST="${MYSQL_ADDRESS%%:*}"
  case "$MYSQL_ADDRESS" in *:*) DB_PORT="${MYSQL_ADDRESS##*:}" ;; esac
fi
DB_USER="${MYSQL_USERNAME:-${MYSQL_USER:-root}}"
DB_PASS="${MYSQL_PASSWORD:-${MYSQL_PASS:-}}"
DB_NAME="${MYSQL_DATABASE:-${MYSQL_DB:-}}"
BACKUP_FILE=""
mkdir -p "$BACKUP_DIR"
if [ -n "$DB_NAME" ] && command -v mysqldump >/dev/null 2>&1; then
  BACKUP_FILE="${BACKUP_DIR}/${DB_NAME}_${TS}.sql.gz"
  log "mysqldump → ${BACKUP_FILE}"
  export MYSQL_PWD="$DB_PASS"   # 从环境读取，命令串不含明文密码（DRY-RUN 回显更安全）
  run "mysqldump -h'$DB_HOST' -P'$DB_PORT' -u'$DB_USER' \
        --single-transaction --quick --routines --triggers --events \
        --databases '$DB_NAME' | gzip > '$BACKUP_FILE'"
  if [ "$DRY_RUN" != "1" ]; then
    ls -1t "$BACKUP_DIR"/*.sql.gz 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | while read -r old; do
      rm -f "$old"; log "清理旧备份：$old"; done
    log "备份完成，大小：$(du -h "$BACKUP_FILE" | cut -f1)"
  fi
else
  warn "缺 MYSQL_DATABASE 或无 mysqldump，跳过备份。"
  if [ "$FORCE" != "1" ] && [ "$DRY_RUN" != "1" ]; then
    printf '  未备份仍继续 migrate？[y/N] '; read -r a; [ "$a" = "y" ] || { echo "已中止"; exit 1; }
  fi
fi

# ------------------------- 2. 拉取最新代码 -------------------------
log "步骤2/5：git fetch + checkout $BRANCH + --ff-only pull"
run "git fetch --all --prune"
run "git checkout '$BRANCH'"
run "git pull --ff-only origin '$BRANCH'"
log "部署后提交：$(git rev-parse --short HEAD) — $(git log -1 --pretty=%s)"

# ------------------------- 3. 依赖 + 静态 + 迁移 + 漂移自检 -------------------------
log "步骤3/5：装依赖 + collectstatic(兜底) + migrate + 模型/迁移漂移自检"
run "'$PIP' install --quiet -r requirements.txt"
run "'$PY' manage.py collectstatic --noinput || true"
run "'$PY' manage.py migrate --noinput"        # 有则应用、无则跳过（幂等）
# 漂移自检：若模型改了但没有对应迁移文件，这里非 0 退出 → set -e 中止，
# 避免带着「代码引用了库里不存在的列」的状态重启对外服务。
run "'$PY' manage.py makemigrations --check --dry-run"

# ------------------------- 4. 重启服务 -------------------------
log "步骤4/5：重启服务"
if [ "$DRY_RUN" = "1" ]; then
  if [ -n "$RESTART_CMD" ]; then run "$RESTART_CMD"
  elif [ -n "$SYSTEMD_SERVICE" ]; then echo "  DRY-RUN: systemctl restart $SYSTEMD_SERVICE"
  else echo "  DRY-RUN: find_gunicorn_master → kill -HUP <pid>（兜底；--preload 请改用 systemd）"
  fi
elif [ -n "$RESTART_CMD" ]; then
  log "使用自定义 RESTART_CMD"
  eval "$RESTART_CMD"
elif [ -n "$SYSTEMD_SERVICE" ]; then
  command -v systemctl >/dev/null 2>&1 || { echo "系统无 systemctl，无法用 SYSTEMD_SERVICE"; exit 1; }
  log "systemctl restart $SYSTEMD_SERVICE"
  systemctl restart "$SYSTEMD_SERVICE"
  sleep 2
  if systemctl is-active --quiet "$SYSTEMD_SERVICE"; then
    log "$SYSTEMD_SERVICE 已 active ✔"
  else
    warn "$SYSTEMD_SERVICE 重启后非 active，最近日志："
    systemctl status "$SYSTEMD_SERVICE" --no-pager -n 20 || journalctl -u "$SYSTEMD_SERVICE" -n 30 --no-pager || true
    exit 1
  fi
else
  # C) 兜底：无 systemd，热重载 gunicorn workers（注意 --preload 下不重载代码，建议改走 A/B）
  if M_PID="$(find_gunicorn_master)"; then
    log "对 gunicorn master pid=$M_PID 发 SIGHUP 热重载（若带 --preload 未载新代码请改用 SYSTEMD_SERVICE/RESTART_CMD）"
    kill -HUP "$M_PID"; sleep 2
  elif [ -n "$GUNICORN_START_CMD" ]; then
    log "未发现运行中 gunicorn，按 GUNICORN_START_CMD 拉起"
    eval "$GUNICORN_START_CMD"; sleep 1
  else
    warn "找不到运行中的 gunicorn，且未配置 SYSTEMD_SERVICE/RESTART_CMD/GUNICORN_START_CMD，无法重启。"
    exit 1
  fi
fi

# ------------------------- 5. 冒烟自检 -------------------------
log "步骤5/5：健康自检"
run "'$PY' manage.py check"
if [ "$DRY_RUN" != "1" ]; then
  sleep 2
  if command -v curl >/dev/null 2>&1; then
    code="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 6 "$HEALTH_URL" || echo 000)"
    if [ "$code" = "000" ]; then
      warn "健康检查 $HEALTH_URL 无响应。确认服务是否起来/端口对：systemctl status ${SYSTEMD_SERVICE:-<svc>}；或 tail logs/app.log"
    else
      log "健康检查 HTTP $code ✔"
    fi
  fi
fi

log "完成 ✅  备份：${BACKUP_FILE:-未生成}   新提交：$(git rev-parse --short HEAD)"
echo  "提示：C端微信小程序(web/)与 docs/ 不在本仓、不在 git，需在微信开发者工具单独上传发布。"
