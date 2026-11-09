#!/usr/bin/env bash
# 把最新 Release 的安装包同步进官网，并生成 website/downloads/version.json。
#
# 官网的「下载」区走的是本站直连（EdgeOne 国内节点），不依赖 GitHub 的下载速度；
# 所以每次发版后跑一次这个脚本，把新包搬进 website/downloads/ 并提交。
#
#   用法： ./scripts/sync-website-downloads.sh
#   依赖： gh（已登录）、python3
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="website/downloads"
mkdir -p "$OUT"

TAG=$(gh release view --json tagName -q .tagName)
echo "最新 Release: $TAG"

# 先清掉旧版本的包，避免仓库里越堆越大（sha256 与 version.json 一起重建）
find "$OUT" -maxdepth 1 \( -name '*.fpk' -o -name '*.sha256' -o -name 'version.json' \) -delete

gh release download "$TAG" --dir "$OUT" --clobber
echo "已下载到 $OUT"

gh release view "$TAG" --json tagName,name,publishedAt,assets,url | python3 -c '
import sys, json, hashlib, os
d = json.load(sys.stdin)
out_dir = "website/downloads"
data = {
    "tag": d["tagName"],
    "name": d["name"],
    "publishedAt": d["publishedAt"][:10],
    "releasePage": d["url"],
    "assets": [],
}
for a in d["assets"]:
    if a["name"].endswith(".sha256"):
        continue
    p = os.path.join(out_dir, a["name"])
    if not os.path.exists(p):
        continue
    data["assets"].append({
        "name": a["name"],
        "size": os.path.getsize(p),
        "sha256": hashlib.sha256(open(p, "rb").read()).hexdigest(),
    })
with open(os.path.join(out_dir, "version.json"), "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print("version.json:", data["tag"], "→", ", ".join(a["name"] for a in data["assets"]))
'

echo
echo "页面里的兜底版本号（npm/html 静态值）如需同步，改 website/index.html 里的 data-ver / data-date / data-name 默认值；"
echo "运行时若有 version.json，页面会自动覆盖成最新值，所以不改也能正确显示。"
echo "别忘了提交： git add website/downloads website/index.html"
