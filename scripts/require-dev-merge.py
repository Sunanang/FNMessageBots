#!/usr/bin/env python3
"""拒绝发布不属于同仓库 dev → main 已合并 PR 的提交。"""
import json
import sys


def matching_dev_merge(pull_requests, repository, revision):
    for item in pull_requests:
        # gh api --paginate --slurp 返回分页数组；也接受单页结果。
        entries = item if isinstance(item, list) else [item]
        for pr in entries:
            head = pr.get("head") or {}
            base = pr.get("base") or {}
            if (pr.get("merged_at")
                    and pr.get("merge_commit_sha") == revision
                    and head.get("ref") == "dev"
                    and (head.get("repo") or {}).get("full_name") == repository
                    and base.get("ref") == "main"
                    and (base.get("repo") or {}).get("full_name") == repository):
                return pr
    raise ValueError("main 发布提交必须来自同仓库已合并的 dev → main PR，禁止直接提交发布。")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: require-dev-merge.py owner/repo commit-sha < pull-requests.json")
    try:
        pr = matching_dev_merge(json.load(sys.stdin), sys.argv[1], sys.argv[2])
    except (ValueError, TypeError, AttributeError) as error:
        raise SystemExit(str(error))
    print(f"Verified dev -> main merge: PR #{pr['number']} ({sys.argv[2]})")
