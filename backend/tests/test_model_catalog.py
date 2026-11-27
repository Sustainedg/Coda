"""models.dev 目录价（P3-3 增强）：解析、provider 别名、前缀写法、坏数据保留旧快照。"""
from __future__ import annotations

import json

import pytest

from app.model_catalog import (
    catalog_meta,
    install_catalog,
    load_from_disk,
    lookup,
    parse_catalog,
    reset_catalog,
)
from app.settings import get_settings

_SAMPLE = {
    "anthropic": {
        "models": {
            "claude-sonnet-4": {
                "cost": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}
            }
        }
    },
    "google": {
        "models": {"gemini-2.5-pro": {"cost": {"input": 1.25, "output": 10.0}}}
    },
}


@pytest.fixture(autouse=True)
def _clean_catalog():
    reset_catalog()
    yield
    reset_catalog()


def test_parse_catalog_reads_rates_and_meta():
    prices = parse_catalog(_SAMPLE, sha256="abc", downloaded_at="t")
    price = prices[("anthropic", "claude-sonnet-4")]
    assert price.input_per_million == 3.0
    assert price.output_per_million == 15.0
    assert price.cache_read_per_million == 0.3
    assert price.cache_creation_per_million == 3.75
    assert price.sha256 == "abc"


def test_lookup_exact_alias_and_prefixed_model():
    assert install_catalog(json.dumps(_SAMPLE).encode())
    assert lookup("anthropic", "claude-sonnet-4").output_per_million == 15.0
    # provider 别名：gemini → google
    assert lookup("gemini", "gemini-2.5-pro").provider == "google"
    # OpenRouter 风格：模型名自带 provider 前缀
    assert lookup("openai", "anthropic/claude-sonnet-4").provider == "anthropic"
    # 不认识的中转模型不继承官方价
    assert lookup("openai", "some-relay-model") is None
    assert catalog_meta()["count"] == 2


def test_invalid_catalog_keeps_previous_snapshot():
    assert install_catalog(json.dumps(_SAMPLE).encode())
    before = catalog_meta()["count"]
    assert not install_catalog(b"not-json")
    assert catalog_meta()["count"] == before
    # 结构化但没有任何 cost 的数据也不覆盖旧快照
    assert not install_catalog(b'{"anthropic": {"models": {}}}')
    assert catalog_meta()["count"] == before


def test_load_from_disk_uses_saved_snapshot():
    path = get_settings().data_dir / "models" / "models.dev.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_SAMPLE), encoding="utf-8")
    assert load_from_disk()
    assert lookup("anthropic", "claude-sonnet-4") is not None
    assert catalog_meta()["count"] == 2
    assert catalog_meta()["sha256"]
