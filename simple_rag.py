"""
极简 LangChain RAG 知识库问答

流程：
  文档加载 → 文本切分 → Embedding → Chroma 向量库 → 检索 → LLM 回答

依赖本机 Ollama：
  - 聊天模型：qwen3.5:9b
  - 向量模型：nomic-embed-text
"""

from pathlib import Path

from langchain_chroma import Chroma
from langchain_community.document_loaders import TextLoader, PyPDFLoader
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ---------- 配置 ----------
DOCS_DIR = Path(__file__).parent / "docs"
DB_DIR = Path(__file__).parent / "my_knowledge_db"
CHAT_MODEL = "qwen3.5:9b"
EMBED_MODEL = "nomic-embed-text"
OLLAMA_BASE = "http://127.0.0.1:11434"


def load_docs(docs_dir: Path):
    """加载 docs 目录下的 .md / .txt / .pdf"""
    docs = []
    for path in docs_dir.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            loader = PyPDFLoader(str(path))
        elif suffix in {".md", ".txt"}:
            loader = TextLoader(str(path), encoding="utf-8")
        else:
            continue
        loaded = loader.load()
        for d in loaded:
            d.metadata["source"] = path.name
        docs.extend(loaded)
        print(f"[加载] {path.name} → {len(loaded)} 页/段")
    return docs


def build_vectorstore(docs):
    """切分文档并写入 Chroma"""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=80,
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
    )
    chunks = splitter.split_documents(docs)
    print(f"[切分] 共 {len(chunks)} 个片段")

    embeddings = OllamaEmbeddings(model=EMBED_MODEL, base_url=OLLAMA_BASE)
    # 每次重建，保证演示简单可复现
    if DB_DIR.exists():
        import shutil
        shutil.rmtree(DB_DIR)

    store = Chroma.from_documents(
        documents=chunks,
        embedding=embeddings,
        persist_directory=str(DB_DIR),
        collection_name="simple_rag",
    )
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
                "你是知识库问答助手。只根据下面提供的资料回答，不要编造。"
                "资料不足时明确说不知道。尽量标注来源（如[来源1]）。\n\n资料：\n{context}",
            ),
            ("human", "{question}"),
        ]
    )

    llm = ChatOllama(
        model=CHAT_MODEL,
        base_url=OLLAMA_BASE,
        temperature=0,
        reasoning=False,  # qwen3.5 默认会深度思考，关掉以便快速演示
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
