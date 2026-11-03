# simple_rag 常见错误与解决方案

本文记录运行 `simple_rag.py` 时遇到的典型问题、原因和已落地的修法。

---

## 1. `ollama pull` 超时

### 报错

```text
ollama pull nomic-embed-text
pulling manifest
Error: pull model manifest: Get "https://registry.ollama.ai/v2/library/nomic-embed-text/manifests/latest":
dial tcp ...:443: i/o timeout
```

### 原因

本机访问 `registry.ollama.ai` 网络不通或超时（常见于未开代理 / 国际链路不稳）。

### 解决方案

1. **先查本地是否已有模型**（很多情况下早就装过，不必再 pull）：

```bash
ollama list
# 或
curl http://127.0.0.1:11434/api/tags
```

若列表里已有 `nomic-embed-text`、`qwen3.5:9b`，直接跑程序即可。

2. 确实没有时：开代理后再 `ollama pull`，或从能访问的机器拷贝 `~/.ollama/models`。

---

## 2. 建索引时报 `502`

### 报错（完整日志）

```text
/Users/edy/Downloads/wangfei/agentTest/.venv/bin/python /Users/edy/Downloads/wangfei/agentTest/simple_rag.py
/Users/edy/Downloads/wangfei/agentTest/simple_rag.py:15: DeprecationWarning: `langchain-community` is being sunset and is no longer actively maintained. See https://github.com/langchain-ai/langchain-community/issues/674 for details and migration guidance toward standalone integration packages.
  from langchain_community.document_loaders import TextLoader, PyPDFLoader
=== 1. 加载文档 ===
[加载] GUIDE_CN.md → 1 页/段

=== 2. 建立向量索引 ===
[切分] 共 162 个片段
Traceback (most recent call last):
  File "/Users/edy/Downloads/wangfei/agentTest/simple_rag.py", line 147, in <module>
    main()
  File "/Users/edy/Downloads/wangfei/agentTest/simple_rag.py", line 127, in main
    store = build_vectorstore(docs)
  File "/Users/edy/Downloads/wangfei/agentTest/simple_rag.py", line 65, in build_vectorstore
    store = Chroma.from_documents(
  File "/Users/edy/Downloads/wangfei/agentTest/.venv/lib/python3.10/site-packages/langchain_chroma/vectorstores.py", line 1431, in from_documents
    return cls.from_texts(
  File "/Users/edy/Downloads/wangfei/agentTest/.venv/lib/python3.10/site-packages/langchain_chroma/vectorstores.py", line 1365, in from_texts
    chroma_collection.add_texts(
  File "/Users/edy/Downloads/wangfei/agentTest/.venv/lib/python3.10/site-packages/langchain_chroma/vectorstores.py", line 627, in add_texts
    embeddings = self._embedding_function.embed_documents(texts)
  File "/Users/edy/Downloads/wangfei/agentTest/.venv/lib/python3.10/site-packages/langchain_ollama/embeddings.py", line 325, in embed_documents
    return self._client.embed(
  File "/Users/edy/Downloads/wangfei/agentTest/.venv/lib/python3.10/site-packages/ollama/_client.py", line 442, in embed
    return self._request(
  File "/Users/edy/Downloads/wangfei/agentTest/.venv/lib/python3.10/site-packages/ollama/_client.py", line 203, in _request
    return cls(**self._request_raw(*args, **kwargs).json())
  File "/Users/edy/Downloads/wangfei/agentTest/.venv/lib/python3.10/site-packages/ollama/_client.py", line 147, in _request_raw
    raise ResponseError(e.response.text, e.response.status_code) from None
ollama._types.ResponseError:  (status code: 502)

Process finished with exit code 1
```

调用链可概括为：

`Chroma.from_documents` → `embed_documents(全部 162 段)` → `ollama embed` → **空 body 502**

> 同一次运行里还有 `langchain-community` 的 `DeprecationWarning`，见第 3 节；修 502 时一并去掉了该依赖。

### 原因

`GUIDE_CN.md` 切成 **162 段**后，旧代码用 `Chroma.from_documents(...)` **一次全部向量化**。Ollama 冷启动或瞬时过载时，容易返回空 502；系统代理劫持本机请求时也可能表现为 502。

### 解决方案：改了哪些代码

修改文件：`simple_rag.py`。核心是把「一次 `from_documents`」改成「预热 + 分批 `add_documents` + 502/503 重试」。

#### ① 新增配置与辅助函数

```python
EMBED_BATCH_SIZE = 16  # 一次向量化太多片段时，Ollama 容易返回 502
EMBED_RETRIES = 5

def _ollama_client_kwargs():
    # 直连本机，避免系统代理把请求转走后返回空 502
    return {"trust_env": False}
```

#### ② 新增 `_add_with_retry()`：单批写入 + 自动重试

```python
def _add_with_retry(store: Chroma, batch: list[Document], batch_no: int, total: int):
    """分批写入；模型换页/未就绪时 Ollama 会回 502/503，自动重试。"""
    for attempt in range(EMBED_RETRIES):
        try:
            store.add_documents(batch)
            print(f"[索引] 批次 {batch_no}/{total}（{len(batch)} 段）完成")
            return
        except ResponseError as e:
            if e.status_code not in (502, 503) or attempt == EMBED_RETRIES - 1:
                raise
            wait = 2 * (attempt + 1)
            print(
                f"  Embedding 返回 {e.status_code}，{wait}s 后重试 "
                f"({attempt + 1}/{EMBED_RETRIES})..."
            )
            time.sleep(wait)
```

#### ③ 重写 `build_vectorstore()`：去掉一次性 `from_documents`

**改前（会炸 502）：**

```python
embeddings = OllamaEmbeddings(model=EMBED_MODEL, base_url=OLLAMA_BASE)
store = Chroma.from_documents(
    documents=chunks,          # 162 段一次塞进去
    embedding=embeddings,
    persist_directory=str(DB_DIR),
    collection_name="simple_rag",
)
```

**改后：**

```python
embeddings = OllamaEmbeddings(
    model=EMBED_MODEL,
    base_url=OLLAMA_BASE,
    client_kwargs=_ollama_client_kwargs(),  # trust_env=False
)

# 预热 embedding 模型
embeddings.embed_query("ping")

store = Chroma(
    collection_name="simple_rag",
    embedding_function=embeddings,
    persist_directory=str(DB_DIR),
)

# 每批最多 16 段
for i in range(0, len(chunks), EMBED_BATCH_SIZE):
    batch = chunks[i : i + EMBED_BATCH_SIZE]
    _add_with_retry(store, batch, ...)
```

#### ④ 顺带去掉报错日志里的弃用警告

**改前：**

```python
from langchain_community.document_loaders import TextLoader, PyPDFLoader
```

**改后：** `load_docs()` 用 `Path.read_text` / `pypdf.PdfReader` 构造 `Document`，不再 import `langchain_community`。

### 改动对照表

| 位置 | 改前 | 改后 |
| --- | --- | --- |
| 建库入口 | `Chroma.from_documents(全部 chunks)` | 空 `Chroma` + 循环 `add_documents` |
| 批量大小 | 一次 162 段 | `EMBED_BATCH_SIZE=16` |
| 失败处理 | 直接崩溃 | `_add_with_retry` 对 502/503 重试 |
| 冷启动 | 无 | 建库前 `embed_query("ping")` |
| Ollama 客户端 | 默认（可能走系统代理） | `trust_env=False` |
| 文档加载 | `langchain_community` Loader | 本地读文件 + `pypdf` |

### 修好后正常日志长这样

```text
=== 2. 建立向量索引 ===
[切分] 共 162 个片段
[预热] 加载 Embedding 模型 nomic-embed-text ...
[索引] 批次 1/11（16 段）完成
[索引] 批次 2/11（16 段）完成
...
[索引] 批次 11/11（… 段）完成
[索引] 已写入 .../my_knowledge_db
```

仍失败时按顺序检查：

```bash
curl http://127.0.0.1:11434/api/tags
curl http://127.0.0.1:11434/api/embeddings \
  -d '{"model":"nomic-embed-text","prompt":"hello"}'
```

---

## 3. `langchain-community` 弃用警告

### 报错

```text
DeprecationWarning: `langchain-community` is being sunset and is no longer actively maintained.
  from langchain_community.document_loaders import TextLoader, PyPDFLoader
```

### 原因

LangChain 正在把社区包拆成独立集成包，`langchain-community` 进入日落期。

### 解决方案（已写入 `simple_rag.py`）

不再依赖 `langchain_community` 的 Loader：

- `.md` / `.txt`：`Path.read_text` + `Document`
- `.pdf`：`pypdf.PdfReader` + `Document`

警告会消失，行为与原来一致。

---

## 4. 问答卡住很久 / 几乎无输出

### 现象

检索片段已打印，但长时间停在「回答」之前，像死机。

### 原因

`qwen3.5` 默认可能开启 **thinking / reasoning**，本地推理会极慢。

### 解决方案（已写入 `simple_rag.py`）

创建 `ChatOllama` 时关闭思考：

```python
ChatOllama(..., reasoning=False)
```

与项目里 `wf_agent.py` 使用 `think=False` 是同一类处理。

---

## 5. 知识库没有的问题只回答「不知道」

### 现象

```text
--- 回答 ---
根据提供的资料，**不知道**关于「……」的具体信息。
```

文档里确实没有答案时，用户希望改用大模型自身知识回答。

### 原因

早期 System Prompt 写成了「只根据资料回答，不足就说不知道」。

### 解决方案（已写入 `simple_rag.py`）

调整 Prompt 策略：

1. 资料能回答 → 按知识库作答，并尽量标注 `[来源1]` 等  
2. 资料无关或不足 → **不要**只说不知道，改用模型通用知识，并在开头标注：

```text
【知识库未覆盖，以下为模型回答】
```

---

## 快速对照表

| 问题 | 关键词 | 优先处理 |
| --- | --- | --- |
| pull 超时 | `i/o timeout` / `registry.ollama.ai` | `ollama list` 看是否已有 |
| 建库 502 | `ResponseError` / `status code: 502` | 分批 + 重试 + 预热（已内置） |
| 弃用警告 | `langchain-community` sunset | 改用本地读文件（已内置） |
| 回答卡住 | 检索完无下文 | `reasoning=False`（已内置） |
| 只会「不知道」 | 题外问题 | 回退模型知识的 Prompt（已内置） |

---

## 正常运行确认

```bash
cd /Users/edy/Downloads/wangfei/agentTest
.venv/bin/python simple_rag.py "这份文档主要讲什么？"
```

预期能看到：加载 → 切分 → 预热 → 分批索引 → 检索片段 → 回答。
