#!/usr/bin/env bash
# ============================================================================
#  把改动提交并推送到 GitHub
#
#  用法：
#    tools/git-sync.sh                     # 有改动就提交（自动写提交说明）并推送
#    tools/git-sync.sh "改了 XX"           # 指定提交说明
#    tools/git-sync.sh --push-only         # 只推送已有提交，不产生新提交
#    tools/git-sync.sh --check             # 只做机密检查，不提交不推送
#
#  ⚠️ 推送前会**重新扫描一遍真实机密值**（企微 Secret / 同步令牌 / BotID）。
#     只要在待提交内容里发现任何一个，就立刻中止 —— 宁可不同步，也不能泄漏。
#     这是给以后任何一次改动兜底的：万一有人把密钥写进代码，这里会拦住。
# ============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

MODE="commit"
MSG=""
case "${1:-}" in
  --push-only) MODE="push" ;;
  --check)     MODE="check" ;;
  "")          ;;
  *)           MSG="$1" ;;
esac

export HOME="${HOME:-/root}"
git config --global --add safe.directory "$REPO" 2>/dev/null || true

# ---------------------------------------------------------------- 机密检查
echo "→ 检查是否会把机密提交上去…"
python3 - "$REPO" <<'PY'
import os, re, subprocess, sys

repo = sys.argv[1]
os.chdir(repo)

def read_val(path, key):
    try:
        s = open(path, encoding="utf-8").read()
    except Exception:
        return None
    m = re.search(rf"^{key}=(.*)$", s, re.M)
    return m.group(1).strip() if m and m.group(1).strip() else None

# 只针对「真正长得像密钥」的值；默认词（admin/123456）不算，
# 否则会和代码里到处出现的默认用户名撞车，天天误报。
targets = {
    "SYNC_TOKEN":         read_val(".env", "SYNC_TOKEN"),
    "WECOM_BOT_SECRET":   read_val("wecom-bot/config.env", "WECOM_BOT_SECRET"),
    "WECOM_BOT_ID":       read_val("wecom-bot/config.env", "WECOM_BOT_ID"),
}
COMMON = {"admin", "123456", "12345678", "password", "root"}
targets = {k: v for k, v in targets.items() if v and v not in COMMON and len(v) >= 12}

# 必须被忽略的路径（不存在不算错）
MUST_IGNORE = [
    ".env", "wecom-bot/config.env", "docs/账号分发清单.txt",
    "data/notices.db", "backups", "public/originals", "public/wecom-media",
    "wecom-bot/venv", ".venv",
]

# 关键：既要扫已跟踪的，也要扫「未跟踪但不会被忽略」的新文件。
# 只扫 ls-files 会漏掉刚创建的新文件 —— 先 git add -A 再扫也一样，
# 但那样万一是机密，东西已经进了暂存区；这里提前把新文件也算进来。
raw = subprocess.run(
    ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
    capture_output=True,
).stdout
tracked = [f.decode("utf-8") for f in raw.split(b"\0") if f]

problems = []

# 1) 机密值是否出现在已跟踪文件里
for label, val in targets.items():
    for f in tracked:
        try:
            if val in open(f, encoding="utf-8", errors="ignore").read():
                problems.append(f"机密 {label} 出现在已跟踪文件 {f}")
        except Exception:
            pass

# 2) 危险路径是否被跟踪
for p in MUST_IGNORE:
    for f in tracked:
        if f == p or f.startswith(p + "/"):
            problems.append(f"危险路径被跟踪: {f}")

if problems:
    print("  ✗ 发现 %d 个问题，已中止：" % len(problems))
    for p in problems:
        print("     - " + p)
    sys.exit(1)

print("  ✓ 待推送 %d 个文件，未发现机密" % len(tracked))
PY

if [ "$MODE" = "check" ]; then
  echo "→ --check 模式，到此为止。"
  exit 0
fi

# ---------------------------------------------------------------- 提交
if [ "$MODE" = "commit" ]; then
  if [ -z "$(git status --porcelain)" ]; then
    echo "→ 工作区没有改动"
  else
    git add -A
    if [ -z "$MSG" ]; then
      MSG="自动同步 $(date '+%Y-%m-%d %H:%M')"
    fi
    git -c user.name="notice-hub" -c user.email="notice-hub@localhost" \
        commit -q -m "$MSG"
    echo "→ 已提交：$MSG"
  fi
fi

# ---------------------------------------------------------------- 推送
if ! git remote get-url origin >/dev/null 2>&1; then
  echo "→ 还没配置远程仓库，跳过推送。"
  echo "   配置方法： git remote add origin git@github.com:<你的账号>/<仓库名>.git"
  exit 0
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" = "HEAD" ]; then
  echo "✗ 当前不在任何分支上（detached HEAD），不推送。"
  exit 1
fi

echo "→ 推送到 origin/$BRANCH …"
git push -u origin "$BRANCH"
echo "✓ 同步完成"
