#!/usr/bin/env bash
# 刷新 FPK 用的离线依赖轮子（vendor/wheels）：只下载 linux x86_64 与 aarch64 的 cp312 二进制轮子。
# 安装包靠这些轮子在没有外网 / PyPI 打不开的机器上也能把依赖装好。
#
#   用法： ./scripts/update-fpk-wheels.sh
#   依赖： python3 + pip（能访问 PyPI）
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="vendor/wheels"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/fnmb-wheels.XXXXXX")"
cleanup() { rm -rf -- "${TMP}"; }
trap cleanup EXIT

PYVER="3.12"          # 与 manifest 里 install_dep_apps=python312 对齐
mkdir -p "${OUT}"

download_arch() {
    local arch="$1" dir="${TMP}/${arch}"
    mkdir -p "${dir}"
    echo "下载 ${arch} 依赖轮子 ..."
    python3 -m pip download -r requirements.txt -d "${dir}" \
        --only-binary=:all: --python-version "${PYVER}" --implementation cp \
        --abi cp312 --abi abi3 --abi none \
        --platform "manylinux_2_17_${arch}" --platform "manylinux2014_${arch}" --platform any \
        >/dev/null
}

download_arch x86_64
download_arch aarch64

rm -f "${OUT}"/*.whl
cp "${TMP}/x86_64"/*.whl "${TMP}/aarch64"/*.whl "${OUT}/"   # 纯 Python 轮子同名，覆盖即去重
ls -1 "${OUT}"/*.whl | wc -l | xargs -I{} echo "已写入 {} 个轮子到 ${OUT}"
du -sh "${OUT}"
