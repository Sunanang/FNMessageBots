#!/usr/bin/env bash
# 按 fpk/manifest 的 version 发布 GitHub Release，标签为 v{version}。
# 同一版本再次合入 main 时覆盖该 Release。Docker 的 latest 与 FPK 版本标签互不影响。
set -euo pipefail

: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${GH_REPO:?GH_REPO is required}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' fpk/manifest | head -n1 | tr -d '[:space:]')"
[[ "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] || { echo "错误: fpk/manifest 版本无效" >&2; exit 1; }

TAG="v${VERSION}"
PACKAGE="dist/FnMessageBot-${VERSION}.fpk"
[ -s "$PACKAGE" ] || { echo "Expected verified package ${PACKAGE}" >&2; exit 1; }
(cd dist && sha256sum --check "$(basename "$PACKAGE").sha256")

TITLE="FnMessageBot ${VERSION}"
NOTES="$(mktemp)"
trap 'rm -f -- "$NOTES"' EXIT
cat > "$NOTES" <<EOF
从 main 自动构建的 FnMessageBot ${VERSION}。

提交：https://github.com/${GH_REPO}/commit/${GITHUB_SHA}

- Docker：\`sunanang/fn-message-bots:latest\`（amd64 / arm64）。Docker 的 \`latest\` 是镜像浮动标签，与本 FPK 的版本标签 \`${TAG}\` 是两套命名。
- FPK：下载 \`FnMessageBot-${VERSION}.fpk\`，并用对应的 \`.sha256\` 文件核对。
- 安装包内部版本由 \`fpk/manifest\` 决定。同一版本再次合入 main 时，会覆盖标签 \`${TAG}\` 上的 Release 与同名资产。
EOF

if gh release view "$TAG" >/dev/null 2>&1; then
    # 先收成草稿，再移动标签并替换资产，避免下载者拿到替换过程中的文件与校验和。
    gh release edit "$TAG" --target "$GITHUB_SHA" --draft --notes-file "$NOTES" --title "$TITLE" --prerelease=false
    gh api --method PATCH "repos/${GH_REPO}/git/refs/tags/${TAG}" -f sha="$GITHUB_SHA" -F force=true >/dev/null
else
    gh release create "$TAG" --target "$GITHUB_SHA" --draft --title "$TITLE" --notes-file "$NOTES"
fi
gh release upload "$TAG" "$PACKAGE" "${PACKAGE}.sha256" --clobber
gh release edit "$TAG" --draft=false --prerelease=false
echo "FPK: https://github.com/${GH_REPO}/releases/download/${TAG}/FnMessageBot-${VERSION}.fpk"
