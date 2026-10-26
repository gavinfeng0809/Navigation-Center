#!/usr/bin/env bash
# notice-hub 一键部署 / 升级脚本（在服务器上执行）
#
#   ./deploy.sh              构建并启动
#   ./deploy.sh --no-build   只重启，不重新构建镜像
#
# 特点：
#   - 幂等，可反复执行
#   - 只重建容器，不动 ./data 里的数据库
#   - 用 docker stop && docker start 的方式替换（避免强制 restart 造成 SQLite WAL 未落盘）

set -euo pipefail

cd "$(dirname "$0")"
BASE="$(pwd)"

BLUE='\033[36m'; GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'; NC='\033[0m'
say()  { printf "${BLUE}[notice-hub]${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}  ✓${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}  !${NC} %s\n" "$*"; }
die()  { printf "${RED}  ✗${NC} %s\n" "$*" >&2; exit 1; }

NO_BUILD=0
[ "${1:-}" = "--no-build" ] && NO_BUILD=1

say "工作目录：$BASE"

# ---- 1. 环境检查 -----------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "未找到 docker"
docker compose version >/dev/null 2>&1 || die "未找到 docker compose 插件"
ok "docker $(docker --version | awk '{print $3}' | tr -d ',') / $(docker compose version --short)"

# ---- 2. 配置文件 -----------------------------------------------------------
if [ ! -f .env ]; then
    cp .env.example .env
    warn "已从 .env.example 生成 .env，当前使用默认密码 notice2026"
    warn "建议修改：vim $BASE/.env"
else
    ok ".env 已存在，保持不变"
fi

# ---- 3. 数据目录（绝不删除） -------------------------------------------------
mkdir -p "$BASE/data"
ok "数据目录 $BASE/data"

# ---- 4. 备份数据库（存在才备份） --------------------------------------------
if [ -f "$BASE/data/notices.db" ]; then
    mkdir -p "$BASE/data/backups"
    STAMP="$(date +%Y%m%d-%H%M%S)"
    # 先做一次 WAL checkpoint，保证备份文件自洽
    docker exec notice-hub python -c "
import sqlite3
c = sqlite3.connect('/app/data/notices.db')
c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
c.close()
" >/dev/null 2>&1 || true
    for suffix in "" "-wal" "-shm"; do
        [ -f "$BASE/data/notices.db$suffix" ] && cp -f "$BASE/data/notices.db$suffix" \
            "$BASE/data/backups/notices-$STAMP.db$suffix"
    done
    ok "已备份数据库到 data/backups/notices-$STAMP.db*"
    # 只保留最近 20 份
    ls -1t "$BASE/data/backups"/notices-*.db 2>/dev/null | tail -n +21 | while read -r f; do
        rm -f "$f" "$f-wal" "$f-shm"
    done
fi

# ---- 5. 构建并启动 ---------------------------------------------------------
if [ "$NO_BUILD" -eq 1 ]; then
    say "跳过构建，仅重建容器"
    docker compose up -d --force-recreate
else
    say "构建镜像并启动（首次构建约 10-30 秒）"
    docker compose up -d --build
fi

# ---- 6. 健康检查 -----------------------------------------------------------
# ⚠️ 端口可能只绑了内网网卡，此时宿主机 curl 127.0.0.1:8091 是不通的，
#    所以健康检查优先走容器内自检，再退回宿主机内网地址。
health_ok() {
    docker exec notice-hub python -c "
import sys, urllib.request
try:
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8091/api/health', timeout=4).status == 200 else 1)
except Exception:
    sys.exit(1)
" >/dev/null 2>&1 && return 0
    LANIP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -n "$LANIP" ] && curl -fsS --max-time 3 "http://$LANIP:8091/api/health" >/dev/null 2>&1
}

say "等待服务就绪…"
for i in $(seq 1 30); do
    if health_ok; then
        ok "健康检查通过"
        break
    fi
    [ "$i" -eq 30 ] && {
        docker logs --tail 40 notice-hub || true
        die "服务未在 30 秒内就绪，请查看上方日志"
    }
    sleep 1
done

# ---- 7. 结果 ---------------------------------------------------------------
echo
say "部署完成"
docker compose ps
echo
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
ok "访问地址：http://${IP:-<服务器IP>}:8091"
ok "数据文件：$BASE/data/notices.db"
warn "数据库请定期备份：cp $BASE/data/notices.db*  到安全位置"
