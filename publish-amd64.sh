#!/bin/bash

# AMD64 架构镜像发布脚本

set -e  # 遇到错误时退出

echo "FN Message Bot AMD64 Docker 镜像发布脚本"
echo "=================================="

# 检查是否安装了 Docker
if ! command -v docker &> /dev/null; then
    echo "错误: 未找到 Docker，请先安装 Docker"
    exit 1
fi

# 检查是否提供了镜像名称参数
if [ $# -eq 0 ]; then
    echo "用法: $0 <docker-image-name> [tag]"
    echo "示例: $0 your-registry/image-name"
    echo "      $0 crpi-ys9neflc9z1wqyy7.cn-hangzhou.personal.cr.aliyuncs.com/sunanang/fn_message_bots"
    exit 1
fi

IMAGE_NAME="$1"
TAG="${2:-latest}"
# 仅推送 tag-amd64，与 arm64 的 tag-arm64 并存；latest 由 merge-manifest 等合成
TAG_ARCH="${TAG}-amd64"
FULL_IMAGE="${IMAGE_NAME}:${TAG_ARCH}"

echo "Image: $FULL_IMAGE"
echo ""

# 国内拉取 base 镜像易超时，默认使用 DaoCloud 镜像源；海外可设 USE_DOCKER_HUB_MIRROR=0
BASE_IMAGE_ARG=""
if [ "${USE_DOCKER_HUB_MIRROR:-1}" = "1" ]; then
    BASE_IMAGE_ARG="--build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim"
    echo "Using China mirror for base image (set USE_DOCKER_HUB_MIRROR=0 to use Docker Hub)"
fi

# 构建并推送 AMD64 镜像（仅 tag-amd64）
echo "Building AMD64 image..."
docker build --platform linux/amd64 $BASE_IMAGE_ARG -t "$FULL_IMAGE" .

# 推送（带重试，国内访问 Docker Hub 易出现 EOF/超时）
push_with_retry() {
    local img="$1"
    local max=3
    local n=1
    while true; do
        echo "Pushing $img (attempt $n/$max)..."
        if docker push "$img"; then
            return 0
        fi
        if [ "$n" -ge "$max" ]; then
            echo "Push failed after $max attempts. Try VPN or retry later."
            return 1
        fi
        n=$((n + 1))
        echo "Retry in 5s..."
        sleep 5
    done
}
push_with_retry "$FULL_IMAGE" || exit 1

echo ""
echo "Done. Pushed: $FULL_IMAGE"
echo "Then run: ./publish-arm64.sh $IMAGE_NAME $TAG, then merge manifest (e.g. GitHub Actions)"
echo ""
echo "You can run the image with:"
echo "  docker run -d --platform linux/amd64 --network=host \\"
echo "    -v /var/log/journal:/var/log/journal:ro \\"
echo "    -v /run/log/journal:/run/log/journal:ro \\"
echo "    -v /var/log/syslog:/var/log/syslog:ro \\"
echo "    -v ./data/logs:/app/logs:rw \\"
echo "    -v ./data/cursor:/tmp/cursor:rw \\"
echo "    -e WECHAT_WEBHOOK_URL='your_webhook_url' \\"
echo "    $FULL_IMAGE"
echo ""
echo "Or: docker-compose -f docker-compose.yml up -d"