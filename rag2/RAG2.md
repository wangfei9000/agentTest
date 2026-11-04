# RAG2：小区名称模糊搜索

按小区名模糊查找 `xiaoqu.txt`，并以 **JSON** 返回结构化结果。

对应脚本：`rag2/simple_rag.py`  
本文由原 `XIAOQU_SEARCH.md` 与调试问答整理融合而成。

---

## 目录

1. [背景：为什么纯向量 RAG 找不到？](#背景为什么纯向量-rag-找不到)
2. [改进思路](#改进思路)
3. [数据格式](#数据格式)
4. [检索怎么做](#检索怎么做)
5. [入口与向量库复用](#入口与向量库复用)
6. [环境与用法](#环境与用法)
7. [和 rag/ 版本对比](#和-rag-版本对比)
8. [关键代码怎么读](#关键代码怎么读)
9. [向量为什么经常找错？](#向量为什么经常找错)
10. [和 SQL LIKE、性能相关的问题](#和-sql-like性能相关的问题)
11. [常见问题](#常见问题)
12. [代码分析总结](#代码分析总结)

---

## 背景：为什么纯向量 RAG 找不到？

`rag/` 那一版是经典 RAG：整文件切成约 500 字一块 → Embedding → Chroma Top-K → LLM 回答。

对「找到 侯位胡同 小区，并以 json 格式返回」这类需求会失败，原因不是数据没入库。

### 数据其实在

```text
2539204630	侯位胡同10号院	北京市
```

### 实测检索排名（分数越小越相似）

| 查询 | 含「侯位胡同」的片段排名 | 当时 `k=3` |
| --- | --- | --- |
| `找到 侯位胡同 小区，并以json格式返回` | **第 4 名** | 进不了上下文 |
| `侯位胡同` | 第 2 名 | 勉强能进 |
| `侯位胡同10号院` | 第 4 名 | 进不了 |

正确片段排在第 4，而 `ask(..., k=3)` 只取前 3 段，模型根本看不到这条记录。

旧版相关代码（`rag/simple_rag.py`）：

```python
# 整文件当一份 Document，再按 500 字切块 → 多条小区揉在一起
splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=80,
    separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
)
chunks = splitter.split_documents(docs)

# 只取 Top-3；正确片段若排第 4 就进不了 Prompt
def ask(store: Chroma, question: str, k: int = 3):
    docs = store.similarity_search(question, k=k)
    ...
```

### 根因

1. **任务类型不对**：按名称模糊搜更适合字符串 / 关键词匹配；向量检索找的是「语义相近」，一堆「某某胡同 + 北京市」彼此太像，专名信号被淹没。
2. **切分方式不对**：`chunk_size=500` 把十几条小区揉进同一段，目标名字只占一小截。
3. **查询太长**：整句「找到…并以 json 格式返回」和数据行形态差很远，更容易捞到别的胡同段。

结论：**库里有数据，但纯向量 RAG 不适合做小区名模糊查找。**

---

## 改进思路

更稳妥的做法：

1. **一行一条索引**（不做大块切分）
2. **关键词 / 模糊匹配为主**
3. **向量检索为辅**（语义补充），按 `id` 去重合并
4. **直接组装 JSON**，不依赖 LLM「猜」结构化字段

```
xiaoqu.txt（TSV）
   ↓
解析 → [{id, name, city}, ...]   ← 始终加载，模糊搜用
   ↓
一行一条 Document → Chroma        ← 可选，混合检索用
   ↓
模糊匹配（主） + 向量检索（辅）
   ↓
按 id 合并排序 → JSON
```

---

## 数据格式

文件：`rag2/docs/xiaoqu.txt`

```text
下面是小区数据，一行一个小区
id  地址    城市
3957926520	建国路89号院16号楼	北京市
2539204630	侯位胡同10号院	北京市
...
```

- 表头 / 说明行会跳过
- 按 Tab（或连续空白）拆成 `id`、`name`、`city`
- 约 1000 条小区

### 对应代码：解析 + 一行一条 Document

```python
def parse_xiaoqu(path: Path) -> list[dict]:
    """解析小区文件 → [{id, name, city}, ...]"""
    ...

def records_to_documents(records: list[dict]) -> list[Document]:
    """一行一条 Document，page_content 突出名称便于向量检索。"""
    Document(
        page_content=f"小区名称：{r['name']}\nid：{r['id']}\n城市：{r['city']}",
        metadata={"id": r["id"], "name": r["name"], "city": r["city"], ...},
    )
```

完整实现见 `simple_rag.py`。

---

## 检索怎么做

### 1. 抽出关键字

从自然语言问句里去掉噪声词：

- 输入：`找到 侯位胡同 小区，并以json格式返回`
- 关键字：`侯位胡同`

噪声包括：找到 / 查找 / 小区 / json / 格式 / 返回 等。

```python
def extract_keyword(question: str) -> str:
    q = question.strip()
    q = QUERY_NOISE.sub(" ", q)                          # 去掉噪声词
    q = re.sub(r"[，,。.!！？?、\s]+", " ", q).strip()   # 收拢标点空白
    return q or question.strip()                         # 洗空则退回原句
```

### 2. 模糊匹配（主）≈ 加强版 LIKE

对每条 `name` 打分：

| 规则 | 说明 |
| --- | --- |
| 关键字是名称子串 | 高分（如「侯位胡同」∈「侯位胡同10号院」） |
| 名称出现在整句问句里 | 高分 |
| `SequenceMatcher` + 字符 bigram | 补漏；bigram 重叠需 ≥ 60%，避免单靠「胡同」误伤 |

低于阈值的结果丢弃。结果暂存在：

```python
scored: list[tuple[dict, float]] = []
# 每个元素是 (小区字典, 分数) 这对 tuple，例如：
# ({"id": "...", "name": "侯位胡同10号院", ...}, 0.95)
```

### 3. 向量检索（辅）——从模型里匹配的入口

**真正用 embedding 模型做相似度匹配的函数是 `vector_search`。**

相关配套：

| 作用 | 位置 |
| --- | --- |
| Embedding 模型名 | `EMBED_MODEL = "nomic-embed-text"` |
| 创建客户端 | `_make_embeddings()` |
| 入库文本 | `records_to_documents()` |
| 建库 / 分批写入 | `build_vectorstore()`，每批 `EMBED_BATCH_SIZE = 16` 条 |
| 查询 | `vector_search()` → `store.similarity_search_with_score` |

核心一行：

```python
pairs = store.similarity_search_with_score(query, k=top_k)
```

含义：

1. 用 `nomic-embed-text` 把 `query`（短关键字）编成向量  
2. 在 Chroma 里找最近的 `k` 条  
3. 返回 `(Document, 距离)` 列表；距离越小越相似，再转成 `0~1` 分数：

```python
score = 1.0 / (1.0 + float(dist))
```

与模糊结果按 `id` 合并：模糊权重约 **0.65**，向量约 **0.35**；最终 `score < 0.4` 丢弃。

### 4. 输出 JSON

```json
{
  "query": "找到 侯位胡同 小区，并以json格式返回",
  "keyword": "侯位胡同",
  "count": 1,
  "results": [
    {
      "id": "2539204630",
      "name": "侯位胡同10号院",
      "city": "北京市"
    }
  ],
  "debug": [
    {
      "id": "2539204630",
      "name": "侯位胡同10号院",
      "score": 0.6013,
      "match": "fuzzy"
    }
  ]
}
```

- `results`：给业务用的干净字段  
- `debug`：`match` 为 `fuzzy` / `vector` / `hybrid`，方便排查

---

## 入口与向量库复用

要点：

- 模糊匹配**始终**读 `xiaoqu.txt`，不依赖向量库  
- 集合名 `xiaoqu_line`；为空则自动重建，避免误用旧版大块索引  
- `--no-vector` 可完全跳过 Chroma / Ollama  
- `--rebuild` 清空并按一行一条重 embed

---

## 环境与用法

- Python 依赖见项目根目录 `requirements.txt`
- 仅模糊匹配：**不需要** Ollama
- 启用向量混合：本机 Ollama + `nomic-embed-text`

```bash
ollama pull nomic-embed-text

# 只模糊匹配（秒级，推荐先试）
.venv/bin/python rag2/simple_rag.py --no-vector "找到 侯位胡同 小区，并以json格式返回"

# 模糊 + 向量（首次建库较慢）
.venv/bin/python rag2/simple_rag.py --rebuild "侯位胡同"

# 文档更新后强制重建
.venv/bin/python rag2/simple_rag.py --rebuild
```

| 参数 | 作用 |
| --- | --- |
| `--no-vector` | 跳过 Chroma，只用内存模糊匹配 |
| `--rebuild` | 清空并重建 `my_knowledge_db/` |
| 其余参数 | 拼成查询语句 |

向量库目录：`rag2/my_knowledge_db/`，集合名：`xiaoqu_line`。

---

## 和 rag/ 版本对比

| | `rag/simple_rag.py` | `rag2/simple_rag.py` |
| --- | --- | --- |
| 目标 | 通用文档问答 | 小区名模糊搜 → JSON |
| 切分 | 约 500 字一块 | 一行一条小区 |
| 检索 | 纯向量 Top-K | 模糊为主 + 向量为辅 |
| 回答 | LLM 生成自然语言 | 直接拼 JSON |
| 启动 | 可复用向量库 | 模糊始终快；向量可选 |

---

## 关键代码怎么读

### `EMBED_BATCH_SIZE = 16`

建索引时**每批写入 16 条**文档再调 embedding，避免一次全塞进 Ollama 导致超时 / 502。

```python
for i in range(0, len(docs), EMBED_BATCH_SIZE):
    batch = docs[i : i + EMBED_BATCH_SIZE]
    _add_with_retry(store, batch, ...)
```

### `extract_keyword`

把自然语言问句洗成接近小区名的关键字，给模糊和向量共用。整句噪声太大会拖垮两边检索。

### `scored: list[tuple[dict, float]] = []`

- `list`：列表  
- `tuple[dict, float]`：每个元素是「(小区记录字典, 分数)」这一对  
- `tuple`：有序、不可变的打包值，创建后用 `for r, score in scored` 拆开

### `similarity_search_with_score(query, k=top_k)`

向量侧唯一检索入口；`k` 只控制返回条数，不改变「库里有多少条」。

---

## 向量为什么经常找错？

### 调试实录（查「侯位胡同」）

合并排序后、过滤前，常见类似结果：

| 名次 | name | match | score | 是否正确 |
| --- | --- | --- | --- | --- |
| 1 | 侯位胡同10号院 | **fuzzy** | ~0.60 | ✅ |
| 2 | 香仁胡同 | vector | ~0.21 | ❌ |
| 3 | 正觉夹道胡同 | vector | ~0.21 | ❌ |
| 4 | 晓教胡同 | vector | ~0.21 | ❌ |
| … | 其它「xx胡同」 | vector | ~0.21 | ❌ |

正确条是 `fuzzy` 而不是 `hybrid`，说明 **向量 Top-K 根本没捞回「侯位胡同10号院」**，只捞到一堆别的胡同。

### 原因

1. **短专名区分度差**：`nomic-embed-text` 对短地名不擅长，容易觉得「都是北京胡同小区」。  
2. **共同字「胡同」把邻居拉近**：语义近邻变成「随便几个胡同」，不是精确的「侯位」。  
3. **分数很弱且挤在一起**：向量加权后约 0.21（≈ `0.35 × 原始分`），多条几乎同分 → 没有强第一名。  
4. **和模糊的分工**：「侯位胡同」⊂「侯位胡同10号院」字符串一抓一个准；向量可能因「10号院」等后缀把目标挤出 Top-K。

### 代码侧的兜底

```python
ranked = [h for h in ranked if h["score"] >= 0.4]
```

纯向量擦边分（~0.21）按设计应被丢掉，最终主要靠 fuzzy。调试时若看到这些弱结果，确认断点是否在过滤之前。

**结论：小区名精确/模糊查找，fuzzy 是主力；vector 只是语义补召回，短专名场景经常不准，属预期现象。**  
只查名称时可直接 `--no-vector`。

---

## 和 SQL LIKE、性能相关的问题

### 像不像数据库 `LIKE`？

| | SQL `LIKE '%侯位%'` / `fuzzy_search` | `vector_search` |
| --- | --- | --- |
| 比什么 | 字符包含 / 编辑相似度 | 语义向量是否接近 |
| 本质 | 字面匹配（模糊是加强版 LIKE） | 另一套「意思相近」 |
| 擅长 | 子串、近似拼写 | 说法不完全一样但仍相关的文本 |
| 不擅长 | 纯语义改写 | 短专名精确区分 |

一句话：**fuzzy ≈ 加强版 LIKE；vector 不是 LIKE 的替代品。**

### 向量库数据多了会不会慢？

| 阶段 | 影响 |
| --- | --- |
| **建库** | 条数 × embedding，明显变慢；建完可复用 |
| **查询·向量** | 只返回 Top-K；千～万级通常仍可接受；更大需 ANN 索引 |
| **查询·模糊** | **O(N) 全表扫**，数据一大往往先卡在这里 |
| **常被忽略** | 每次向量查询都要先调 Ollama 做 embedding，开销常比比对本身大 |

### `vector_search` 会比 `LIKE` 快多少？

**没有固定倍数。** 小到中等规模（约千条）时：

- 模糊 / `LIKE` 往往更快（纯内存字符串）  
- 向量每次还要 embedding，可能更慢  
- 向量的价值是**补召回**，不是加速版 LIKE  

数据大到全表 `LIKE '%xx%'` 开始卡时，带索引的向量检索优势才更明显。

---

## 常见问题

### 搜不到 / 结果不对

1. 确认 `rag2/docs/xiaoqu.txt` 里是否有该名称（可能全称带「几号院」）。  
2. 先看打印的 `keyword=` 是否抽对；噪声词可在 `QUERY_NOISE` 补充。  
3. 看 `debug.match`：`fuzzy` / `vector` / `hybrid`，以及 `score` 是否过低被过滤。  
4. 若一堆无关「xx胡同」且 `match=vector`、分很低：属短专名向量弱区分，信 fuzzy 或加 `--no-vector`。

### 向量重建很慢

约 1000 条 × 本地 Embedding，可能要几分钟。日常调试用 `--no-vector`；名称查找场景模糊通常已够用。

### 旧向量库干扰

加 `--rebuild` 按一行一条重建；或直接 `--no-vector`。

---

## 代码分析总结

### 一句话定位

这不是「通用 RAG 问答」，而是**结构化小区目录的混合检索器**：内存模糊匹配保证按名能命中，可选 Chroma 向量补语义，最后**程序直接拼 JSON**，全程不调用聊天 LLM。

### 模块地图

| 区块 | 函数 | 职责 |
| --- | --- | --- |
| 配置 | 常量、`QUERY_NOISE` | 路径、模型、集合名、问句噪声词 |
| Ollama | `_make_embeddings` | 本机 Embedding（`nomic-embed-text`） |
| 数据 | `parse_xiaoqu`、`records_to_documents` | TSV → 记录 → 一行一条 Document |
| 模糊 | `extract_keyword`、`fuzzy_search` | 抽关键字；对 `name` 打分 |
| 向量 | `load/build_vectorstore`、`vector_search` | 持久化、分批写入、短查询检索 |
| 混合 | `hybrid_search`、`search_as_json` | 加权合并、过滤、输出 JSON |
| 入口 | `main` | CLI、是否建库、交互循环 |

```
main
 ├─ parse_xiaoqu(xiaoqu.txt)          → records（必做）
 ├─ [--no-vector] 跳过
 │   或 load_vectorstore / build_vectorstore → store
 └─ search_as_json(records, store, q)
       └─ hybrid_search
             ├─ fuzzy_search(records)     ← 主
             └─ vector_search(store)      ← 辅（store 可为 None）
```

### 关键设计决策

| 决策 | 原因 |
| --- | --- |
| 不做大块切分 | 避免多条小区揉进一块，专名被稀释 |
| 模糊始终读文件 | 启动快；可完全离线 |
| 向量用短关键字 | 入库是「小区名称：xxx」，不是「找到…json」 |
| 模糊权重 0.65 > 向量 0.35 | 按名查找时子串命中更可信 |
| bigram 门槛 0.6 | 防止只靠「胡同」误伤 |
| 最终分 ≥ 0.4 | 丢掉纯向量擦边脏结果 |
| 直接拼 JSON | 字段已在记录里，避免 LLM 编造 |
| 分批 16 + 重试 | 本机 Ollama 大批量 embed 易挂 |

### 打分速查

**模糊原始分：**

| 条件 | 分数 |
| --- | --- |
| `keyword in name` | ≥ 0.85 |
| `name in question` | 0.95 |
| 否则 | `SequenceMatcher`；bigram ≥ 0.6 可抬分 |
| 入选 | ≥ 0.55 |

**混合最终分：**

```text
vector_only  ≈ 0.35 * vector_score
fuzzy_only   ≈ 0.65 * fuzzy_score
hybrid       ≈ 两者相加
最终门槛     ≥ 0.4，再取 top_k（默认 5）
```

### 当前边界

1. 模糊是 O(N) 全表扫描；万级以上可考虑倒排或 `rapidfuzz`。  
2. `QUERY_NOISE` 手写规则，问法大变可能抽错关键字。  
3. 改了 `xiaoqu.txt` 后向量需 `--rebuild`；模糊下次启动即生效。  
4. 无聊天模型：只返回结构化 JSON。  
5. `vector_search` 失败会退回纯模糊（演示友好；生产可加告警）。

---

## 完整源码位置

```text
rag2/simple_rag.py
```

更细的模块摘录仍可参考同目录 `XIAOQU_SEARCH.md`；日常以本文 `RAG2.md` 为总览即可。
