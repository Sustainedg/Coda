"""技能：解析、统一编目、智能体分配与运行时物化。

技能的相关约定见 AGENTS.md §3（契约）与 §4（扩展手册）。
"""
from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

import app.runtime.skills as skill_service
from app.agents import AgentContext, get_agent
from app.agents.skills import (
    SkillMount,
    SkillSpec,
    parse_skill_dir,
    safe_skill_dirname,
    scan_skill_root,
)
from app.db import SessionLocal
from app.models import Conversation, Skill
from app.settings import get_settings
from helpers import make_gateway, make_profile

SKILL_MD = """---
name: {name}
description: {description}
{extra}---

# {name}

正文第一段。
"""


def write_skill(
    root: Path,
    slug: str,
    *,
    name: str | None = None,
    extra: str = "",
    body_extra: str = "",
) -> Path:
    directory = root / slug
    (directory / "scripts").mkdir(parents=True, exist_ok=True)
    (directory / "references").mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        SKILL_MD.format(name=name or slug, description=f"{slug} 的说明", extra=extra) + body_extra,
        encoding="utf-8",
    )
    (directory / "scripts" / "run.sh").write_text("echo hi\n", encoding="utf-8")
    (directory / "references" / "note.md").write_text("note\n", encoding="utf-8")
    return directory


def spec_for(
    directory: Path, *, type_id: str = "custom-cli", source_kind: str = "user"
) -> SkillSpec:
    spec = parse_skill_dir(directory, type_id=type_id, source_kind=source_kind)
    assert spec is not None
    return spec


def context(**overrides) -> AgentContext:
    data: dict = {"agent_id": "a1", "agent_name": "测试智能体", "prompt": "hi"}
    data.update(overrides)
    return AgentContext(**data)


# --- 解析 -------------------------------------------------------------------


def test_parse_skill_dir_reads_metadata_and_files(tmp_path: Path):
    directory = write_skill(
        tmp_path,
        "demo-skill",
        extra=(
            "version: 1.2.0\nauthor: 张三\nallowed-tools:\n  - Read\n  - Bash(git *)\n"
            "metadata:\n  short-description: 简短说明\n"
        ),
    )
    spec = spec_for(directory)

    assert spec.id == "custom-cli:demo-skill"
    assert spec.name == "demo-skill"
    assert spec.description == "demo-skill 的说明"
    assert spec.version == "1.2.0"
    assert spec.author == "张三"
    assert spec.allowed_tools == ["Read", "Bash(git *)"]
    assert {item.path for item in spec.files} == {
        "SKILL.md", "scripts/run.sh", "references/note.md",
    }
    assert spec.total_bytes > 0
    assert spec.fingerprint


def test_parse_skill_dir_falls_back_when_frontmatter_is_broken(tmp_path: Path):
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        '---\nname: "未闭合\ndescription: 坏掉的\n---\n\n# 标题\n\n这是正文第一段。\n',
        encoding="utf-8",
    )
    spec = spec_for(directory)

    # frontmatter 解析失败时退化为「目录名 + 正文第一段」，不能让整个编目崩掉。
    assert spec.name == "broken"
    assert spec.description == "这是正文第一段。"


def test_parse_skill_dir_without_skill_file_returns_none(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert parse_skill_dir(empty, type_id="custom-cli") is None


def test_scan_skill_root_ignores_symlinks_and_missing_root(tmp_path: Path):
    write_skill(tmp_path, "real")
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(tmp_path / "real", target_is_directory=True)
    except OSError:  # 某些文件系统不支持符号链接
        linked = None  # type: ignore[assignment]

    slugs = {spec.slug for spec in scan_skill_root(tmp_path, type_id="custom-cli")}
    assert "real" in slugs
    if linked is not None:
        assert "linked" not in slugs
    assert scan_skill_root(tmp_path / "不存在", type_id="custom-cli") == []


@pytest.mark.parametrize("slug", ["..", ".", "a/b", "", "有中文", "a\\b"])
def test_safe_skill_dirname_rejects_unsafe_names(slug: str):
    assert safe_skill_dirname(slug) is None


def test_safe_skill_dirname_accepts_plain_names():
    assert safe_skill_dirname("demo-skill.v2") == "demo-skill.v2"


# --- 物化 -------------------------------------------------------------------


def test_materialize_copies_skills_and_drops_stale_ones(tmp_path: Path):
    source = tmp_path / "source"
    write_skill(source, "alpha")
    write_skill(source, "beta")
    specs = [spec_for(source / "alpha"), spec_for(source / "beta")]

    home = tmp_path / "home"
    warnings = skill_service.materialize(home, SkillMount(home_name="claude-home"), specs)
    assert warnings == []
    assert (home / "skills" / "alpha" / "scripts" / "run.sh").is_file()
    assert (home / "skills" / "beta" / "SKILL.md").is_file()
    assert (home.stat().st_mode & 0o777) == 0o700

    # 取消分配后，技能必须从隔离目录里消失。
    warnings = skill_service.materialize(home, SkillMount(home_name="claude-home"), specs[:1])
    assert warnings == []
    assert (home / "skills" / "alpha").is_dir()
    assert not (home / "skills" / "beta").exists()


def test_materialize_carries_and_patches_kernel_settings(tmp_path: Path):
    source = tmp_path / "source"
    write_skill(source, "alpha")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {"env": {"ANTHROPIC_AUTH_TOKEN": "sk-x"}, "enabledPlugins": {"keep@market": True}}
        ),
        encoding="utf-8",
    )
    mount = SkillMount(
        home_name="claude-home",
        carry=[(str(settings_file), "settings.json")],
        patch={
            "settings.json": {
                "enabledPlugins": {"builtin@market": False},
                "disableBundledSkills": True,
            }
        },
    )

    home = tmp_path / "home"
    assert skill_service.materialize(home, mount, [spec_for(source / "alpha")]) == []

    data = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert data["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-x"
    assert data["enabledPlugins"] == {"keep@market": True, "builtin@market": False}
    assert data["disableBundledSkills"] is True
    assert (home / "settings.json").stat().st_mode & 0o777 == 0o600


def test_materialize_skips_unsafe_and_missing_skills(tmp_path: Path):
    source = tmp_path / "source"
    write_skill(source, "alpha")
    good = spec_for(source / "alpha")
    broken = SkillSpec(
        type_id="custom-cli", slug="../逃逸", name="危险技能", path=str(source / "alpha")
    )
    gone = SkillSpec(type_id="custom-cli", slug="gone", name="已删除", path=str(source / "不存在"))

    home = tmp_path / "home"
    warnings = skill_service.materialize(
        home, SkillMount(home_name="claude-home"), [good, broken, gone]
    )

    assert any("目录名不安全" in item for item in warnings)
    assert any("源目录已不存在" in item for item in warnings)
    assert sorted(path.name for path in (home / "skills").iterdir()) == ["alpha"]
    assert not (tmp_path / "逃逸").exists()


def test_materialize_rejects_carry_path_traversal(tmp_path: Path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    mount = SkillMount(home_name="claude-home", carry=[(str(outside), "../逃逸.json")])

    home = tmp_path / "home"
    warnings = skill_service.materialize(home, mount, [])
    assert any("携带路径不安全" in item for item in warnings)
    assert not (tmp_path / "逃逸.json").exists()


def test_materialize_skips_rebuild_when_nothing_changed(tmp_path: Path):
    """快速路径：分配与源内容都没变化时，不再整棵重建（mtime 保持不变）。"""
    source = tmp_path / "source"
    write_skill(source, "alpha")
    write_skill(source, "beta")
    specs = [spec_for(source / "alpha"), spec_for(source / "beta")]
    mount = SkillMount(home_name="claude-home")

    home = tmp_path / "home"
    assert skill_service.materialize(home, mount, specs) == []
    marker = (home / "skills" / "alpha" / "SKILL.md")
    first_copy = marker.read_text(encoding="utf-8")
    before = marker.stat().st_mtime_ns
    # 隔离目录里已经有别的内容（内核会话记录等）：快速路径不能把整个家拆掉。
    (home / "session.jsonl").write_text("[]\n", encoding="utf-8")

    assert skill_service.materialize(home, mount, specs) == []
    assert marker.stat().st_mtime_ns == before, "无变化时不应重抄文件"
    assert marker.read_text(encoding="utf-8") == first_copy
    assert (home / "session.jsonl").is_file(), "快速路径不得动家目录里的其它内容"
    # 清单文件存在且结构正确。
    manifest = json.loads((home / ".skills-manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert sorted(manifest["entries"]) == ["alpha", "beta"]


def test_materialize_rebuilds_when_assignment_or_content_changes(tmp_path: Path):
    """变更后必须重建：取消分配的技能立刻消失、更新的技能立刻生效。"""
    source = tmp_path / "source"
    write_skill(source, "alpha")
    write_skill(source, "beta")
    mount = SkillMount(home_name="claude-home")
    home = tmp_path / "home"

    # 取消分配：集合变了 → 重建，beta 必须立刻消失。
    assert skill_service.materialize(home, mount, [spec_for(source / "beta")]) == []
    assert (home / "skills" / "beta" / "SKILL.md").is_file()
    assert skill_service.materialize(home, mount, [spec_for(source / "alpha")]) == []
    assert not (home / "skills" / "beta").exists()
    assert (home / "skills" / "alpha" / "SKILL.md").is_file()

    # 重新分配回去但内容更新（原地改写，size 不变）：mtime 变化必须触发重建。
    assert skill_service.materialize(home, mount, [spec_for(source / "alpha")]) == []
    target = home / "skills" / "alpha" / "SKILL.md"
    old_text = target.read_text(encoding="utf-8")
    (source / "alpha" / "SKILL.md").write_text(old_text, encoding="utf-8")
    specs = [spec_for(source / "alpha")]
    assert skill_service.materialize(home, mount, specs) == []
    # 源被原地改写后 mtime 更新，下一轮必然走全量重建路径。
    assert skill_service.materialize(home, mount, specs) == []
    assert target.is_file()


def test_materialize_rebuilds_when_manifest_is_broken(tmp_path: Path):
    """清单损坏（读不出 / 结构不对）时当「变了」处理，走全量重建兜底。"""
    source = tmp_path / "source"
    write_skill(source, "alpha")
    specs = [spec_for(source / "alpha")]
    mount = SkillMount(home_name="claude-home")
    home = tmp_path / "home"

    assert skill_service.materialize(home, mount, specs) == []
    # 在物化结果里塞一个源目录没有的文件：只有全量重建（rmtree + copytree）
    # 才会把它清掉——用它观测「这一轮到底走没走重建」。mtime 观测不可靠：
    # 重建后 mtime 可能与重建前相同（本机文件系统时间粒度粗）。
    stale = home / "skills" / "alpha" / "stale.txt"
    stale.write_text("残留\n", encoding="utf-8")

    # 坏 JSON：必须当作「变了」处理，走全量重建（残留文件被清掉）。
    (home / ".skills-manifest.json").write_text("{ not json", encoding="utf-8")
    assert skill_service.materialize(home, mount, specs) == []
    assert not stale.exists()

    # 清单被删 + 目标目录被外部删除：也必须重建。
    (home / ".skills-manifest.json").unlink()
    skill_service.shutil.rmtree(home / "skills")
    assert skill_service.materialize(home, mount, specs) == []
    assert (home / "skills" / "alpha" / "SKILL.md").is_file()

    # 清单结构对但版本号不认识：同样必须重建。
    payload = json.loads((home / ".skills-manifest.json").read_text(encoding="utf-8"))
    payload["version"] = 99
    (home / ".skills-manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    stale.write_text("残留\n", encoding="utf-8")
    assert skill_service.materialize(home, mount, specs) == []
    assert not stale.exists()


def test_materialize_fast_path_still_runs_carry_and_patch(tmp_path: Path):
    """快速路径复用技能目录，但 carry / patch 每轮照做（内核配置可能在轮外被改）。"""
    source = tmp_path / "source"
    write_skill(source, "alpha")
    specs = [spec_for(source / "alpha")]
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")
    mount = SkillMount(
        home_name="claude-home",
        carry=[(str(settings_file), "settings.json")],
        patch={"settings.json": {"disableBundledSkills": True}},
    )
    home = tmp_path / "home"

    assert skill_service.materialize(home, mount, specs) == []
    # 轮外改了源 settings / 删了目标：下一轮即使走快速路径也要重写配置。
    (home / "settings.json").unlink()
    settings_file.write_text('{"env": {}}', encoding="utf-8")

    assert skill_service.materialize(home, mount, specs) == []
    data = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert data["env"] == {}
    assert data["disableBundledSkills"] is True


def test_materialize_rewrites_frontmatter_after_rebuild(tmp_path: Path):
    """重建后的副本仍然同步 frontmatter name（快速路径不掺和，行为与慢路径一致）。"""
    source = write_skill(tmp_path / "src", "access")
    spec = spec_for(source, source_kind="plugin")
    spec.slug = "telegram-access"
    mount = SkillMount(home_name="h")
    home = tmp_path / "home"

    assert skill_service.materialize(home, mount, [spec]) == []
    copied = home / "skills" / "telegram-access" / "SKILL.md"
    assert "name: telegram-access" in copied.read_text(encoding="utf-8")

    # 触发一次全量重建（改源内容）：frontmatter 同步必须照常执行。
    (source / "SKILL.md").write_text(
        copied.read_text(encoding="utf-8"), encoding="utf-8"
    )
    spec = spec_for(source, source_kind="plugin")
    spec.slug = "telegram-access"
    assert skill_service.materialize(home, mount, [spec]) == []
    assert "name: telegram-access" in copied.read_text(encoding="utf-8")


# --- 适配器声明 --------------------------------------------------------------


def test_claude_isolates_skills_strictly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    definition = get_agent("claude-code")

    plain = definition.build_invocation(context())
    assert "CLAUDE_CONFIG_DIR" not in plain.env

    home = tmp_path / "home" / "claude-home"
    ctx = context(home_dir=str(home), workspace_dir=str(tmp_path / "ws"))
    invocation = definition.build_invocation(ctx)
    assert invocation.env["CLAUDE_CONFIG_DIR"] == str(home)
    assert invocation.env["CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"] == "1"

    mount = definition.skill_mount(ctx)
    assert mount.home_name == "claude-home"
    assert [name for _, name in mount.carry] == ["settings.json", "settings.local.json"]
    patch = mount.patch["settings.json"]
    assert patch["disableBundledSkills"] is True
    assert patch["skillOverrides"]["doctor"] == "off"
    assert set(patch["enabledPlugins"].values()) == {False}


def test_claude_warns_about_workspace_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "ws"
    (workspace / ".claude" / "skills" / "自带").mkdir(parents=True)
    (workspace / ".claude" / "skills" / "自带" / "SKILL.md").write_text(
        "---\nname: 自带\n---\n", encoding="utf-8"
    )

    mount = get_agent("claude-code").skill_mount(context(workspace_dir=str(workspace)))
    assert any(".claude/skills" in note for note in mount.notes)


def test_claude_discovers_personal_and_installed_plugin_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """插件技能只认「已安装清单」里的安装目录，不看市场目录的克隆。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    write_skill(tmp_path / ".claude" / "skills", "personal")

    plugins = tmp_path / ".claude" / "plugins"
    # 市场目录的克隆：可浏览、未安装 —— 编目时绝不能出现。
    write_skill(plugins / "marketplaces" / "mp" / "plugins" / "p" / "skills", "catalog-only")
    # 真正装上的插件：清单登记在 cache/<市场>/<插件>/<版本>/ 下。
    installed = write_skill(plugins / "cache" / "mp" / "p" / "1.0.0" / "skills", "plugin-skill")
    (plugins / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "p@mp": [
                        {"scope": "user", "installPath": str(installed.parent), "version": "1.0.0"}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    found = {spec.id: spec for spec in get_agent("claude-code").discover_skills()}
    assert found["claude-code:personal"].source_kind == "user"
    # 插件技能带插件名前缀，避免与其它插件的同名技能互相覆盖。
    assert found["claude-code:p-plugin-skill"].source_kind == "plugin"
    assert "claude-code:catalog-only" not in found, "市场目录的克隆不该被编目"


def test_claude_same_name_skills_from_different_plugins_coexist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """两个插件各带一个 access：必须都在，且能被分别调用（目录名不同）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    plugins = tmp_path / ".claude" / "plugins"
    entries = {}
    for channel in ("discord", "telegram"):
        install = plugins / "cache" / "mp" / channel / "1.0.0"
        write_skill(install / "skills", "access")
        entries[f"{channel}@mp"] = [
            {"scope": "user", "installPath": str(install), "version": "1.0.0"}
        ]
    (plugins / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": entries}), encoding="utf-8"
    )

    found = {spec.id: spec for spec in get_agent("claude-code").discover_skills()}
    assert "claude-code:discord-access" in found
    assert "claude-code:telegram-access" in found
    # 原始目录名留档，展示名带上来源，列表里不会出现两个一模一样的「access」。
    assert found["claude-code:telegram-access"].extra["original_slug"] == "access"
    assert found["claude-code:discord-access"].name == "access · discord"
    assert found["claude-code:telegram-access"].name == "access · telegram"


def test_plugin_namespace_keeps_unsafe_slugs_and_is_idempotent():
    """前缀不会让不安全的目录名消失，重复应用也不会叠加前缀。"""
    from app.agents.skills import _apply_namespace

    spec = SkillSpec(type_id="t", slug="access", name="access")
    _apply_namespace(spec, "telegram")
    assert spec.slug == "telegram-access"
    _apply_namespace(spec, "telegram")
    assert spec.slug == "telegram-access", "重复应用不能变成 telegram-telegram-access"

    unsafe = SkillSpec(type_id="t", slug="技能 名", name="x")
    _apply_namespace(unsafe, "telegram")
    assert unsafe.slug == "技能 名", "无法安全加前缀时保留原名，不能丢技能"


def test_claude_plugin_catalog_clone_is_never_a_skill_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """没有任何已安装插件时，技能根也不能指向 plugins/（市场克隆所在处）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    plugins = tmp_path / ".claude" / "plugins"
    write_skill(plugins / "marketplaces" / "mp" / "plugins" / "p" / "skills", "catalog-only")

    roots = get_agent("claude-code").skill_roots()
    assert all(Path(root.path) != plugins for root in roots)
    assert all("marketplaces" not in root.path for root in roots)
    assert get_agent("claude-code").discover_skills() == []


def test_claude_empty_manifest_does_not_fall_back_to_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """清单存在但说「没装插件」时，缓存里的残留不能被当成已安装。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    plugins = tmp_path / ".claude" / "plugins"
    # 已卸载插件留在缓存里的目录
    write_skill(plugins / "cache" / "mp" / "leftover" / "1.0.0" / "skills", "ghost-skill")
    (plugins / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {}}), encoding="utf-8"
    )

    assert get_agent("claude-code").discover_skills() == []


def test_claude_plugin_cache_fallback_without_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """旧版本内核没有 installed_plugins.json 时，按 cache 布局兜底扫描（同样加前缀）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    cache = tmp_path / ".claude" / "plugins" / "cache"
    write_skill(cache / "mp" / "p" / "1.0.0" / "skills", "plugin-skill")

    found = {spec.id: spec for spec in get_agent("claude-code").discover_skills()}
    assert found["claude-code:p-plugin-skill"].source_kind == "plugin"


def test_claude_plugin_manifest_survives_broken_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """清单损坏不能让技能页整体失败：退回空列表，个人技能照常编目。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    write_skill(tmp_path / ".claude" / "skills", "personal")
    state = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
    state.parent.mkdir(parents=True)
    state.write_text("{ not json", encoding="utf-8")

    found = {spec.id: spec for spec in get_agent("claude-code").discover_skills()}
    assert found["claude-code:personal"].source_kind == "user"


def test_hermes_marks_bundled_skills_as_system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Hermes 的内置技能与用户技能同目录，靠 .bundled_manifest 区分归属。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    skills = tmp_path / "hermes" / "skills"
    write_skill(skills / "apple", "apple-notes")
    write_skill(skills / "mine", "my-own-skill")
    (skills / ".bundled_manifest").write_text("apple-notes:abc123\n", encoding="utf-8")

    found = {spec.id: spec for spec in get_agent("hermes-agent").discover_skills()}
    assert found["hermes-agent:apple-notes"].source_kind == "system"
    assert found["hermes-agent:my-own-skill"].source_kind == "user"


def test_hermes_manifest_ignores_market_and_curator_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """市场技能不能因为名字撞上清单就被标成自带；curator 抑制表也算自带。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    skills = tmp_path / "hermes" / "skills"
    write_skill(skills / ".skillhub", "hub-skill")
    write_skill(skills / "p", "pruned-builtin")
    (skills / ".bundled_manifest").write_text("hub-skill:aaa\n", encoding="utf-8")
    (skills / ".curator_suppressed").write_text("pruned-builtin\n", encoding="utf-8")

    found = {spec.id: spec for spec in get_agent("hermes-agent").discover_skills()}
    assert found["hermes-agent:hub-skill"].source_kind == "market"
    assert found["hermes-agent:pruned-builtin"].source_kind == "system"


def test_hermes_without_manifest_keeps_skills_deletable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """没有清单（旧内核）时退回旧行为：全部当用户技能，至少不会把用户技能锁死。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    write_skill(tmp_path / "hermes" / "skills" / "x", "some-skill")

    found = {spec.id: spec for spec in get_agent("hermes-agent").discover_skills()}
    assert found["hermes-agent:some-skill"].source_kind == "user"


def test_codex_uses_isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    root = tmp_path / "codex"
    write_skill(root / "skills", "user-skill")
    write_skill(root / "skills" / ".system", "sys-skill")
    (root / "config.toml").write_text('model = "gpt-5-codex"\n', encoding="utf-8")
    (root / "auth.json").write_text("{}\n", encoding="utf-8")

    definition = get_agent("codex")
    found = {spec.id: spec for spec in definition.discover_skills()}
    assert found["codex:user-skill"].source_kind == "user"
    assert found["codex:sys-skill"].source_kind == "system"

    home = tmp_path / "home" / "codex-home"
    ctx = context(home_dir=str(home))
    assert definition.build_invocation(ctx).env["CODEX_HOME"] == str(home)
    mount = definition.skill_mount(ctx)
    assert sorted(name for _, name in mount.carry) == ["auth.json", "config.toml"]


# --- 接口与端到端 ------------------------------------------------------------


async def make_agent(client: httpx.AsyncClient, type_id: str = "custom-cli") -> dict:
    gateway = await make_gateway(client)
    return await make_profile(
        client,
        name=f"{type_id} 智能体",
        type_id=type_id,
        gateway_id=gateway["id"],
        model=None,
        options={"command": "echo"} if type_id == "custom-cli" else {},
    )


async def make_role(client: httpx.AsyncClient, agent: dict) -> dict:
    return await make_profile(
        client,
        name="审查员",
        type_id=agent["type_id"],
        gateway_id=agent["gateway_id"],
        system_prompt="你是审查员",
        options={"command": "echo"} if agent["type_id"] == "custom-cli" else {},
    )


async def read_sse(response: httpx.Response) -> list[dict]:
    events: list[dict] = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把编目结果替换成临时目录里的技能，避免依赖开发机上的真实技能。"""
    root = tmp_path / "catalog"
    write_skill(root, "alpha")
    write_skill(root, "beta")

    def fake() -> dict[str, SkillSpec]:
        # 与真实适配器一致：每次都重新扫描磁盘，目录消失时技能随之失效。
        return {spec.id: spec for spec in scan_skill_root(root, type_id="custom-cli")}

    monkeypatch.setattr(skill_service, "discover_catalog", fake)
    return root


async def test_skills_api_lists_rescans_and_shows_detail(client: httpx.AsyncClient, catalog: Path):
    assert (await client.get("/api/skills")).json()["items"] == []

    result = (await client.post("/api/skills/rescan")).json()
    assert result == {
        "added": 2,
        "updated": 0,
        "missing": 0,
        "removed": 0,
        "unassigned": 0,
        "total": 2,
    }

    items = (await client.get("/api/skills")).json()["items"]
    assert [item["slug"] for item in items] == ["alpha", "beta"]
    assert items[0]["source_label"] == "个人技能（共享）"
    assert items[0]["file_count"] == 3
    assert items[0]["used_by_agents"] == 0

    filtered = (await client.get("/api/skills", params={"q": "beta"})).json()["items"]
    assert [item["slug"] for item in filtered] == ["beta"]

    detail = (await client.get("/api/skills/custom-cli:alpha")).json()
    assert detail["body"].startswith("---")
    assert detail["path"].startswith(str(catalog))
    assert {item["path"] for item in detail["files"]} == {
        "SKILL.md", "scripts/run.sh", "references/note.md",
    }
    assert detail["agents"] == []

    assert (await client.get("/api/skills/nope")).status_code == 404

    # 目录消失后：没有任何分配的技能直接从编目清理（无分配可断链，
    # 不在列表里堆「未扫描到」的死条目）；目录恢复时重扫会重新登记。
    skill_service.shutil.rmtree(catalog / "alpha")
    result = (await client.post("/api/skills/rescan")).json()
    assert result["removed"] == 1
    assert result["missing"] == 0
    items = (await client.get("/api/skills")).json()["items"]
    assert [item["slug"] for item in items] == ["beta"]
    assert (await client.get("/api/skills/custom-cli:alpha")).status_code == 404


async def test_gone_skill_assignment_is_removed_with_it(
    client: httpx.AsyncClient, catalog: Path
):
    """技能目录消失时，指向它的智能体分配也随之解除（不留幽灵引用）。"""
    await client.post("/api/skills/rescan")
    agent = await make_agent(client)
    await client.put(f"/api/agents/{agent['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]})

    skill_service.shutil.rmtree(catalog / "alpha")
    result = (await client.post("/api/skills/rescan")).json()
    assert result["removed"] == 1
    assert result["unassigned"] == 1
    assert result["missing"] == 0

    # 编目行没了，智能体的技能列表里也不再出现它。
    assert (await client.get("/api/skills/custom-cli:alpha")).status_code == 404
    listed = (await client.get(f"/api/agents/{agent['id']}")).json()
    assert "custom-cli:alpha" not in listed["skill_ids"]


async def test_scan_miss_keeps_assignments(
    client: httpx.AsyncClient, catalog: Path, monkeypatch: pytest.MonkeyPatch
):
    """适配器这一轮没扫到（目录还在）时只标失效，不能把分配清掉。"""
    await client.post("/api/skills/rescan")
    agent = await make_agent(client)
    await client.put(f"/api/agents/{agent['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]})

    # 模拟扫描落空：目录仍然存在，但没有 discover 出任何技能。
    monkeypatch.setattr(skill_service, "discover_catalog", lambda: {})
    result = (await client.post("/api/skills/rescan")).json()
    assert result["missing"] == 2
    assert result["removed"] == 0
    assert result["unassigned"] == 0

    items = (await client.get("/api/skills")).json()["items"]
    assert all(item["available"] is False for item in items)
    listed = (await client.get(f"/api/agents/{agent['id']}")).json()
    assert "custom-cli:alpha" in listed["skill_ids"]


async def test_missing_skill_root_keeps_assignments(
    client: httpx.AsyncClient, catalog: Path
):
    """技能根整个不在（数据卷没挂上）时，这一轮无法判断，不能清掉分配。"""
    await client.post("/api/skills/rescan")
    agent = await make_agent(client)
    await client.put(f"/api/agents/{agent['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]})

    # 连同技能根一起消失：不能把「根不可用」当成「技能被删」。
    skill_service.shutil.rmtree(catalog)
    result = (await client.post("/api/skills/rescan")).json()
    assert result["removed"] == 0
    assert result["unassigned"] == 0
    assert result["missing"] == 2

    listed = (await client.get(f"/api/agents/{agent['id']}")).json()
    assert "custom-cli:alpha" in listed["skill_ids"]


async def test_corrupt_library_owner_does_not_break_rescan(client: httpx.AsyncClient):
    """库里的归属被改坏时，重扫要跳过引用登记清理，不能让整个编目 500。"""
    from app.models import Skill

    async with SessionLocal() as session:
        session.add(
            Skill(
                id="custom-cli:lib:bad/id:alpha",
                type_id="custom-cli",
                slug="alpha",
                name="归属损坏的技能",
                source_kind="user",
                owner_id="bad/id",
                path="/nonexistent",
                available=False,
            )
        )
        await session.commit()

    response = await client.post("/api/skills/rescan")
    assert response.status_code == 200
    assert response.json()["removed"] >= 1


async def test_gone_library_skill_is_pruned_with_its_mounts(
    client: httpx.AsyncClient, transfer_home: Path
):
    """用户技能库实体目录消失：编目行、智能体分配与 `.mounts.json` 引用一起清掉。"""
    library = skill_service.get_settings().skill_libraries_root / "local"
    write_skill(library, "alpha")
    skill_service.write_library_mounts(library, {"alpha": ["custom-cli"]})
    await client.post("/api/skills/rescan")

    agent = await make_agent(client)
    await client.put(
        f"/api/agents/{agent['id']}/skills",
        json={"skill_ids": ["custom-cli:lib:local:alpha"]},
    )
    skill_service.shutil.rmtree(library / "alpha")
    result = (await client.post("/api/skills/rescan")).json()
    assert result["removed"] == 1
    assert result["unassigned"] == 1
    assert skill_service.read_library_mounts(library) == {}
    assert (await client.get("/api/skills/custom-cli:lib:local:alpha")).status_code == 404
    listed = (await client.get(f"/api/agents/{agent['id']}")).json()
    assert "custom-cli:lib:local:alpha" not in listed["skill_ids"]


async def test_removed_market_skills_are_hidden_from_list(client: httpx.AsyncClient):
    """市场技能卸载后（旧数据里可能只标了 available=False）不该继续出现在列表里。"""
    from app.models import Skill

    async with SessionLocal() as session:
        session.add(
            Skill(
                id="custom-cli:gone-market",
                type_id="custom-cli",
                slug="gone-market",
                name="已卸载的市场技能",
                source_kind="market",
                path="/nonexistent",
                available=False,
            )
        )
        await session.commit()

    items = (await client.get("/api/skills")).json()["items"]
    assert all(item["slug"] != "gone-market" for item in items)
    assert (await client.get("/api/skills", params={"source_kind": "market"})).json()["items"] == []
    # 显式查询失效技能时仍能看到，方便排查历史分配。
    assert [
        item["slug"]
        for item in (await client.get("/api/skills", params={"available": False})).json()["items"]
    ] == ["gone-market"]
    # 重扫时没有任何分配的失效行会被直接清理，不再永远躺在表里。
    assert (await client.post("/api/skills/rescan")).json()["removed"] >= 1
    assert (await client.get("/api/skills", params={"available": False})).json()["items"] == []


async def test_delete_non_system_skill_removes_dir_and_assignment(
    client: httpx.AsyncClient, catalog: Path, monkeypatch: pytest.MonkeyPatch
):
    """个人技能可以从技能页删除：目录、编目行与智能体分配一起消失。"""
    monkeypatch.setattr(
        skill_service, "skill_roots_for_type", lambda _type_id: [skill_service.SkillRoot(str(catalog))]
    )
    await client.post("/api/skills/rescan")
    agent = await make_agent(client)
    role = await make_role(client, agent)
    assigned = await client.put(
        f"/api/agents/{role['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]}
    )
    assert assigned.status_code == 200, assigned.text
    assert (catalog / "alpha").is_dir()

    response = await client.delete("/api/skills/custom-cli:alpha")
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "alpha"
    assert not (catalog / "alpha").exists()
    assert (await client.get("/api/skills/custom-cli:alpha")).status_code == 404
    roles = (await client.get("/api/agents")).json()["items"]
    assert next(item for item in roles if item["id"] == role["id"])["skill_ids"] == []


async def test_delete_refuses_system_skill_and_outside_path(
    client: httpx.AsyncClient, catalog: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """内核自带技能不能删；数据库里的路径越出技能根时也拒绝删除。"""
    from app.models import Skill

    monkeypatch.setattr(
        skill_service, "skill_roots_for_type", lambda _type_id: [skill_service.SkillRoot(str(catalog))]
    )
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# 越界技能\n", encoding="utf-8")

    async with SessionLocal() as session:
        session.add(
            Skill(
                id="custom-cli:builtin",
                type_id="custom-cli",
                slug="builtin",
                name="内核技能",
                source_kind="system",
                path=str(catalog / "alpha"),
            )
        )
        session.add(
            Skill(
                id="custom-cli:outside",
                type_id="custom-cli",
                slug="outside",
                name="越界技能",
                source_kind="user",
                path=str(outside),
            )
        )
        await session.commit()

    response = await client.delete("/api/skills/custom-cli:builtin")
    assert response.status_code == 422
    assert "内核自带" in response.json()["detail"]

    response = await client.delete("/api/skills/custom-cli:outside")
    assert response.status_code == 422
    assert "不在" in response.json()["detail"]
    assert outside.is_dir()


async def test_delete_refuses_installed_plugin_skill(
    client: httpx.AsyncClient, catalog: Path, monkeypatch: pytest.MonkeyPatch
):
    """已安装插件里的技能不能单独删：删了插件就残缺，重扫还会加回来。"""
    from app.models import Skill

    monkeypatch.setattr(
        skill_service, "skill_roots_for_type", lambda _type_id: [skill_service.SkillRoot(str(catalog))]
    )
    async with SessionLocal() as session:
        session.add(
            Skill(
                id="custom-cli:plugin-skill",
                type_id="custom-cli",
                slug="plugin-skill",
                name="插件里的技能",
                source_kind="plugin",
                path=str(catalog / "alpha"),
            )
        )
        await session.commit()

    items = (await client.get("/api/skills")).json()["items"]
    listed = next(item for item in items if item["slug"] == "plugin-skill")
    assert listed["deletable"] is False

    response = await client.delete("/api/skills/custom-cli:plugin-skill")
    assert response.status_code == 422
    assert "插件" in response.json()["detail"]
    assert (catalog / "alpha").is_dir()


async def test_delete_missing_skill_returns_404(client: httpx.AsyncClient):
    """不存在的技能返回 404（不靠中文字符串匹配状态码）。"""
    response = await client.delete("/api/skills/custom-cli:missing")
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


async def test_delete_reports_directory_failure(
    client: httpx.AsyncClient, catalog: Path, monkeypatch: pytest.MonkeyPatch
):
    """磁盘删不掉时要显式报错，不能假装成功（否则重扫会把技能加回来）。"""
    monkeypatch.setattr(
        skill_service, "skill_roots_for_type", lambda _type_id: [skill_service.SkillRoot(str(catalog))]
    )
    await client.post("/api/skills/rescan")

    def refuse(_path, *_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(skill_service.shutil, "rmtree", refuse)
    response = await client.delete("/api/skills/custom-cli:alpha")
    assert response.status_code == 422
    assert "删除" in response.json()["detail"]
    assert (catalog / "alpha").is_dir()
    # 编目行还在：失败不能留下「数据库已删、目录还在」的半截状态。
    assert (await client.get("/api/skills/custom-cli:alpha")).status_code == 200


async def test_agent_skills_come_only_from_assignment(client: httpx.AsyncClient, catalog: Path):
    await client.post("/api/skills/rescan")
    agent = await make_agent(client)
    role = await make_role(client, agent)
    assert role["skill_ids"] == []

    response = await client.put(
        f"/api/agents/{role['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["skill_ids"] == ["custom-cli:alpha"]
    assert [brief["name"] for brief in response.json()["skills"]] == ["alpha"]

    roles = (await client.get("/api/agents")).json()["items"]
    listed = next(item for item in roles if item["id"] == role["id"])
    assert listed["skill_ids"] == ["custom-cli:alpha"]

    detail = (await client.get("/api/skills/custom-cli:alpha")).json()
    assert [entry["name"] for entry in detail["agents"]] == ["审查员"]

    # 类型不匹配 / 不存在的技能都要被服务端挡住（界面同样禁止选择）。
    codex_agent = await make_agent(client, "codex")
    codex_role = await make_role(client, codex_agent)
    mismatch = await client.put(
        f"/api/agents/{codex_role['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]}
    )
    assert mismatch.status_code == 422
    assert "不匹配" in mismatch.json()["detail"]

    unknown = await client.put(
        f"/api/agents/{role['id']}/skills", json={"skill_ids": ["custom-cli:不存在"]}
    )
    assert unknown.status_code == 422
    assert "不存在" in unknown.json()["detail"]

    # 整表替换：清空分配
    cleared = await client.put(f"/api/agents/{role['id']}/skills", json={"skill_ids": []})
    assert cleared.json()["skill_ids"] == []


async def test_deleted_agent_stops_counting_as_a_skill_user(client: httpx.AsyncClient, catalog: Path):
    """软删除的智能体不再算技能的"使用中"智能体，但分配关系保留，历史对话仍能复现技能。"""
    await client.post("/api/skills/rescan")
    agent = await make_agent(client)
    role = await make_role(client, agent)
    await client.put(f"/api/agents/{role['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]})
    assert (await client.get("/api/skills/custom-cli:alpha")).json()["used_by_agents"] == 1

    assert (await client.delete(f"/api/agents/{role['id']}")).status_code == 204

    detail = (await client.get("/api/skills/custom-cli:alpha")).json()
    assert detail["agents"] == []
    assert detail["used_by_agents"] == 0
    async with SessionLocal() as session:
        assert await skill_service.agent_skill_ids(session, role["id"]) == ["custom-cli:alpha"]


async def test_agent_copies_skills_on_duplicate_and_drops_mismatch_on_retype(
    client: httpx.AsyncClient, catalog: Path
):
    await client.post("/api/skills/rescan")
    agent = await make_agent(client)
    other = await make_agent(client)
    role = await make_role(client, agent)
    await client.put(f"/api/agents/{role['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]})

    clone = (await client.post(f"/api/agents/{role['id']}/duplicate")).json()
    assert clone["skill_ids"] == ["custom-cli:alpha"]

    # 换网关（内核不变）时保留；换内核时丢弃不匹配的技能。
    same = (
        await client.patch(f"/api/agents/{role['id']}", json={"gateway_id": other["gateway_id"]})
    ).json()
    assert same["skill_ids"] == ["custom-cli:alpha"]

    rebound = (await client.patch(f"/api/agents/{role['id']}", json={"type_id": "codex"})).json()
    assert rebound["skill_ids"] == []


async def test_run_materializes_assigned_skills_into_isolated_home(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    # 本用例只验证「启动子进程之前已经物化」，不需要真实内核跑完一轮；
    # 给它一个很短的运行上限，免得本机装了 codex 时把测试拖成无限等待。
    monkeypatch.setattr(get_settings(), "run_timeout_seconds", 5)
    root = tmp_path / "codex"
    write_skill(root / "skills", "user-skill")
    write_skill(root / "skills" / ".system", "sys-skill")
    (root / "config.toml").write_text('model = "gpt-5-codex"\n', encoding="utf-8")
    (root / "auth.json").write_text("{}\n", encoding="utf-8")

    specs = get_agent("codex").discover_skills()
    monkeypatch.setattr(
        skill_service, "discover_catalog", lambda: {spec.id: spec for spec in specs}
    )
    await client.post("/api/skills/rescan")

    agent = await make_agent(client, "codex")
    role = await make_role(client, agent)
    await client.put(f"/api/agents/{role['id']}/skills", json={"skill_ids": ["codex:user-skill"]})
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": role["id"], "title": "技能验证"})
    ).json()

    # 模拟「隔离之前就存在的旧会话」：隔离目录里没有它的记录，必须改成新会话，否则续接必然失败。
    async with SessionLocal() as session:
        stored = await session.get(Conversation, conversation["id"])
        stored.session_id = "旧会话-id"
        await session.commit()

    response = await client.post(
        f"/api/conversations/{conversation['id']}/messages", json={"content": "你好"}
    )
    # 不管本机是否装了 codex，技能物化都发生在启动子进程之前，必须已经完成。
    events = await read_sse(response)
    logs = [event.get("text") or "" for event in events if event["type"] == "log"]
    assert any("已改为新会话" in line for line in logs), logs

    home = get_settings().agent_homes_root / conversation["id"] / "codex-home"
    assert (home / "skills" / "user-skill" / "SKILL.md").is_file()
    names = {path.name for path in (home / "skills").iterdir()}
    # 装了 Codex CLI 的机器上，内核启动时会自己预置 ``skills/.system`` 系统技能目录，
    # 那不是智能体分配带进去的，所以允许它存在；除此之外必须只有分配到的那个技能。
    assert "user-skill" in names, names
    assert names - {"user-skill", ".system"} == set(), names
    # 源目录里故意放在 ``.system`` 下的未分配技能，绝不能连目录一起被带进来
    # （整目录复制会把它塞进隔离目录，等于「未分配的技能悄悄生效」）。
    assert not (home / "skills" / ".system" / "sys-skill").exists()
    assert (home / "config.toml").is_file()
    assert (home / "auth.json").stat().st_mode & 0o777 == 0o600

    # 删除对话时隔离目录一并回收。
    assert (await client.delete(f"/api/conversations/{conversation['id']}")).status_code == 204
    assert not home.exists()


def test_materialize_syncs_frontmatter_name_to_directory(tmp_path: Path):
    """物化副本的 frontmatter name 要与目录名一致，否则按名字找技能的内核会找不到。"""
    from app.runtime.skills import materialize

    source = write_skill(tmp_path / "src", "access")
    spec = spec_for(source, source_kind="plugin")
    spec.slug = "telegram-access"  # 模拟命名空间前缀后的目录名

    home = tmp_path / "home"
    warnings = materialize(home, SkillMount(home_name="h"), [spec])

    assert warnings == []
    copied = (home / "skills" / "telegram-access" / "SKILL.md").read_text(encoding="utf-8")
    assert "name: telegram-access" in copied
    # 只改副本：源目录必须原样保留。
    assert "name: access" in (source / "SKILL.md").read_text(encoding="utf-8")
    # 其它元数据不能被顺手改掉。
    assert "description: access 的说明" in copied


def test_materialize_leaves_matching_name_untouched(tmp_path: Path):
    """目录名与 frontmatter 一致时不该改写文件（避免无谓地动别人的技能）。"""
    from app.runtime.skills import materialize

    source = write_skill(tmp_path / "src", "plain")
    original = (source / "SKILL.md").read_text(encoding="utf-8")
    home = tmp_path / "home"

    assert materialize(home, SkillMount(home_name="h"), [spec_for(source)]) == []
    assert (home / "skills" / "plain" / "SKILL.md").read_text(encoding="utf-8") == original


def test_seed_skills_is_idempotent_and_skips_when_present(tmp_path: Path, monkeypatch):
    """内核自带技能播种：已存在就跳过，绝不重复执行子进程。"""
    from app.agents.builtin import codex as codex_mod

    home = tmp_path / "codex-home"
    monkeypatch.setattr(codex_mod, "codex_home", lambda: home)

    called: list[list[str]] = []

    def fake_run(argv, **kwargs):
        called.append(argv)

        class Result:
            returncode = 0
            stdout = b""
            stderr = b""

        return Result()

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(codex_mod.shutil, "which", lambda _: "/usr/bin/codex")

    agent = get_agent("codex")

    # 目录不存在 → 会跑一次子进程播种
    (home / "skills").mkdir(parents=True)
    actions = agent.seed_skills()
    assert len(called) == 1, called
    assert called[0][:3] == ["codex", "debug", "prompt-input"]
    # 假子进程没真的写目录，所以这里不产生动作描述
    assert actions == []

    # 目录已存在 → 直接跳过，不再起子进程
    (home / "skills" / ".system").mkdir()
    called.clear()
    assert agent.seed_skills() == []
    assert called == [], "已有 .system 时不应再执行播种命令"


def test_seed_skills_never_raises_when_binary_missing(tmp_path: Path, monkeypatch):
    """没装内核时播种必须静默返回空列表，不能把启动或安装搞挂。"""
    from app.agents.builtin import codex as codex_mod

    monkeypatch.setattr(codex_mod, "codex_home", lambda: tmp_path / "h")
    monkeypatch.setattr(codex_mod.shutil, "which", lambda _: None)

    assert get_agent("codex").seed_skills() == []


# --- 导出 / 导入 / 跨内核挂载 -------------------------------------------------


def _make_zip(files: dict[str, str], *, top: str | None = "demo") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, body in files.items():
            zf.writestr(f"{top}/{name}" if top else name, body)
    return buffer.getvalue()


@pytest.fixture
def transfer_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """两个内核各有一个临时技能根；编目直接扫这两个根，不碰开发机上的真实技能。"""
    from app.agents.skills import SkillRoot, scan_skill_roots

    roots = {
        "custom-cli": [SkillRoot(str(tmp_path / "custom" / "skills"), source_kind="user")],
        "claude-code": [SkillRoot(str(tmp_path / "claude" / "skills"), source_kind="user")],
    }

    def fake_roots(type_id: str) -> list[SkillRoot]:
        return roots[type_id]

    def fake_catalog() -> dict[str, SkillSpec]:
        catalog: dict[str, SkillSpec] = {}
        for type_id, items in roots.items():
            for spec in scan_skill_roots(items, type_id=type_id):
                catalog[spec.id] = spec
        return catalog

    monkeypatch.setattr(skill_service, "skill_roots_for_type", fake_roots)
    monkeypatch.setattr(skill_service, "discover_catalog", fake_catalog)
    return tmp_path


async def test_export_skill_returns_zip(client: httpx.AsyncClient, transfer_home: Path):
    """导出是只读的：把源目录原样打包，顶层目录名用技能 slug。"""
    write_skill(transfer_home / "custom" / "skills", "alpha")
    await client.post("/api/skills/rescan")

    response = await client.get("/api/skills/custom-cli:alpha/export")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="alpha.zip"' in response.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = set(zf.namelist())
        assert "alpha/SKILL.md" in names
        assert "alpha/scripts/run.sh" in names
        assert "alpha/references/note.md" in names

    # 导出不删除源目录
    assert (transfer_home / "custom" / "skills" / "alpha" / "SKILL.md").is_file()


async def test_export_missing_directory_is_reported(client: httpx.AsyncClient, catalog: Path):
    await client.post("/api/skills/rescan")
    # 目录在两次重扫之间被外部删掉：编目行还在，导出时按「目录已不存在」报错。
    skill_service.shutil.rmtree(catalog / "alpha")

    response = await client.get("/api/skills/custom-cli:alpha/export")
    assert response.status_code == 422
    assert "目录已不存在" in response.json()["detail"]


async def test_import_skill_extracts_into_user_root(
    client: httpx.AsyncClient, transfer_home: Path
):
    """导入的 zip 落在目标内核的用户技能根，并立刻进入统一编目。"""
    archive = _make_zip(
        {
            "SKILL.md": SKILL_MD.format(name="导入技能", description="导入的示例", extra=""),
            "scripts/run.sh": "echo hi\n",
        },
        top="imported-skill",
    )
    response = await client.post(
        "/api/skills/import",
        json={
            "type_id": "custom-cli",
            "name": "随便起的名字.zip",
            "content_base64": base64.b64encode(archive).decode(),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # 目录名取压缩包里的顶层目录，而不是上传时的文件名；落点是当前用户的技能库。
    assert body["skill_id"] == "custom-cli:lib:local:imported-skill"
    assert body["slug"] == "imported-skill"
    # 技能库按 slug 只存一份：路径里不再有内核子目录。
    library = skill_service.get_settings().skill_libraries_root / "local"
    assert (library / "imported-skill" / "SKILL.md").is_file()

    items = (await client.get("/api/skills")).json()["items"]
    assert [item["slug"] for item in items] == ["imported-skill"]
    assert items[0]["available"] is True
    assert items[0]["is_own"] is True


async def test_import_flat_archive_uses_filename_as_slug(
    client: httpx.AsyncClient, transfer_home: Path
):
    """压缩包没有顶层目录（SKILL.md 就在根部）时，用上传文件名推导目录名。"""
    archive = _make_zip(
        {"SKILL.md": SKILL_MD.format(name="扁平技能", description="扁平", extra="")}, top=None
    )
    response = await client.post(
        "/api/skills/import",
        json={
            "type_id": "custom-cli",
            "name": "flat-skill.zip",
            "content_base64": base64.b64encode(archive).decode(),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["slug"] == "flat-skill"
    library = skill_service.get_settings().skill_libraries_root / "local"
    assert (library / "flat-skill" / "SKILL.md").is_file()


async def test_import_rejects_bad_archive_and_missing_skill_md(
    client: httpx.AsyncClient, transfer_home: Path
):
    bad = await client.post(
        "/api/skills/import",
        json={
            "type_id": "custom-cli",
            "content_base64": base64.b64encode(b"not a zip").decode(),
        },
    )
    assert bad.status_code == 422

    no_skill = await client.post(
        "/api/skills/import",
        json={
            "type_id": "custom-cli",
            "content_base64": base64.b64encode(_make_zip({"readme.md": "hi"}, top="pkg")).decode(),
        },
    )
    assert no_skill.status_code == 422
    assert "SKILL.md" in no_skill.json()["detail"]

    not_base64 = await client.post(
        "/api/skills/import", json={"type_id": "custom-cli", "content_base64": "!!!"}
    )
    assert not_base64.status_code == 422


async def test_import_refuses_same_name_without_overwriting(
    client: httpx.AsyncClient, transfer_home: Path
):
    write_skill(transfer_home / "custom" / "skills", "alpha")
    await client.post("/api/skills/rescan")
    original = (transfer_home / "custom" / "skills" / "alpha" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    archive = _make_zip(
        {"SKILL.md": SKILL_MD.format(name="覆盖版", description="覆盖", extra="")}, top="alpha"
    )
    response = await client.post(
        "/api/skills/import",
        json={"type_id": "custom-cli", "content_base64": base64.b64encode(archive).decode()},
    )
    assert response.status_code == 422
    assert "同名技能" in response.json()["detail"]
    # 旧技能原样保留
    assert (transfer_home / "custom" / "skills" / "alpha" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == original


async def test_copy_skill_to_another_kernel_mounts_the_same_copy(
    client: httpx.AsyncClient, transfer_home: Path
):
    """挂载到另一个内核：只收一份文件、登记一条引用，两个内核共用同一个实体目录。"""
    write_skill(transfer_home / "custom" / "skills", "alpha")
    await client.post("/api/skills/rescan")

    response = await client.post(
        "/api/skills/custom-cli:alpha/copy", json={"type_id": "claude-code"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["skill_id"] == "claude-code:lib:local:alpha"
    library = skill_service.get_settings().skill_libraries_root / "local"
    assert (library / "alpha" / "SKILL.md").is_file()
    # 只有一份实体：原来按内核分的子目录不再产生。
    assert not (library / "claude-code").exists()
    assert not (library / "custom-cli").exists()
    assert (transfer_home / "custom" / "skills" / "alpha" / "SKILL.md").is_file()

    items = (await client.get("/api/skills")).json()["items"]
    shared = next(item for item in items if item["id"] == "claude-code:lib:local:alpha")
    assert shared["mounted_kernels"] == ["claude-code"]
    assert shared["slug"] == "alpha"
    # 两行的 path 指向同一个技能实体。
    detail = (await client.get("/api/skills/claude-code:lib:local:alpha")).json()
    assert detail["path"] == str(library / "alpha")

    slugs = {item["id"] for item in items}
    assert {"custom-cli:alpha", "claude-code:lib:local:alpha"} <= slugs

    # 再从库里的那一行挂到原内核会报「同名」（同一个内核不能重复引用）。
    again = await client.post(
        "/api/skills/claude-code:lib:local:alpha/copy", json={"type_id": "custom-cli"}
    )
    assert again.status_code == 422
    assert "同名技能" in again.json()["detail"]

    # 再次从共享行挂到同一内核：目标实体内容一致，应幂等成功，不能被当成「同名」拒绝。
    repeat = await client.post(
        "/api/skills/custom-cli:alpha/copy", json={"type_id": "claude-code"}
    )
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["skill_id"] == "claude-code:lib:local:alpha"


async def test_copy_all_kernels_mounts_everywhere_and_reports_conflicts(
    client: httpx.AsyncClient, transfer_home: Path
):
    """「挂载到全部内核」：逐个登记引用，冲突的内核只跳过并汇报，不挡住其它内核。"""
    write_skill(transfer_home / "custom" / "skills", "alpha")
    await client.post("/api/skills/rescan")

    response = await client.post("/api/skills/custom-cli:alpha/copy-all")
    assert response.status_code == 200, response.text
    body = response.json()
    # transfer_home 只声明了 custom-cli 与 claude-code 两个支持技能的内核
    #（fake_roots 只覆盖这两个），所以批量挂载也只会命中它们。
    # 源是共享技能：收进技能库时只登记目标内核的引用（源内核用的是自己的原生目录）。
    assert body["mounted"] == ["claude-code"]
    assert body["skipped"] == []
    library = skill_service.get_settings().skill_libraries_root / "local"
    mounts = skill_service.read_library_mounts(library)
    assert set(mounts["alpha"]) == {"claude-code"}

    # 幂等：从库里的行再挂一次，源内核有同名共享技能会被跳过，但没有新目标可挂。
    again = await client.post("/api/skills/claude-code:lib:local:alpha/copy-all")
    assert again.status_code == 200, again.text
    assert again.json()["mounted"] == []
    assert [item["type_id"] for item in again.json()["skipped"]] == ["custom-cli"]
    assert "同名技能" in again.json()["skipped"][0]["reason"]

    # 同名冲突的内核被跳过：目标内核根下有内容不同的同名共享技能。
    write_skill(transfer_home / "claude" / "skills", "beta", name="claude 版")
    write_skill(transfer_home / "custom" / "skills", "beta")
    await client.post("/api/skills/rescan")
    conflict = await client.post("/api/skills/custom-cli:beta/copy-all")
    assert conflict.status_code == 200, conflict.text
    skipped = conflict.json()["skipped"]
    assert [item["type_id"] for item in skipped] == ["claude-code"]
    assert "同名技能" in skipped[0]["reason"]

    missing = await client.post("/api/skills/custom-cli:missing/copy-all")
    assert missing.status_code == 404


async def test_copy_shared_skill_refuses_existing_personal_with_same_slug(
    client: httpx.AsyncClient, transfer_home: Path
):
    """库里有同名个人技能时，复制共享技能不能悄悄把个人那份当成挂载点。

    回归：`copy_skill_to_kernel` 只判断目标目录「有没有 SKILL.md」，不看内容，
    导致接口回 200，但返回的是用户自己的同名技能（共享版根本没被复制）。
    """
    library = skill_service.get_settings().skill_libraries_root / "local"
    archive = _make_zip(
        {"SKILL.md": SKILL_MD.format(name="个人版", description="个人技能", extra="")},
        top="alpha",
    )
    imported = await client.post(
        "/api/skills/import",
        json={"type_id": "custom-cli", "content_base64": base64.b64encode(archive).decode()},
    )
    assert imported.status_code == 200, imported.text

    # 共享技能里也有一个 alpha，但内容与个人版不同。
    write_skill(transfer_home / "claude" / "skills", "alpha", name="共享版")
    await client.post("/api/skills/rescan")

    conflict = await client.post(
        "/api/skills/claude-code:alpha/copy", json={"type_id": "custom-cli"}
    )
    assert conflict.status_code == 422, conflict.text
    assert "同名技能" in conflict.json()["detail"]

    # 个人技能的原文件没有被共享版顶替。
    text = (library / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert "个人版" in text
    detail = (await client.get("/api/skills/custom-cli:lib:local:alpha")).json()
    assert detail["name"] == "个人版"


def test_uninstall_legacy_native_market_skill_does_not_write_mounts(tmp_path: Path):
    """旧布局原生目录里的市场技能（目录已消失）卸载时，不能写幽灵引用登记。

    回归：`_remove_installed_skill` 对非库行也走了 `mounts_for_row()` 的默认挂载分支，
    会把一个根本不存在的实体的 slug 写进 `.mounts.json`，污染后续同名技能的挂载。
    """
    row = Skill(
        id="claude-code:ghost",
        type_id="claude-code",
        slug="ghost",
        name="幽灵技能",
        source_kind=skill_service.MARKET_SOURCE,
        path=str(tmp_path / "gone"),
    )
    assert skill_service._remove_installed_skill("claude-code", row) == []
    library = skill_service.get_settings().skill_libraries_root / "local"
    assert "ghost" not in skill_service.read_library_mounts(library)


async def test_copy_rejects_same_kernel_and_existing_target(
    client: httpx.AsyncClient, transfer_home: Path
):
    write_skill(transfer_home / "custom" / "skills", "alpha")
    write_skill(transfer_home / "claude" / "skills", "alpha")
    await client.post("/api/skills/rescan")

    same = await client.post("/api/skills/custom-cli:alpha/copy", json={"type_id": "custom-cli"})
    assert same.status_code == 422
    assert "相同" in same.json()["detail"]

    conflict = await client.post(
        "/api/skills/custom-cli:alpha/copy", json={"type_id": "claude-code"}
    )
    assert conflict.status_code == 422
    assert "同名技能" in conflict.json()["detail"]

    missing = await client.post(
        "/api/skills/custom-cli:missing/copy", json={"type_id": "claude-code"}
    )
    assert missing.status_code == 404


async def test_export_and_copy_refuse_paths_outside_declared_roots(
    client: httpx.AsyncClient, catalog: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """数据库里的路径越出适配器声明的技能根时，导出 / 复制都要拒绝（与删除同一道护栏）。"""
    from app.models import Skill

    monkeypatch.setattr(
        skill_service,
        "skill_roots_for_type",
        lambda _type_id: [skill_service.SkillRoot(str(catalog))],
    )
    outside = write_skill(tmp_path / "outside-root", "outside")
    async with SessionLocal() as session:
        session.add(
            Skill(
                id="custom-cli:outside",
                type_id="custom-cli",
                slug="outside",
                name="越界技能",
                source_kind="user",
                path=str(outside),
            )
        )
        await session.commit()

    exported = await client.get("/api/skills/custom-cli:outside/export")
    assert exported.status_code == 422
    assert "不在它所属的技能目录内" in exported.json()["detail"]

    copied = await client.post(
        "/api/skills/custom-cli:outside/copy", json={"type_id": "claude-code"}
    )
    assert copied.status_code == 422
    assert "不在它所属的技能目录内" in copied.json()["detail"]


async def test_rescan_migrates_legacy_per_kernel_library(
    client: httpx.AsyncClient, transfer_home: Path
):
    """旧的「每内核一份」布局自动迁成「按 slug 一份 + 引用登记」。"""
    library = skill_service.get_settings().skill_libraries_root / "local"
    write_skill(library / "claude-code", "alpha")
    write_skill(library / "custom-cli", "alpha")  # 内容一致：应合并成一份
    write_skill(library / "claude-code", "beta")

    await client.post("/api/skills/rescan")

    assert (library / "alpha" / "SKILL.md").is_file()
    assert (library / "beta" / "SKILL.md").is_file()
    assert not (library / "claude-code").exists()
    assert not (library / "custom-cli").exists()
    mounts = skill_service.read_library_mounts(library)
    assert set(mounts["alpha"]) == {"claude-code", "custom-cli"}
    assert mounts["beta"] == ["claude-code"]

    items = (await client.get("/api/skills")).json()["items"]
    assert {
        "claude-code:lib:local:alpha",
        "custom-cli:lib:local:alpha",
        "claude-code:lib:local:beta",
    } <= {item["id"] for item in items}
    detail = (await client.get("/api/skills/custom-cli:lib:local:alpha")).json()
    assert detail["path"] == str(library / "alpha")


async def test_delete_shared_library_skill_unmounts_one_kernel(
    client: httpx.AsyncClient, transfer_home: Path
):
    """删除只摘本内核引用；最后一个引用消失才删实体目录。"""
    archive = _make_zip(
        {"SKILL.md": SKILL_MD.format(name="共享版", description="共享", extra="")}, top="alpha"
    )
    imported = await client.post(
        "/api/skills/import",
        json={"type_id": "custom-cli", "content_base64": base64.b64encode(archive).decode()},
    )
    assert imported.status_code == 200, imported.text
    mounted = await client.post(
        "/api/skills/custom-cli:lib:local:alpha/copy", json={"type_id": "claude-code"}
    )
    assert mounted.status_code == 200, mounted.text
    library = skill_service.get_settings().skill_libraries_root / "local"
    assert (library / "alpha" / "SKILL.md").is_file()

    removed = await client.delete("/api/skills/claude-code:lib:local:alpha")
    assert removed.status_code == 200, removed.text
    assert removed.json()["remaining_kernels"] == ["custom-cli"]
    assert (library / "alpha" / "SKILL.md").is_file()
    left = (await client.get("/api/skills")).json()["items"]
    assert [item["id"] for item in left] == ["custom-cli:lib:local:alpha"]

    final = await client.delete("/api/skills/custom-cli:lib:local:alpha")
    assert final.status_code == 200, final.text
    assert final.json()["remaining_kernels"] == []
    assert not (library / "alpha").exists()
