"""豆包 CLI（``doubao``）适配器。

豆包 CLI 是**账号登录型**内核：没有 API Key，也不接受平台注入的自定义网关，
靠 ``doubao login`` 扫码把登录态写进自己的数据目录。因此本适配器：

* 不声明 ``api_key_env`` / ``base_url_env`` / ``default_gateway``（启动播种会跳过）；
* headless 走 ``-p - --output-format stream-json``，事件形状与 Claude Code 同构；
* 技能隔离靠覆盖子进程 ``HOME``（``~/.agents/skills`` 等相对 HOME 的根），
  原生程序、登录态与设备身份（``mssdk``）则留在真实数据目录（``XDG_DATA_HOME``）；
  macOS 无 XDG 机制，靠把 ``Application Support`` 下的豆包数据目录软链进隔离 HOME；
* 人格拼进提示词：内核只读工作目录的 ``AGENTS.md``，写进去会污染用户项目。

平台与适配器的边界不变：这里只描述「怎么启动、怎么解析、配置落在哪」。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

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
)
from ..registry import register_agent
from ..skills import SkillMount, SkillRoot, SkillSpec, scan_skill_roots
from .util import as_list, flatten_content, npm_cli, npm_install_argv, npm_prepare_argv

#: npm 包与可执行文件名。
_PACKAGE = "@bytedance/doubao-cli"
_BINARY = "doubao"

#: 官方安装文档要求显式指定 npm 官方源（镜像站可能没有这个包）。
_REGISTRY = "https://registry.npmjs.org/"

#: 用户级技能根（官方文档列的其中两个；其余 ``~/.claude/skills`` 等由 HOME 隔离兜住）。
_USER_SKILL_ROOTS = (".agents/skills", "Doubao/skills")

#: 工作区级技能根：内核会额外扫描这些目录，提示用户它们不在分配范围内。
_WORKSPACE_SKILL_ROOTS = (".doubao", ".agents", ".claude", ".codex", ".gemini", ".trae", ".workbuddy")


def _probe_env() -> dict[str, str]:
    """探测账号状态的子进程环境：不弹更新、不输出彩色。"""
    env = {**os.environ}
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("DOUBAO_CLI_NO_UPDATE_CHECK", "1")
    return env


#: macOS 上随 ``os.homedir()/Library/Application Support`` 解析、必须软链到真实目录的
#: 豆包数据目录。``doubao_cli`` 是原生程序与登录态；``mssdk`` 是设备身份的「安全存储」
#: （字节设备指纹 SDK 的数据根）——漏掉它时每次运行都会在隔离目录里重建设备身份，
#: 触发 ``auth_unavailable: 无法访问用于保存设备身份的安全存储``，对话直接起不来。
_MACOS_DATA_NAMES = ("doubao_cli", "mssdk")

#: Windows 上随用户目录解析、必须接到真实目录的豆包数据目录。
#:
#: Windows 原生程序（``doubao.exe``）**不认** ``XDG_DATA_HOME``，也**不认** ``HOME``：
#: 它走 ``SHGetFolderPath`` / Known Folder API 按「用户文件夹」解析
#: （实测二进制里带 ``FOLDERID_Profile`` GUID 与 ``SHGetFolderPathW`` 导入）。
#: 而 ``agent_infra.dll`` 里那串 ``USERPROFILE`` → ``HOMEDRIVE`` → ``HOMEPATH``
#: 的 UTF-16 回退链说明：它解析「用户目录」时会**先看环境变量**，取不到才退回系统 API。
#:
#: 于是隔离 HOME 一旦把 ``USERPROFILE`` 指到隔离目录，原生程序就会去
#: ``<隔离目录>\AppData\Local\doubao_cli`` 建数据目录 —— 那条路径在隔离目录里
#: 并不存在，且原生程序建目录失败时只会抛「无法创建应用数据目录」这一句，
#: 不会提示真正的原因（技能根隔离顺手把用户目录也改了）。
#:
#: 修法与 macOS 同源：把真实目录**接进**隔离目录（Windows 上是目录联接 / junction），
#: 让原生程序在隔离目录里照样看到真实的数据与登录态。
_WINDOWS_DATA_NAMES = ("doubao_cli", "mssdk")


def _link_macos_data(home_dir: str) -> None:
    """macOS：把隔离 HOME 里的豆包数据目录软链到真实目录。

    ``runtime.cjs`` 在 darwin 上按 ``os.homedir()/Library/Application Support`` 找原生程序
    与登录态，而 Node 的 ``os.homedir()`` 认的是 ``HOME``。技能隔离要求把 HOME 换掉，
    不补这条链接，豆包会「已安装但找不到运行时」；设备身份（``mssdk``）也按同一根解析，
    不链的话登录态与设备身份会分家，报「安全存储不可用」。Linux 有 ``XDG_DATA_HOME``
    （豆包原生程序与 ``mssdk`` 都认它，适配器已显式指向真实目录），Windows 则由
    :func:`_link_windows_data` 负责。
    """
    if sys.platform != "darwin":
        return
    real_home = os.environ.get("HOME") or str(Path.home())
    support = Path(real_home) / "Library" / "Application Support"
    for name in _MACOS_DATA_NAMES:
        real = support / name
        target = Path(home_dir) / "Library" / "Application Support" / name
        try:
            if target.exists() or target.is_symlink():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(real, target_is_directory=True)
        except OSError:
            # 尽力而为：链接建不出来时运行会报「运行时缺失」，但不该阻断构建启动命令。
            continue


def _link_windows_data(
    home_dir: str,
    *,
    link_dir: Callable[[Path, Path], None] | None = None,
) -> None:
    """Windows：把隔离目录里的豆包数据目录接到真实目录（目录联接 / 符号链接）。

    Windows 原生程序按「用户文件夹」解析数据根（见 :data:`_WINDOWS_DATA_NAMES` 的说明），
    隔离 HOME 改了 ``USERPROFILE`` 之后它就会在隔离目录里另起一份数据：
    既找不到真实登录态，也会因为设备身份（``mssdk``）落在陌生目录而触发
    「无法访问用于保存设备身份的安全存储」。

    这里在隔离目录下建出与真实布局同名的目录，并**接到真实目录**上：
    优先用目录联接（junction，不需要管理员权限），失败再退回符号链接
    （同样失败就跳过——让内核自己报「无法创建应用数据目录」，不要阻断启动命令）。

    ``link_dir`` 只为单测注入：真实路径是 :func:`_windows_link_dir`。
    """
    if sys.platform != "win32":
        return
    real_local = os.environ.get("LOCALAPPDATA")
    if not real_local:
        return
    link = link_dir or _windows_link_dir
    base = Path(home_dir) / "AppData" / "Local"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 骨架建不出来就不必再谈链接：运行时的报错会说明问题（尽力而为，不阻断启动）。
        return
    for name in _WINDOWS_DATA_NAMES:
        real = Path(real_local) / name
        target = base / name
        try:
            if target.exists() or target.is_symlink():
                continue
            link(target, real)
        except (OSError, subprocess.SubprocessError):
            # 尽力而为：链接建不出来（含 mklink 超时）时内核会自己报
            # 「无法创建应用数据目录」，比在这里打断整次运行更接近用户能自己修的状态。
            continue


def _windows_link_dir(link: Path, target: Path) -> None:
    """在 Windows 上把 ``link`` 接到目录 ``target``。

    优先用目录联接（``mklink /J``）：它不需要管理员权限，也不会被内核当成
    「符号链接」而被安全检查拒掉。联接建不起来时退回符号链接（需要开发者模式）；
    两条路都走不通就抛出，由调用方跳过——宁可让内核报「无法创建应用数据目录」，
    也不要造一个假的空目录把登录态静默丢掉。
    """
    if target.is_dir() and sys.platform == "win32":
        result = subprocess.run(
            [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(link),
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
            # 联接是本机瞬时操作；给个上限，免得异常环境把对话启动挂死（铁律 10）。
            timeout=10,
        )
        if result.returncode == 0 and link.exists():
            return
    link.symlink_to(target, target_is_directory=True)


def _auth_argv(exe: str) -> list[str]:
    """``doubao auth status`` 的实际启动形态。

    Windows 上 npm 给的是 ``doubao.cmd``，``subprocess`` 不能直接执行，要交给
    ``cmd.exe /d /s /c``；POSIX 原样执行。

    拼法与 ``runtime/runner.py:_spawn_argv`` 完全一致：整条命令交给
    ``subprocess.list2cmdline`` 拼成一个字符串。平台里 npm 垫片只有这一种拼法
    （单测也钉着它），探测和真实运行才不会在引号 / 空格上产生两种行为。
    """
    args = [exe, "auth", "status"]
    if sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat")):
        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(args),
        ]
    return args


def _first_line(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return None


@register_agent
class DoubaoCliAgent(AgentDefinition):
    id = "doubao-cli"
    name = "豆包 CLI"
    vendor = "字节跳动"
    description = (
        "豆包的终端智能体。扫码登录后可直接分析代码、读写文件、执行命令，"
        "输出逐行 JSON，支持多轮会话续接与技能。"
    )
    runtime = Runtime.CLI
    executable = _BINARY
    provisioner = RuntimeProvisioner(
        manager="npm",
        package=_PACKAGE,
        install_argv=npm_install_argv(_PACKAGE, bin_name=_BINARY, registry=_REGISTRY),
        update_argv=npm_install_argv(
            _PACKAGE, bin_name=_BINARY, latest=True, registry=_REGISTRY
        ),
        uninstall_argv=npm_cli("uninstall", "-g", _PACKAGE),
        latest_argv=npm_cli("view", _PACKAGE, "version"),
        prepare_argv=npm_prepare_argv(_PACKAGE),
        #: 账号登录：二维码图片由框架读成 data URL 推给界面（见 provisioning.py）。
        login_argv=[_BINARY, "login", "--qr=png"],
        homepage="https://www.doubao.com/work/docs/zh-cn/doubao-cli",
        note=(
            "仅支持 Windows x64 / macOS ARM64 / Linux x64；"
            "Linux 需要 libgcrypt 运行库（Debian/Ubuntu 用 apt 安装对应包）。"
            "首次安装需要下载约 236 MB 原生程序。"
        ),
    )
    #: 账号型内核：平台没有可注入的密钥，也不接管模型路由。
    api_key_env = None
    base_url_env = None
    default_gateway = None
    default_model = None
    protocol = "openai"
    default_auth_style = "bearer"
    docs_url = "https://www.doubao.com/work/docs/zh-cn/doubao-cli"
    logo = "/icons/kernels/doubao.png"
    logo_style = "image"
    capabilities = Capabilities(
        streaming=True,
        sessions=True,
        #: 只认工作目录的 AGENTS.md；写进去会改用户项目，所以人格拼提示词。
        native_system_prompt=False,
        tools=True,
        #: 没有可声明的本地 MCP 配置面，别写假的 mount。
        mcp=False,
        permission_modes=True,
        working_directory=True,
        skills=True,
    )
    config_fields = (
        ConfigField(
            "permission_mode",
            "权限模式",
            FieldType.SELECT,
            default="always_allow",
            options=[
                {"value": "always_allow", "label": "always_allow · 允许所有操作"},
                {"value": "ask_risky", "label": "ask_risky · 风险操作时确认（非交互下会被拒）"},
                {"value": "ask_always", "label": "ask_always · 始终确认（非交互下会被拒）"},
            ],
            help=(
                "非交互运行（-p）没有授权弹窗，ask_* 模式下需要确认的操作会被直接拒绝。"
                "无人值守场景请使用 always_allow，并自行确认运行环境与任务范围。"
            ),
        ),
        ConfigField(
            "bypass_sandbox",
            "请求无限制访问",
            FieldType.BOOLEAN,
            default=False,
            help="对应 --dangerously-bypass-approvals-and-sandbox，与权限模式互斥。",
        ),
        ConfigField(
            "effort",
            "推理强度",
            FieldType.SELECT,
            default="",
            options=[
                {"value": "", "label": "跟随模型默认"},
                {"value": "minimal", "label": "minimal"},
                {"value": "low", "label": "low"},
                {"value": "medium", "label": "medium"},
                {"value": "high", "label": "high"},
                {"value": "xhigh", "label": "xhigh"},
                {"value": "max", "label": "max"},
            ],
        ),
        ConfigField("extra_args", "额外命令行参数", FieldType.STRING_LIST, advanced=True),
        ConfigField("env", "环境变量", FieldType.KEYVALUE, advanced=True),
    )

    # -- 登录状态 ------------------------------------------------------------

    def auth_status(self) -> AuthStatus:
        """跑一次 ``doubao auth status``（本地检查，不联网）判断登录态。

        ``--verify`` 会联网验证，慢且受代理影响，这里不用。任何异常都折算成
        ``logged_in=None`` + 中文原因，绝不向接口层抛。
        """
        exe = shutil.which(self.executable or _BINARY)
        if not exe:
            return AuthStatus(required=True, logged_in=None, detail="尚未安装豆包 CLI")
        try:
            proc = subprocess.run(
                _auth_argv(exe),
                capture_output=True,
                text=True,
                timeout=10,
                env=_probe_env(),
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - 探测失败只影响一处展示
            return AuthStatus(required=True, logged_in=None, detail=f"探测登录状态失败：{exc}")
        output = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
        lowered = output.lower()
        if "login_required" in lowered or "未登录" in output or "not logged in" in lowered:
            return AuthStatus(
                required=True,
                logged_in=False,
                detail="未登录：点「登录」扫码，或在本机终端执行 doubao login",
            )
        if "auth_unavailable" in lowered:
            return AuthStatus(
                required=True,
                logged_in=None,
                detail=_first_line(output) or "登录状态暂不可用（设备注册失败）",
            )
        if proc.returncode == 0:
            account = None
            for line in output.splitlines():
                if any(key in line for key in ("账号", "用户", "手机号", "邮箱")):
                    account = line.strip()[:80]
                    break
            return AuthStatus(
                required=True,
                logged_in=True,
                detail=_first_line(output) or "已登录",
                account=account,
            )
        return AuthStatus(
            required=True,
            logged_in=None,
            detail=_first_line(output) or f"auth status 退出码 {proc.returncode}",
        )

    async def probe(self, ctx: AgentContext) -> tuple[bool, str]:
        """连通性测试：能跑 + 已登录才算就绪。"""
        availability = await asyncio.to_thread(self.availability)
        if not availability.installed:
            return False, availability.detail or "缺少可执行文件"
        status = await asyncio.to_thread(self.auth_status)
        if status is None:
            return True, "CLI 已就绪"
        if status.logged_in:
            return True, f"已登录 · {status.account or availability.version or '豆包 CLI'}"
        return False, status.detail or "未登录"

    # -- 技能 ----------------------------------------------------------------

    def skill_roots(self) -> list[SkillRoot]:
        """本机共享技能根；运行时会被隔离 HOME 挡住，只留分配到的技能。"""
        home = Path.home()
        return [
            SkillRoot(str(home / name), source_kind="user", installable=False)
            for name in _USER_SKILL_ROOTS
        ]

    def discover_skills(self) -> list[SkillSpec]:
        return scan_skill_roots(self.skill_roots(), type_id=self.id)

    def skill_mount(self, ctx: AgentContext) -> SkillMount:
        """严格仅分配：隔离 HOME，只把分配到的技能放进 ``~/.agents/skills``。"""
        notes: list[str] = []
        if ctx.workspace_dir:
            for name in _WORKSPACE_SKILL_ROOTS:
                extra = Path(ctx.workspace_dir) / name / "skills"
                if extra.is_dir() and any(extra.iterdir()):
                    notes.append(
                        f"工作目录自带的 {extra} 会被内核额外加载，这些技能不在分配范围内"
                    )
            if (Path(ctx.workspace_dir) / "AGENTS.md").is_file():
                notes.append(
                    "工作目录的 AGENTS.md 会被豆包 CLI 自动读取；平台的人格是拼在提示词里的"
                )
        return SkillMount(
            home_name="doubao-home",
            #: 豆包 CLI 会扫描 ``~/.agents/skills``；HOME 被指到隔离目录后，
            #: 这个路径就是「只含分配技能」的那一份。
            skills_subdir=".agents/skills",
            notes=notes,
        )

    # -- 构造启动命令 --------------------------------------------------------

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        argv: list[str] = [self.executable or _BINARY]
        if ctx.workspace_dir:
            # CLI 要求 -C 排在所有业务参数之前。
            argv += ["-C", ctx.workspace_dir]
        argv += ["--output-format", "stream-json"]

        options = ctx.options
        if ctx.model:
            argv += ["--model", ctx.model]
        effort = options.get("effort")
        if effort:
            argv += ["--effort", str(effort)]
        if options.get("bypass_sandbox"):
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            argv += ["--permission-mode", options.get("permission_mode") or "always_allow"]
        if ctx.session_id:
            argv += ["--resume", ctx.session_id]
        argv += [str(arg) for arg in as_list(options.get("extra_args") or [])]
        # 提示词走 stdin：规避命令行长度与跨平台引号问题。
        argv += ["-p", "-"]

        prompt = ctx.prompt or ""
        if ctx.system_prompt:
            # 内核没有系统提示词旗标，AGENTS.md 又属于用户项目：拼在最前面最安全。
            prompt = f"{ctx.system_prompt}\n\n---\n\n{prompt}"

        env = self.effective_env(ctx)
        env.setdefault("CI", "1")
        env.setdefault("NO_COLOR", "1")
        env.setdefault("DOUBAO_CLI_NO_UPDATE_CHECK", "1")
        if ctx.home_dir:
            real_home = os.environ.get("HOME") or str(Path.home())
            _link_macos_data(ctx.home_dir)
            # 技能根跟着 HOME 走：隔离目录里只有分配到的技能。
            env["HOME"] = ctx.home_dir
            if sys.platform == "win32":
                # Windows 上 Node / Rust 取「用户主目录」认 USERPROFILE，不认 HOME。
                env["USERPROFILE"] = ctx.home_dir
                # 原生程序按「用户文件夹」解析数据根，而它优先读 USERPROFILE：
                # 隔离 HOME 改了它就会去隔离目录里另起一份数据（找不到登录态，
                # 还会重建设备身份）。把真实数据目录接到隔离目录里，让它照样看到。
                _link_windows_data(ctx.home_dir)
                # 真实 LOCALAPPDATA 原样传下去：内核的 npm 垫片（runtime.cjs）在
                # win32 上就是按 ``process.env.LOCALAPPDATA`` 找原生程序的，
                # 这个变量被换掉就会「已安装但找不到运行时」。
                real_local = os.environ.get("LOCALAPPDATA")
                if real_local:
                    env["LOCALAPPDATA"] = real_local
            else:
                # 原生程序与登录态在 XDG_DATA_HOME 下，必须显式指向真实目录。
                env.setdefault(
                    "XDG_DATA_HOME",
                    os.environ.get("XDG_DATA_HOME") or str(Path(real_home) / ".local" / "share"),
                )
            # HOME 换了之后运行器补的是隔离目录的 .local/bin，真实安装目录自己保住。
            # 分隔符必须走 os.pathsep：Windows 上是分号，写死冒号会把整条 PATH 拼坏。
            real_bin = str(Path(real_home) / ".local" / "bin")
            env["PATH"] = f"{real_bin}{os.pathsep}{os.environ.get('PATH', '')}"

        return Invocation(
            argv=argv,
            env=env,
            cwd=ctx.workspace_dir,
            stdin=prompt,
        )

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
        yield from self._dispatch(payload, run)

    def _dispatch(self, payload: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        kind = payload.get("type")
        sid = payload.get("session_id")
        if sid and sid != run.state.get("session"):
            run.state["session"] = sid
            yield session(sid)

        if kind == "stream_event":
            yield from self._from_stream_event(payload, run)
        elif kind == "assistant":
            yield from self._from_assistant(payload.get("message") or {}, run)
        elif kind == "user":
            yield from self._from_tool_results(payload.get("message") or {}, run)
        elif kind == "result":
            yield from self._from_result(payload, run)

    def _from_stream_event(self, payload: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        event = payload.get("event") or {}
        if event.get("type") != "content_block_delta":
            return
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            message_id = payload.get("message_id")
            if message_id:
                run.state.setdefault("streamed", set()).add(message_id)
            yield text_delta(delta["text"])

    def _from_assistant(self, message: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        message_id = message.get("id")
        streamed: set[str] = run.state.setdefault("streamed", set())
        emitted_tools: dict[str, str] = run.state.setdefault("tools", {})
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                # 逐字增量已经发过就不再重复整段；没有增量（工具轮 / 极短回复）才补发。
                if message_id not in streamed:
                    streamed.add(message_id)
                    yield text_delta(block["text"])
            elif block.get("type") == "tool_use":
                call_id = str(block.get("id") or "")
                status = block.get("input_status")
                # 参数还在流式输入（input_status=unavailable）时先等完整输入。
                if status and status != "available":
                    continue
                payload_input = block.get("input") or {}
                fingerprint = json.dumps(payload_input, sort_keys=True, ensure_ascii=False, default=str)
                if call_id and emitted_tools.get(call_id) == fingerprint:
                    continue
                if call_id:
                    emitted_tools[call_id] = fingerprint
                yield tool_call(
                    block.get("name") or "tool",
                    call_id or None,
                    **(payload_input if isinstance(payload_input, dict) else {"input": payload_input}),
                )

    def _from_tool_results(self, message: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        emitted: set[str] = run.state.setdefault("tool_results", set())
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call_id = str(block.get("tool_use_id") or "")
            if call_id and call_id in emitted:
                continue
            if call_id:
                emitted.add(call_id)
            yield tool_result(
                call_id or None,
                flatten_content(block.get("content") or block.get("result")),
                bool(block.get("isError") or block.get("is_error")),
            )

    def _from_result(self, payload: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        for denial in payload.get("permission_denials") or []:
            if isinstance(denial, dict):
                name = denial.get("tool_name") or "工具"
                prompt = denial.get("prompt")
                yield log(f"权限被拒绝：{name}{f'（{prompt}）' if prompt else ''}", "warn")
        if payload.get("is_error"):
            run.state["is_error"] = True
            errors = payload.get("errors") or []
            message = flatten_content(errors) or payload.get("code") or "运行失败"
            yield error(str(message))
