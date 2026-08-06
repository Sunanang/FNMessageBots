#!/usr/bin/env bash
# 在 fpk/ 目录执行 fnpack build，产物复制到 dist-fpk/
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FPK_DIR="${ROOT}/fpk"
OUT_DIR="${ROOT}/dist-fpk"
APPNAME="FnMessageBot"

if [ ! -d "${FPK_DIR}" ]; then
  echo "error: missing ${FPK_DIR}" >&2
  exit 1
fi

resolve_fnpack() {
  if [ -n "${FNPACK_BIN:-}" ] && [ -x "${FNPACK_BIN}" ]; then
    printf "%s" "${FNPACK_BIN}"
    return 0
  fi
  if command -v fnpack >/dev/null 2>&1; then
    command -v fnpack
    return 0
  fi
  return 1
}

if ! FNPACK="$(resolve_fnpack)"; then
  cat >&2 <<'EOF'
error: 未找到 fnpack。
请从 https://static2.fnnas.com/fnpack/ 下载并安装，或设置环境变量 FNPACK_BIN 指向可执行文件。
EOF
  exit 1
fi

mkdir -p "${OUT_DIR}"
cd "${FPK_DIR}"
"${FNPACK}" build

VERSION="$(
  awk -F'=' '/^version[[:space:]]*=/{gsub(/[[:space:]]/, "", $2); print $2; exit}' manifest
)"
OUT_NAME="${APPNAME}"
if [ -n "${VERSION}" ]; then
  OUT_NAME="${APPNAME}_${VERSION}"
fi

if [ -f "${FPK_DIR}/${APPNAME}.fpk" ]; then
  cp -f "${FPK_DIR}/${APPNAME}.fpk" "${OUT_DIR}/${OUT_NAME}.fpk"
  echo "built: ${OUT_DIR}/${OUT_NAME}.fpk"
elif [ -f "${ROOT}/${APPNAME}.fpk" ]; then
  cp -f "${ROOT}/${APPNAME}.fpk" "${OUT_DIR}/${OUT_NAME}.fpk"
  echo "built: ${OUT_DIR}/${OUT_NAME}.fpk"
else
  echo "warn: fnpack finished but ${APPNAME}.fpk not found; check fnpack output above" >&2
  ls -la "${FPK_DIR}" "${ROOT}" 2>/dev/null | head -40 || true
  exit 1
fi
