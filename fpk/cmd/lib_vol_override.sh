#!/bin/bash
# 仅追加「不宜写死」的可选挂载到 docker-compose.yaml 标记区：
# - 额外 /volN（/vol1 已在主文件）
# - ups.conf（无 UPS 时文件不存在，写死会导致 compose 失败）
# 并写调试日志到 TRIM_PKGVAR/fnmb_compose_binds.log

_FNMB_LOG=""

_fnmb_log() {
  local msg="$1"
  if [ -n "${TRIM_PKGVAR:-}" ]; then
    _FNMB_LOG="${TRIM_PKGVAR}/fnmb_compose_binds.log"
    mkdir -p "${TRIM_PKGVAR}" 2>/dev/null || true
    echo "$(date '+%F %T') $msg" >>"${_FNMB_LOG}" 2>/dev/null || true
  fi
}

_fnmb_is_mountpoint() {
  local p="$1"
  [ -d "$p" ] || return 1
  if command -v findmnt >/dev/null 2>&1; then
    if [ "$(findmnt -n -o TARGET --target "$p" 2>/dev/null | head -1)" = "$p" ]; then
      return 0
    fi
  fi
  if command -v mountpoint >/dev/null 2>&1; then
    mountpoint -q "$p" 2>/dev/null && return 0
  fi
  if [ -r /proc/mounts ]; then
    awk -v p="$p" '$2 == p { found=1 } END { exit !found }' /proc/mounts 2>/dev/null && return 0
  fi
  return 1
}

_fnmb_path_exists() {
  local p="$1"
  [ -e "$p" ] || [ -L "$p" ]
}

_fnmb_push_bind() {
  local host_path="$1"
  local mode="${2:-ro}"
  local ctn_path="${3:-$host_path}"
  local line existing
  [ -n "$host_path" ] || return 0
  _fnmb_path_exists "$host_path" || return 0
  line="      - ${host_path}:${ctn_path}:${mode}"
  for existing in "${_FNMB_BIND_LINES[@]+"${_FNMB_BIND_LINES[@]}"}"; do
    if [ "$existing" = "$line" ]; then
      return 0
    fi
  done
  _FNMB_BIND_LINES+=("$line")
  _fnmb_log "bind: $line"
}

_fnmb_collect_extra_vols() {
  local d name n
  local -a found=()
  shopt -s nullglob
  for d in /vol[0-9]* /volume[0-9]*; do
    [ -d "$d" ] || continue
    name="${d#/}"
    if ! printf '%s' "$name" | grep -Eq '^(vol|volume)[0-9]+$'; then
      continue
    fi
    # /vol1～/vol4 已在主 compose 写死；此处仅补 /vol5+
    case "$d" in
      /vol1|/vol2|/vol3|/vol4|/volume1|/volume2|/volume3|/volume4) continue ;;
    esac
    if _fnmb_is_mountpoint "$d"; then
      found+=("$d")
    fi
  done
  shopt -u nullglob

  [ "${#found[@]}" -gt 0 ] || return 0

  while IFS= read -r d; do
    [ -n "$d" ] && _fnmb_push_bind "$d" ro
  done < <(
    printf '%s\n' "${found[@]}" | while IFS= read -r d; do
      n="${d##*[!0-9]}"
      printf '%05d\t%s\n' "${n:-0}" "$d"
    done | sort -n | cut -f2-
  )
}

_fnmb_collect_optional_binds() {
  # 业务库/docker.sock/vol1-4/ups/dpkg 已写死在 compose；此处仅补 /vol5+ 与 API socket
  _FNMB_BIND_LINES=()
  _fnmb_log "collect optional binds begin (extra vols + api socket only)"
  _fnmb_collect_extra_vols
  if [ -S /var/run/trim_open_gateway_apiscope.socket ] || [ -e /var/run/trim_open_gateway_apiscope.socket ]; then
    _fnmb_push_bind /var/run/trim_open_gateway_apiscope.socket rw
  else
    _fnmb_log "skip open gateway socket (not found)"
  fi
  _fnmb_log "collect done, count=${#_FNMB_BIND_LINES[@]}"
}

_fnmb_resolve_docker_dirs() {
  local -a dirs=()
  local root d
  if [ -n "${TRIM_APPDEST:-}" ]; then
    dirs+=("${TRIM_APPDEST}/docker")
    dirs+=("${TRIM_APPDEST}/target/docker")
    dirs+=("${TRIM_APPDEST}/app/docker")
  fi
  root="$(cd -P "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
  dirs+=("${root}/app/docker")
  dirs+=("${root}/target/docker")

  for d in "${dirs[@]}"; do
    if [ -f "${d}/docker-compose.base.yaml" ] || [ -f "${d}/docker-compose.yaml" ]; then
      printf '%s\n' "$d"
    fi
  done | awk 'NF && !seen[$0]++'
}

_fnmb_render_compose_file() {
  local docker_dir="$1"
  local base out tmp optf line
  local -a opt_lines=()

  base="${docker_dir}/docker-compose.base.yaml"
  out="${docker_dir}/docker-compose.yaml"
  if [ ! -f "$base" ]; then
    _fnmb_log "no base in $docker_dir"
    return 1
  fi
  if ! grep -q 'FNMB_OPTIONAL_VOLUMES_BEGIN' "$base" 2>/dev/null; then
    _fnmb_log "no markers in base $base"
    return 1
  fi

  opt_lines+=("      # FNMB_OPTIONAL_VOLUMES_BEGIN")
  opt_lines+=("      # auto-generated — extra /volN and ups.conf")
  if [ "${#_FNMB_BIND_LINES[@]}" -gt 0 ]; then
    for line in "${_FNMB_BIND_LINES[@]}"; do
      opt_lines+=("$line")
    done
  else
    opt_lines+=("      # (no extra optional paths)")
  fi
  opt_lines+=("      # FNMB_OPTIONAL_VOLUMES_END")

  optf="${docker_dir}/.fnmb_opt_vols.$$"
  tmp="${out}.tmp.$$"
  printf '%s\n' "${opt_lines[@]}" >"$optf"
  awk -v opt_file="$optf" '
    BEGIN {
      while ((getline line < opt_file) > 0) { opts[++n] = line }
      close(opt_file)
      in_opt = 0
    }
    /FNMB_OPTIONAL_VOLUMES_BEGIN/ {
      for (i = 1; i <= n; i++) print opts[i]
      in_opt = 1
      next
    }
    /FNMB_OPTIONAL_VOLUMES_END/ {
      in_opt = 0
      next
    }
    in_opt { next }
    { print }
  ' "$base" >"$tmp"
  rm -f "$optf"

  if [ ! -s "$tmp" ]; then
    rm -f "$tmp"
    _fnmb_log "render failed for $docker_dir"
    return 1
  fi
  mv -f "$tmp" "$out"
  rm -f "${docker_dir}/docker-compose.override.yml"
  _fnmb_log "rendered $out"
  return 0
}

_fnmb_compose_recreate() {
  local docker_dir="$1"
  _fnmb_log "recreate in $docker_dir"
  (
    cd "$docker_dir" || exit 0
    export TRIM_PKGVAR="${TRIM_PKGVAR:-}"
    export TRIM_SERVICE_PORT="${TRIM_SERVICE_PORT:-18230}"
    export TRIM_APPNAME="${TRIM_APPNAME:-FnMessageBot}"
    export TRIM_API_TOKEN="${TRIM_API_TOKEN:-}"
    export TRIM_SYS_VERSION="${TRIM_SYS_VERSION:-}"
    if docker compose version >/dev/null 2>&1; then
      docker compose -f docker-compose.yaml up -d --force-recreate --remove-orphans
    elif command -v docker-compose >/dev/null 2>&1; then
      docker-compose -f docker-compose.yaml up -d --force-recreate --remove-orphans
    fi
  ) >>"${_FNMB_LOG:-/dev/null}" 2>&1 || _fnmb_log "recreate failed"
}

fnmb_write_vol_compose_override() {
  fnmb_write_compose_override "$@"
}

fnmb_write_compose_override() {
  local docker_dir do_recreate="${1:-}"
  _fnmb_log "fnmb_write_compose_override recreate=${do_recreate} APPDEST=${TRIM_APPDEST:-} PKGVAR=${TRIM_PKGVAR:-}"
  _fnmb_collect_optional_binds

  while IFS= read -r docker_dir; do
    [ -n "$docker_dir" ] || continue
    _fnmb_log "docker_dir=$docker_dir"
    if [ ! -f "${docker_dir}/docker-compose.base.yaml" ]; then
      _fnmb_log "skip (no base): $docker_dir"
      continue
    fi
    _fnmb_render_compose_file "$docker_dir" || continue
    if [ "$do_recreate" = "recreate" ]; then
      _fnmb_compose_recreate "$docker_dir"
    fi
  done < <(_fnmb_resolve_docker_dirs)

  return 0
}
