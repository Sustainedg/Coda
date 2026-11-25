"""内置适配器共用的小工具函数。"""
from __future__ import annotations

import json
import sys
from typing import Any


def truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in value.replace(",", " ").split() if part]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)]


def flatten_content(content: Any, limit: int = 4000) -> str:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        text = "\n".join(parts)
    elif content is None:
        text = ""
    else:
        text = json.dumps(content, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"\n… (+{len(text) - limit} chars)"


def dig(payload: Any, *path: str, default: Any = None) -> Any:
    cursor = payload
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            return default
        cursor = cursor[key]
    return cursor


#: 「缓存 token 与 input_tokens 分离」的字段（Anthropic / Claude Code 记法）。
_EXCLUSIVE_CACHE_KEYS = (
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)
_INPUT_KEYS = ("input_tokens", "inputTokens", "prompt_tokens")
_CACHE_READ_KEYS = (
    "cache_read_input_tokens",
    "cached_input_tokens",
    "cachedInputTokens",
    "cached_tokens",
    "prompt_cache_hit_tokens",
    "cache_read_tokens",
)
_CACHE_CREATE_KEYS = (
    "cache_creation_input_tokens",
    "cache_creation_tokens",
    "cache_write_input_tokens",
    "cacheWriteInputTokens",
)


def _number(usage: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return 0


def context_tokens(usage: Any) -> int:
    """从一次模型请求的 usage 里取出**输入侧 token 总数**（上下文占用）。

    不同内核的记法不一样：
    * Anthropic 风格（``input_tokens`` + ``cache_read_input_tokens``）两者分离，需要相加；
    * OpenAI 风格（``prompt_tokens`` 含 ``cached_tokens``）缓存是子集，不能重复相加。
    """
    if not isinstance(usage, dict) or not usage:
        return 0
    total = _number(usage, _INPUT_KEYS)
    if any(key in usage for key in _EXCLUSIVE_CACHE_KEYS):
        total += _number(usage, _CACHE_READ_KEYS) + _number(usage, _CACHE_CREATE_KEYS)
    return total if total > 0 else 0


#: npm 全局安装 / 更新的包装脚本（Node 实现，三平台同一套逻辑）。
#:
#: 为什么不再用 ``sh -c``：Windows 上没有 ``sh``（除非用户装了 Git Bash），
#: ``npm install -g`` 会在「未找到命令 sh」上直接失败 —— 桌面版必须能跑。
#: 包装脚本改由 Node 执行（npm 本身就意味着 node 可用；桌面版还内嵌了一份），
#: 路径、软链、.cmd 垫片这些差异全部在脚本里按 ``process.platform`` 处理。
_NPM_INSTALL_JS = r"""
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync, execFileSync } = require('node:child_process');
const WIN = process.platform === 'win32';
const NPM = WIN ? 'npm.cmd' : 'npm';
const read = (args) => execFileSync(NPM, args, { encoding: 'utf8', shell: WIN }).trim();
const prefix = read(['prefix', '-g']);
const root = read(['root', '-g']);
const bin = WIN ? prefix : path.join(prefix, 'bin');
// Windows 上 npm 生成的 .cmd / .ps1 是**本前缀自己的**普通文件（不是链接），
// 桌面版又把前缀钉在自己 userData 里，不存在「另一个 node 占着同名垫片」的场景，
// 因此不需要挪占位；直接交给 npm 覆盖。POSIX 才保留链接检查。
const names = WIN ? [] : [''];
const name = __BIN_NAME__;
const spec = __SPEC__;
const moved = [];
for (const ext of names) {
  const link = path.join(bin, name + ext);
  if (!fs.existsSync(link)) continue;
  let ours = false;
  try {
    const real = fs.realpathSync(link);
    ours = real === root || real.startsWith(root + path.sep);
  } catch (e) {
    ours = false;
  }
  if (ours) continue;
  const bak = path.join(bin, '.' + name + ext + '.coda-bak');
  fs.renameSync(link, bak);
  moved.push([link, bak]);
  console.log('已把本机既有的 ' + link + ' 挪到 ' + bak + '：它不属于平台的这份安装，npm 不肯直接覆盖');
}
const registryArgs = __REGISTRY_ARGS__;
const result = spawnSync(NPM, ['install', '-g', spec, ...registryArgs], { stdio: 'inherit', shell: WIN });
if (result.status !== 0 && moved.length) {
  for (const [link, bak] of moved) {
    try { fs.renameSync(bak, link); } catch (e) {}
  }
  console.log('安装未成功，已把 ' + moved.map((m) => m[0]).join('、') + ' 放回原位');
}
process.exit(result.status === null ? 1 : result.status);
"""

#: 清扫脚本（Node 实现）：删掉 npm 上次被中断留下的 rename 中转目录。
_NPM_PREPARE_JS = r"""
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const WIN = process.platform === 'win32';
const NPM = WIN ? 'npm.cmd' : 'npm';
const root = execFileSync(NPM, ['root', '-g'], { encoding: 'utf8', shell: WIN }).trim();
for (const [scope, name] of __SPECS__) {
  const dir = scope ? path.join(root, scope) : root;
  let entries = [];
  try { entries = fs.readdirSync(dir); } catch (e) { continue; }
  for (const entry of entries) {
    if (entry.startsWith('.' + name + '-')) {
      const target = path.join(dir, entry);
      fs.rmSync(target, { recursive: true, force: true });
      console.log('已清掉残留：' + target);
    }
  }
}
"""


def is_windows(platform: str | None = None) -> bool:
    """当前（或指定的）平台是不是 Windows。单测用它验证 Windows 分支，不必真在 Windows 上跑。"""
    return (platform or sys.platform) == "win32"


def npm_cli(*args: str, platform: str | None = None) -> list[str]:
    """npm 命令的跨平台形态（卸载 / 查询最新版本这类直连 npm 的动作）。

    POSIX 上直接 exec ``npm``（它是带 shebang 的 JS 入口，可执行）；
    Windows 上 npm 是 ``npm.cmd``，CreateProcess 不认识 ``.cmd``，必须借
    ``cmd.exe /d /s /c`` 解释 —— 否则会在「未找到命令 npm」上失败。
    """
    if is_windows(platform):
        return ["cmd", "/d", "/s", "/c", "npm", *args]
    return ["npm", *args]


def npm_prepare_argv(*packages: str) -> list[str]:
    """构造「清掉 npm 上一次被中断留下的中转目录」的清扫命令。

    npm 在卸载 / 覆盖安装前，会把旧目录 rename 成一个隐藏中转目录
    （``.<包名>-<绝对路径的 sha1 前 8 位>``）再删除。这一步若被打断（超时、服务重启），
    残留目录会让之后**每一次**安装 / 更新 / 卸载都永久失败在 ``ENOTEMPTY``：
    中转名只由绝对路径决定，每次都一样，rename 永远撞上那个非空目录。

    这里按包名先把残留清掉 —— 「用哪个包管理器、残留叫什么名字」是适配器的知识，
    所以这份声明写在适配器层（铁律 1），框架只负责照常执行 ``prepare_argv``。
    脚本由 Node 执行（Windows 没有 ``sh``，见 ``_NPM_PREPARE_JS`` 的注释）。
    """
    pairs: list[list[str]] = []
    for package in packages:
        # 作用域包（@scope/name）的残留目录在 <root>/<scope>/ 下，普通包在 <root>/ 下
        scope, _, name = package.rpartition("/")
        pairs.append([scope, name])
    script = _NPM_PREPARE_JS.replace("__SPECS__", json.dumps(pairs, ensure_ascii=False))
    return ["node", "-e", script]


def npm_install_argv(
    package: str,
    *,
    bin_name: str,
    latest: bool = False,
    registry: str | None = None,
) -> list[str]:
    """构造「npm 全局安装 / 更新」的命令，顺带处理本机既有的 bin 占位。

    本机的 ``<npm 前缀>/bin/<bin_name>`` 可能已经被别的安装占着（例如系统里另一个
    node 装的同一内核，或用户手写的垫片）。npm 只肯覆盖**指向自己安装目录**的链接，
    否则整个动作失败在 ``EEXIST: file already exists``（连包都不装）。

    这里先看那个占位是不是我们的（解析后落在 npm 全局目录里）：不是就挪到一个隐藏备份名
    让出位置；万一 npm 自己失败，再把备份放回去 —— 不留「命令没了、包也没装上」的半截状态。
    备份留在原地，用户随时可以自己挪回去。占位本来就是我们的链接时为无操作。

    Windows 上不挪占位：桌面版把 npm 全局前缀钉在 App 私有的 userData 目录里，
    那里的 ``.cmd`` / ``.ps1`` 垫片都是本前缀自己的（不是链接），npm 直接覆盖即可；
    也就不存在「另一个 node 占着同名命令」的冲突场景。

    ``registry`` 用于官方文档明确要求指定源的包（镜像站可能没有）：只影响这一次安装，
    不改用户本机的 npm 配置。
    """
    spec = f"{package}@latest" if latest else package
    registry_args = ["--registry", registry] if registry else []
    script = (
        _NPM_INSTALL_JS.replace("__BIN_NAME__", json.dumps(bin_name))
        .replace("__SPEC__", json.dumps(spec))
        .replace("__REGISTRY_ARGS__", json.dumps(registry_args))
    )
    return ["node", "-e", script]


def workspace_only(ctx: Any) -> bool:
    """智能体是否选了「文件访问范围 = 仅工作目录」。

    ``file_access_scope`` 是多个内核共用的语义（Claude Code / Codex 等各自
    落到自己的机制），读取方式只有这一份，避免各适配器自己写默认值时对不齐。
    """
    return (ctx.option("file_access_scope") or "unrestricted") == "workspace_only"
