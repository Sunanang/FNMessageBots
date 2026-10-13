#!/usr/bin/env bash
# 构建飞牛 FPK 包：临时把顶层 src/ 复制到 fpk/cmd/fnmessagebots/src，在 fpk/ 内执行 fnpack build，产物移至 dist/。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FPK_DIR="${ROOT}/fpk"
SRC_DST="${FPK_DIR}/cmd/fnmessagebots/src"
DIST="${ROOT}/dist"

FNPACK="${FNPACK:-$(command -v fnpack || true)}"
[ -z "${FNPACK}" ] && [ -x "${HOME}/.local/bin/fnpack" ] && FNPACK="${HOME}/.local/bin/fnpack"
if [ -z "${FNPACK}" ]; then
    echo "错误: 未找到 fnpack（可通过 FNPACK=/path/to/fnpack 指定）" >&2
    exit 1
fi

VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' "${FPK_DIR}/manifest" | head -n1 | tr -d '[:space:]')"
[ -n "${VERSION}" ] || { echo "错误: 无法从 fpk/manifest 读取 version" >&2; exit 1; }

cleanup() { rm -rf "${SRC_DST}"; rm -f "${FPK_DIR}"/*.fpk; }
trap cleanup EXIT
cleanup

mkdir -p "${SRC_DST}"
rsync -a --delete --exclude '__pycache__/' --exclude '*.py[cod]' --exclude '.DS_Store' --exclude 'data/' \
    "${ROOT}/src/" "${SRC_DST}/"
find "${FPK_DIR}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "${FPK_DIR}" -name '.DS_Store' -delete 2>/dev/null || true

# 只在 fpk/ 内构建，fnpack 看不到仓库其他文件
(cd "${FPK_DIR}" && "${FNPACK}" build)

PKG="$(ls -t "${FPK_DIR}"/*.fpk 2>/dev/null | head -n1 || true)"
[ -n "${PKG}" ] || { echo "错误: fnpack 未生成 .fpk 文件" >&2; exit 1; }
mkdir -p "${DIST}"
OUT="${DIST}/FnMessageBot-${VERSION}.fpk"
mv -f "${PKG}" "${OUT}"
echo "已生成: ${OUT} ($(du -h "${OUT}" | cut -f1))"
