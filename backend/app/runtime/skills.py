"""技能：统一编目、智能体分配与运行时物化。

* 内核自带 / 宿主机上的技能由适配器的 :meth:`AgentDefinition.discover_skills` 只读扫描，
  全平台共享；
* 用户自己的技能（导入 / 市场安装 / 挂载到其它内核）**按 slug 只存一份**：
  ``<数据目录>/skill-libraries/<owner_id>/<slug>``，按用户隔离；
* 「各内核引用同一份技能」登记在 ``<owner_id>/.mounts.json``，编目时按引用展开成
  「一个内核一行」（``skills.id = <type>:lib:<owner>:<slug>``）。所有行指向同一个
  ``path``，因此市场更新覆盖实体后，挂载到它的所有内核同时生效；
* 两类技能都汇进 ``skills`` 表，并按智能体的分配结果在运行时物化到该内核的隔离目录，
  从而保证「智能体能用的技能 == 分配到的技能」。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import delete, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import AgentContext, AgentDefinition, all_agents, get_agent
from ..agents.skills import (
    LIBRARY_ID_MARK,
    MARKET_MARKER_FILE,
    MARKET_SOURCE,
    MAX_SKILL_BYTES,
    SKILL_FILE,
    SKIP_DIRS,
    MarketInstallInfo,
    SkillMount,
    SkillRoot,
    SkillSpec,
    is_library_skill_id,
    library_skill_id,
    parse_skill_dir,
    read_market_marker,
    safe_skill_dirname,
    scan_skill_root,
    write_market_marker,
)
from ..models import AgentProfile, AgentSkill, Skill
from ..settings import get_settings
from ..skillhub import MAX_ARCHIVE_BYTES, SkillHubError, extract_skill_archive
from .isolation import ensure_home, home_for, note_fresh_home, safe_relative

logger = logging.getLogger("coda")

#: 技能来源的中文标签
SOURCE_LABELS = {
    "user": "个人技能",
    "project": "项目技能",
    "plugin": "插件技能",
    "system": "内核系统技能",
    "manual": "平台技能",
    MARKET_SOURCE: "腾讯 SkillHub",
}

#: 同名技能的去重优先级（越小越优先）；市场技能是用户显式安装的，优先级最高
SOURCE_PRECEDENCE = {
    "manual": 0,
    MARKET_SOURCE: 1,
    "user": 2,
    "project": 3,
    "plugin": 4,
    "system": 5,
}

#: 详情页读取 SKILL.md 正文的上限
MAX_BODY_BYTES = 200_000

#: 没有真实用户（鉴权关闭的单用户部署）时，用户技能库用的目录键。
LOCAL_OWNER_KEY = "local"
_OWNER_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def owner_key(owner_id: str | None) -> str:
    """把用户 id 变成安全的目录名；没有用户时落到 ``local``（单用户部署）。"""
    if not owner_id:
        return LOCAL_OWNER_KEY
    if not _OWNER_KEY.match(owner_id):
        raise SkillError(f"用户标识不合法：{owner_id}")
    return owner_id


#: 用户技能库里的「内核引用」登记文件：``{"<slug>": ["<type-id>", ...]}``。
#: 技能实体按 slug 只存一份；这个文件记录它被哪些内核挂载（编目时据此展开成每内核一行）。
MOUNTS_FILE = ".mounts.json"


def library_root(owner_id: str | None) -> Path:
    """某个用户的技能库目录（平台托管，按用户隔离）。

    新布局下这里直接放技能实体 ``<slug>/``，不再按内核分子目录：一份技能被多个内核
    引用时共用同一个实体目录，市场更新覆盖一次，所有挂载它的内核同时生效。
    """
    return get_settings().skill_libraries_root / owner_key(owner_id)


def library_skill_dir(owner_id: str | None, slug: str) -> Path:
    """技能库里的实体目录 ``<owner>/<slug>``；slug 不安全时抛 :class:`SkillError`。"""
    safe = safe_skill_dirname(slug)
    if safe is None:
        raise SkillError(f"技能标识不合法：{slug}")
    return library_root(owner_id) / safe


def read_library_mounts(owner_dir: Path) -> dict[str, list[str]]:
    """读取技能库的内核引用登记；文件不存在 / 损坏时返回空表（尽力而为）。

    条目缺失表示「这份技能还没登记过引用」，由调用方决定默认值（新放进去的技能
    默认对全部支持技能的内核可见）。
    """
    try:
        data = json.loads((Path(owner_dir) / MOUNTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    mounts: dict[str, list[str]] = {}
    for slug, kernels in data.items():
        if not isinstance(slug, str) or not slug or not isinstance(kernels, list):
            continue
        # 这里只做「读」：键不会用来拼路径（扫描时按真实目录名查），保留原样即可。
        # 不按 ASCII 过滤，否则手工放进来的中文目录名会丢引用登记。
        mounts[slug] = list(dict.fromkeys(str(item) for item in kernels if item))
    return mounts


def write_library_mounts(owner_dir: Path, mounts: dict[str, list[str]]) -> None:
    """原子写入内核引用登记（先写临时文件再替换，避免读到半截 JSON）。"""
    owner_dir = Path(owner_dir)
    owner_dir.mkdir(parents=True, exist_ok=True)
    clean = {
        slug: list(dict.fromkeys(kernels))
        for slug, kernels in sorted(mounts.items())
        if isinstance(slug, str) and slug and kernels
    }
    payload = json.dumps(clean, ensure_ascii=False, indent=2)
    # 临时文件名要唯一：多进程 / 并发请求同时写时，公用一个 .tmp 会互相踩内容。
    fd, tmp_name = tempfile.mkstemp(prefix=f"{MOUNTS_FILE}.", dir=owner_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, owner_dir / MOUNTS_FILE)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _skill_capable_type_ids() -> list[str]:
    """声明了技能根的内核 id 列表（用来给「没有引用登记」的技能选默认挂载）。"""
    ids: list[str] = []
    for definition in all_agents():
        try:
            if skill_roots_for_type(definition.id):
                ids.append(definition.id)
        except SkillError:
            # 不支持技能的内核是常态（没有声明技能根），不算错误。
            continue
        except Exception as exc:  # noqa: BLE001 - 单个适配器异常不影响其它内核
            # 真异常必须留痕，否则某个适配器坏了会完全查不到线索（铁律 11）。
            logger.warning("检查内核 %s 的技能根失败：%s", definition.id, exc)
    return ids


def is_library_row(row: Skill) -> bool:
    """这一行是否是平台托管的用户技能（区别于内核自带 / 宿主机共享技能）。"""
    return is_library_skill_id(row.id)


# --- 编目 -------------------------------------------------------------------


def _annotate_library_spec(spec: SkillSpec, *, owner_id: str | None) -> SkillSpec:
    """给从用户技能库扫出来的技能补上归属与来源。

    库里带市场安装标记（``.skillhub.json``）的是「市场技能」，其余是用户自己放的「个人技能」。
    """
    spec.owner_id = owner_id
    spec.library = True
    info = read_market_marker(spec.path)
    if info is not None:
        spec.source_kind = MARKET_SOURCE
        spec.source_slug = info.slug or spec.slug
        spec.source_url = info.source_url
    else:
        spec.source_kind = "user"
    return spec


def discover_library_skills(owners: Iterable[str | None]) -> dict[str, SkillSpec]:
    """扫描这些用户的技能库（``<用户>/<slug>``），按「内核引用」展开成每内核一行。

    技能实体只解析一次，然后按 ``.mounts.json`` 里的引用来复制描述（``type_id`` 不同）。
    没有登记过的技能目录（例如用户手工放进去的）默认挂到所有支持技能的内核上；
    引用了未知 / 不支持技能的内核时跳过该条，避免一条坏登记拖垮整个技能库。
    """
    catalog: dict[str, SkillSpec] = {}
    capable = set(_skill_capable_type_ids())
    for owner_id in owners:
        owner_dir = library_root(owner_id)
        try:
            entries = sorted(child for child in owner_dir.iterdir() if child.is_dir())
        except OSError:
            continue
        mounts = read_library_mounts(owner_dir)
        for skill_dir in entries:
            try:
                base = parse_skill_dir(skill_dir, type_id="", source_kind="user")
            except Exception as exc:  # noqa: BLE001 - 单个坏目录不能拖垮整个技能库
                logger.warning("扫描技能库目录 %s 失败：%s", skill_dir, exc)
                continue
            if base is None:
                continue
            kernels = mounts.get(skill_dir.name)
            if not kernels:
                kernels = sorted(capable)
            for type_id in dict.fromkeys(kernels):
                if type_id not in capable:
                    continue
                spec = _annotate_library_spec(replace(base, type_id=type_id), owner_id=owner_id)
                catalog[spec.id] = spec
    return catalog


def migrate_library_layout(owners: Iterable[str | None]) -> int:
    """把旧的「每内核一份」布局迁到「按 slug 一份 + 引用登记」，返回迁移的目录数。

    旧布局：``<owner>/<type_id>/<slug>``；新布局：``<owner>/<slug>`` + ``.mounts.json``。
    * 目标不存在：直接移动，并把旧内核登记为引用；
    * 目标已存在、内容一致：删掉重复的一份，合并引用；
    * 目标已存在、内容不同：优先保留市场安装的那份；都非市场时保留先到的一份，
      另一份挪进 ``<owner>/.conflicts/``（不参与扫描），并记一条告警。
    迁移尽力而为：单个目录失败只记日志，绝不阻塞启动 / 重扫。
    """
    moved = 0
    for owner_id in owners:
        owner_dir = library_root(owner_id)
        try:
            children = sorted(child for child in owner_dir.iterdir() if child.is_dir())
        except OSError:
            continue
        mounts = read_library_mounts(owner_dir)
        touched = False
        for type_dir in children:
            # 新布局的技能目录直接含 SKILL.md；旧布局的内核目录不含（子目录才是技能）。
            if (type_dir / SKILL_FILE).is_file():
                continue
            try:
                skill_roots_for_type(type_dir.name)
            except SkillError:
                continue
            for skill_dir in sorted(child for child in type_dir.iterdir() if child.is_dir()):
                if not (skill_dir / SKILL_FILE).is_file():
                    continue
                slug = skill_dir.name
                target = owner_dir / slug
                try:
                    if not target.exists():
                        shutil.move(str(skill_dir), str(target))
                    else:
                        _merge_legacy_skill(skill_dir, target, type_dir.name)
                except OSError as exc:
                    logger.warning("迁移技能库目录 %s 失败：%s", skill_dir, exc)
                    continue
                mounts.setdefault(slug, [])
                if type_dir.name not in mounts[slug]:
                    mounts[slug].append(type_dir.name)
                touched = True
                moved += 1
            try:
                type_dir.rmdir()
            except OSError:
                pass
        if touched:
            write_library_mounts(owner_dir, mounts)
    return moved


def _merge_legacy_skill(legacy: Path, target: Path, type_id: str) -> None:
    """把旧布局里与目标同 slug 的另一份合并掉（内容一致删重复，不一致留档）。"""
    left = parse_skill_dir(legacy, type_id="", source_kind="user")
    right = parse_skill_dir(target, type_id="", source_kind="user")
    if left is not None and right is not None and left.fingerprint == right.fingerprint:
        shutil.rmtree(legacy)
        return
    # 内容不同：优先保留市场安装的那份，另一份挪到 .conflicts 留档（不参与编目）。
    legacy_is_market = read_market_marker(legacy) is not None
    target_is_market = read_market_marker(target) is not None
    conflicts = target.parent / ".conflicts"
    conflicts.mkdir(parents=True, exist_ok=True)
    name = f"{target.name}--{type_id}"
    destination = conflicts / name
    counter = 2
    while destination.exists():
        destination = conflicts / f"{name}-{counter}"
        counter += 1
    if legacy_is_market and not target_is_market:
        # 旧目录是市场安装的那份（更新过）：先把落选的挪走，再把市场份放到正式位置。
        shutil.move(str(target), str(destination))
        shutil.move(str(legacy), str(target))
        kept = "市场份"
    else:
        shutil.move(str(legacy), str(destination))
        kept = "目标份"
    logger.warning(
        "技能库迁移：%s 与 %s 内容不同，已保留 %s，另一份挪到 %s",
        legacy,
        target,
        kept,
        destination,
    )


def discover_catalog() -> dict[str, SkillSpec]:
    """聚合所有适配器声明的技能；单个适配器出错不影响其余技能。"""
    catalog: dict[str, SkillSpec] = {}
    for definition in all_agents():
        try:
            specs = definition.discover_skills() or []
        except Exception as exc:  # noqa: BLE001 - 扫描失败不能拖垮整个技能目录
            logger.warning("扫描智能体 %s 的技能失败：%s", definition.id, exc)
            continue
        for spec in specs:
            current = catalog.get(spec.id)
            if current is None or _rank(spec) < _rank(current):
                catalog[spec.id] = spec
    return catalog


def _rank(spec: SkillSpec) -> int:
    return SOURCE_PRECEDENCE.get(spec.source_kind, 9)


async def _library_owners(session: AsyncSession) -> list[str | None]:
    """要扫描哪些用户的技能库：磁盘上的目录 + 数据库里登记过的归属。"""
    keys: set[str] = set()
    root = get_settings().skill_libraries_root
    try:
        if root.is_dir():
            keys.update(
                child.name
                for child in root.iterdir()
                if child.is_dir() and _OWNER_KEY.match(child.name)
            )
    except OSError:
        pass
    # 目录暂时不见、但库里还登记着的技能也要扫一次（这样它们才会被标记为失效）；
    # 不扫的话它们会永远停在「可用」状态。
    statement = select(Skill.owner_id).where(Skill.id.like(f"%{LIBRARY_ID_MARK}%")).distinct()
    for (owner_id,) in (await session.execute(statement)).all():
        try:
            keys.add(owner_key(owner_id))
        except SkillError:
            # 库里的归属被改坏时跳过这一行，不能让整个编目失败。
            continue
    return [None if key == LOCAL_OWNER_KEY else key for key in sorted(keys)]


async def rescan(session: AsyncSession) -> dict[str, int]:
    """重新扫描内核技能目录与各用户的技能库，并同步到 ``skills`` 表。

    先做一次（幂等的）旧布局迁移：``<owner>/<type>/<slug>`` → ``<owner>/<slug>``
    并补齐引用登记，否则升级上来的技能库会扫不到。
    技能目录**真的从磁盘上消失**时，连同 ``agent_skills`` 里的分配一起清掉 ——
    技能都没了，分配就是幽灵引用（运行时本来也会跳过），留着只会让智能体的
    技能列表和磁盘对不上。只有「目录还在、只是这次没扫到」（适配器扫失败等）
    才保留编目行并标记 ``available=False``，免得一次扫描落空就把分配全清了；
    连技能根（父目录）都不在时同样按「这一轮无法判断」处理，不动分配
    （见 :func:`_skill_dir_gone`）。
    """
    owners = await _library_owners(session)
    try:
        migrated = migrate_library_layout(owners)
    except Exception as exc:  # noqa: BLE001 - 迁移失败不能拦住整次重扫
        logger.warning("技能库布局迁移失败：%s", exc)
        migrated = 0
    if migrated:
        logger.info("技能库布局迁移完成：%s 份技能已改为按 slug 存一份", migrated)
    discovered = discover_catalog()
    discovered.update(discover_library_skills(owners))
    rows = {row.id: row for row in (await session.execute(select(Skill))).scalars().all()}
    now = datetime.now(timezone.utc)
    added = updated = 0

    for skill_id, spec in discovered.items():
        row = rows.get(skill_id)
        fields = spec.to_row()
        if row is None:
            session.add(Skill(**fields, available=True, discovered_at=now, updated_at=now))
            added += 1
            continue
        changed = any(getattr(row, key) != value for key, value in fields.items())
        if changed or not row.available:
            for key, value in fields.items():
                setattr(row, key, value)
            row.available = True
            row.updated_at = now
            updated += 1

    gone: list[Skill] = []  # 目录真的从磁盘上没了、技能根还在：连分配一起清
    missing = 0  # 这一轮没扫到（目录还在或技能根暂不可用）：只标失效，不动分配
    for skill_id, row in rows.items():
        if skill_id in discovered:
            continue
        if _skill_dir_gone(row):
            gone.append(row)
        elif row.available:
            row.available = False
            row.updated_at = now
            missing += 1

    removed, unassigned, prunable = await _purge_gone_skills(session, gone)

    await session.commit()
    # 磁盘登记在事务提交之后再摘：提交失败时不能留下「库里有行、登记已删」的错位。
    _prune_stale_library_mounts(prunable)
    return {
        "added": added,
        "updated": updated,
        "missing": missing,
        "removed": removed,
        "unassigned": unassigned,
        "total": len(discovered),
    }


def _skill_dir_gone(row: Skill) -> bool:
    """技能目录是否**真的**从磁盘上没了（区别于「这次扫描没扫到」）。

    以磁盘为准，但要区分两种「不存在」：

    * 技能目录没了、它所在的技能根还在 —— 技能确实被删了，可以清编目与分配；
    * 连技能根（父目录）都不在了 —— 可能是数据卷 / 内核配置目录暂时没挂上，
      这一轮无法判断，按「没扫到」处理，只标失效、**不动分配**，免得一次
      扫描落空就把用户的分配全清掉。

    路径为空串（异常数据）同样不当作「已消失」，避免误删分配。
    """
    path = Path(row.path)
    if not path.name:
        return False
    return not path.is_dir() and path.parent.is_dir()


async def _purge_gone_skills(
    session: AsyncSession, gone: list[Skill]
) -> tuple[int, int, list[tuple[str | None, str]]]:
    """目录已从磁盘消失的技能：解除智能体分配并删除编目行。

    返回 ``(删除的行数, 解除的分配数, 待清理引用登记的用户技能)``。技能本体都没了，
    指向它的分配就是幽灵引用（运行时本来也会跳过），留着只会让智能体的技能列表和
    磁盘对不上。技能重新出现后重扫会重新登记，但分配要重新勾选 —— 这是技能消失后
    必然的结果。

    引用登记（``.mounts.json``）由调用方在事务提交后再摘：提交失败时不能留下
    「库里有行、登记已删」的错位；而且提交后的 ORM 行已被删除，不能再读属性。
    """
    if not gone:
        return 0, 0, []
    prunable = [(row.owner_id, row.slug) for row in gone if is_library_row(row)]
    ids = [row.id for row in gone]
    result = await session.execute(delete(AgentSkill).where(AgentSkill.skill_id.in_(ids)))
    unassigned = result.rowcount or 0
    for row in gone:
        await session.delete(row)
    return len(gone), unassigned, prunable


def _prune_stale_library_mounts(entries: list[tuple[str | None, str]]) -> None:
    """实体目录已消失的用户技能：把 ``.mounts.json`` 里的失效引用登记摘掉。

    留着就是「幽灵引用」——以后导入同名技能会被错误地挂到这些内核上。
    只处理调用方确认过「技能目录不存在、但技能根还在」的条目；技能库根目录本身
    不见了（数据卷没挂上）或归属被改坏时一律不动登记，免得把用户的引用清单清空。
    """
    prunable: dict[str | None, set[str]] = {}
    for owner_id, slug in entries:
        prunable.setdefault(owner_id, set()).add(slug)

    for owner_id, slugs in prunable.items():
        try:
            owner_dir = library_root(owner_id)
        except SkillError:
            # 库里的归属被改坏时跳过这一行，不能让整个编目失败（与 _library_owners 一致）。
            logger.warning("技能库归属不合法，跳过引用登记清理：%r", owner_id)
            continue
        if not owner_dir.is_dir():
            # 根目录都不在：可能是数据卷暂时不可用，不能据此清空引用登记。
            continue
        mounts = read_library_mounts(owner_dir)
        gone: list[str] = []
        for slug in sorted(slugs):
            try:
                if library_skill_dir(owner_id, slug).is_dir():
                    continue  # 目录还在：登记不能动
            except SkillError:
                continue
            if slug in mounts:
                gone.append(slug)
        if not gone:
            continue
        for slug in gone:
            mounts.pop(slug, None)
        try:
            write_library_mounts(owner_dir, mounts)
        except OSError as exc:
            logger.warning("清理技能库引用登记 %s 失败：%s", owner_dir, exc)
            continue
        logger.info("技能库 %s 已清理失效引用登记：%s", owner_dir, "、".join(gone))


async def list_rows(
    session: AsyncSession,
    *,
    type_id: str | None = None,
    source_kind: str | None = None,
    available: bool | None = None,
    query: str | None = None,
    owner_id: str | None = None,
    all_owners: bool = False,
) -> list[Skill]:
    """列出技能；``all_owners=False`` 时只返回共享技能 + 指定用户自己的技能。"""
    statement = select(Skill).order_by(Skill.type_id, Skill.source_kind, Skill.name)
    if not all_owners:
        if owner_id is None:
            statement = statement.where(Skill.owner_id.is_(None))
        else:
            statement = statement.where(
                or_(Skill.owner_id.is_(None), Skill.owner_id == owner_id)
            )
    if type_id:
        statement = statement.where(Skill.type_id == type_id)
    if source_kind:
        statement = statement.where(Skill.source_kind == source_kind)
    if available is not None:
        statement = statement.where(Skill.available == available)  # noqa: E712
    else:
        # 市场技能被卸载、插件技能随插件卸载后目录就没了，它们不该再出现在技能列表里
        # （用户看到的「已卸载」）。``available=False`` 现在只表示「目录还在、这一轮
        # 没扫到」（技能根暂时不可用 / 扫描失败），列表里显示为「未扫描到」，分配不受影响。
        statement = statement.where(
            or_(
                Skill.source_kind.not_in(HIDDEN_WHEN_MISSING),  # type: ignore[attr-defined]
                Skill.available == True,  # noqa: E712
            )
        )
    rows = (await session.execute(statement)).scalars().all()
    if query:
        needle = query.strip().lower()
        rows = [
            row
            for row in rows
            if needle in row.name.lower()
            or needle in row.slug.lower()
            or needle in (row.description or "").lower()
        ]
    return list(rows)


async def get_row(session: AsyncSession, skill_id: str) -> Skill | None:
    return await session.get(Skill, skill_id)


#: 由别人（内核或插件管理器）托管的来源：目录归它们所有，平台只能读、不能删。
#: ``plugin`` 是「已安装插件」里的技能 —— 单独删一个技能会让那个插件残缺，
#: 要卸载应该走插件管理器整包卸载。
READONLY_SOURCES = frozenset({"system", "plugin"})

#: 目录消失后就不该再出现在列表里的来源：它们跟着「卸载」动作整体消失，
#: 留一行「未扫描到」只会误导（也删不掉）。内核自带技能仍保留一行，方便排查。
HIDDEN_WHEN_MISSING = frozenset({MARKET_SOURCE, "plugin"})


def is_deletable(row: Skill) -> bool:
    """只有「目录归平台管」的技能允许从技能页删除。

    内核自带（``system``）与已安装插件（``plugin``）里的技能只读：前者删了内核就残缺，
    后者删了插件就残缺，而且重扫还会把它们加回来（用户会看到「删不掉」）。
    """
    return row.source_kind not in READONLY_SOURCES


async def usage_counts(
    session: AsyncSession, owner_id: str | None = None
) -> dict[str, int]:
    """每个技能被多少个智能体使用（已软删除的智能体不再算"使用中"）。

    ``owner_id`` 非空时只数该用户的智能体（成员看自己的用量，管理员传 ``None`` 看全局）。
    """
    statement = (
        select(AgentSkill.skill_id)
        .join(AgentProfile, AgentProfile.id == AgentSkill.agent_profile_id)
        .where(AgentProfile.deleted_at.is_(None))
    )
    if owner_id is not None:
        statement = statement.where(AgentProfile.owner_id == owner_id)
    counts: dict[str, int] = {}
    for skill_id in (await session.execute(statement)).scalars().all():
        counts[skill_id] = counts.get(skill_id, 0) + 1
    return counts


async def agents_using(
    session: AsyncSession, skill_id: str, owner_id: str | None = None
) -> list[AgentProfile]:
    statement = (
        select(AgentProfile)
        .join(AgentSkill, AgentSkill.agent_profile_id == AgentProfile.id)
        .where(AgentSkill.skill_id == skill_id, AgentProfile.deleted_at.is_(None))
        .order_by(AgentProfile.created_at)
    )
    if owner_id is not None:
        statement = statement.where(AgentProfile.owner_id == owner_id)
    return list((await session.execute(statement)).scalars().all())


def read_body(skill_path: str) -> tuple[str, bool]:
    """读取 SKILL.md 正文（供详情页展示）；返回 ``(正文, 是否被截断)``。

    ``skill_path`` 既接受技能目录（读目录下的 ``SKILL.md``），也接受 ``SKILL.md`` 文件本身。
    """
    base = Path(skill_path)
    target = base / SKILL_FILE if base.is_dir() or not base.suffix else base
    try:
        data = target.read_bytes()
    except OSError:
        return "", False
    truncated = len(data) > MAX_BODY_BYTES
    return data[:MAX_BODY_BYTES].decode("utf-8", "replace"), truncated


# --- 分配 -------------------------------------------------------------------


async def agent_skill_ids(session: AsyncSession, profile_id: str) -> list[str]:
    rows = (
        await session.execute(
            select(AgentSkill).where(AgentSkill.agent_profile_id == profile_id).order_by(AgentSkill.position)
        )
    ).scalars().all()
    return [row.skill_id for row in rows]


async def set_agent_skills(
    session: AsyncSession, profile: AgentProfile, skill_ids: list[str]
) -> list[str]:
    """整表替换智能体的技能分配；返回实际写入的 skill_id 列表。

    只接受「与智能体内核一致且可用」的技能：界面已经禁止选择不匹配的技能，
    这里是服务端兜底。
    """
    wanted = list(dict.fromkeys(skill_ids))
    for skill_id in wanted:
        row = await session.get(Skill, skill_id)
        if row is None:
            raise ValueError(f"技能不存在：{skill_id}")
        if row.type_id != profile.type_id:
            raise ValueError(
                f"技能「{row.name}」属于 {row.type_id}，"
                f"与该智能体的内核（{profile.type_id}）不匹配"
            )
        # 用户技能库按用户隔离：智能体只能分配「共享技能 + 自己主人的技能」。
        if row.owner_id is not None and row.owner_id != profile.owner_id:
            raise ValueError(f"技能「{row.name}」不在该智能体的技能库里，无法分配")

    for link in (
        await session.execute(select(AgentSkill).where(AgentSkill.agent_profile_id == profile.id))
    ).scalars().all():
        await session.delete(link)
    await session.flush()

    for position, skill_id in enumerate(wanted):
        session.add(AgentSkill(agent_profile_id=profile.id, skill_id=skill_id, position=position))
    await session.commit()
    return wanted


async def skills_for_agent(
    session: AsyncSession,
    profile: AgentProfile,
    *,
    override_ids: list[str] | None = None,
) -> list[SkillSpec]:
    """智能体在本轮运行中真正生效的技能（只取同内核且目录仍在的）。

    ``override_ids``：协作域的节点级分配（None = 跟随智能体页面的分配；
    给了列表就以它为准）。安全边界与默认路径完全一致：同内核、可用、归属校验。
    """
    ids = override_ids if override_ids is not None else await agent_skill_ids(session, profile.id)
    if not ids:
        return []
    rows = (await session.execute(select(Skill).where(Skill.id.in_(ids)))).scalars().all()
    by_id = {row.id: row for row in rows}
    specs: list[SkillSpec] = []
    for skill_id in ids:
        row = by_id.get(skill_id)
        if row is None or not row.available or row.type_id != profile.type_id:
            continue
        # 旧分配关系如果指向了别人的技能，运行时也必须挡住（按用户隔离）。
        if row.owner_id is not None and row.owner_id != profile.owner_id:
            continue
        specs.append(spec_from_row(row))
    return specs


def spec_from_row(row: Skill) -> SkillSpec:
    return SkillSpec(
        type_id=row.type_id,
        slug=row.slug,
        name=row.name,
        owner_id=row.owner_id,
        library=is_library_row(row),
        description=row.description or "",
        source_kind=row.source_kind,
        path=row.path,
        version=row.version,
        author=row.author,
        when_to_use=row.when_to_use,
        allowed_tools=list(row.allowed_tools or []),
        total_bytes=row.total_bytes or 0,
        fingerprint=row.fingerprint or "",
        note=row.note,
        source_slug=row.slug if row.source_kind == MARKET_SOURCE else None,
        source_url=(row.source_url or None),
    )


# --- 物化 -------------------------------------------------------------------


async def prepare_skill_home(
    session: AsyncSession,
    definition: AgentDefinition,
    profile: AgentProfile,
    conversation: Any,
    ctx: AgentContext,
    *,
    scope_id: str | None = None,
    override_ids: list[str] | None = None,
) -> list[str]:
    """把智能体分配到的技能物化进该内核的隔离目录，并把结果写回 ``ctx``。

    ``scope_id`` 是隔离目录的作用域（默认用对话 id；协作运行会传自己的作用域）。
    传了 ``scope_id`` 时 ``conversation`` 可以为 ``None``（协作运行没有对话）。
    ``override_ids`` 是协作域的节点级技能分配（None = 跟随智能体页面）。
    返回需要提示给用户的说明/告警（由运行层作为 ``log`` 事件暴露）。
    """
    ctx.skills = await skills_for_agent(session, profile, override_ids=override_ids)
    mount = definition.skill_mount(ctx)
    if mount is None:
        return []
    # 技能与 MCP 共用同一个「家」：MCP 物化（prepare_mcp_home）会写进同一个目录，
    # 所以这里定下的 home_name 就是内核的隔离配置目录名。
    home = home_for(scope_id or conversation.id, mount.home_name)
    if ctx.home_dir and Path(ctx.home_dir) != home:
        # 上一轮（或另一段物化）已经定过目录：以它为准，避免技能与 MCP 各写一个家。
        home = Path(ctx.home_dir)
    first_run = not home.exists()
    warnings = materialize(home, mount, ctx.skills)
    ctx.home_dir = str(home)
    warnings += note_fresh_home(ctx, first_run=first_run)
    return [*mount.notes, *warnings]


def materialize(home: Path, mount: SkillMount, specs: Iterable[SkillSpec]) -> list[str]:
    """按物化计划把技能目录与内核配置写进隔离目录；返回告警列表。

    技能集合与源内容都没变化时走**快速路径**：直接复用上一轮物化出的技能目录，
    跳过整棵重建（见 :data:`SKILLS_MANIFEST_FILE`）。取消分配、技能更新、
    清单损坏都会让比对失败，回到全量重建——「取消分配立即消失」的强一致语义不变。
    ``carry`` / ``patch`` 不走快速路径：内核配置文件可能在两轮之间被用户改动，每轮照做。
    """
    warnings = ensure_home(home)
    if warnings:
        return warnings
    specs = list(specs)

    if _reusable_skill_tree(home, mount, specs):
        for source_path, dest_rel in mount.carry:
            _carry_file(home, source_path, dest_rel, warnings)
        for dest_rel, patch in mount.patch.items():
            _patch_json(home / dest_rel, patch, warnings)
        return warnings

    skills_root = home / mount.skills_subdir
    # 每次重建受管技能目录：取消分配后技能必须立刻消失。
    had_stale_root = skills_root.exists()
    if had_stale_root:
        shutil.rmtree(skills_root, ignore_errors=True)
    try:
        skills_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [f"无法创建技能目录 {skills_root}：{exc}"]

    written: dict[str, dict[str, Any]] = {}
    for spec in specs:
        name = safe_skill_dirname(spec.slug)
        source = Path(spec.path) if spec.path else None
        if name is None:
            warnings.append(f"技能「{spec.name}」的目录名不安全，已跳过")
            continue
        if source is None or not source.is_dir():
            warnings.append(f"技能「{spec.name}」的源目录已不存在：{spec.path}")
            continue
        if spec.total_bytes > MAX_SKILL_BYTES:
            warnings.append(
                f"技能「{spec.name}」约 {spec.total_bytes // 1024} KB，超过单技能上限，已跳过"
            )
            continue
        # 签名必须在复制**之前**采集：复制与统计之间源目录若被改动，
        # 清单里会是新签名而副本是旧内容，下一轮就会误复用旧副本。
        signature = _source_signature(source)
        target = skills_root / name
        try:
            shutil.copytree(source, target, symlinks=False, dirs_exist_ok=True)
        except OSError as exc:
            warnings.append(f"技能「{spec.name}」复制失败：{exc}")
            continue
        # 复制成功且签名拿得到才登记进清单；拿不到（stat 失败）就空着这一条，
        # 下一轮比对必然失败、走全量重建，兜底永远安全。
        if signature is not None:
            written[name] = {"source": str(source), "files": signature}
        # 只在「名字被改过」时同步 frontmatter：加了命名空间的技能 slug 与源目录名不同，
        # 其余情况（市场技能常见的 `目录 kdocs-skill / name: kdocs`）必须原样保留。
        if name != source.name:
            _sync_frontmatter_name(target / SKILL_FILE, name, warnings)

    # 旧目录删不干净（权限等）时残留文件会与新一轮集合混杂，此时不写清单：
    # 宁可下轮再做一次全量重建，也不能把「脏目录」标记成可复用。
    if not _tree_is_clean(skills_root, set(written)):
        logger.warning("技能目录 %s 重建后仍残留旧内容，跳过物化清单写入", skills_root)
    else:
        _write_skills_manifest(home, mount, written)

    for source_path, dest_rel in mount.carry:
        _carry_file(home, source_path, dest_rel, warnings)

    for dest_rel, patch in mount.patch.items():
        _patch_json(home / dest_rel, patch, warnings)

    return warnings


#: 隔离家目录里的「物化清单」：记录上一轮成功物化的技能及其源文件签名。
#: 下一轮物化先比对它：集合与源都没变化时跳过重建，直接复用上一轮的结果。
SKILLS_MANIFEST_FILE = ".skills-manifest.json"
_SKILLS_MANIFEST_VERSION = 1


def _source_signature(source: Path) -> dict[str, list[int]] | None:
    """收集技能源目录的文件签名（相对路径 → ``[大小, mtime_ns]``）。

    与 :func:`shutil.copytree` 的复制范围保持一致（不跳目录、符号链接按其指向统计）；
    任何一个文件 stat 失败都返回 ``None``，调用方按「有变化」处理，走全量重建。
    比对逐文件的 (大小, mtime)——与 rsync 的默认判定同款：内容原地改写即使
    大小不变，mtime 也会变，市场更新（整目录替换）更是必然触发重建。
    """
    signature: dict[str, list[int]] = {}
    visited: set[str] = set()
    try:
        base = source.resolve()
    except OSError:
        return None
    for dirpath, dirnames, filenames in os.walk(source, followlinks=True):
        current = Path(dirpath)
        try:
            real = current.resolve()
        except OSError:
            return None
        if real in visited:  # 符号链接成环时停止递归
            dirnames[:] = []
            continue
        visited.add(real)
        dirnames.sort()
        for filename in sorted(filenames):
            path = current / filename
            try:
                stat = path.stat()
                signature[path.relative_to(source).as_posix()] = [stat.st_size, stat.st_mtime_ns]
            except OSError:
                return None
    return signature


def _read_skills_manifest(home: Path) -> dict[str, Any] | None:
    """读物化清单；文件不存在 / 损坏 / 结构不对都返回 ``None``（当「变了」处理）。"""
    try:
        data = json.loads((home / SKILLS_MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != _SKILLS_MANIFEST_VERSION:
        return None
    if not isinstance(data.get("entries"), dict) or not isinstance(data.get("skills_subdir"), str):
        return None
    return data


def _write_skills_manifest(home: Path, mount: SkillMount, written: dict[str, dict[str, Any]]) -> None:
    """原子写入物化清单（先写临时文件再替换）。

    写失败只留日志不打扰用户：清单是纯性能优化，坏掉的清单在下一轮会按
    「有变化」处理，走全量重建，行为正确只是慢一点。
    """
    payload = json.dumps(
        {
            "version": _SKILLS_MANIFEST_VERSION,
            "skills_subdir": mount.skills_subdir,
            "entries": written,
        },
        ensure_ascii=False,
        indent=2,
    )
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{SKILLS_MANIFEST_FILE}.", dir=home)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, home / SKILLS_MANIFEST_FILE)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning("写入物化清单 %s 失败：%s", home / SKILLS_MANIFEST_FILE, exc)


def _expected_manifest_entries(specs: list[SkillSpec]) -> dict[str, dict[str, Any]] | None:
    """按本轮要物化的技能算出「期望清单条目」；有任何问题技能时返回 ``None``。

    目录名不安全 / 源目录不存在 / 超过体积上限的技能必须走全量重建——
    快速路径不会产出它们的告警，而用户每轮都该看到。
    """
    expected: dict[str, dict[str, Any]] = {}
    for spec in specs:
        name = safe_skill_dirname(spec.slug)
        source = Path(spec.path) if spec.path else None
        if name is None or source is None or not source.is_dir():
            return None
        if spec.total_bytes > MAX_SKILL_BYTES:
            return None
        signature = _source_signature(source)
        if signature is None:
            return None
        # 同名技能在慢路径里后写覆盖（copytree dirs_exist_ok），期望条目保持同样语义。
        expected[name] = {"source": str(source), "files": signature}
    return expected


def _reusable_skill_tree(home: Path, mount: SkillMount, specs: list[SkillSpec]) -> bool:
    """上一轮物化出的技能目录能否直接复用（集合、源内容、磁盘状态三重一致）。"""
    manifest = _read_skills_manifest(home)
    if manifest is None:
        return False
    if manifest["skills_subdir"] != mount.skills_subdir:
        return False
    expected = _expected_manifest_entries(specs)
    if expected is None or expected != manifest["entries"]:
        return False
    # 磁盘侧兜底：上一轮写入的技能目录都还在（防外部删除 / 半途而废的清理）。
    skills_root = home / mount.skills_subdir
    if not skills_root.is_dir():
        return False
    return all((skills_root / name).is_dir() for name in expected)


def _tree_is_clean(skills_root: Path, written: set[str]) -> bool:
    """重建后的技能目录是否恰好只含刚写入的技能（没有 rmtree 没删掉的残留）。

    注意允许出现文件（例如 Codex 内核启动后自己写入 ``skills/.system``）——
    判定只关心「顶层子目录 ⊆ 本轮写入的技能名」：多出来的目录意味着 rmtree
    没删干净，新旧技能会混在一起。``written`` 为空时目录必须没有任何子目录。
    """
    try:
        names = {child.name for child in skills_root.iterdir() if child.is_dir()}
    except OSError:
        return False
    return names <= written


def _sync_frontmatter_name(path: Path, name: str, warnings: list[str]) -> None:
    """让物化副本的 frontmatter ``name`` 与目录名一致（尽力而为）。

    内核之间认技能的方式不同：Claude Code 按**目录名**调用，Hermes 按 frontmatter
    的 ``name`` 查找。加了命名空间的技能目录叫 ``telegram-access``，若文件里仍写着
    ``name: access``，Hermes 就找不到它、Claude Code 也会报「名字对不上」。

    只改物化出来的**副本**，源目录始终只读。改写失败不影响复制结果（技能照旧可用），
    但会在告警里说明，避免「明明分配了却调不到」。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return
    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None:
        return
    for index in range(1, closing):
        if not lines[index].startswith("name:"):
            continue
        current = lines[index].split(":", 1)[1].strip().strip("\"'")
        if current == name:
            return
        lines[index] = f"name: {name}\n"
        try:
            path.write_text("".join(lines), encoding="utf-8")
        except OSError as exc:
            warnings.append(f"技能「{name}」的 SKILL.md 改名失败：{exc}")
        return


def _carry_file(home: Path, source_path: str, dest_rel: str, warnings: list[str]) -> None:
    target_rel = _safe_relative(dest_rel)
    if target_rel is None:
        warnings.append(f"内核配置的携带路径不安全，已跳过：{dest_rel}")
        return
    source = Path(source_path)
    if not source.is_file():
        return
    target = home / target_rel
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
    except OSError as exc:
        warnings.append(f"携带内核配置 {dest_rel} 失败：{exc}")


def _patch_json(path: Path, patch: dict[str, Any], warnings: list[str]) -> None:
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            warnings.append(f"内核配置 {path.name} 无法解析，已按空配置重建")
            data = {}
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            merged = dict(data[key])
            merged.update(value)
            data[key] = merged
        else:
            data[key] = value
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as exc:
        warnings.append(f"写入内核配置 {path.name} 失败：{exc}")


#: 兼容旧调用点：路径安全校验的实现已挪到 :mod:`runtime.isolation`（技能与 MCP 共用一份）。
_safe_relative = safe_relative


def cleanup_conversation(conversation_id: str) -> None:
    """删除对话时清掉它的隔离配置目录（尽力而为）。"""
    home = get_settings().agent_homes_root / conversation_id
    shutil.rmtree(home, ignore_errors=True)


# --- 技能市场（安装 / 卸载）--------------------------------------------------


class SkillError(Exception):
    """技能操作失败：消息面向用户。"""


class SkillNotFound(SkillError):
    """技能不存在（API 层据此映射 404，不靠中文字符串匹配）。"""


def skill_roots_for_type(type_id: str) -> list[SkillRoot]:
    """某个内核声明的技能根；未知内核或该内核不支持技能时抛 :class:`SkillError`。"""
    try:
        definition = get_agent(type_id)
    except KeyError:
        raise SkillError(f"未知的智能体类型：{type_id}") from None
    roots = definition.skill_roots()
    if not roots:
        raise SkillError(f"{definition.name} 不支持技能：没有声明技能目录")
    return roots


def _is_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _delete_tree(target: Path, name: str) -> None:
    """删掉一个技能目录；失败要显式暴露。

    不能 ``ignore_errors=True``：删到一半失败时接口会回「已删除」，但目录还在，
    下一次重扫又把它加回编目 —— 用户会看到技能删不掉却没有任何提示。
    """
    try:
        shutil.rmtree(target)
    except FileNotFoundError:
        # 检查与删除之间目录被别的进程删掉了：结果与目标一致，视为成功。
        return
    except OSError as exc:
        raise SkillError(f"删除「{name}」的目录失败：{exc}（请检查文件权限后重试）") from exc


def _default_mounts() -> list[str]:
    """没有引用登记的技能默认挂到哪些内核：所有声明了技能根的内核。"""
    return sorted(_skill_capable_type_ids())


def mounts_for_row(
    row: Skill, *, mounts: dict[str, list[str]] | None = None
) -> list[str]:
    """这一行对应的技能实体被哪些内核引用（未登记时回落到默认值）。

    ``mounts`` 可传入**已经读好的**引用登记（同一 owner 批量描述多行时只读一次文件），
    不传则现读。
    """
    if mounts is None:
        mounts = read_library_mounts(library_root(row.owner_id))
    kernels = mounts.get(row.slug)
    return list(kernels) if kernels else _default_mounts()


def _set_mounts(row: Skill, kernels: Iterable[str]) -> list[str]:
    """把技能实体的内核引用改成 ``kernels``；空列表表示没人用了（删引用条目）。"""
    owner_dir = library_root(row.owner_id)
    mounts = read_library_mounts(owner_dir)
    remaining = list(dict.fromkeys(kernels))
    if remaining:
        mounts[row.slug] = remaining
    else:
        mounts.pop(row.slug, None)
    write_library_mounts(owner_dir, mounts)
    return remaining


def _unmount_library_skill(row: Skill) -> list[str]:
    """从库里摘掉「这一行所属内核」的引用，返回仍引用它的内核。

    只有最后一个引用消失时才删实体目录——先摘引用的语义保证另一个内核的技能继续可用。
    """
    target = Path(row.path)
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise SkillError(f"「{row.name}」的路径不是一个技能目录，拒绝删除")
        if not (target / SKILL_FILE).is_file():
            raise SkillError(f"「{row.name}」目录内没有 {SKILL_FILE}，拒绝删除")
        if not _is_declared_skill_dir(row, target):
            raise SkillError(f"「{row.name}」不在它所属的技能目录内，拒绝删除")
    remaining = [kernel for kernel in mounts_for_row(row) if kernel != row.type_id]
    remaining = _set_mounts(row, remaining)
    if not remaining:
        _delete_tree(target, row.name)
    return remaining


def _remove_installed_skill(type_id: str, row: Skill) -> list[str]:
    """卸载一个市场技能：只摘「本内核」的引用，越界或非市场技能一律拒绝。

    判据：目录里有市场安装标记（``.skillhub.json``），且目录落在当前用户技能库内。
    技能实体被多个内核共享，所以只有最后一个引用消失时才真的删目录；
    返回值是仍引用它的内核 id 列表（空 = 实体已删除）。
    """
    if row.source_kind != MARKET_SOURCE:
        raise SkillError(f"「{row.name}」不是市场技能，不能卸载")
    target = Path(row.path)
    if not target.is_dir():
        if not is_library_row(row):
            # 非库技能（例如升级前装进内核原生目录的老市场技能）没有引用登记，
            # 不能拿 mounts_for_row() 的默认值去写 `.mounts.json` —— 那会留下一条
            # 「幽灵引用」，让以后同名技能的导入被错误地挂到一堆内核上。
            return []
        # 目录已经不见了：只摘掉本内核的引用，别把其它内核的引用也一起抹掉。
        remaining = [kernel for kernel in mounts_for_row(row) if kernel != row.type_id]
        return _set_mounts(row, remaining)
    if read_market_marker(target) is None:
        raise SkillError(
            f"「{row.name}」缺少市场安装标记，拒绝删除（可能不是平台安装的技能）"
        )
    if is_library_row(row):
        return _unmount_library_skill(row)
    # 兼容升级前装进内核原生目录的老市场技能：仍按适配器声明的可写根校验。
    allowed = any(
        root.installable and _is_under(Path(root.path), target)
        for root in skill_roots_for_type(type_id)
    )
    if not allowed:
        raise SkillError(f"「{row.name}」不在可写入的技能目录内，拒绝删除")
    _delete_tree(target, row.name)
    return []


def _remove_skill_directory(row: Skill) -> None:
    """删掉技能自己的源目录；只允许删该技能所属技能根 / 用户技能库之下的目录。

    这是技能页「删除」的磁盘侧护栏：数据库里的路径即便被改坏，也只能删到允许的目录内部，
    并且目标必须真的像一个技能目录（含 ``SKILL.md``、不是符号链接、不是技能根本身）。
    """
    target = Path(row.path)
    if not target.exists():
        return
    if target.is_symlink() or not target.is_dir():
        raise SkillError(f"「{row.name}」的路径不是一个技能目录，拒绝删除")
    if not (target / SKILL_FILE).is_file():
        raise SkillError(f"「{row.name}」目录内没有 {SKILL_FILE}，拒绝删除")
    if not _is_declared_skill_dir(row, target):
        raise SkillError(f"「{row.name}」不在它所属的技能目录内，拒绝删除")
    _delete_tree(target, row.name)


async def _drop_skill_rows(session: AsyncSession, rows: Iterable[Skill]) -> None:
    """断开这些行的智能体分配并删除编目行，然后重扫（目录已删时不会加回来）。"""
    rows = list(rows)
    ids = [row.id for row in rows]
    if ids:
        await session.execute(delete(AgentSkill).where(AgentSkill.skill_id.in_(ids)))
    for row in rows:
        await session.delete(row)
    await session.commit()
    await rescan(session)


async def _drop_skill_row(session: AsyncSession, row: Skill) -> None:
    await _drop_skill_rows(session, [row])


async def _library_siblings(session: AsyncSession, row: Skill) -> list[Skill]:
    """同一份技能实体在其它内核下的编目行（同一个 owner + slug）。"""
    owner_filter = (
        Skill.owner_id.is_(None) if row.owner_id is None else Skill.owner_id == row.owner_id
    )
    statement = select(Skill).where(
        Skill.slug == row.slug,
        owner_filter,
        Skill.id.like(f"%{LIBRARY_ID_MARK}%"),
    )
    return list((await session.execute(statement)).scalars().all())


async def delete_skill(session: AsyncSession, skill_id: str) -> dict[str, Any]:
    """删除一个由平台管理的技能（个人 / 项目 / 市场…）。

    用户技能库里的一份技能可能被多个内核引用：删除 = 先摘掉本内核的引用；
    还有别的内核在用时保留实体目录，最后一个引用消失才删目录与全部编目行。
    返回 ``{"name", "remaining_kernels"}``。
    """
    row = await get_row(session, skill_id)
    if row is None:
        raise SkillNotFound(f"技能不存在：{skill_id}")
    if not is_deletable(row):
        if row.source_kind == "plugin":
            raise SkillError(
                f"「{row.name}」属于已安装的插件，不能单独删除；请在插件管理器里卸载整个插件"
            )
        raise SkillError(f"「{row.name}」是内核自带技能，不能删除")
    name = row.name
    if is_library_row(row):
        remaining = _unmount_library_skill(row)
        if remaining:
            await _drop_skill_row(session, row)
            return {"name": name, "remaining_kernels": remaining}
        await _drop_skill_rows(session, await _library_siblings(session, row))
        return {"name": name, "remaining_kernels": []}
    _remove_skill_directory(row)
    await _drop_skill_row(session, row)
    return {"name": name, "remaining_kernels": []}


async def wait_for_market_registration(
    session: AsyncSession, skill_id: str, *, attempts: int = 3, interval: float = 0.4
) -> Skill | None:
    """安装落盘后等编目把它登记进 ``skills`` 表（文件系统可见性有轻微延迟）。"""
    for _ in range(max(1, attempts)):
        row = await get_row(session, skill_id)
        if row is not None:
            return row
        await asyncio.sleep(interval)
    return await get_row(session, skill_id)


async def install_market_skill(
    session: AsyncSession,
    *,
    type_id: str,
    slug: str,
    version: str | None,
    archive: bytes,
    owner_id: str | None = None,
    source_url: str | None = None,
    display_slug: str | None = None,
) -> dict[str, Any]:
    """把一个市场技能的 zip 落盘到**当前用户**的技能库，并挂载到指定内核。

    技能实体按 slug 只存一份：如果它已经存在（装到过别的内核），这里就是**更新**，
    覆盖唯一实体后所有已挂载的内核同时生效；否则解压一份新实体。
    步骤：``<技能库>/<slug>`` 落盘 → 市场标记 → 引用登记里加上本次内核 → 重扫。
    返回 ``{"skill_id", "name", "warnings"}``；失败抛 :class:`SkillError`。
    """
    safe = safe_skill_dirname(slug)
    if safe is None:
        raise SkillError(f"技能标识不合法：{slug}")
    skill_roots_for_type(type_id)  # 未知内核 / 不支持技能的内核先挡住
    root = library_root(owner_id)
    target = root / safe
    if target.exists() and read_market_marker(target) is None:
        # 同 slug 的个人技能在库里：不能悄悄覆盖用户自己的东西。
        raise SkillError(
            f"技能库里已有同名技能「{safe}」（不是市场技能），请先删除或改名后再安装"
        )
    new_id = library_skill_id(type_id, safe, owner_id)
    if await _same_slug_conflict(
        session, type_id=type_id, slug=safe, owner_id=owner_id, new_id=new_id
    ):
        # 目标内核里已有同名共享技能：物化到隔离目录会撞名，先挡住。
        raise _name_taken(type_id, safe, "安装")
    try:
        root.mkdir(parents=True, exist_ok=True)
        extract_skill_archive(archive, target)
    except OSError as exc:
        raise SkillError(f"安装失败：无法写入技能目录（{exc}）") from None

    write_market_marker(
        target,
        MarketInstallInfo(
            slug=display_slug or safe,
            version=version,
            source_url=source_url,
            installed_at=datetime.now(timezone.utc).isoformat(),
        ),
    )
    # 保留原来挂载的内核（更新时它们也要生效），再加上本次目标内核。
    mounts = read_library_mounts(root)
    kernels = mounts.get(safe) or []
    if type_id not in kernels:
        kernels.append(type_id)
    mounts[safe] = kernels
    write_library_mounts(root, mounts)

    await rescan(session)
    skill_id = library_skill_id(type_id, safe, owner_id)
    row = await wait_for_market_registration(session, skill_id)
    if row is None:
        # 目录已落盘，但编目还没收录（通常是该目录被别的技能根重复覆盖）——给出可操作提示。
        raise SkillError(
            f"技能已解压到 {target}，但编目未收录；"
            "请到「技能」页点一次「重新扫描」"
        )
    return {"skill_id": row.id, "name": row.name, "warnings": []}


async def uninstall_market_skill(
    session: AsyncSession, skill_id: str
) -> dict[str, Any]:
    """卸载一个市场技能：摘掉本内核引用、断开分配；最后一个引用消失才删实体目录。

    返回 ``{"name", "remaining_kernels"}``：``remaining_kernels`` 非空表示技能仍被
    其它内核共享（实体保留），调用方可以据此给出更准确的提示。
    """
    row = await get_row(session, skill_id)
    if row is None:
        raise SkillNotFound(f"技能不存在：{skill_id}")
    name = row.name
    remaining = _remove_installed_skill(row.type_id, row)
    if remaining:
        await _drop_skill_row(session, row)
    else:
        # 卸载 = 从平台移除：先断开「智能体 → 技能」的分配（外键是 RESTRICT），
        # 再删编目行。否则技能会以「目录已消失」的形式继续留在技能页与市场已装列表里。
        rows = await _library_siblings(session, row) if is_library_row(row) else [row]
        await _drop_skill_rows(session, rows)
    return {"name": name, "remaining_kernels": remaining}


# --- 导出 / 导入 / 挂载到其它内核 -------------------------------------------------
#
# 三个动作都只动「技能目录 / 引用登记」，不碰智能体分配：
# * 导出是把源目录打成 zip（只读）；
# * 导入是把一个 zip 解压进「当前用户的技能库」，并挂载到指定内核；
# * 复制是把同一个技能实体「挂载」到当前用户的另一个内核（不再复制文件）。
# 导入 / 复制后的新技能仍要「智能体分配」才在运行时生效（与市场安装同一套规则）。

#: 导入 / 复制时单个技能允许的体积上限（与技能市场下载对齐）
MAX_TRANSFER_BYTES = MAX_ARCHIVE_BYTES


def _ensure_skill_capable(type_id: str) -> None:
    """未知内核 / 不支持技能的内核直接报错。"""
    skill_roots_for_type(type_id)


async def _same_slug_conflict(
    session: AsyncSession,
    *,
    type_id: str,
    slug: str,
    owner_id: str | None,
    new_id: str,
) -> bool:
    """导入 / 复制前检查「同内核 + 同 slug」是否已被共享技能或自己的技能占用。

    占用时拒绝：同一个内核下两份同名技能，物化到隔离目录会互相覆盖，调用名也会撞车。
    """
    statement = select(Skill.id).where(
        Skill.type_id == type_id,
        Skill.slug == slug,
        Skill.available == True,  # noqa: E712
    )
    if owner_id is None:
        statement = statement.where(Skill.owner_id.is_(None))
    else:
        statement = statement.where(
            or_(Skill.owner_id.is_(None), Skill.owner_id == owner_id)
        )
    return any(skill_id != new_id for (skill_id,) in (await session.execute(statement)).all())


def _type_label(type_id: str) -> str:
    try:
        return get_agent(type_id).name
    except KeyError:
        return type_id


def _name_taken(type_id: str, slug: str, action: str) -> SkillError:
    """同名技能的报错（导入 / 复制共用，避免两边文案走样）。

    技能库按 slug 只存一份，所以冲突可能来自别的内核；提示里给出「删除 / 换名」两条路。
    """
    return SkillError(
        f"{_type_label(type_id)} 的技能库里已有同名技能「{slug}」，"
        f"请先删除同名技能或换个目录名后再{action}"
    )


def _safe_slug(text: str) -> str | None:
    """尽量把一段文字变成安全的技能目录名；中文等无法直接使用时返回 ``None``。"""
    raw = "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch in "._-")) else "-" for ch in (text or "")
    )
    raw = re.sub(r"-{2,}", "-", raw).strip("-._")
    return safe_skill_dirname(raw)


def _is_declared_skill_dir(row: Skill, target: Path) -> bool:
    """``target`` 是否是该技能允许被操作的目录（技能根内部，且不是根本身）。

    * 用户技能库里的技能只能落在「该用户」的库目录内部（一份实体被多个内核引用）；
    * 共享技能只能落在适配器声明过的技能根内部。

    导出 / 复制 / 删除前用它挡掉「数据库里的路径被改坏」的情况。
    """
    if is_library_row(row):
        bases = [library_root(row.owner_id)]
    else:
        bases = [Path(root.path) for root in skill_roots_for_type(row.type_id)]
    for base in bases:
        try:
            if target.resolve() == base.resolve():
                continue
        except OSError:
            continue
        if _is_under(base, target):
            return True
    return False


def _archive_top_dir(archive: bytes) -> str | None:
    """压缩包是否「恰好一层顶层目录」；是则返回该目录名，否则 ``None``。"""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            tops: set[str] = set()
            nested = False
            for info in zf.infolist():
                if info.is_dir():
                    continue
                parts = [part for part in Path(info.filename).parts if part not in ("", ".")]
                if not parts:
                    continue
                tops.add(parts[0])
                if len(parts) > 1:
                    nested = True
    except (zipfile.BadZipFile, OSError):
        return None
    if len(tops) == 1 and nested:
        return next(iter(tops))
    return None


def export_skill_archive(row: Skill) -> bytes:
    """把一个技能的源目录打包成 zip（顶层目录名用技能 slug）。

    只读导出：不跟随符号链接、不改源目录；体积超过上限直接报错，
    免得生成一个自己都导不回来的压缩包。
    """
    base = Path(row.path)
    if not base.is_dir():
        raise SkillError(f"「{row.name}」的目录已不存在，无法导出")
    if not (base / SKILL_FILE).is_file():
        raise SkillError(f"「{row.name}」目录内没有 {SKILL_FILE}，无法导出")
    # 与删除同一道护栏：数据库里的路径即便被改坏，也只能碰内核声明过的技能根内部。
    if not _is_declared_skill_dir(row, base):
        raise SkillError(f"「{row.name}」不在它所属的技能目录内，拒绝导出")
    top = safe_skill_dirname(row.slug) or "skill"
    buffer = io.BytesIO()
    total = 0
    try:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                current = Path(dirpath)
                dirnames[:] = [
                    name
                    for name in sorted(dirnames)
                    if name not in SKIP_DIRS and not (current / name).is_symlink()
                ]
                for filename in sorted(filenames):
                    path = current / filename
                    if path.is_symlink() or not path.is_file():
                        continue
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    total += size
                    if total > MAX_TRANSFER_BYTES:
                        raise SkillError(
                            f"「{row.name}」超过单个技能 {MAX_TRANSFER_BYTES // 1024 // 1024} MB 的导出上限"
                        )
                    zf.write(path, arcname=f"{top}/{path.relative_to(base).as_posix()}")
    except OSError as exc:
        raise SkillError(f"打包「{row.name}」失败：{exc}") from exc
    return buffer.getvalue()


def _find_extracted_skill_dir(payload: Path) -> Path:
    """在解压结果里找到技能目录：根目录本身就是技能，或恰好一个子目录是技能。"""
    if (payload / SKILL_FILE).is_file():
        return payload
    candidates = [
        child
        for child in sorted(payload.iterdir())
        if child.is_dir() and (child / SKILL_FILE).is_file()
    ]
    if not candidates:
        raise SkillError(f"压缩包里没有 {SKILL_FILE}，不是可导入的技能包")
    if len(candidates) > 1:
        raise SkillError("压缩包里有多份技能（多个 SKILL.md），请一次只导入一个")
    return candidates[0]


async def import_skill_archive(
    session: AsyncSession,
    *,
    type_id: str,
    archive: bytes,
    owner_id: str | None = None,
    filename: str | None = None,
) -> dict[str, str]:
    """把一个技能压缩包导入**当前用户**的技能库，并挂载到指定内核。

    返回 ``{"skill_id", "name", "slug", "type_id"}``；失败抛 :class:`SkillError`。
    同名技能不会覆盖（避免悄悄改掉正在被调用的技能）：先删掉旧的再导入。
    """
    if not archive:
        raise SkillError("压缩包内容为空")
    if len(archive) > MAX_TRANSFER_BYTES:
        raise SkillError(
            f"压缩包超过 {MAX_TRANSFER_BYTES // 1024 // 1024} MB，已拒绝导入"
        )
    _ensure_skill_capable(type_id)
    base = library_root(owner_id)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SkillError(f"无法创建技能目录 {base}：{exc}") from None

    staging = Path(tempfile.mkdtemp(prefix="coda-skill-import-"))
    try:
        payload = staging / "payload"
        try:
            extract_skill_archive(archive, payload)
        except SkillHubError as exc:
            raise SkillError(f"导入失败：{exc}") from None
        skill_dir = _find_extracted_skill_dir(payload)

        # 目录名优先级：压缩包里的顶层技能目录名 → 上传文件名 → 兜底名。
        # 目录名就是内核调用技能时用的名字，不能带路径分隔符或非 ASCII 字符。
        # 解压时单层顶层目录已被剥掉，所以这里优先看压缩包原始结构。
        slug = (
            _safe_slug(_archive_top_dir(archive) or "")
            or _safe_slug(skill_dir.name if skill_dir != payload else "")
            or _safe_slug(Path(filename or "").stem)
            or "imported-skill"
        )
        target = base / slug
        new_id = library_skill_id(type_id, slug, owner_id)
        if target.exists() or await _same_slug_conflict(
            session, type_id=type_id, slug=slug, owner_id=owner_id, new_id=new_id
        ):
            raise _name_taken(type_id, slug, "导入")
        # 导入的是「用户技能」：市场安装标记不应跟着搬过来，否则会被误认成市场安装。
        (skill_dir / MARKET_MARKER_FILE).unlink(missing_ok=True)
        try:
            # copy 而不是 move：copytree 在目标已存在时直接报错，不会把技能塞进别人的目录里。
            shutil.copytree(skill_dir, target, symlinks=False)
        except FileExistsError:
            raise _name_taken(type_id, slug, "导入") from None
        except OSError as exc:
            shutil.rmtree(target, ignore_errors=True)
            raise SkillError(f"导入失败：无法写入技能目录（{exc}）") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # 导入 = 把这一份实体挂到指定内核（不默认铺到所有内核）。
    mounts = read_library_mounts(base)
    mounts[slug] = list(dict.fromkeys([*mounts.get(slug, []), type_id]))
    write_library_mounts(base, mounts)

    await rescan(session)
    row = await wait_for_market_registration(session, new_id)
    if row is None:
        raise SkillError(
            f"技能已导入到 {target}，但编目未收录；请到「技能」页点一次「重新扫描」"
        )
    return {"skill_id": row.id, "name": row.name, "slug": row.slug, "type_id": type_id}


def _ignore_copy_symlinks(directory: str, names: list[str]) -> set[str]:
    """复制技能时跳过符号链接与市场安装标记（不把链接指向的外部文件带进目标内核）。"""
    base = Path(directory)
    ignored = {name for name in names if (base / name).is_symlink()}
    if MARKET_MARKER_FILE in names:
        ignored.add(MARKET_MARKER_FILE)
    return ignored


async def copy_skill_to_kernel(
    session: AsyncSession, *, skill_id: str, type_id: str, owner_id: str | None = None
) -> dict[str, str]:
    """把一份技能**挂载**到当前用户的另一个内核（技能库按 slug 只存一份）。

    * 源已经是当前用户技能库里的实体：只登记引用，不复制文件；
    * 源是共享技能（内核自带 / 宿主机）：第一次复制时把文件收进技能库，之后同样只登记引用；
    * 不复制智能体分配；目标内核里已有同 slug 的共享技能时拒绝（物化会撞名）。

    这样市场更新覆盖唯一实体后，挂载到它的所有内核同时生效。
    """
    row = await get_row(session, skill_id)
    if row is None:
        raise SkillNotFound(f"技能不存在：{skill_id}")
    if row.type_id == type_id:
        raise SkillError("目标内核与技能所属内核相同，无需复制")
    _ensure_skill_capable(type_id)
    base = library_root(owner_id)
    source = Path(row.path)
    if not source.is_dir() or not (source / SKILL_FILE).is_file():
        raise SkillError(f"「{row.name}」的源目录已不存在，无法复制")
    if not _is_declared_skill_dir(row, source):
        raise SkillError(f"「{row.name}」不在它所属的技能目录内，拒绝复制")
    slug = safe_skill_dirname(row.slug)
    if slug is None:
        raise SkillError(f"「{row.name}」的目录名不安全，无法复制")
    target = base / slug
    new_id = library_skill_id(type_id, slug, owner_id)
    if await _same_slug_conflict(
        session, type_id=type_id, slug=slug, owner_id=owner_id, new_id=new_id
    ):
        raise _name_taken(type_id, slug, "复制")

    # 源已经是当前用户技能库里的实体：同一份文件，只登记引用。
    already_ours = is_library_row(row) and _is_under(base, source)
    if not already_ours:
        if target.exists():
            # 技能库按 slug 只存一份：同名实体已经在了。只有内容与源一致才视为
            # 「之前复制过」并直接登记引用；内容不同说明是用户自己的同名技能，
            # 不能悄悄把它当成源技能的挂载点（否则接口回成功、用户却用错技能）。
            if not (target / SKILL_FILE).is_file():
                raise _name_taken(type_id, slug, "复制")
            source_spec = parse_skill_dir(source, type_id="", source_kind="user")
            target_spec = parse_skill_dir(target, type_id="", source_kind="user")
            if (
                source_spec is None
                or target_spec is None
                or source_spec.fingerprint != target_spec.fingerprint
            ):
                raise _name_taken(type_id, slug, "复制")
        else:
            try:
                base.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, target, symlinks=False, ignore=_ignore_copy_symlinks)
            except FileExistsError:
                # 检查与复制之间目标被另一个请求建出来了：不能把别人的目录删掉。
                raise _name_taken(type_id, slug, "复制") from None
            except OSError as exc:
                shutil.rmtree(target, ignore_errors=True)
                raise SkillError(f"挂载「{row.name}」失败：{exc}") from None

    mounts = read_library_mounts(base)
    kernels = mounts.get(slug) or []
    if type_id not in kernels:
        kernels.append(type_id)
    mounts[slug] = kernels
    write_library_mounts(base, mounts)

    await rescan(session)
    new_row = await wait_for_market_registration(session, new_id)
    if new_row is None:
        raise SkillError(
            f"技能已挂载到 {target}，但编目未收录；请到「技能」页点一次「重新扫描」"
        )
    return {
        "skill_id": new_row.id,
        "name": new_row.name,
        "slug": new_row.slug,
        "type_id": type_id,
    }


async def copy_skill_to_all_kernels(
    session: AsyncSession, *, skill_id: str, owner_id: str | None = None
) -> dict[str, Any]:
    """把一份技能挂载到**全部**支持技能的内核（批量版 :func:`copy_skill_to_kernel`）。

    尽力而为：逐个内核调用单个挂载，冲突（同名技能等）只跳过并记进
    ``skipped``，不让一个内核挡住其余内核；返回
    ``{"name", "mounted", "skipped", "warnings"}`` 供界面汇总展示。
    """
    row = await get_row(session, skill_id)
    if row is None:
        raise SkillNotFound(f"技能不存在：{skill_id}")
    if is_library_row(row):
        already = set(mounts_for_row(row))
    else:
        already = {row.type_id}
    mounted: list[str] = []
    skipped: list[dict[str, str]] = []
    warnings: list[str] = []
    for type_id in sorted(_skill_capable_type_ids()):
        if type_id in already:
            continue
        try:
            await copy_skill_to_kernel(
                session, skill_id=skill_id, type_id=type_id, owner_id=owner_id
            )
            mounted.append(type_id)
        except SkillError as exc:
            skipped.append({"type_id": type_id, "reason": str(exc)})
            warnings.append(f"挂载到 {_type_label(type_id)} 失败：{exc}")
    return {
        "name": row.name,
        "mounted": mounted,
        "skipped": skipped,
        "warnings": warnings,
    }
