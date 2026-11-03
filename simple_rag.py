"""
极简 LangChain RAG 知识库问答

流程：
  文档加载 → 文本切分 → Embedding → Chroma 向量库 → 检索 → LLM 回答

依赖本机 Ollama：
  - 聊天模型：qwen3.5:9b
  - 向量模型：nomic-embed-text
"""

import shutil
import time
from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from ollama import ResponseError

# ---------- 配置 ----------
DOCS_DIR = Path(__file__).parent / "docs"
DB_DIR = Path(__file__).parent / "my_knowledge_db"
CHAT_MODEL = "qwen3.5:9b"
EMBED_MODEL = "nomic-embed-text"
OLLAMA_BASE = "http://127.0.0.1:11434"
EMBED_BATCH_SIZE = 16  # 一次向量化太多片段时，Ollama 容易返回 502
EMBED_RETRIES = 5


def _ollama_client_kwargs():
    # 直连本机，避免系统代理把请求转走后返回空 502
    return {"trust_env": False}


def load_docs(docs_dir: Path):
    """加载 docs 目录下的 .md / .txt / .pdf"""
    docs = []
    for path in docs_dir.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            loaded = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    loaded.append(
                        Document(
                            page_content=text,
                            metadata={"source": path.name, "page": i},
                        )
                    )
        elif suffix in {".md", ".txt"}:
            loaded = [
                Document(
                    page_content=path.read_text(encoding="utf-8"),
                    metadata={"source": path.name},
                )
            ]
        else:
            continue
        docs.extend(loaded)
        print(f"[加载] {path.name} → {len(loaded)} 页/段")
    return docs


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


def build_vectorstore(docs):
    """切分文档并写入 Chroma"""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=80,
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
    )
    chunks = splitter.split_documents(docs)
    print(f"[切分] 共 {len(chunks)} 个片段")

    embeddings = OllamaEmbeddings(
        model=EMBED_MODEL,
        base_url=OLLAMA_BASE,
        client_kwargs=_ollama_client_kwargs(),
    )

    # 预热 embedding 模型，避免第一批大批量请求时冷启动 502
    print(f"[预热] 加载 Embedding 模型 {EMBED_MODEL} ...")
    for attempt in range(EMBED_RETRIES):
        try:
            embeddings.embed_query("ping")
            break
        except ResponseError as e:
            if e.status_code not in (502, 503) or attempt == EMBED_RETRIES - 1:
                raise
            wait = 2 * (attempt + 1)
            print(f"  预热返回 {e.status_code}，{wait}s 后重试 ({attempt + 1}/{EMBED_RETRIES})...")
            time.sleep(wait)

    if DB_DIR.exists():
        shutil.rmtree(DB_DIR)

    store = Chroma(
        collection_name="simple_rag",
        embedding_function=embeddings,
        persist_directory=str(DB_DIR),
    )

    total_batches = (len(chunks) + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE
    for i in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[i : i + EMBED_BATCH_SIZE]
        _add_with_retry(store, batch, i // EMBED_BATCH_SIZE + 1, total_batches)

    print(f"[索引] 已写入 {DB_DIR}")
    return store


def ask(store: Chroma, question: str, k: int = 3):
    """检索相关片段，再交给 LLM 生成回答"""
    docs = store.similarity_search(question, k=k)

    print("\n--- 检索到的片段 ---")
    context_parts = []
    for i, doc in enumerate(docs, 1):
        source = doc.metadata.get("source", "未知")
        page = doc.metadata.get("page")
        loc = f"{source}" + (f" / 第{page + 1}页" if page is not None else "")
        print(f"[{i}] {loc}\n{doc.page_content[:200]}...\n")
        context_parts.append(f"[来源{i}] {loc}\n{doc.page_content}")

    context = "\n\n".join(context_parts)

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "你是问答助手，优先使用下面「知识库资料」回答。\n"
                "规则：\n"
                "1. 若资料能回答问题：据此作答，并尽量标注来源（如[来源1]）。\n"
                "2. 若资料与问题无关、或不足以回答：不要说「不知道」，"
                "改用你自己的通用知识回答，并在开头注明："
                "【知识库未覆盖，以下为模型回答】。\n"
                "3. 不要把无关资料硬套进答案。\n\n"
                "知识库资料：\n{context}",
            ),
            ("human", "{question}"),
        ]
    )

    llm = ChatOllama(
        model=CHAT_MODEL,
        base_url=OLLAMA_BASE,
        temperature=0,
        reasoning=False,  # qwen3.5 默认会深度思考，关掉以便快速演示
        client_kwargs=_ollama_client_kwargs(),
    )
    chain = prompt | llm
    answer = chain.invoke({"context": context, "question": question})

    print("--- 回答 ---")
    print(answer.content)
    return answer.content


def main():
    import sys

    if not DOCS_DIR.exists():
        raise SystemExit(f"请先创建文档目录：{DOCS_DIR}")

    print("=== 1. 加载文档 ===")
    docs = load_docs(DOCS_DIR)
    if not docs:
        raise SystemExit("docs/ 下没有可读的 .md / .txt / .pdf")

    print("\n=== 2. 建立向量索引 ===")
    store = build_vectorstore(docs)

    # 有命令行参数时直接问答后退出；否则跑两道示例题再进入交互
    if len(sys.argv) > 1:
        ask(store, " ".join(sys.argv[1:]))
        return

    print("\n=== 3. 问答 ===")
    ask(store, "Python 基础需要学习哪些内容？")
    ask(store, "装饰器是做什么用的？")

    print("\n=== 进入交互（输入 q 退出）===")
    while True:
        q = input("\n你的问题> ").strip()
        if not q or q.lower() in {"q", "quit", "exit"}:
            break
        ask(store, q)


if __name__ == "__main__":
    main()
