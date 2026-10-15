#!/usr/bin/env bash
# 先核对提交、架构，再从不可变 digest 更新三个 latest 标签。
set -euo pipefail

IMAGE="${1:?Docker image is required}"
REVISION="${2:?Commit SHA is required}"
[[ "$REVISION" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid commit SHA' >&2; exit 1; }

resolve_image() {
    local arch="$1" ref="${IMAGE}:sha-${REVISION}-$1" config digest
    digest="$(docker buildx imagetools inspect "$ref" --format '{{.Manifest.Digest}}')" || return 1
    [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "Invalid digest for $ref" >&2; return 1; }
    config="$(docker buildx imagetools inspect "$IMAGE@$digest" --format '{{json .Image}}')" || return 1
    jq --exit-status --arg arch "$arch" --arg revision "$REVISION" \
        '.os == "linux" and .architecture == $arch and .config.Labels["org.opencontainers.image.revision"] == $revision' \
        <<< "$config" >/dev/null || { echo "Wrong platform or commit for $ref" >&2; return 1; }
    printf '%s@%s' "$IMAGE" "$digest"
}

AMD64="$(resolve_image amd64)"
ARM64="$(resolve_image arm64)"
docker buildx imagetools create --prefer-index=false --tag "${IMAGE}:latest-amd64" "$AMD64"
docker buildx imagetools create --prefer-index=false --tag "${IMAGE}:latest-arm64" "$ARM64"
docker buildx imagetools create --tag "${IMAGE}:latest" "$AMD64" "$ARM64"

docker buildx imagetools inspect "${IMAGE}:latest" --raw | jq --exit-status \
    '[.manifests[].platform | select(.os == "linux") | .architecture] | sort == ["amd64", "arm64"]' >/dev/null
echo "Published ${IMAGE}:latest-amd64, latest-arm64 and latest ($REVISION)"
