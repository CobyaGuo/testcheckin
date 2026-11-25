#!/usr/bin/env bash
# =====================================================================
# deploy.sh — 把本目录（command-panel 桌面工具源码）通过 GitHub API
#             追加部署到 GitHub Pages 仓库的 command-panel/ 子目录
#
# 与 checkin-web/deploy.sh 同一套链路（blob -> tree -> commit -> ref，
# 纯 REST API，不 clone 不 push），但语义不同：
#   · checkin-web 版：用 dist/ 整体替换仓库根目录
#   · 本版本：合并模式 —— 以远程现有 tree 为 base，只新增/更新
#     command-panel/ 前缀下的文件，仓库其余内容原样保留；
#     远程 command-panel/ 下已不存在于本地的旧文件会被删除（目录级同步）
#
# 只上传有改动的文件：先本地算 git blob SHA，与远程同路径同 SHA 则
# 复用既有 blob，零传输。
#
# 禁传项（硬约束，与命令面板本地铁律一致）：
#   logs/（运行日志）、__pycache__/、.workbuddy/、*.log、.git/
#
# 用法（密钥只从环境变量读取，绝不写死在脚本里）:
#   GITHUB_TOKEN=xxxx bash deploy.sh
#   GITHUB_OWNER=CobyaGuo GITHUB_REPO=testcheckin GITHUB_BRANCH=main GITHUB_TOKEN=xxxx bash deploy.sh
# =====================================================================
set -euo pipefail

OWNER="${GITHUB_OWNER:-CobyaGuo}"
REPO="${GITHUB_REPO:-testcheckin}"
BRANCH="${GITHUB_BRANCH:-main}"
PREFIX="${DEPLOY_PREFIX:-command-panel}"   # 仓库内子目录前缀
TOKEN="${GITHUB_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  echo "ERROR: 请设置环境变量 GITHUB_TOKEN" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python}"

# ---- 预检：交付文件必须在 --------------------------------------------------
for f in command_panel.py ssh_console.py requirements.txt; do
  if [ ! -f "$ROOT/$f" ]; then
    echo "ERROR: 缺少交付文件 $f" >&2
    exit 1
  fi
done

echo ">> 通过 GitHub API 部署 $ROOT -> $OWNER/$REPO@$BRANCH:$PREFIX/ ..."
GITHUB_TOKEN="$TOKEN" \
GITHUB_OWNER="$OWNER" \
GITHUB_REPO="$REPO" \
GITHUB_BRANCH="$BRANCH" \
DEPLOY_PREFIX="$PREFIX" \
SRC="$ROOT" \
"$PYTHON" - <<'PYEOF'
import os, json, base64, hashlib, datetime, time, urllib.request, urllib.error

TOKEN   = os.environ["GITHUB_TOKEN"]
OWNER   = os.environ["GITHUB_OWNER"]
REPO    = os.environ["GITHUB_REPO"]
BRANCH  = os.environ["GITHUB_BRANCH"]
PREFIX  = os.environ["DEPLOY_PREFIX"].strip("/")
SRC     = os.environ["SRC"]
API     = f"https://api.github.com/repos/{OWNER}/{REPO}"

class ApiError(Exception):
    pass

def api(method, path, data=None):
    """带重试的 REST 调用：网络/代理类错误指数退避重试，4xx 业务错误不重试。"""
    url = API + path
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    last_err = None
    for attempt in range(5):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            err = ApiError(f"HTTP {e.code} {method} {path}: {detail[:600]}")
            if e.code >= 500 and attempt < 4:      # 5xx 网关抖动，退避重试
                last_err = err
                time.sleep(2 ** attempt)
                continue
            raise err
        except urllib.error.URLError as e:
            err = ApiError(f"URL {method} {path}: {e.reason}")
            if attempt < 4:                        # 代理/连接瞬断，退避重试
                last_err = err
                print(f"      ! 网络抖动（{e.reason}），第 {attempt + 2} 次尝试 ...")
                time.sleep(2 ** attempt)
                continue
            raise err
    raise last_err

# 禁传项：运行时数据与本地杂物，绝不进仓库
def is_excluded(rel):
    parts = rel.split("/")
    if "logs" in parts or "__pycache__" in parts or ".workbuddy" in parts or ".git" in parts:
        return True
    return rel.endswith(".log") or rel.endswith(".pyc")

# ---- 收集本地文件（git blob SHA 本地先算，便于与远程比对零传输复用）----
def local_blob_sha(raw):
    h = hashlib.sha1()
    h.update(b"blob %d\x00" % len(raw))
    h.update(raw)
    return h.hexdigest()

local = {}   # rel(含前缀) -> (raw, sha)
for root, dirs, files in os.walk(SRC):
    dirs[:] = [d for d in dirs if d not in ("logs", "__pycache__", ".workbuddy", ".git")]
    for f in sorted(files):
        full = os.path.join(root, f)
        rel = os.path.relpath(full, SRC).replace(os.sep, "/")
        if is_excluded(rel):
            print(f"      x {rel}  (运行时数据/杂物，禁止上传)")
            continue
        with open(full, "rb") as fh:
            raw = fh.read()
        local[f"{PREFIX}/{rel}"] = (raw, local_blob_sha(raw))
if not local:
    print("ERROR: 没有可上传的文件", file=os.sys.stderr)
    raise SystemExit(1)

# ---- 读取远程状态 ----
print(f"  · 读取分支 {BRANCH} 引用 ...")
ref = api("GET", f"/git/refs/heads/{BRANCH}")
base_commit_sha = ref["object"]["sha"]
commit = api("GET", f"/git/commits/{base_commit_sha}")
base_tree_sha = commit["tree"]["sha"]

print("  · 读取远程完整 tree（合并模式，保留仓库其余内容）...")
tree = api("GET", f"/git/trees/{base_tree_sha}?recursive=1")
remote_blobs = {t["path"]: t["sha"] for t in tree.get("tree", []) if t["type"] == "blob"}

# ---- 计算 diff：只动 PREFIX 下的路径 ----
changes = []          # tree entries（新增/修改）
uploaded = reused = 0
for path, (raw, sha) in sorted(local.items()):
    if remote_blobs.get(path) == sha:
        reused += 1
        print(f"      ~ {path}  ({len(raw)//1024} KB, 复用远程 blob)")
        continue
    api("POST", "/git/blobs", {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"})
    uploaded += 1
    print(f"      + {path}  ({len(raw)//1024} KB)")

# 远程 PREFIX 下、本地已不存在的旧文件 -> 删除（目录级同步）
for path in sorted(remote_blobs):
    if path.startswith(PREFIX + "/") and path not in local:
        changes.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
        print(f"      - {path}  (本地已删除，从仓库移除)")

for path, (_, sha) in local.items():
    if remote_blobs.get(path) != sha:
        changes.append({"path": path, "mode": "100644", "type": "blob", "sha": sha})

if not changes:
    print()
    print("✅ 无变化，远程已是最新，不创建空提交")
    print(f"   目录: https://github.com/{OWNER}/{REPO}/tree/{BRANCH}/{PREFIX}")
    raise SystemExit(0)

# ---- 合并 tree：base_tree 保证其余文件原样保留，只提交本次变更项 ----
print(f"  · 创建合并 tree（基于远程 {base_tree_sha[:10]}，变更 {len(changes)} 项）...")
new_tree = api("POST", "/git/trees", {"base_tree": base_tree_sha, "tree": changes})

msg = ("deploy: " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
       + f" 追加部署 {PREFIX}/ 源码")
print(f"  · 创建 commit: {msg}")
new_commit = api("POST", "/git/commits", {
    "message": msg,
    "tree": new_tree["sha"],
    "parents": [base_commit_sha],
})

print(f"  · 更新 {BRANCH} 引用 -> {new_commit['sha'][:10]} ...")
api("PATCH", f"/git/refs/heads/{BRANCH}", {"sha": new_commit["sha"]})

print()
print("✅ 部署完成（合并模式，仓库其余内容未动）")
print(f"   提交: {new_commit['sha']}")
print(f"   新传 {uploaded} 个、复用 {reused} 个、删除 {len(changes) - uploaded} 个")
print(f"   目录: https://github.com/{OWNER}/{REPO}/tree/{BRANCH}/{PREFIX}")
print(f"   站点: https://{OWNER}.github.io/{REPO}/{PREFIX}/")
PYEOF
