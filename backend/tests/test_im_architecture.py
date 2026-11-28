"""IM 域的分层守卫：平台适配器是叶子节点，框架层不知道具体平台。

这些断言失败通常意味着有人（很可能是 AI）在 `im/service.py` 或接口层为某个平台
开分支，或让平台适配器反向依赖了上层模块。遇到失败请先读 `docs/功能文档/im.md` 的分层说明。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.im.base import PLATFORMS
from app.im.platforms import ADAPTERS
from app.im.tables import ADDITIVE_COLUMNS

APP_DIR = Path(__file__).resolve().parent.parent / "app"
IM_DIR = APP_DIR / "im"
PLATFORM_DIR = IM_DIR / "platforms"

#: 平台适配器（叶子层）不得依赖的上层 / 存储模块。
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
    "collab",
}

CJK = re.compile(r"[一-鿿]")


def _forbidden_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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
            # platforms/*.py 用 `from ..base import ...`（level=2，回到 app.im）
            if node.level >= 3:
                violations.append(f"from {'.' * node.level}{module} import ...")
            elif node.level == 2 and module.split(".")[0] in FORBIDDEN_APP_ROOTS:
                violations.append(f"from {'.' * node.level}{module} import ...")
    return violations


@pytest.mark.parametrize("path", sorted(PLATFORM_DIR.glob("*.py")), ids=lambda p: p.name)
def test_platform_adapters_do_not_depend_on_upper_layers(path: Path):
    assert _forbidden_imports(path) == [], (
        f"{path.name} 反向依赖了上层模块：{_forbidden_imports(path)}。"
        "平台适配器只能依赖 im/base.py、im/format.py、im/crypto.py、标准库、"
        "httpx 与 websockets。"
    )


def test_framework_layers_do_not_hardcode_platform_ids():
    """`im/service.py` 与接口层不得为具体平台开分支（那是适配器的知识）。"""
    offenders: list[str] = []
    for path in sorted([*IM_DIR.glob("*.py")]):
        if path.name == "base.py":
            continue  # 平台元数据的唯一来源就在这里
        text = path.read_text(encoding="utf-8")
        for platform in PLATFORMS:
            if f'"{platform.id}"' in text or f"'{platform.id}'" in text:
                offenders.append(f"{path.name} 出现了硬编码的平台 id：{platform.id}")
    assert offenders == [], "\n".join(offenders)


def test_only_bridge_touches_the_runtime_layer():
    """IM 域与对话域只有 `bridge.py` 一个交界（AGENTS.md §2）。

    `service.py` 只管连接与路由；一旦它自己 import `app.runtime.*`，取消 / 落库
    这类语义就会在两处各写一份，日后必然漂移。
    """
    offenders: list[str] = []
    for path in sorted(IM_DIR.glob("*.py")):
        if path.name == "bridge.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "from ..runtime" in text or "import app.runtime" in text:
            offenders.append(path.name)
    assert offenders == [], (
        f"这些模块绕过了 bridge 直接依赖运行层：{offenders}。"
        "IM 域碰运行层只允许经 `im/bridge.py`。"
    )


def test_frontend_does_not_map_platform_ids_to_icons():
    """平台图标来自后端元数据；前端不得按平台 id 写映射（铁律 1）。"""
    frontend = APP_DIR.parent.parent / "frontend" / "src"
    offenders: list[str] = []
    for path in (
        frontend / "components" / "AgentImSettings.tsx",
    ):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for platform in PLATFORMS:
            if f"'{platform.id}'" in text:
                offenders.append(f"{path.name} 硬编码了平台 id：{platform.id}")
    assert offenders == [], (
        f"{offenders}。平台清单（名字 / 图标 / 字段）由 `im/base.py` 的 "
        "PLATFORMS 提供，前端只渲染，不按 id 分支。"
    )


def test_every_available_platform_has_an_adapter():
    available = {platform.id for platform in PLATFORMS if platform.available}
    assert available <= set(ADAPTERS), f"这些平台没有适配器：{available - set(ADAPTERS)}"


def test_platform_metadata_is_localised_and_complete():
    for platform in PLATFORMS:
        assert platform.name, f"{platform.id} 缺少名字"
        if platform.available:
            assert platform.fields or platform.id == "weixin", f"{platform.id} 没有任何字段"
        for item in platform.fields:
            assert CJK.search(item.label), f"{platform.id}.{item.key} 的 label 必须是中文"


def test_trace_options_are_declared_in_one_place():
    """「发什么」的开关只在 base.py 声明一次；界面与接口都按它读写。"""
    from app.im.base import OPTION_KEYS, clean_options

    assert set(OPTION_KEYS) == {"show_thinking", "show_tools", "show_logs"}
    # 缺省 / 脏数据一律按「都不发」
    assert clean_options(None) == {key: False for key in OPTION_KEYS}
    assert clean_options({"show_tools": "yes", "show_logs": True}) == {
        "show_thinking": False,
        "show_tools": False,
        "show_logs": True,
    }
    # 未声明的键不会进库
    assert "nope" not in clean_options({"nope": True})


def test_im_additive_columns_are_declared_for_existing_tables():
    """后加的可选列必须登记，否则旧库启动后会报 no such column。"""
    assert "im_connections" in ADDITIVE_COLUMNS
    # 过程内容开关是后加的列：老库必须能补上，否则旧连接一读就报
    # no such column
    assert "options" in ADDITIVE_COLUMNS["im_connections"]
