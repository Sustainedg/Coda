# Basic Memory 切换 API Embedding 方案

> **状态：研究完成，等用户选定服务商 + 提供 API Key 后实施**
> 一句话结论：**当前中文语义检索是失效的**——无关查询得分比相关查询还高。根因是用了纯英文模型 `bge-small-en-v1.5`。换成多语言或 API 版 embedding 即可修复。
> 本文所有结论均基于 2026-11-28 对本机 basic-memory **1.0.0** 的实测（含分数对照实验），未做无据推测。

---

## 一、问题现象

### 1.1 中文语义检索失效（实测）

| 查询 | 预期 | 实测最高分 |
| --- | --- | --- |
| `部署流程`（相关） | 高分 | **0.751** |
| `量子纠缠与超导材料`（无关） | 低分 | **0.824** ⚠️ |
| `法国大革命的历史意义`（无关） | 低分 | 0.778 |

**无关查询的分数反而更高。** 语义检索最基本的「相关高分、无关低分」完全失效。

### 1.2 英文检索正常

| 查询 | 最高分 | 分布 |
| --- | --- | --- |
| `multidimensional table image recognition`（相关） | 1.000 | 1.000 / 0.715 / 0.695 ✅ |
| `quantum entanglement superconductivity`（无关） | 0.605 | 0.605 / 0.552 / 0.551 ✅ |

区分度清晰。

### 1.3 向量空间被压扁

直接读库计算余弦相似度：

```
chunk0-chunk1: 0.855    chunk3-chunk4: 0.887
chunk2-chunk3: 0.781    chunk0-chunk2: 0.673
```

任意两个不相干分块相似度都在 0.62–0.89。**所有中文句子彼此都「很像」。**

### 1.4 中文 FTS 反而是好的 ✅

```
MATCH '多维'    → 15 命中
MATCH '图片'    → 17 命中
MATCH '图片识别' → 2 命中
```

`script_ngrams` 的 bigram 兜底在正常工作。

**结论：当前中文召回实际只靠关键词那一路，向量那一路在拖后腿。**

---

## 二、根因

```
bge-small-en-v1.5
        ^^^^
```

`-en-` = 英文专用模型，384 维，中文几乎没训练过。

后果链：

```
中文文本 → 映射到低区分度区域
        → 任何中文查询都拿 0.7+
        → semantic_min_similarity: 0.55 阈值形同虚设
        → hybrid 融合时向量噪声污染 FTS 准确结果
```

---

## 三、当前环境取证

| 项目 | 实际值 | 证据 |
| --- | --- | --- |
| 数据库 | SQLite | `/app/data/home/.basic-memory/memory.db` |
| 向量库 | sqlite-vec（`vec0`） | `CREATE VIRTUAL TABLE ... USING vec0(...)` |
| 向量模型 | fastembed `bge-small-en-v1.5` | 65 MB 已下载 |
| 维度 | 384 | `embedding float[384]` |
| 向量数 | 224（全部 ready） | `search_vector_chunks` |
| 阈值 | 0.55 | `semantic_min_similarity` |
| 重排 | 关闭 | `reranker_enabled: false` |

**注意**：`config.json` 里 `"semantic_vector_index": "pgvector"` 是**烟雾弹**，实际跑的是 `sqlite-vec`（`search_vector_chunks.vector_index = 'sqlite-vec'`）。

---

## 四、配置项清单（全部已存在，共 24 个）

`bm config list` 实测确认以下 key 都在：

| 关键配置项 | 当前值 |
| --- | --- |
| `semantic_embedding_provider` | `fastembed` |
| `semantic_embedding_model` | `bge-small-en-v1.5` |
| `semantic_embedding_api_base` | (not set) |
| `semantic_embedding_api_key` | (not set) |
| `semantic_embedding_dimensions` | (not set) |
| `semantic_embedding_batch_size` | 2 |
| `semantic_embedding_request_concurrency` | 4 |

其余（`document_prefix` / `query_prefix` / `input_type` 等）均为可选高级项。

**优先级实测**：

```
BASIC_MEMORY_* 环境变量  >  config.json  >  代码默认值
```

```
$ BASIC_MEMORY_SEMANTIC_EMBEDDING_MODEL=test-model-x bm config get semantic_embedding_model
semantic_embedding_model = test-model-x
Overridden by $BASIC_MEMORY_SEMANTIC_EMBEDDING_MODEL = test-model-x
```

---

## 五、实施方案

### 5.1 服务商对照表

| 服务商 | `api_base` | `model` | `dimensions` |
| --- | --- | --- | --- |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `BAAI/bge-m3` | 1024 |
| 阿里云百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `text-embedding-v3` | 1024 |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | `embedding-3` | 2048 |
| OpenAI | 留空 | `text-embedding-3-small` | 1536 |

> ⚠️ 维度以各家文档为准，配错会在首次 embed 时直接报错。

### 5.2 执行步骤

```bash
# 1. 备份配置
cp ~/.basic-memory/config.json ~/.basic-memory/config.json.bak

# 2. 写入 5 个核心参数（走 Pydantic 校验）
bm config set semantic_embedding_provider openai
bm config set semantic_embedding_api_base https://api.siliconflow.cn/v1
bm config set semantic_embedding_api_key sk-xxxx
bm config set semantic_embedding_model BAAI/bge-m3
bm config set semantic_embedding_dimensions 1024

# 3. 重嵌全部向量
bm reindex --embeddings

# 4. 重启 MCP（配置只在启动时读）
```

### 5.3 或者用环境变量（不污染配置文件）

在 MCP 的 `env` 块里加：

```json
"env": {
  "BASIC_MEMORY_SEMANTIC_EMBEDDING_PROVIDER": "openai",
  "BASIC_MEMORY_SEMANTIC_EMBEDDING_API_BASE": "https://api.siliconflow.cn/v1",
  "BASIC_MEMORY_SEMANTIC_EMBEDDING_API_KEY": "sk-xxxx",
  "BASIC_MEMORY_SEMANTIC_EMBEDDING_MODEL": "BAAI/bge-m3",
  "BASIC_MEMORY_SEMANTIC_EMBEDDING_DIMENSIONS": "1024"
}
```

> ⚠️ `mcp.json` 位于 `/app/data/agent-homes/<hash>/claude-home/`，是平台按会话生成的，改完要确认下次会话还在。

---

## 六、三个已知坑

### 坑 1：维度变了会自动重建向量表 ✅

```python
# repository/sqlite_vec_index.py:117
dimensions_changed = bool(vector_sql and expected_dimensions not in vector_sql)
if dimensions_changed:
    await session.execute(text("DROP TABLE IF EXISTS search_vector_embeddings"))
    # 同时把 chunks 状态全改回 pending
```

**不需要 `bm reset`。** 会自动 DROP 重建 + 标记全量待重嵌。

### 坑 2：现有 224 个向量全部作废

它们是用 bge-small-en 生成的，语义空间完全不同。有 identity 机制兜底：

```
search_vector_chunks.embedding_model = 'FastEmbedEmbeddingProvider:bge-small-en-v1.5:384'
```

换 provider 后 identity 不匹配，会自动重算，不会悄悄用旧向量。

### 坑 3：`dimensions` 只做本地校验，不下发给 API

```python
# openai_provider.py
response = await client.embeddings.create(
    model=self.model_name,
    input=batch,
)   # ← 没有传 dimensions 参数
```

填小了会报：

```
Embedding model returned 1024-dimensional vectors but provider was configured for 512 dimensions.
```

---

## 七、验收标准

切换完成后，重跑对照实验：

| 查询 | 目标 |
| --- | --- |
| `部署流程`（相关） | 明显高于无关查询 |
| `量子纠缠与超导材料`（无关） | **低于 0.55 阈值** |
| `法国大革命的历史意义`（无关） | 低于 0.55 阈值 |

**判定标准：无关查询的最高分必须显著低于相关查询的最低分。**

---

## 八、备选：本地多语言模型（不想走 API 时）

| 模型 | 维度 | 说明 |
| --- | --- | --- |
| `intfloat/multilingual-e5-small` | 384 | **维度不变，可直接复用现有表** ✅ 最省事 |
| `BAAI/bge-m3` | 1024 | 多语言最强，但慢 |
| `jinaai/jina-embeddings-v3` | 1024 | 质量好 |

```bash
bm config set semantic_embedding_model intfloat/multilingual-e5-small
bm reindex --embeddings
```

优点：免费、离线、维度不用改。
缺点：本地 CPU 推理慢，质量不如 API 版。

---

## 九、待办清单

- [ ] 用户选定服务商
- [ ] 提供 API Key
- [ ] 备份 `config.json`
- [ ] 写入 5 个核心参数
- [ ] 执行 `bm reindex --embeddings`
- [ ] 重启 MCP 进程
- [ ] 重跑中文对照实验验证
- [ ] 更新本文档状态为「已实施」

---

## 十、关联文档

- `docs/计划任务/basic-memory-layering-plan.md` —— 分层架构；embedding 配置是系统设置面板的第一个落地场景
