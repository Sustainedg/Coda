"""架构约束测试：把 AGENTS.md 里的分层铁律固化成可执行检查。

这些断言失败通常意味着有人（很可能是 AI）在框架层为某个具体智能体开分支，
或让适配器反向依赖了上层模块。遇到失败请先读 AGENTS.md §1/§2，再改代码。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.agents import AgentContext, all_agents, known_ids
from app.agents.base import FieldType, Runtime
from app.agents.events import EventType

APP_DIR = Path(__file__).resolve().parent.parent / "app"
AGENTS_DIR = APP_DIR / "agents"
BUILTIN_DIR = AGENTS_DIR / "builtin"

#: 适配器（叶子层）不得依赖的上层 / 存储模块
FORBIDDEN_APP_ROOTS = {
    "api",
    "runtime",
    "db",
    "models",
    "schemas",
    "security",
    "settings",
    "bootstrap",
    "main",
}

#: 规范化事件协议（AGENTS.md 铁律 5）。改这里就等于改协议，必须四处同步。
DOCUMENTED_EVENTS = {
    "run.started",
    "message.delta",
    "thinking.delta",
    "tool.call",
    "tool.result",
    "usage",
    # 原始 usage 小票（保留嵌套/驼峰），与数值计费的 usage 分开。
    "usage.raw",
    # 单次模型请求的输入 token（上下文窗口占用），与整轮累计的 usage 分开。
    "context.usage",
    # 内核上报的真实上下文窗口大小（上限设为「自动」时平台跟随它）。
    "context.window",
    # 内核侧原生压缩通知（如 Claude Code auto-compact），运行层据此提醒并失效旧快照。
    "context.compacted",
    "session",
    "log",
    "error",
    "run.finished",
}

#: 内置适配器（新增适配器时这里也要加，用于断言注册表完整）
BUILTIN_IDS = {
    "claude-code",
    "codex",
    "antigravity-cli",
    "openai-harness",
    "custom-cli",
    "hermes-agent",
    "deepseek-harness",
    "codebuddy-cli",
    "doubao-cli",
}

CJK = re.compile(r"[\u4e00-\u9fff]")


def _adapter_modules() -> list[Path]:
    return sorted(
        [
            AGENTS_DIR / "base.py",
            AGENTS_DIR / "events.py",
            AGENTS_DIR / "registry.py",
            AGENTS_DIR / "skills.py",
            AGENTS_DIR / "mcp.py",
            AGENTS_DIR / "metering.py",
            *BUILTIN_DIR.glob("*.py"),
        ]
    )


def _package_parts(path: Path) -> list[str]:
    return list(path.resolve().relative_to(APP_DIR).parts[:-1])


def _forbidden_imports(path: Path) -> list[str]:
    """返回该文件中违反「适配器不得反向依赖上层」的 import 语句。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _package_parts(path)
    violations: list[str] = []

    def check_absolute(module: str) -> None:
        if module == "app" or module.startswith("app."):
            head = module.split(".")[1] if "." in module else ""
            if head in FORBIDDEN_APP_ROOTS:
                violations.append(f"import {module}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                check_absolute(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0:
                check_absolute(module)
                continue
            # 相对导入：level=L 表示在当前包基础上向上回退 L-1 层。
            base = package[: len(package) - (node.level - 1)]
            if not base:  # 指到了 app 顶层的兄弟模块（例如 `from ..models import X`）
                head = module.split(".")[0] if module else ""
                if head in FORBIDDEN_APP_ROOTS:
                    violations.append(f"from {'.' * node.level}{module} import ...")
    return violations


@pytest.mark.parametrize("path", _adapter_modules(), ids=lambda p: p.name)
def test_adapters_do_not_depend_on_upper_layers(path: Path):
    assert _forbidden_imports(path) == [], (
        f"{path.name} 反向依赖了上层模块：{_forbidden_imports(path)}。"
        "适配器只能依赖 agents/*、标准库与 httpx（AGENTS.md 铁律 2）。"
    )


def test_registry_knows_all_builtin_adapters():
    assert BUILTIN_IDS <= set(known_ids())


def test_every_adapter_declares_its_contract():
    for definition in all_agents():
        assert definition.id and definition.name, f"{definition.id} 缺少 id/name"
        assert definition.description, f"{definition.id} 缺少 description"
        assert isinstance(definition.auto_provision, bool), f"{definition.id}.auto_provision 必须是布尔值"
        if definition.runtime is Runtime.CLI and definition.auto_provision:
            assert definition.executable, f"CLI 适配器 {definition.id} 必须声明 executable"
        keys = [field.key for field in definition.config_fields]
        assert len(keys) == len(set(keys)), f"{definition.id} 的 config_fields key 重复"


def test_config_field_types_are_valid():
    valid = {member.value for member in FieldType}
    for definition in all_agents():
        for field in definition.config_fields:
            assert field.type.value in valid, f"{definition.id}.{field.key} 的 FieldType 非法"


def test_declared_provisioners_are_complete():
    """声明了运行时维护方式的适配器必须给出包管理器与安装 / 卸载命令（AGENTS.md §4.10）。"""
    for definition in all_agents():
        provisioner = definition.provisioner
        if provisioner is None:
            continue
        assert provisioner.manager, f"{definition.id} 的 provisioner 缺少包管理器标识"
        assert provisioner.install_argv, f"{definition.id} 的 provisioner 缺少「安装」命令"
        assert provisioner.uninstall_argv, f"{definition.id} 的 provisioner 缺少「卸载」命令"


def test_adapter_metadata_and_labels_are_localised():
    """新增适配器时，name/description/字段 label 必须是中文（AGENTS.md 铁律 7 与 §6）。"""
    for definition in all_agents():
        assert CJK.search(definition.description), f"{definition.id} 的 description 必须是中文"
        for field in definition.config_fields:
            assert CJK.search(field.label), f"{definition.id}.{field.key} 的 label 必须是中文"


def test_event_protocol_matches_the_documented_contract():
    actual = {member.value for member in EventType}
    assert actual == DOCUMENTED_EVENTS, (
        "事件协议发生变化：请在 events.py、runtime/service.consume_run、"
        "前端 applyEvent + types.ts、docs/架构/architecture.md 四处同步（AGENTS.md 铁律 5）。"
    )


def test_framework_layers_do_not_hardcode_agent_ids():
    """框架层（api/runtime）不得为具体智能体开分支（AGENTS.md 铁律 1）。"""
    offenders: list[str] = []
    for path in sorted(list((APP_DIR / "api").glob("*.py")) + list((APP_DIR / "runtime").glob("*.py"))):
        text = path.read_text(encoding="utf-8")
        for agent_id in BUILTIN_IDS:
            if f'"{agent_id}"' in text or f"'{agent_id}'" in text:
                offenders.append(f"{path.name} 出现了硬编码的智能体 id：{agent_id}")
    assert offenders == [], "\n".join(offenders)


#: 技能根与内核隔离变量的具体知识只应存在于适配器里（AGENTS.md 铁律 1/2）
KERNEL_SKILL_MARKERS = (".claude/skills", ".codex/skills", "CODEX_HOME", "CLAUDE_CONFIG_DIR")


def test_framework_layers_do_not_hardcode_kernel_skills():
    """框架层不得知道「哪个内核把技能放在哪」（那是适配器的知识）。"""
    offenders: list[str] = []
    framework_files = list((APP_DIR / "api").glob("*.py"))
    framework_files += list((APP_DIR / "runtime").glob("*.py"))
    for path in sorted(framework_files):
        text = path.read_text(encoding="utf-8")
        for marker in KERNEL_SKILL_MARKERS:
            if marker in text:
                offenders.append(f"{path.name} 出现了硬编码的内核技能标识：{marker}")
    assert offenders == [], "\n".join(offenders)


#: 内核 MCP 配置的具体知识（配置文件名、命令行开关、JSON/TOML 键）只应存在于适配器里。
KERNEL_MCP_MARKERS = (
    ".claude.json",
    "mcpServers",
    "--mcp-config",
    "--strict-mcp-config",
    "--allowed-mcp-server-names",
    "[mcp_servers",
)


def test_framework_layers_do_not_hardcode_kernel_mcp():
    """框架层不得知道「哪个内核把 MCP 配置放在哪、长什么形状」（那是适配器的知识）。"""
    offenders: list[str] = []
    framework_files = list((APP_DIR / "api").glob("*.py"))
    framework_files += list((APP_DIR / "runtime").glob("*.py"))
    for path in sorted(framework_files):
        text = path.read_text(encoding="utf-8")
        for marker in KERNEL_MCP_MARKERS:
            if marker in text:
                offenders.append(f"{path.name} 出现了硬编码的内核 MCP 标识：{marker}")
    assert offenders == [], "\n".join(offenders)


def test_mcp_capable_adapters_declare_a_mount():
    """声明了 mcp 能力的适配器必须声明物化方式，否则「分配的 MCP」无处落地。"""
    ctx = AgentContext(agent_id="probe", agent_name="probe")
    for definition in all_agents():
        if not definition.capabilities.mcp:
            continue
        assert definition.mcp_mount(ctx) is not None, (
            f"{definition.id} 声明了 mcp 能力，但没有实现 mcp_mount()"
        )


def test_skill_capable_adapters_declare_a_mount():
    """声明了 skills 能力的适配器必须声明物化方式，否则「仅分配」无处落地。"""
    ctx = AgentContext(agent_id="probe", agent_name="probe")
    for definition in all_agents():
        if not definition.capabilities.skills:
            continue
        assert definition.skill_mount(ctx) is not None, (
            f"{definition.id} 声明了 skills 能力，但没有实现 skill_mount()"
        )
