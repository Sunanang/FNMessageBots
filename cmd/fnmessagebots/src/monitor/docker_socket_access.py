"""Docker Engine Unix socket 可读性诊断（授权或挂载后仍可能因 UID/GID 无权限）。"""

from __future__ import annotations

import os
import stat as stat_mod
from typing import Optional

try:
    import docker as docker_sdk  # type: ignore
except ImportError:
    docker_sdk = None  # noqa: N816


def _mode_str(mode: int) -> str:
    return stat_mod.filemode(mode)


def _permission_hint(st: os.stat_result) -> str:
    sock_gid = int(st.st_gid)
    proc_uid = os.getuid()
    proc_gid = os.getgid()
    proc_groups = sorted(set(os.getgroups()))
    in_docker_gid = sock_gid in proc_groups or proc_gid == sock_gid
    lines = [
        f"当前进程 uid={proc_uid} gid={proc_gid}，附属组 GID={proc_groups}；",
        f"socket 权限 {_mode_str(st.st_mode)}，属主 uid={st.st_uid} gid={sock_gid}。",
    ]
    if not in_docker_gid:
        lines.append(
            f"Docker 版可在 compose 中增加 group_add: [\"{sock_gid}\"] 后重建容器；"
            f"应用市场版请将运行用户加入 docker 组或对 socket 配置 ACL（见常见问题第 11 条）；"
            f"也可临时以 root 运行（请自行评估安全风险）。"
        )
    return " ".join(lines)


def check_docker_socket_access(sock_path: str) -> Optional[str]:
    """
    返回告警文案；socket 可用（含 Docker API ping）时返回 None。
    """
    sp = (sock_path or "").strip() or "/var/run/docker.sock"
    faq = "详见常见问题 · 第11条（/faq#faq-docker-sock）。"

    try:
        st = os.stat(sp)
    except FileNotFoundError:
        return (
            f"Docker：未找到 {sp}。"
            f"请确认已授权或挂载宿主 /var/run/docker.sock。"
            f"{faq}"
        )
    except PermissionError:
        return f"Docker：无法访问 {sp}（stat 被拒绝）。{faq}"

    if not stat_mod.S_ISSOCK(st.st_mode):
        return (
            f"Docker：{sp} 不是 Unix socket（当前为 {_mode_str(st.st_mode)}），"
            f"请检查路径或挂载目标是否正确。{faq}"
        )

    if docker_sdk is not None:
        client = None
        try:
            client = docker_sdk.DockerClient(base_url=f"unix://{sp}", timeout=3)
            client.ping()
            return None
        except Exception as api_err:
            if os.access(sp, os.R_OK | os.W_OK):
                return (
                    f"Docker：{sp} 存在且文件可读写，但连接 Docker Engine 失败（{api_err}）。"
                    f"请确认 Docker 服务正常。{faq}"
                )
            return (
                f"Docker：{sp} 存在但当前进程无法读写（{api_err}）。"
                f"{_permission_hint(st)} {faq}"
            )
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    if os.access(sp, os.R_OK | os.W_OK):
        return None

    return (
        f"Docker：{sp} 存在但当前进程无法读写。"
        f"{_permission_hint(st)} {faq}"
    )
