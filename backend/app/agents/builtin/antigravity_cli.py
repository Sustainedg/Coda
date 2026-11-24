"""Google Antigravity CLI（``agy``）适配器。

Antigravity CLI 是 Google 的终端智能体，底层沿用 Gemini CLI 的代码谱系
（配置落在 ``~/.gemini/antigravity-cli/``、``-p`` / ``--output-format stream-json``
等旗标与 Gemini 同形）。它是**账号登录型**内核：

* 没有可注入的 API Key，平台不接管模型路由（不声明 ``api_key_env`` /
  ``base_url_env`` / ``default_gateway``，启动播种会跳过）；
* 登录态靠一次 Google OAuth 授权写进内核自己的数据目录；无桌面时用
  ``ANTIGRAVITY_REMOTE_OOB_AUTH=1`` 可让内核走「打印链接 → 回填授权码」的
  非交互流程（见 ``antigravity_login.py``）；
* 人格拼进提示词：内核只读工作目录的 ``AGENTS.md``，写进去会污染用户项目。

平台与适配器的边界不变：这里只描述「怎么装、怎么启动、怎么解析、配置落在哪」。
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

from ..base import (
    AgentContext,
    AgentDefinition,
    AgentRun,
    AuthStatus,
    Capabilities,
    ConfigField,
    FieldType,
    Invocation,
    Runtime,
    RuntimeProvisioner,
)
from ..events import (
    AgentEvent,
    error,
    log,
    session,
    text_delta,
    tool_call,
    tool_result,
    usage,
)
from ..registry import register_agent
from .util import as_list, flatten_content

#: 官方安装脚本（下载并校验签名的原生产物）。`agy` 会随后台自更新。
_INSTALL_URL = "https://antigravity.google/cli/install.sh"

#: 登录助手：无桌面环境用 OOB 模式拿到「授权链接 + 回填授权码」。
_LOGIN_HELPER = Path(__file__).resolve().with_name("antigravity_login.py")

#: 内核自己的全局配置目录（日志里确认：``$HOME/.gemini/antigravity-cli``）。
#: 与 Gemini CLI 共用 ``.gemini`` 根，但子目录独立。
_APP_DIRNAME = Path(".gemini") / "antigravity-cli"

#: 扫描凭据时忽略的目录：日志 / 会话库 / 崩溃报告与登录态无关，读它们只会拖慢探测。
_SKIP_DIRS = {"log", "crashes", "conversations", "brain", "cache", "updater", "builtin"}

#: 单个凭据文件的大小上限；token 文件远小于这个值。
_MAX_CRED_BYTES = 512 * 1024


def app_data_dir() -> Path:
    """Antigravity CLI 的数据目录。"""
    return Path.home() / _APP_DIRNAME


def _iter_json_files(root: Path) -> Iterable[Path]:
    """遍历数据目录里的小 JSON 文件（跳过日志 / 会话库等无关子目录）。"""
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if not name.endswith(".json"):
                continue
            path = Path(current) / name
            try:
                if path.stat().st_size > _MAX_CRED_BYTES:
                    continue
            except OSError:
                continue
            yield path


def _find_token(value: Any, depth: int = 0) -> dict[str, Any] | None:
    """在任意嵌套的 JSON 结构里找「带 access/refresh token 的那一层」。"""
    if depth > 4:
        return None
    if isinstance(value, dict):
        if value.get("access_token") or value.get("refresh_token"):
            return value
        for item in value.values():
            found = _find_token(item, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_token(item, depth + 1)
            if found is not None:
                return found
    return None


def read_credentials() -> dict[str, Any] | None:
    """尽力而为地读出一份已登录凭据；文件缺失 / 损坏都返回 ``None``。

    Antigravity 的凭据文件名没有公开约定，这里**不猜文件名**：扫描数据目录里的
    小 JSON，找带 ``access_token`` / ``refresh_token`` 的那一层。
    """
    root = app_data_dir()
    if not root.is_dir():
        return None
    for path in _iter_json_files(root):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        token = _find_token(data)
        if token is not None:
            # 账号信息可能和 token 不在同一层（例如外层是 ``{email, tokens}``）：
            # 合并一层外层字段，账号展示才拿得到邮箱。
            merged = dict(token)
            if isinstance(data, dict):
                for key in ("email", "account", "user_email"):
                    if data.get(key) and not merged.get(key):
                        merged[key] = data[key]
            return merged
    return None


def _email_from_id_token(token: Any) -> str | None:
    """从 OIDC 的 ``id_token``（JWT）里取邮箱，取不到就返回 ``None``。"""
    if not isinstance(token, str) or token.count(".") < 2:
        return None
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, OSError):
        return None
    email = data.get("email") if isinstance(data, dict) else None
    return email if isinstance(email, str) and email.strip() else None


def account_of(credentials: dict[str, Any] | None) -> str | None:
    """凭据里的账号标识（优先邮箱）。"""
    if not credentials:
        return None
    for key in ("email", "account", "user_email"):
        value = credentials.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _email_from_id_token(credentials.get("id_token"))


@register_agent
class AntigravityCliAgent(AgentDefinition):
    id = "antigravity-cli"
    name = "Antigravity CLI"
    #: 取代已下线的 Gemini CLI：启动时框架把存量智能体迁到这里（框架不硬编码这个映射）。
    legacy_ids = ("gemini-cli",)
    vendor = "Google"
    description = (
        "Google 的终端智能体（Antigravity CLI）。账号登录后可直接分析代码、"
        "读写文件、执行命令，输出逐行 JSON，支持多轮会话续接。"
    )
    runtime = Runtime.CLI
    executable = "agy"
    provisioner = RuntimeProvisioner(
        manager="官方脚本",
        package="antigravity-cli",
        #: 官方安装脚本会下载并按 SHA512 校验原生产物，装到 ``~/.local/bin/agy``。
        #: 代理由框架注入（``_provision_env``），Google 域名在国内必须走代理。
        install_argv=["bash", "-c", f"curl -fsSL {_INSTALL_URL} | bash"],
        #: ``agy update`` 自带增量自更新；日常运行也会后台自更新。
        update_argv=["agy", "update"],
        uninstall_argv=[
            sys.executable or "python3",
            "-c",
            "import pathlib; (pathlib.Path.home()/'.local'/'bin'/'agy').unlink(missing_ok=True)",
        ],
        #: 没有「查询最新版本」子命令（自更新），保持空列表：界面不显示「检查更新」。
        latest_argv=[],
        #: 无桌面时用 OOB 模式登录：助手把链接 / 二维码 / 授权码输入框推给界面。
        login_argv=[sys.executable or "python3", str(_LOGIN_HELPER)],
        homepage="https://antigravity.google/docs/cli-getting-started",
        note=(
            "登录：在「内核管理」点「登录」会发起一次 Google OAuth，显示授权链接与"
            "二维码；用浏览器登录后把页面上的授权码粘回界面即可（授权窗口约 60 秒，"
            "请先把浏览器打开好）。Google 域名在国内需要代理，请先在「设置 · 网络代理」"
            "里配好，否则安装、登录与调用都会超时。"
        ),
    )
    #: 账号型内核：平台没有可注入的密钥，也不接管模型路由。
    api_key_env = None
    base_url_env = None
    default_gateway = None
    #: 模型的真实取值由内核按账号能力决定；平台不填默认值，避免传一个内核不认的名字。
    default_model = None
    protocol = "openai"
    default_auth_style = "bearer"
    docs_url = "https://antigravity.google/docs/cli-getting-started"
    #: Antigravity 官方图标（蓝底 A 形 + 彩色光晕）。
    logo = "/icons/kernels/antigravity.webp"
    logo_style = "image"
    capabilities = Capabilities(
        streaming=True,
        sessions=True,
        #: 只认工作目录的 AGENTS.md；写进去会改用户项目，所以人格拼提示词。
        native_system_prompt=False,
        tools=True,
        #: 本地 MCP 配置面尚未确认（``agy mcp`` 的落盘格式未知），先不声明假 mount。
        mcp=False,
        permission_modes=True,
        working_directory=True,
    )
    config_fields = (
        ConfigField(
            "approval_mode",
            "审批模式",
            FieldType.SELECT,
            default="yolo",
            options=[
                {"value": "yolo", "label": "yolo · 自动批准所有操作"},
                {"value": "auto_edit", "label": "auto_edit · 自动应用文件编辑"},
                {"value": "default", "label": "default · 每次询问"},
                {"value": "plan", "label": "plan · 只读规划模式"},
            ],
        ),
        ConfigField(
            "sandbox",
            "启用沙箱",
            FieldType.BOOLEAN,
            default=False,
            advanced=True,
            help="用内核自带的沙箱限制终端操作；需要本机支持沙箱运行环境。",
        ),
        ConfigField("extra_args", "额外命令行参数", FieldType.STRING_LIST, advanced=True),
        ConfigField("env", "环境变量", FieldType.KEYVALUE, advanced=True),
    )

    # -- 登录状态 ------------------------------------------------------------

    def auth_status(self) -> AuthStatus:
        """尽力而为地判断本机是否已有可用的登录态（不联网、不启动内核）。

        凭据文件名没有公开约定，所以按内容探测：扫描数据目录里的小 JSON，
        找到带 ``access_token`` / ``refresh_token`` 的那一层即视为已登录。
        """
        credentials = read_credentials()
        if credentials is None:
            return AuthStatus(required=True, logged_in=False, detail="尚未登录 Google 账号")
        account = account_of(credentials)
        return AuthStatus(
            required=True,
            logged_in=True,
            detail=f"已登录：{account}" if account else "已登录",
            account=account,
        )

    # -- 构造启动命令 --------------------------------------------------------

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        argv: list[str] = [self.executable or "agy", "--output-format", "stream-json"]

        mode = ctx.option("approval_mode") or "yolo"
        if mode == "yolo":
            argv.append("--dangerously-skip-permissions")
        elif mode == "auto_edit":
            argv += ["--mode", "accept-edits"]
        elif mode == "plan":
            argv += ["--mode", "plan"]
        # default：不带任何自动批准旗标，交给内核逐次询问。

        # ``default_model`` 是平台哨兵；传下去会被内核当成非法模型名，跳过硬编码值。
        if ctx.model and ctx.model != self.default_model:
            argv += ["--model", ctx.model]
        if ctx.session_id:
            argv += ["--conversation", ctx.session_id]
        if ctx.option("sandbox"):
            argv.append("--sandbox")
        for arg in as_list(ctx.option("extra_args") or []):
            argv.append(str(arg))

        prompt = ctx.prompt or ""
        if ctx.system_prompt:
            # 内核没有系统提示词旗标，AGENTS.md 又属于用户项目：拼在最前面最安全。
            prompt = f"{ctx.system_prompt}\n\n---\n\n{prompt}"

        env = self.effective_env(ctx)
        env.setdefault("CI", "1")
        env.setdefault("NO_COLOR", "1")

        # 提示词走 stdin（``-p ""`` 在打印模式下读取 stdin）：规避命令行长度与引号问题。
        argv += ["-p", ""]
        return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=prompt)

    # -- 解析输出 ------------------------------------------------------------

    def parse_line(self, line: str, run: AgentRun) -> Iterable[AgentEvent]:
        line = line.strip()
        if not line:
            return
        if not line.startswith("{"):
            yield log(line, "debug")
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            yield log(line, "debug")
            return
        if not isinstance(payload, dict):
            yield log(line, "debug")
            return
        yield from self._dispatch(payload, run)

    def _dispatch(self, payload: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        kind = str(payload.get("type") or payload.get("event") or "").lower()
        subtype = str(payload.get("subtype") or "").lower()

        # 会话建立：Gemini 谱系用 init，Claude Code 谱系用 system + subtype=init。
        sid = payload.get("session_id") or payload.get("conversation_id")
        if sid and not run.state.get("session"):
            run.state["session"] = sid
            yield session(str(sid))
        if kind in ("init", "start", "session") or (kind == "system" and subtype == "init"):
            if payload.get("model"):
                yield log(f"Antigravity 已就绪 · 模型 {payload['model']}")
            return
        if kind == "system":
            return

        if kind == "error":
            run.state["is_error"] = True
            yield error(flatten_content(payload.get("message") or payload))
            return

        # Claude Code 风格的嵌套消息：assistant / user 的 message.content 是块列表。
        message = payload.get("message")
        if isinstance(message, dict):
            produced = False
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = str(block.get("type") or "").lower()
                    if btype in ("text", "output_text") and block.get("text"):
                        if kind == "assistant":
                            run.state["emitted_text"] = True
                            yield text_delta(str(block["text"]))
                            produced = True
                    elif btype in ("tool_use", "tool_call"):
                        args = block.get("input")
                        yield tool_call(
                            str(block.get("name") or "tool"),
                            block.get("id"),
                            **(args if isinstance(args, dict) else {}),
                        )
                        produced = True
                    elif btype == "tool_result":
                        yield tool_result(
                            block.get("tool_use_id") or block.get("id"),
                            flatten_content(block.get("content")),
                            bool(block.get("is_error")),
                        )
                        produced = True
            elif isinstance(content, str) and kind == "assistant":
                run.state["emitted_text"] = True
                yield text_delta(content)
                produced = True
            if produced:
                return

        if kind in ("tool_call", "tool_use", "tool"):
            name = payload.get("tool_name") or payload.get("name") or "tool"
            args = payload.get("parameters") or payload.get("args") or payload.get("input") or {}
            yield tool_call(
                str(name),
                payload.get("tool_id") or payload.get("id"),
                **(args if isinstance(args, dict) else {"input": args}),
            )
            return

        if kind in ("tool_result", "tool_output"):
            yield tool_result(
                payload.get("tool_id") or payload.get("id"),
                flatten_content(payload.get("output") or payload.get("result")),
                bool(payload.get("is_error") or payload.get("error")),
            )
            return

        if kind in ("result", "done", "complete", "finish"):
            stats = payload.get("stats") or payload.get("usage") or {}
            if isinstance(stats, dict):
                counters = {
                    k: v
                    for k, v in stats.items()
                    if str(k).endswith("_tokens") or k in ("duration_ms", "total_tokens")
                }
                if counters:
                    yield usage(**counters)
            if payload.get("error") or payload.get("is_error"):
                run.state["is_error"] = True
                yield error(flatten_content(payload.get("error") or payload.get("result")))
                return
            # 有些实现只在 result 里给最终文本（没有流式片段）：补发一次，避免空白回复。
            final_text = payload.get("result")
            if (
                isinstance(final_text, str)
                and final_text.strip()
                and not run.state.get("emitted_text")
            ):
                run.state["emitted_text"] = True
                yield text_delta(final_text)
            return

        # 文本：兼容 message / assistant / response / delta 等平铺形状。
        if kind in ("message", "assistant", "response", "delta", "text", ""):
            role = payload.get("role")
            text = (
                payload.get("content")
                or payload.get("text")
                or payload.get("delta")
                or payload.get("message")
            )
            if isinstance(text, (dict, list)):
                text = flatten_content(text)
            if text and role in (None, "assistant", "model"):
                run.state["emitted_text"] = True
                yield text_delta(str(text))
                return

        yield log(line_of(payload), "debug")


def line_of(payload: dict[str, Any]) -> str:
    """未知事件降级成一行调试日志（不静默吞掉）。"""
    try:
        return json.dumps(payload, ensure_ascii=False)[:500]
    except (TypeError, ValueError):
        return str(payload)[:500]
