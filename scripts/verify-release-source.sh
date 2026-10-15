#!/usr/bin/env bash
# GitHub 的 commit -> PR 索引可能晚于 push 事件；有限重试，不放宽来源规则。
set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

for attempt in 1 2 3 4 5 6; do
    if gh api --paginate --slurp "repos/$GITHUB_REPOSITORY/commits/$GITHUB_SHA/pulls" \
        | python3 scripts/require-dev-merge.py "$GITHUB_REPOSITORY" "$GITHUB_SHA"; then
        exit 0
    fi
    if [ "$attempt" -lt 6 ]; then
        echo "Waiting for GitHub merge metadata (attempt $attempt/6)..."
        sleep 10
    fi
done
echo '::error::No verified dev -> main merge found; publication refused.' >&2
exit 1
