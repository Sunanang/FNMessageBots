#!/usr/bin/env bash
# main 使用固定的 latest 预发布；版本号不变时也能更新最新构建。
set -euo pipefail

: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${GH_REPO:?GH_REPO is required}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

shopt -s nullglob
packages=()
for package in dist/FnMessageBot-*.fpk; do
    [ "$package" = dist/FnMessageBot-latest.fpk ] || packages+=("$package")
done
[ "${#packages[@]}" -eq 1 ] || { echo 'Expected exactly one verified FPK' >&2; exit 1; }
PACKAGE="${packages[0]}"
(cd dist && sha256sum --check "$(basename "$PACKAGE").sha256")
cp "$PACKAGE" dist/FnMessageBot-latest.fpk
(cd dist && sha256sum FnMessageBot-latest.fpk > FnMessageBot-latest.fpk.sha256)

NOTES="$(mktemp)"
trap 'rm -f -- "$NOTES"' EXIT
cat > "$NOTES" <<EOF
从 main 自动构建的最新版本，随通过检查的提交更新。

提交：https://github.com/${GH_REPO}/commit/${GITHUB_SHA}

- Docker：\`sunanang/fn-message-bots:latest\`（amd64 / arm64）
- FPK：下载 \`FnMessageBot-latest.fpk\`，并用对应的 \`.sha256\` 文件核对。
- 安装包内部版本由 \`fpk/manifest\` 决定；本预发布不代替正式版本发布。
EOF

if gh release view latest >/dev/null 2>&1; then
    gh api --method PATCH "repos/$GH_REPO/git/refs/tags/latest" -f sha="$GITHUB_SHA" -F force=true >/dev/null
    # 发布时先置为草稿，避免下载者拿到替换过程中的文件与校验和。
    gh release edit latest --draft --notes-file "$NOTES" --title '最新构建（main）' --prerelease --latest=false
else
    gh release create latest --target "$GITHUB_SHA" --draft --prerelease --latest=false \
        --title '最新构建（main）' --notes-file "$NOTES"
fi
gh release upload latest "$PACKAGE" "${PACKAGE}.sha256" \
    dist/FnMessageBot-latest.fpk dist/FnMessageBot-latest.fpk.sha256 --clobber
gh release edit latest --draft=false --prerelease --latest=false
echo "FPK: https://github.com/$GH_REPO/releases/download/latest/FnMessageBot-latest.fpk"
