"""models.dev 目录价：后台下载快照、磁盘缓存、按 provider/model 查价。

参考价来源是 ``https://models.dev/api.json``（每个 provider / model 的每百万 token 参考价）。
设计原则与计划一致：

* **不阻塞启动**：先读磁盘上的旧快照，再在后台拉新；失败就保留旧快照；
* **不重算历史**：只在写入新账本行时给成本，目录刷新不会回头改旧行；
* **不认识就 unknown**：自定义中转的模型名不继承官方价，缺价保持 ``None``，不假装 0。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .egress import wrap_client
from .settings import get_settings

logger = logging.getLogger("coda")

#: 下载上限：20 MiB（与对照实现的保护一致）。
MAX_CATALOG_BYTES = 20 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 15.0

#: 我们账本里的 provider（网关协议）→ models.dev 的 provider id。
_PROVIDER_ALIASES = {
    "openai": "openai",
    "anthropic": "anthropic",
    "gemini": "google",
    "google": "google",
    "deepseek": "deepseek",
    "zhipuai": "zhipuai",
    "zai": "zai",
    "moonshot": "moonshotai",
}


@dataclass(frozen=True)
class CatalogPrice:
    provider: str
    model: str
    input_per_million: float
    output_per_million: float
    cache_read_per_million: float
    cache_creation_per_million: float
    sha256: str | None = None
    downloaded_at: str | None = None

    def rates(self) -> dict[str, float]:
        return {
            "input": self.input_per_million,
            "output": self.output_per_million,
            "cache_read": self.cache_read_per_million,
            "cache_creation": self.cache_creation_per_million,
        }


_prices: dict[tuple[str, str], CatalogPrice] = {}
_meta: dict[str, Any] = {}


def _catalog_path() -> Path:
    return get_settings().data_dir / "models" / "models.dev.json"


def _num(container: Any, *keys: str) -> float:
    for key in keys:
        value = container.get(key) if isinstance(container, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def parse_catalog(
    data: Any,
    *,
    sha256: str | None = None,
    downloaded_at: str | None = None,
) -> dict[tuple[str, str], CatalogPrice]:
    """解析 models.dev 的 ``api.json``；结构不认识就返回空表（不抛异常）。"""
    prices: dict[tuple[str, str], CatalogPrice] = {}
    if not isinstance(data, dict):
        return prices
    for provider_id, provider in data.items():
        models = provider.get("models") if isinstance(provider, dict) else None
        if not isinstance(models, dict):
            continue
        pid = str(provider_id).strip().lower()
        for model_id, model in models.items():
            cost = model.get("cost") if isinstance(model, dict) else None
            if not isinstance(cost, dict):
                continue
            mid = str(model_id).strip()
            if not pid or not mid:
                continue
            prices[(pid, mid.lower())] = CatalogPrice(
                provider=pid,
                model=mid,
                input_per_million=_num(cost, "input"),
                output_per_million=_num(cost, "output"),
                cache_read_per_million=_num(cost, "cache_read", "cacheRead", "cached_input"),
                cache_creation_per_million=_num(
                    cost, "cache_write", "cacheWrite", "cache_creation"
                ),
                sha256=sha256,
                downloaded_at=downloaded_at,
            )
    return prices


def install_catalog(raw: bytes, *, downloaded_at: str | None = None) -> bool:
    """把一份原始 JSON 装进内存快照；结构为空 / 非法时保留旧快照。"""
    global _prices, _meta
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("模型目录不是合法 JSON，忽略")
        return False
    sha = hashlib.sha256(raw).hexdigest()
    stamp = downloaded_at or datetime.now(timezone.utc).isoformat()
    prices = parse_catalog(data, sha256=sha, downloaded_at=stamp)
    if not prices:
        logger.warning("模型目录为空或结构不认识，保留旧快照")
        return False
    _prices = prices
    _meta = {"sha256": sha, "downloaded_at": stamp, "count": len(prices)}
    return True


def load_from_disk() -> bool:
    """启动时先读磁盘快照（快照可能比这次下载旧，但总比没有强）。"""
    path = _catalog_path()
    try:
        raw = path.read_bytes()
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return False
    if len(raw) > MAX_CATALOG_BYTES:
        return False
    return install_catalog(raw, downloaded_at=stamp)


def lookup(provider: str | None, model: str | None) -> CatalogPrice | None:
    """按 provider/model 查目录价；支持 provider 别名与 ``provider/model`` 前缀写法。"""
    name = (model or "").strip()
    if not name:
        return None
    pid = (provider or "").strip().lower()

    if pid:
        found = _prices.get((pid, name.lower()))
        if found:
            return found
        alias = _PROVIDER_ALIASES.get(pid)
        if alias and alias != pid:
            found = _prices.get((alias, name.lower()))
            if found:
                return found

    # OpenRouter 风格：模型名本身带 provider 前缀，如 anthropic/claude-sonnet-4。
    if "/" in name:
        head, _, tail = name.partition("/")
        head, tail = head.strip().lower(), tail.strip()
        if head and tail:
            for candidate in (head, _PROVIDER_ALIASES.get(head, head)):
                found = _prices.get((candidate, tail.lower()))
                if found:
                    return found
    return None


def catalog_meta() -> dict[str, Any]:
    return dict(_meta)


def reset_catalog() -> None:
    """测试用：清空内存快照。"""
    global _prices, _meta
    _prices = {}
    _meta = {}


async def refresh_from_network() -> bool:
    """后台从 models.dev 拉一次目录；失败保留旧快照，绝不抛给调用方。"""
    url = (get_settings().models_dev_url or "").strip()
    if not url:
        return False
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS), follow_redirects=True
        ) as raw_client:
            # 遵守平台出口策略（off / block / allowlist）；被拦时保留磁盘快照。
            client = wrap_client(raw_client)
            response = await client.get(url)
            response.raise_for_status()
            raw = response.content
        if len(raw) > MAX_CATALOG_BYTES:
            logger.warning("模型目录超过 %s 字节，已忽略", MAX_CATALOG_BYTES)
            return False
        if not install_catalog(raw):
            return False
        path = _catalog_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_bytes(raw)
            tmp.replace(path)
        except OSError:
            logger.warning("模型目录写盘失败（内存快照仍可用）", exc_info=True)
        logger.info("模型目录已更新：%s 条", len(_prices))
        return True
    except Exception:  # noqa: BLE001 - 目录刷新失败不能影响主流程
        logger.warning("模型目录下载失败，保留旧快照", exc_info=True)
        return False


__all__ = [
    "MAX_CATALOG_BYTES",
    "CatalogPrice",
    "parse_catalog",
    "install_catalog",
    "load_from_disk",
    "lookup",
    "catalog_meta",
    "reset_catalog",
    "refresh_from_network",
]
