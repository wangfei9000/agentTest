# 极简 LangChain RAG 知识库问答

基于 `simple_rag.py`：把本地文档做成向量知识库，再用大模型按检索结果回答问题。

## 整体流程

```
docs/ 文档
   ↓
加载（md / txt / pdf）
   ↓
切分（RecursiveCharacterTextSplitter）
   ↓
Embedding（nomic-embed-text）
   ↓
写入 Chroma（my_knowledge_db/）
   ↓
相似度检索 Top-K
   ↓
拼进 Prompt → qwen3.5:9b 生成回答
```

这是确定性的「先检索再生成」，不经过 Agent / Tool 调用。

## 环境准备

1. 本机已启动 [Ollama](https://ollama.com)，默认地址：`http://127.0.0.1:11434`
2. 需要两个模型：

| 用途 | 模型名 |
| --- | --- |
| 聊天回答 | `qwen3.5:9b` |
| 文本向量化 | `nomic-embed-text` |

```bash
ollama pull qwen3.5:9b
ollama pull nomic-embed-text
```

若 `pull` 超时，可先执行 `ollama list` 看本地是否已有模型；已有则无需再拉。

3. 安装 Python 依赖：

```bash
cd /path/to/agentTest
.venv/bin/pip install -r requirements.txt
```

## 目录说明

```
agentTest/
├── simple_rag.py          # 主程序
├── SIMPLE_RAG.md          # 本文档
├── docs/                  # 知识库原始文档（放入 .md / .txt / .pdf）
│   └── python_notes.md
├── my_knowledge_db/       # Chroma 向量库（运行时自动生成）
└── requirements.txt
```

## 怎么跑

```bash
# 交互模式：先跑两道示例题，再进入问答循环（输入 q 退出）
.venv/bin/python simple_rag.py

# 单次问答：问完即退出
.venv/bin/python simple_rag.py 装饰器是做什么用的？
```

把你的文档放进 `docs/` 后重新运行即可；程序每次会重建向量库，保证演示可复现。

## 代码结构

| 函数 | 作用 |
| --- | --- |
| `load_docs()` | 递归读取 `docs/`，按后缀选 `TextLoader` 或 `PyPDFLoader` |
| `build_vectorstore()` | 切分文档 → Embedding → 写入 Chroma |
| `ask()` | 检索 Top-K 片段 → 填入 Prompt → 调用 LLM 回答 |
| `main()` | 串联上述步骤，支持交互 / 命令行参数两种用法 |

### 关键配置（文件顶部）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DOCS_DIR` | `./docs` | 文档目录 |
| `DB_DIR` | `./my_knowledge_db` | 向量库目录 |
| `CHAT_MODEL` | `qwen3.5:9b` | Ollama 聊天模型 |
| `EMBED_MODEL` | `nomic-embed-text` | Ollama Embedding 模型 |
| `OLLAMA_BASE` | `http://127.0.0.1:11434` | Ollama 地址 |

### 切分参数

- `chunk_size=500`：每段约 500 字符
- `chunk_overlap=80`：相邻段保留重叠，减少上下文被切断
- 分隔符优先按段落、换行、中文标点切开

### 问答策略

- 检索 `k=3` 条最相关片段
- 资料够用时：按知识库回答，并尽量标注 `[来源1]` 等
- 资料无关或不足时：回退到模型自身知识，开头标注 `【知识库未覆盖，以下为模型回答】`
- `ChatOllama(reasoning=False)`：关闭 qwen3.5 的深度思考，加快演示速度

## 运行时会看到什么

1. `[加载] xxx.md → N 页/段`
2. `[切分] 共 N 个片段` / `[索引] 已写入 ...`
3. `--- 检索到的片段 ---`：打印来源与内容摘要
4. `--- 回答 ---`：模型基于检索结果生成的最终答案

## 常见问题

**`docs/` 不存在或没有可读文件**  
程序会 `raise SystemExit` 并提示原因，不会继续建库。

**Ollama 连不上**  
确认服务在跑：`curl http://127.0.0.1:11434/api/tags`。

**建索引时报 `502`**  
片段多时，Ollama 冷启动或一次向量化过多会返回空 502。脚本已改为：预热模型、每批 16 段、502/503 自动重试。仍失败时先确认 `ollama list` 有 `nomic-embed-text`，并保证 Ollama 在跑。

**回答很慢**  
`qwen3.5:9b` 在 CPU 上推理较慢；可换更小模型，或保持 `reasoning=False`。

**换 Embedding 模型后检索不准**  
向量维度/语义空间会变，需要删掉 `my_knowledge_db/` 后重新建库（本脚本每次本就会重建）。

## 和菜鸟教程的差异

| 项目 | 菜鸟教程 | 本实现 |
| --- | --- | --- |
| 聊天模型 | DeepSeek API | 本机 Ollama `qwen3.5:9b` |
| Embedding | 阿里百炼 | 本机 Ollama `nomic-embed-text` |
| 问答方式 | Agent + `search_knowledge` 工具 | 固定：检索 → 生成 |
| 索引更新 | 文件哈希增量更新 | 每次清空重建（更简单） |
