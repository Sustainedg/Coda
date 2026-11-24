#!/usr/bin/env bash
# Coda 容器入口：镜像封装 Python / Node / git 环境，并内置一份当前代码与 venv
# 作为离线兜底；应用版本仍由 /app 卷与更新器管理。
#
# 目录约定（与 app/selfupdate.py 的托管模式一致）：
#   /app/releases/<Vx.y>/   各版本目录（backend/ + frontend/dist + …）
#   /app/releases/current   软链 → 正在运行的版本目录
#   /app/data               数据目录（SQLite / 工作区 / 密钥），独立卷
#   /app/data/home          HOME：内核安装态、用户配置与会话（独立卷内，重建不丢）
#
# 启动逻辑：
#   1. current 已指向完整版本 → 直接启动（界面里更新后 execv 重启也走这里）
#   2. /app/releases 下有完整版本但没有 current → 选最高版本、建链、启动
#   3. 空卷 → 从 GitHub 下载最新 Release 装进去
#   4. 网络不可达 / 没有 Release → 用镜像自带的 /image-app 兜底
# 产物包按 app/selfupdate.py 的同一套安全基线处理：sha256 强校验、拒绝路径穿越与
# 符号链接、校验标志文件；校验失败 / 包不安全时直接拒绝安装，不拿旧代码顶替。
set -euo pipefail

APP_ROOT="/app"
RELEASES="$APP_ROOT/releases"
DATA_DIR="${CODA_DATA_DIR:-/app/data}"
REPO_SLUG="${CODA_UPDATE_REPO:-Sustainedg/Coda}"

#: 一个版本目录算「完整」的标志文件（与 selfupdate.py 的 _PAYLOAD_MARKERS 一致）
PAYLOAD_MARKERS=(backend/app/__init__.py backend/app/main.py frontend/dist/index.html)

# HOME 指向数据卷：npm 全局包与各内核自己的 ~/.claude / ~/.codex / ~/.hermes /
# ~/.gemini 都跟着数据卷走。允许运维覆盖 HOME / NPM_CONFIG_PREFIX，未覆盖时取默认值。
export HOME="${HOME:-$DATA_DIR/home}"
export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX:-$HOME/.local}"
case ":${PATH:-}:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:${PATH:-/usr/local/bin:/usr/bin:/bin}" ;;
esac

mkdir -p "$RELEASES" "$DATA_DIR" "$HOME" "$NPM_CONFIG_PREFIX"
# 上次被强杀（EXIT trap 没来得及跑）留下的安装临时目录
rm -rf "$RELEASES"/.install.* "$RELEASES"/.bundled.* 2>/dev/null || true

# ---- 工具函数 ----
log() { echo "[entrypoint] $*"; }
die() { echo "[entrypoint] ✗ $*" >&2; exit 1; }

#: 安装期代理：只作用于「下 Release / 装依赖 / 装内核」这些一次性动作，
#: 不污染智能体运行时（它们要直连网关）。用法：CODA_INSTALL_PROXY=http://host:port
install_proxy() {
  [ -n "${CODA_INSTALL_PROXY:-}" ] && printf '%s' "$CODA_INSTALL_PROXY" || true
}

# 在（可选）代理环境下执行命令：curl / git / uv / npm / pip 都认这几个变量。
#: 注意 pip **不认** CODA_INSTALL_PROXY，只认 HTTP(S)_PROXY —— 所以装依赖
#: 必须经由这里，否则就是一个直连 PyPI 的裸奔进程。
run_with_install_proxy() {
  local proxy
  proxy="$(install_proxy)"
  if [ -n "$proxy" ]; then
    HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy" http_proxy="$proxy" https_proxy="$proxy" "$@"
  else
    "$@"
  fi
}

#: 安装过程中的临时目录：成功路径显式清理，异常 / die 由 EXIT trap 兜底。
CLEANUP_DIRS=()
cleanup_add() { CLEANUP_DIRS+=("$1"); }
cleanup_now() { rm -rf "$1" 2>/dev/null || true; }
cleanup_all() {
  local d
  for d in "${CLEANUP_DIRS[@]}"; do
    rm -rf "$d" 2>/dev/null || true
  done
}
trap cleanup_all EXIT

version_key() {
  # V0.44.1 → "000044001"（用 0 填充对齐，sort 能直接比）
  local v="${1#V}"; v="${v#v}"
  printf '%s\n' "$v" | awk -F. '{ printf "%03d%03d%03d\n", $1+0, $2+0, $3+0 }'
}

payload_complete() {
  local dir="$1" marker
  for marker in "${PAYLOAD_MARKERS[@]}"; do
    [ -f "$dir/$marker" ] || return 1
  done
  return 0
}

pick_latest_dir() {
  local best="" best_key="" d key
  for d in "$RELEASES"/V*; do
    [ -d "$d" ] || continue
    # 半截解包 / 被删文件的目录不参与选择，避免选中后起不来
    payload_complete "$d" || continue
    key="$(version_key "$(basename "$d")")"
    if [ -z "$best_key" ] || [ "$key" -gt "$best_key" ]; then
      best="$d"; best_key="$key"
    fi
  done
  [ -n "$best" ] && echo "$best" || return 1
}

# 安全解压：约束与 app/selfupdate.py:_safe_extract 同一套。
#   拒绝绝对路径 / 父级穿越 / 符号链接 / 设备文件，限制文件数与解压体积。
# 退出码：0 成功；65 包内容不安全；66 不是有效 tar.gz；67 超出数量 / 体积上限。
safe_extract() {
  local archive="$1" dest="$2"
  python3 -I - "$archive" "$dest" <<'PY'
import os
import sys
import tarfile

UNSAFE, BAD, TOO_MANY = 65, 66, 67
MAX_FILES = 20_000
MAX_BYTES = 512 * 1024 * 1024

archive, dest = sys.argv[1], sys.argv[2]
root = os.path.realpath(dest)
count = 0
total = 0

try:
    tf = tarfile.open(archive, mode="r:gz")
except (tarfile.TarError, OSError):
    print("下载到的不是有效的 tar.gz 更新包", file=sys.stderr)
    sys.exit(BAD)

with tf:
    for member in tf.getmembers():
        parts = [p for p in member.name.replace("\\", "/").split("/") if p not in ("", ".")]
        if not parts or ".." in parts or member.name.startswith(("/", "\\")):
            print(f"更新包内路径不安全：{member.name}", file=sys.stderr)
            sys.exit(UNSAFE)
        if member.issym() or member.islnk():
            print(f"更新包内含符号链接：{member.name}", file=sys.stderr)
            sys.exit(UNSAFE)
        if not (member.isfile() or member.isdir()):
            print(f"更新包内含不支持的条目：{member.name}", file=sys.stderr)
            sys.exit(UNSAFE)
        target = os.path.realpath(os.path.join(root, *parts))
        # resolve 后必须仍在 dest 之下（双保险，防拼接逃逸）
        if target != root and not target.startswith(root + os.sep):
            print(f"更新包内路径不安全：{member.name}", file=sys.stderr)
            sys.exit(UNSAFE)
        if member.isdir():
            os.makedirs(target, exist_ok=True)
            continue
        count += 1
        total += member.size
        if count > MAX_FILES:
            print("更新包内文件过多，已拒绝安装", file=sys.stderr)
            sys.exit(TOO_MANY)
        if total > MAX_BYTES:
            print("更新包解压后体积过大，已拒绝安装", file=sys.stderr)
            sys.exit(TOO_MANY)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        source = tf.extractfile(member)
        if source is None:
            print(f"更新包内条目无法读取：{member.name}", file=sys.stderr)
            sys.exit(BAD)
        with source, open(target, "wb") as out:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
PY
}

install_latest_release() {
  log "空卷：从 GitHub 下载最新 Release（$REPO_SLUG）"
  # 下载 Release 元数据（走代理：容器内访问 GitHub 常需要）
  local api="https://api.github.com/repos/$REPO_SLUG/releases/latest"
  local curl_args=(--silent --show-error --fail --location)
  # 代理优先级：显式 HTTP(S)_PROXY > CODA_INSTALL_PROXY（安装期代理）
  local proxy="${HTTPS_PROXY:-${HTTP_PROXY:-${https_proxy:-${http_proxy:-$(install_proxy)}}}}"
  if [ -n "$proxy" ]; then
    curl_args+=(--proxy "$proxy")
  fi
  local meta asset_url tag version
  meta="$(curl "${curl_args[@]}" --max-time 60 -H 'Accept: application/vnd.github+json' "$api")" \
    || { install_bundled "GitHub 不可达（$api）"; return; }
  tag="$(printf '%s' "$meta" | grep -oP '(?<="tag_name": ")[^"]+' | head -1 || true)"
  asset_url="$(printf '%s' "$meta" | grep -oP '(?<="browser_download_url": ")[^"]+coda\.tar\.gz' | head -1 || true)"
  if [ -z "$tag" ] || [ -z "$asset_url" ]; then
    install_bundled "最新 Release（${tag:-未知版本}）没有 coda.tar.gz 产物"
    return
  fi
  version="V${tag#v}"; version="V${version#V}"

  local tmp stage rc sha actual
  tmp="$(mktemp -d)"
  cleanup_add "$tmp"
  log "下载 $version ← $asset_url"
  # 产物包可能几十 MB，桥接慢链路要给足时间（元数据请求才用 60 秒）
  if ! curl "${curl_args[@]}" --max-time 600 --output "$tmp/coda.tar.gz" "$asset_url"; then
    cleanup_now "$tmp"
    install_bundled "下载产物包失败"
    return
  fi
  # .sha256 资产存在就强制校验；取不到时只警告（与 selfupdate.py 的「无校验值则跳过」一致）
  if curl "${curl_args[@]}" --max-time 60 --output "$tmp/coda.tar.gz.sha256" "${asset_url}.sha256" 2>/dev/null; then
    sha="$(grep -oE '[0-9a-fA-F]{64}' "$tmp/coda.tar.gz.sha256" | head -1 || true)"
    if [ -n "$sha" ]; then
      sha="${sha,,}"
      actual="$(sha256sum "$tmp/coda.tar.gz" | cut -d' ' -f1)"
      if [ "$actual" != "$sha" ]; then
        # 校验失败是安全信号：宁可起不来，也不悄悄退回旧代码
        die "产物包 sha256 校验失败（期望 ${sha:0:12}…，实际 ${actual:0:12}…），拒绝安装"
      fi
      log "sha256 校验通过"
    fi
  else
    log "⚠ 未取到 .sha256 资产，跳过校验"
  fi

  # 先解到同卷的临时目录，校验完整后再原子改名：中途失败不会留下半截版本
  stage="$(mktemp -d "$RELEASES/.install.XXXXXX")"
  cleanup_add "$stage"
  rc=0
  safe_extract "$tmp/coda.tar.gz" "$stage" || rc=$?
  case "$rc" in
    0) ;;
    65|67) die "产物包未通过安全校验（见上一行），拒绝安装" ;;
    *) cleanup_now "$tmp"; cleanup_now "$stage"; install_bundled "产物包解压失败"; return ;;
  esac
  if ! payload_complete "$stage"; then
    cleanup_now "$tmp"; cleanup_now "$stage"
    install_bundled "产物包内容不完整"
    return
  fi
  rm -rf "$RELEASES/$version"
  mv "$stage" "$RELEASES/$version"
  cleanup_now "$tmp"
  cleanup_now "$stage"
  log "已安装 $version"
}

# 用镜像自带的代码（/image-app）起一个版本 —— 这是**空卷时的默认路径**。
# 构建镜像时源码就已经 COPY 进去了，「构建什么就运行什么」，版本升级统一走
# 界面里的更新机制（下载 Release → releases/<版本>/ → 原子切 current → execv 重启）。
install_bundled() {
  local reason="${1:-使用镜像自带代码}"
  local bundled="/image-app"
  if ! payload_complete "$bundled"; then
    die "$reason，且镜像没有完整的自带代码兜底（/image-app 缺少标志文件）。离线部署请先手动把版本目录放进 $RELEASES 再启动。"
  fi
  local version stage
  version="$(grep -oP '(?<=^__version__ = ")[^"]+' "$bundled/backend/app/__init__.py" 2>/dev/null || true)"
  [ -n "$version" ] || die "$reason，且读不到镜像自带代码的版本号（/image-app/backend/app/__init__.py）"
  log "$reason → 用镜像自带代码 $version 兜底"
  stage="$(mktemp -d "$RELEASES/.bundled.XXXXXX")"
  cleanup_add "$stage"
  cp -a "$bundled/." "$stage/"
  payload_complete "$stage" || die "复制镜像自带代码后仍不完整，无法启动"
  rm -rf "$RELEASES/$version"
  mv "$stage" "$RELEASES/$version"
  cleanup_now "$stage"
  log "已安装 $version（兜底；有 Release 后可在界面里点击更新）"
}

# ---- 选定要运行的版本 ----
# current 指向的版本必须完整（半截解包 / 文件被删时重新选择）
if [ -d "$RELEASES/current/" ] && ! payload_complete "$RELEASES/current"; then
  log "current 指向的版本不完整，重新选择"
  rm -f "$RELEASES/current"
fi
if [ ! -e "$RELEASES/current" ] || [ ! -d "$RELEASES/current/" ]; then
  if ! pick_latest_dir >/dev/null 2>&1; then
    # 空卷：默认用镜像自带代码。镜像构建时已经把源码打进去了，
    # 再去 GitHub 拉 Release 只会把「构建进去的新代码」换成线上旧包。
    # 确实想沿用旧行为（空卷先联网拉最新 Release）时设
    # CODA_FETCH_RELEASE_ON_EMPTY=1。
    if [ "${CODA_FETCH_RELEASE_ON_EMPTY:-0}" = "1" ]; then
      install_latest_release
    else
      install_bundled
    fi
  fi
  TARGET="$(pick_latest_dir)"
  ln -sfn "$TARGET" "$RELEASES/current"
  log "current → $(basename "$TARGET")"
fi

RUN_DIR="$RELEASES/current"
[ -f "$RUN_DIR/backend/requirements.txt" ] || die "$RUN_DIR 里没有 backend/requirements.txt"

# ---- 依赖（版本变了 requirements 也会变；用标记文件避免每次启动重装）----
# 镜像里预装了一份 venv 模板（/image-venv），首启直接复制它，避免容器里联网 pip install；
# requirements 与模板不一致（Release 带了新依赖）才回退到在线安装。
#
# 在线安装必须**又快又不挂住启动**：
#   * 走 CODA_INSTALL_PROXY（pip 只认 HTTP(S)_PROXY，不认那个变量名本身）；
#   * 可用 CODA_PIP_INDEX_URL 指到国内镜像（清华 / 阿里），绕开 PyPI 直连；
#   * 收紧重试与超时 —— 网络不通时快速失败并给出可执行的提示，而不是静默卡二十分钟。
VENV="$APP_ROOT/venv"
MARKER="$VENV/.requirements-stamp"
STAMP="$(cat "$RUN_DIR/backend/requirements.txt" | sha256sum | cut -d' ' -f1)"

#: pip 安装参数：镜像源可选，重试/超时写死成「快速失败」。
pip_install_args() {
  local args=(-q --disable-pip-version-check --retries 2 --timeout 15)
  local index="${CODA_PIP_INDEX_URL:-}"
  if [ -n "$index" ]; then
    args+=(-i "$index")
    # 国内镜像多是纯 https 单源，额外源反而会拖慢解析；用 host 信任即可。
    args+=(--trusted-host "$(printf '%s' "$index" | sed -E 's#^[a-z]+://##; s#/.*$##')")
  fi
  printf '%s\n' "${args[@]}"
}

if [ ! -x "$VENV/bin/python" ] || [ "$(cat "$MARKER" 2>/dev/null || true)" != "$STAMP" ]; then
  IMAGE_VENV_STAMP=""
  if [ -x /image-venv/bin/python ] && [ -f /image-venv/.requirements-stamp ]; then
    IMAGE_VENV_STAMP="$(cat /image-venv/.requirements-stamp 2>/dev/null || true)"
  fi
  if [ "$IMAGE_VENV_STAMP" = "$STAMP" ]; then
    log "从镜像预装 venv 复制虚拟环境（离线可用）"
    rm -rf "$VENV"
    cp -a /image-venv "$VENV"
  else
    log "准备虚拟环境（requirements 与镜像模板不一致，在线安装）"
    if [ -n "$(install_proxy)" ]; then
      log "pip 走安装期代理 $(install_proxy)"
    elif [ -n "${CODA_PIP_INDEX_URL:-}" ]; then
      log "pip 走镜像源 $CODA_PIP_INDEX_URL"
    else
      log "⚠ 未配置安装期代理或镜像源，将直连 PyPI（国内网络可能很慢）"
    fi
    python3 -m venv "$VENV"
    mapfile -t PIP_ARGS < <(pip_install_args)
    run_with_install_proxy "$VENV/bin/pip" install "${PIP_ARGS[@]}" -U pip
    run_with_install_proxy "$VENV/bin/pip" install "${PIP_ARGS[@]}" \
      -r "$RUN_DIR/backend/requirements.txt"
  fi
  echo "$STAMP" > "$MARKER"
fi

# ---- 内核：镜像不预装，全部装在 HOME 数据卷（$NPM_CONFIG_PREFIX）里 ----
# 这样 Claude Code / Codex 的安装态跟着数据卷走，容器重建不需要重装。
# 用户在界面里「卸载」某个内核时删的是 HOME 下 npm 全局目录里的包，重启后这里补回去；
# 装失败不致命（界面里仍可手动装）。网络不通时可用 CODA_SKIP_KERNEL_INSTALL=1 跳过。
ensure_kernels() {
  # 内核已经物化在数据卷里、或运维自己管理内核时，可跳过启动期补装，
  # 避免 GitHub / npm 不通时把启动拖住（界面里仍可手动安装）。
  if [ "${CODA_SKIP_KERNEL_INSTALL:-0}" = "1" ]; then
    log "跳过启动期内核补装（CODA_SKIP_KERNEL_INSTALL=1）"
    return 0
  fi
  command -v npm >/dev/null 2>&1 || return 0
  local pkg
  for pkg in @anthropic-ai/claude-code @openai/codex; do
    local bin
    case "$pkg" in
      @anthropic-ai/claude-code) bin=claude ;;
      @openai/codex)             bin=codex ;;
    esac
    if ! command -v "$bin" >/dev/null 2>&1; then
      log "补装内核 $bin（$pkg，最多 5 分钟）"
      timeout 300 run_with_install_proxy npm install -g "$pkg" >/dev/null 2>&1 \
        || log "⚠ 补装 $bin 失败或超时，可在界面里手动安装"
    fi
  done
  # Hermes：curl 脚本安装（不是 npm 包）。它要 clone 仓库 + 下 uv / Python / Node +
  # 装依赖，慢网络下 5 分钟远远不够；默认给 20 分钟，可用 CODA_KERNEL_INSTALL_TIMEOUT 覆盖。
  if ! command -v hermes >/dev/null 2>&1; then
    log "补装内核 hermes（最多 ${CODA_KERNEL_INSTALL_TIMEOUT:-1200} 秒，超时跳过）"
    timeout "${CODA_KERNEL_INSTALL_TIMEOUT:-1200}" \
      run_with_install_proxy bash -c 'curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash' \
      >/dev/null 2>&1 \
      || log "⚠ 补装 hermes 失败或超时，可在界面里手动安装"
  fi
}

# ---- 技能播种：让「技能」页在内核刚装好时就有内容 ----
# 各内核的内置技能由**各自的安装器 / 首次运行**播种进 HOME，镜像不预置：
#   Codex  → ~/.codex/skills/.system（`codex debug prompt-input` 触发，离线可跑）
#   Hermes → ~/.hermes/skills（`hermes --run-module tools.skills_sync`，幂等）
#   Claude → ~/.claude/skills（用户技能，随用户自己的配置走；镜像不代建）
# 只做**幂等补齐**：已有技能就跳过，绝不覆盖用户改过的内容。
#
# 注意：这里**不受 CODA_SKIP_KERNEL_INSTALL 影响**。那个开关管的是「装内核」，
# 而播种只对**已经装好的内核**做补齐 —— 用户自己点安装的内核同样需要它的内置技能，
# 否则「技能」页永远只有市场技能、看不到内核自带的那些。
# 需要连播种也跳过时，单独设 CODA_SKIP_KERNEL_SKILLS=1。
seed_kernel_skills() {
  [ "${CODA_SKIP_KERNEL_SKILLS:-0}" = "1" ] && return 0

  if command -v codex >/dev/null 2>&1 && [ ! -d "$HOME/.codex/skills/.system" ]; then
    log "播种 Codex 系统技能"
    timeout 120 codex debug prompt-input >/dev/null 2>&1 || true
  fi

  if command -v hermes >/dev/null 2>&1; then
    local skills_dir="$HOME/.hermes/skills"
    if [ ! -d "$skills_dir" ] \
      || [ -z "$(find "$skills_dir" -name SKILL.md -print -quit 2>/dev/null)" ]; then
      log "播种 Hermes 内置技能"
      timeout 300 hermes --run-module tools.skills_sync >/dev/null 2>&1 || true
    fi
  fi
}
# npm 全局 bin 与用户 ~/.local/bin 都放进 PATH，让安装后的内核命令能被找到。
# npm 缺失时不能让 `set -e` 直接退出（界面里仍可手动安装），因此先取前缀再判断。
NPM_PREFIX="$(npm prefix -g 2>/dev/null || true)"
if [ -n "$NPM_PREFIX" ]; then
  NPM_BIN="$NPM_PREFIX/bin"
  [ -d "$NPM_BIN" ] && case ":$PATH:" in *":$NPM_BIN:"*) ;; *) export PATH="$NPM_BIN:$PATH" ;; esac
fi
[ -d "$HOME/.local/bin" ] && case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
ensure_kernels
seed_kernel_skills

# ---- 启动（exec：uvicorn 接管 PID 1，界面更新时 os.execv 原地替换的就是它）----
export CODA_DATA_DIR="$DATA_DIR"
log "启动 Coda（$(basename "$RUN_DIR")）→ :${CODA_PORT:-8010}"
cd "$RUN_DIR/backend"
exec "$VENV/bin/python" -m uvicorn app.main:app \
  --host "${CODA_HOST:-0.0.0.0}" --port "${CODA_PORT:-8010}"
