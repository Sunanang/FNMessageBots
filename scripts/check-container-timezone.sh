#!/usr/bin/env bash
# 使用真实镜像默认值，不注入 TZ；然后验证显式 TZ 覆盖仍然生效。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
image="${1:?Usage: check-container-timezone.sh IMAGE}"
docker run --rm --network none --read-only --tmpfs /tmp -i --entrypoint python \
    "$image" - Asia/Shanghai < tests/check_container_timezone.py
for zone in UTC America/New_York; do
    docker run --rm --network none --read-only --tmpfs /tmp -i -e "TZ=$zone" --entrypoint python \
        "$image" - "$zone" < tests/check_container_timezone.py
done
