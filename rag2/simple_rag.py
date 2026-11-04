"""
小区名称模糊搜索 → 返回 JSON

思路：
  1. xiaoqu.txt 一行一条记录（不做 500 字大块切分）
  2. 关键词 / 模糊匹配为主（适合按名称查找）
  3. 向量检索为辅（语义补充），结果按 id 去重合并
  4. 直接组装 JSON，不依赖 LLM「猜」结构化字段

依赖本机 Ollama（仅向量混合检索时需要）：
  - 向量模型：nomic-embed-text
"""

from __future__ import annotations

import json
import re
import shutil
import time
from difflib import SequenceMatcher
from pathlib import Path

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_ollama import OllamaEmbeddings
from ollama import ResponseError

# ---------- 配置 ----------
DOCS_DIR = Path(__file__).parent / "docs"
XIAOQU_FILE = DOCS_DIR / "xiaoqu.txt"
DB_DIR = Path(__file__).parent / "my_knowledge_db"
EMBED_MODEL = "nomic-embed-text"
OLLAMA_BASE = "http://127.0.0.1:11434"
EMBED_BATCH_SIZE = 16
EMBED_RETRIES = 5
COLLECTION = "xiaoqu_line"

# 从自然语言问句里剥掉的噪声词，便于抽出小区名关键字
QUERY_NOISE = re.compile(
    r"(找到|查找|搜索|查询|帮我|请|一下|小区|社区|的数据|的信息|"
    r"并以?json格式返回|以json格式返回|json格式|json|格式|返回|给我)",
    re.I,
)


def _ollama_client_kwargs():
    return {"trust_env": False}


def _make_embeddings():
    return OllamaEmbeddings(
        model=EMBED_MODEL,
        base_url=OLLAMA_BASE,
        client_kwargs=_ollama_client_kwargs(),
    )


# ---------- 数据加载：一行一条 ----------
def parse_xiaoqu(path: Path) -> list[dict]:
    """解析小区文件 → [{id, name, city}, ...]"""
    records = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("下面是") or line.startswith("id"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            parts = re.split(r"\s{2,}|\t", line)
        if len(parts) < 3:
            continue
        cid, name, city = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not cid.isdigit() or not name:
            continue
        records.append(
            {
                "id": cid,
                "name": name,
                "city": city,
                "line_no": line_no,
                "source": path.name,
            }
        )
    return records


def records_to_documents(records: list[dict]) -> list[Document]:
    """一行一条 Document，page_content 突出名称便于向量检索。"""
    docs = []
    for r in records:
        docs.append(
            Document(
                page_content=f"小区名称：{r['name']}\nid：{r['id']}\n城市：{r['city']}",
                metadata={
                    "id": r["id"],
                    "name": r["name"],
                    "city": r["city"],
                    "source": r["source"],
                },
            )
        )
    return docs


# ---------- 关键词 / 模糊匹配 ----------
def extract_keyword(question: str) -> str:
    """从问句中抽出近似小区名的关键字。"""
    q = question.strip()
    q = QUERY_NOISE.sub(" ", q)
    q = re.sub(r"[，,。.!！？?、\s]+", " ", q).strip()
    return q or question.strip()


def fuzzy_search(records: list[dict], question: str, top_k: int = 5) -> list[tuple[dict, float]]:
    """
    按名称模糊搜：
      - 关键字是名称子串 / 名称是问句子串 → 高分
      - 否则用 SequenceMatcher 相似度
    """
    keyword = extract_keyword(question)
    scored: list[tuple[dict, float]] = []

    for r in records:
        name = r["name"]
        if keyword and keyword in name:
            # 子串命中：名称越接近关键字，分越高
            score = 1.0 - 0.15 * (len(name) - len(keyword)) / max(len(name), 1)
            score = max(score, 0.85)
        elif name and name in question:
            score = 0.95
        else:
            score = SequenceMatcher(None, keyword, name).ratio()
            # 字符 bigram 重叠：至少 60% 才采纳，避免单靠「胡同」误伤
            if len(keyword) >= 2 and len(name) >= 2:
                kw_bi = {keyword[i : i + 2] for i in range(len(keyword) - 1)}
                nm_bi = {name[i : i + 2] for i in range(len(name) - 1)}
                overlap = len(kw_bi & nm_bi) / len(kw_bi) if kw_bi else 0.0
                if overlap >= 0.6:
                    score = max(score, overlap)

        if score >= 0.55:
            scored.append((r, score))

    scored.sort(key=lambda x: (-x[1], len(x[0]["name"])))
    return scored[:top_k]


# ---------- 向量库：一行一条 ----------
def _add_with_retry(store: Chroma, batch: list[Document], batch_no: int, total: int):
    for attempt in range(EMBED_RETRIES):
        try:
            store.add_documents(batch)
            print(f"[索引] 批次 {batch_no}/{total}（{len(batch)} 条）完成")
            return
        except ResponseError as e:
            if e.status_code not in (502, 503) or attempt == EMBED_RETRIES - 1:
                raise
            wait = 2 * (attempt + 1)
            print(f"  Embedding 返回 {e.status_code}，{wait}s 后重试 ({attempt + 1}/{EMBED_RETRIES})...")
            time.sleep(wait)


def load_vectorstore() -> Chroma:
    store = Chroma(
        collection_name=COLLECTION,
        embedding_function=_make_embeddings(),
        persist_directory=str(DB_DIR),
    )
    print(f"[索引] 复用已有向量库：{DB_DIR}（{store._collection.count()} 条）")
    return store


def build_vectorstore(docs: list[Document]) -> Chroma:
    print(f"[索引] 一行一条，共 {len(docs)} 条待写入")
    embeddings = _make_embeddings()

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
        collection_name=COLLECTION,
        embedding_function=embeddings,
        persist_directory=str(DB_DIR),
    )

    total_batches = (len(docs) + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE
    for i in range(0, len(docs), EMBED_BATCH_SIZE):
        batch = docs[i : i + EMBED_BATCH_SIZE]
        _add_with_retry(store, batch, i // EMBED_BATCH_SIZE + 1, total_batches)

    print(f"[索引] 已写入 {DB_DIR}")
    return store


def vector_search(store: Chroma | None, question: str, top_k: int = 5) -> list[tuple[dict, float]]:
    if store is None:
        return []
    keyword = extract_keyword(question)
    # 用短关键字检索，比整句「找到…json」更接近入库文本
    query = keyword or question
    try:
        pairs = store.similarity_search_with_score(query, k=top_k)
    except Exception as e:
        print(f"[向量] 检索失败（可忽略，仍用模糊匹配）：{e}")
        return []

    out = []
    for doc, dist in pairs:
        # Chroma/L2：距离越小越相似 → 转成 0~1 分
        score = 1.0 / (1.0 + float(dist))
        out.append(
            (
                {
                    "id": str(doc.metadata.get("id", "")),
                    "name": str(doc.metadata.get("name", "")),
                    "city": str(doc.metadata.get("city", "")),
                    "source": str(doc.metadata.get("source", "")),
                },
                score,
            )
        )
    return out


# ---------- 混合检索 + JSON ----------
def hybrid_search(
    records: list[dict],
    store: Chroma | None,
    question: str,
    top_k: int = 5,
) -> list[dict]:
    fuzzy_hits = fuzzy_search(records, question, top_k=top_k * 2)
    vector_hits = vector_search(store, question, top_k=top_k)

    print(f"[检索] 关键字={extract_keyword(question)!r}")
    print(f"[检索] 模糊命中 {len(fuzzy_hits)}，向量命中 {len(vector_hits)}")

    # 模糊权重更高：名称查找场景更可信
    merged: dict[str, dict] = {}
    for r, score in vector_hits:
        if not r["id"]:
            continue
        merged[r["id"]] = {
            "id": r["id"],
            "name": r["name"],
            "city": r["city"],
            "score": round(0.35 * score, 4),
            "match": "vector",
        }
    for r, score in fuzzy_hits:
        prev = merged.get(r["id"])
        fuzzy_score = 0.65 * score
        if prev:
            total = round(prev["score"] + fuzzy_score, 4)
            merged[r["id"]] = {
                "id": r["id"],
                "name": r["name"],
                "city": r["city"],
                "score": total,
                "match": "hybrid",
            }
        else:
            merged[r["id"]] = {
                "id": r["id"],
                "name": r["name"],
                "city": r["city"],
                "score": round(fuzzy_score, 4),
                "match": "fuzzy",
            }

    ranked = sorted(merged.values(), key=lambda x: -x["score"])
    # 丢掉明显弱相关（例如仅向量擦边、或模糊分过低）
    ranked = [h for h in ranked if h["score"] >= 0.4]
    return ranked[:top_k]


def search_as_json(
    records: list[dict],
    store: Chroma | None,
    question: str,
    top_k: int = 5,
) -> str:
    hits = hybrid_search(records, store, question, top_k=top_k)
    payload = {
        "query": question,
        "keyword": extract_keyword(question),
        "count": len(hits),
        "results": [
            {"id": h["id"], "name": h["name"], "city": h["city"]} for h in hits
        ],
        "debug": [
            {"id": h["id"], "name": h["name"], "score": h["score"], "match": h["match"]}
            for h in hits
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print("\n--- JSON ---")
    print(text)
    return text


def main():
    import sys

    rebuild = "--rebuild" in sys.argv
    no_vector = "--no-vector" in sys.argv
    args = [a for a in sys.argv[1:] if a not in {"--rebuild", "--no-vector"}]

    if not XIAOQU_FILE.exists():
        raise SystemExit(f"缺少小区数据文件：{XIAOQU_FILE}")

    print("=== 1. 加载小区数据（一行一条）===")
    records = parse_xiaoqu(XIAOQU_FILE)
    print(f"[加载] {XIAOQU_FILE.name} → {len(records)} 条小区")
    if not records:
        raise SystemExit("未解析到任何小区记录")

    store = None
    if no_vector:
        print("=== 2. 跳过向量库（仅模糊匹配）===")
    else:
        need_build = rebuild or not DB_DIR.exists()
        if not need_build:
            print("=== 2. 加载已有向量库 ===")
            store = load_vectorstore()
            if store._collection.count() == 0:
                print("[索引] 集合为空（可能是旧库），改为重建一行一条索引")
                need_build = True
                store = None
        if need_build:
            print("=== 2. 建立向量索引（一行一条）===")
            docs = records_to_documents(records)
            store = build_vectorstore(docs)

    question = " ".join(args) if args else "找到 侯位胡同 小区，并以json格式返回"

    print("\n=== 3. 模糊 + 向量混合检索 ===")
    search_as_json(records, store, question)

    if args:
        return

    print("\n=== 进入交互（输入 q 退出；可用 --no-vector 跳过向量）===")
    while True:
        q = input("\n你的问题> ").strip()
        if not q or q.lower() in {"q", "quit", "exit"}:
            break
        search_as_json(records, store, q)


if __name__ == "__main__":
    main()
