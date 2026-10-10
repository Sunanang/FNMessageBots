"""按部署环境生成权限操作说明；只生成命令，应用不执行授权。"""
from __future__ import annotations

import os
import pwd
import shlex
from pathlib import Path


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def service_user() -> str:
    if os.getenv("FNMB_GATEWAY_SOCKET") or os.getenv("TRIM_APPNAME") == "FnMessageBot":
        return "FnMessageBot"
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


def db_acl_commands(path: str) -> str:
    """规范化符号链接，给父目录进入权限、库及已有 WAL/SHM 读取权限。"""
    return f'''sudo bash -s -- {shlex.quote(path)} {shlex.quote(service_user())} <<'FNMB_ACL'
db=$(realpath -e -- "$1") || exit 1
test -f "$db" || exit 1
account=$2
dir=$(dirname -- "$db")
while [ "$dir" != / ]; do
    setfacl -m "u:$account:x" -- "$dir" || exit 1
    dir=$(dirname -- "$dir")
done
setfacl -m "u:$account:rx" -- "$(dirname -- "$db")" || exit 1
setfacl -m "u:$account:r" -- "$db" || exit 1
for side in "$db-wal" "$db-shm"; do
    if [ -f "$side" ]; then
        setfacl -m "u:$account:r" -- "$side" || exit 1
    fi
done
FNMB_ACL'''


def journal_permission_action() -> str:
    if in_container():
        return ("在 Compose 挂载 /var/log/journal、/run/log/journal 和 /etc/machine-id；"
                "非 root 容器把宿主 systemd-journal 的 GID 加入 group_add，再重建容器。")
    return (f"在 NAS SSH 执行 sudo usermod -aG systemd-journal {shlex.quote(service_user())}，"
            "然后在应用中心停止并重新启动应用，再保存配置检查。")


def docker_permission_commands(path: str) -> str:
    import grp
    user = shlex.quote(service_user())
    try:
        group = grp.getgrgid(os.stat(path).st_gid).gr_name
    except (OSError, KeyError):
        group = ""
    if group == "docker":
        return f"sudo usermod -aG docker {user}"
    # 不把应用加入 root 等系统组；直接授予实际 socket 的 ACL。
    return f"sudo setfacl -m u:{user}:rw -- {shlex.quote(path)}"
