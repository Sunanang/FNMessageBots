#!/usr/bin/env bash
# 在独立临时目录组装 FPK；源码、依赖与资源均使用主仓库当前版本。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FPK_DIR="${ROOT}/fpk"
DIST="${ROOT}/dist"

FNPACK="${FNPACK:-$(command -v fnpack || true)}"
[ -z "${FNPACK}" ] && [ -x "${HOME}/.local/bin/fnpack" ] && FNPACK="${HOME}/.local/bin/fnpack"
if [ -z "${FNPACK}" ]; then
    echo "错误: 未找到 fnpack（可通过 FNPACK=/path/to/fnpack 指定）" >&2
    exit 1
fi

VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' "${FPK_DIR}/manifest" | head -n1 | tr -d '[:space:]')"
[[ "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] || { echo "错误: fpk/manifest 版本无效" >&2; exit 1; }

BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/fnmb-fpk.XXXXXX")"
PAYLOAD="${BUILD_DIR}/payload"
cleanup() { rm -rf -- "${BUILD_DIR}"; }
trap cleanup EXIT

rsync -a --exclude '__pycache__/' --exclude '*.py[cod]' --exclude '.DS_Store' \
    --exclude '.gitignore' --exclude '*.fpk' --exclude 'data/' \
    --exclude 'cmd/fnmessagebots/src/' "${FPK_DIR}/" "${PAYLOAD}/"
CODE_DIR="${PAYLOAD}/cmd/fnmessagebots"
mkdir -p "${CODE_DIR}/src"
rsync -a --delete --exclude '__pycache__/' --exclude '*.py[cod]' --exclude '.DS_Store' --exclude 'data/' \
    "${ROOT}/src/" "${CODE_DIR}/src/"
cp "${ROOT}/requirements.txt" "${CODE_DIR}/requirements.txt"
rsync -a --delete --exclude '.DS_Store' "${ROOT}/assets/" "${CODE_DIR}/assets/"

# fnpack 将产物写到当前目录；不在源仓库中生成、删除或覆盖临时源码。
(cd "${BUILD_DIR}" && "${FNPACK}" build --directory "${PAYLOAD}")

PKG="${BUILD_DIR}/FnMessageBot.fpk"
[ -s "${PKG}" ] || { echo "错误: fnpack 未生成非空的 FnMessageBot.fpk" >&2; exit 1; }
mkdir -p "${DIST}"
OUT="${DIST}/FnMessageBot-${VERSION}.fpk"
mv -f "${PKG}" "${OUT}"
python3 - "${OUT}" <<'PY'
import hashlib
import sys
from pathlib import Path

package = Path(sys.argv[1])
digest = hashlib.sha256(package.read_bytes()).hexdigest()
Path(str(package) + '.sha256').write_text(f'{digest}  {package.name}\n', encoding='utf-8')
PY
echo "已生成: ${OUT} ($(du -h "${OUT}" | cut -f1))"
