"""估值助手页面服务。

运行：
    .venv/bin/python agent_server.py

浏览器打开 http://127.0.0.1:8765
模型仍使用本机 Ollama 的 qwen3.5:9b，工具调用循环与 agent_wf.py 相同。
"""

from __future__ import annotations

import json
import re
import sys
import threading

sys.stdout.reconfigure(line_buffering=True)
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ollama import Client, Message, ResponseError

from tools import TOOL_LABELS, TOOLS_FOR_MODEL, call_tool, using_sample_data

HOST = "127.0.0.1"
PORT = 8765
OLLAMA_HOST = "http://127.0.0.1:11434"
MODEL = "qwen3.5:9b"
MAX_ROUNDS = 10
ROOT = Path(__file__).resolve().parent

SYSTEM_PROMPT = (
    "你是房地产估价助手，用中文回答。价格和案例必须来自工具，不要编造。"
    "用户要求估值或估价时，必须调用 valuation，最终回答给出评估单价和总价。"
    "面积从用户原话提取，单位是平方米，「90平」就是 area=90，不能传 0。"
    "只有用户明确要查成交案例时才调用 search_cases。不要用案例列表代替估价结果。"
    "用户要求出报告时调用 generate_report。"
    "参数只填工具需要的字段。"
)

_AREA_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:平方米|平米|㎡|m²|m2|平)", re.IGNORECASE)

client = Client(host=OLLAMA_HOST, trust_env=False)


class Cancelled(Exception):
    """用户停止或浏览器断开。"""


class Session:
    def __init__(self) -> None:
        now = time.time()
        self.id = uuid.uuid4().hex[:12]
        self.title = "新对话"
        self.created_at = now
        self.updated_at = now
        self.messages: list = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.transcript: list[dict] = []
        self.busy = False
        self.cancel = threading.Event()
        self.lock = threading.Lock()


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()


def create_session() -> Session:
    session = Session()
    with SESSIONS_LOCK:
        SESSIONS[session.id] = session
    return session


def get_session(session_id: str) -> Session | None:
    with SESSIONS_LOCK:
        return SESSIONS.get(session_id)


def list_sessions() -> list[dict]:
    with SESSIONS_LOCK:
        rows = list(SESSIONS.values())
    rows.sort(key=lambda item: item.updated_at, reverse=True)
    return [public_session(item) for item in rows]


def public_session(session: Session, with_transcript: bool = False) -> dict:
    data = {
        "id": session.id,
        "title": session.title,
        "updated_at": session.updated_at,
    }
    if with_transcript:
        with session.lock:
            data["transcript"] = list(session.transcript)
    return data


def ollama_reachable() -> bool:
    request = urllib.request.Request(OLLAMA_HOST + "/api/version")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def _latest_user_text(session: Session) -> str:
    for message in reversed(session.messages):
        if _role(message) != "user":
            continue
        if isinstance(message, dict):
            return message.get("content") or ""
        return getattr(message, "content", None) or ""
    return ""


def _asks_for_valuation(text: str) -> bool:
    if not any(word in text for word in ("估值", "估价")):
        return False
    if any(word in text for word in ("出报告", "写报告", "生成报告", "一份报告")):
        return any(word in text for word in ("请估值", "做估值", "要估值"))
    return True


def _valuation_args_from_text(text: str) -> dict | None:
    """从「请估值」原话里取出地址、面积和物业类型。缺面积时返回空，交给模型追问。"""
    matched = _AREA_RE.search(text)
    if not matched:
        return None
    area = float(matched.group(1))
    if area <= 0:
        return None
    property_type = "住宅"
    for name in ("别墅", "商办", "商业"):
        if name in text:
            property_type = name
            break
    address = _AREA_RE.sub(" ", text)
    for word in (
        "请帮我", "帮我", "请做估值", "请估值", "做估值", "要估值",
        "估值", "估价", "住宅", "别墅", "商办", "商业", "面积",
    ):
        address = address.replace(word, " ")
    address = re.sub(r"[\s,，。；;、]+", " ", address).strip()
    if len(address) < 2:
        return None
    return {"address": address, "area": area, "property_type": property_type}


def _fill_valuation_args(args: dict, parsed: dict) -> dict:
    filled = dict(args)
    try:
        area = float(filled.get("area") or 0)
    except (TypeError, ValueError):
        area = 0
    if area <= 0:
        filled["area"] = parsed["area"]
    if not str(filled.get("address") or "").strip():
        filled["address"] = parsed["address"]
    if not str(filled.get("property_type") or "").strip():
        filled["property_type"] = parsed["property_type"]
    return filled


def _execute_tool(session: Session, emit, name: str, args: dict) -> bool:
    call_id = uuid.uuid4().hex[:8]
    label = TOOL_LABELS.get(name, name)
    print(f"  调用 {name}({args})")
    emit({
        "type": "tool_call",
        "call_id": call_id,
        "name": name,
        "label": label,
        "arguments": args,
    })
    emit({"type": "status", "text": f"正在调用{label}"})
    result = call_tool(name, args)
    print(f"  结果 ok={result.get('ok')} {result.get('summary') or result.get('error')}")
    session.messages.append({
        "role": "tool",
        "tool_name": name,
        "content": json.dumps(result, ensure_ascii=False),
    })
    _append_transcript(session, {
        "type": "tool",
        "call_id": call_id,
        "name": name,
        "label": label,
        "arguments": args,
        "result": result,
        "ok": bool(result.get("ok")),
    })
    emit({
        "type": "tool_result",
        "call_id": call_id,
        "name": name,
        "label": label,
        "arguments": args,
        "result": result,
        "ok": bool(result.get("ok")),
    })
    return bool(result.get("ok"))


def _inject_valuation(session: Session, emit, args: dict) -> bool:
    """模型没走估值时，补一次 valuation，避免把案例列表当成估价结果。"""
    session.messages.append(Message(
        role="assistant",
        content="",
        tool_calls=[Message.ToolCall(
            function=Message.ToolCall.Function(name="valuation", arguments=args),
        )],
    ))
    return _execute_tool(session, emit, "valuation", args)


def run_turn(session: Session, emit) -> None:
    """跑一轮用户请求。emit 在连接断开时抛出 Cancelled。"""
    committed = False
    round_msg = len(session.messages)
    round_tr = len(session.transcript)
    user_text = _latest_user_text(session)
    valuation_ok = False
    forced_valuation = False
    try:
        for iteration in range(MAX_ROUNDS):
            _ensure(session)
            round_msg = len(session.messages)
            round_tr = len(session.transcript)
            emit({
                "type": "status",
                "text": f"正在请求模型（第 {iteration + 1} 轮）",
            })
            print(f"\n[{session.id}] 第 {iteration + 1} 轮")
            response = _chat_with_retry(session, emit)
            _ensure(session)
            session.messages.append(response.message)

            tool_calls = response.message.tool_calls or []
            parsed = _valuation_args_from_text(user_text) if _asks_for_valuation(user_text) else None
            if tool_calls:
                names = [tool_call.function.name for tool_call in tool_calls]
                if parsed and "valuation" not in names and not forced_valuation:
                    session.messages.pop()
                    print(f"  用户要求估值，不执行案例查询，改为估值 {parsed}")
                    valuation_ok = _inject_valuation(session, emit, parsed)
                    forced_valuation = True
                    continue
                for tool_call in tool_calls:
                    _ensure(session)
                    name = tool_call.function.name
                    args = _normalize_args(tool_call.function.arguments)
                    if name == "valuation" and parsed:
                        args = _fill_valuation_args(args, parsed)
                    ok = _execute_tool(session, emit, name, args)
                    if name == "valuation" and ok:
                        valuation_ok = True
                continue

            if parsed and not valuation_ok and not forced_valuation:
                session.messages.pop()
                print(f"  估值尚未完成，补调用 valuation {parsed}")
                valuation_ok = _inject_valuation(session, emit, parsed)
                forced_valuation = True
                continue

            content = (response.message.content or "").strip()
            if not content:
                content = "模型没有给出文字结论。"
            _append_transcript(session, {"type": "assistant", "content": content})
            committed = True
            print("  最终回答已生成")
            emit({"type": "assistant", "content": content})
            emit({"type": "done"})
            return

        content = "已达到最大循环次数，这次没有形成最终结论。可以换一种说法再试。"
        _append_transcript(session, {"type": "assistant", "content": content})
        committed = True
        emit({"type": "assistant", "content": content})
        emit({"type": "done"})
    except Cancelled:
        if not committed:
            _rollback(session, round_msg, round_tr)
        _emit_quietly(emit, {"type": "stopped"})
    except ResponseError as exc:
        if not committed:
            _rollback(session, round_msg, round_tr)
        message = f"模型请求失败（{exc.status_code}）。可以重试。"
        _append_transcript(session, {"type": "error", "message": message})
        _emit_quietly(emit, {"type": "error", "message": message, "retry": True})
    except Exception as exc:
        if not committed:
            _rollback(session, round_msg, round_tr)
        message = f"处理失败：{exc}"
        _append_transcript(session, {"type": "error", "message": message})
        _emit_quietly(emit, {"type": "error", "message": message, "retry": True})


def _chat_with_retry(session: Session, emit):
    for attempt in range(3):
        _ensure(session)
        try:
            return client.chat(
                model=MODEL,
                messages=session.messages,
                tools=TOOLS_FOR_MODEL,
                think=False,
            )
        except ResponseError as exc:
            if exc.status_code not in (502, 503) or attempt == 2:
                raise
            print(f"  模型返回 {exc.status_code}，3 秒后重试 ({attempt + 1}/3)")
            emit({
                "type": "status",
                "text": f"模型返回 {exc.status_code}，3 秒后重试（{attempt + 1}/3）",
            })
            if session.cancel.wait(3):
                raise Cancelled() from exc
    raise RuntimeError("模型请求失败")


def _ensure(session: Session) -> None:
    if session.cancel.is_set():
        raise Cancelled()


def _rollback(session: Session, message_index: int, transcript_index: int) -> None:
    del session.messages[message_index:]
    with session.lock:
        del session.transcript[transcript_index:]


def _append_transcript(session: Session, item: dict) -> None:
    with session.lock:
        session.transcript.append(item)
        session.updated_at = time.time()


def _emit_quietly(emit, payload: dict) -> None:
    try:
        emit(payload)
    except Cancelled:
        pass


def _normalize_args(raw) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        return dict(raw)
    except (TypeError, ValueError):
        return {}


def _role(message) -> str | None:
    if isinstance(message, dict):
        return message.get("role")
    return getattr(message, "role", None)


def _can_retry(session: Session) -> bool:
    visible = [item for item in session.messages if _role(item) != "system"]
    if not visible:
        return False
    return _role(visible[-1]) in {"user", "tool"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self._path()
        if path in {"/", "/index.html"}:
            self._send_html()
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/api/health":
            self._send_json(200, {
                "ok": True,
                "model": MODEL,
                "ollama": ollama_reachable(),
                "sample_data": using_sample_data(),
            })
            return
        if path == "/api/sessions":
            self._send_json(200, {"sessions": list_sessions()})
            return
        if path.startswith("/api/sessions/"):
            session = get_session(path.removeprefix("/api/sessions/"))
            if session is None:
                self._send_json(404, {"message": "会话不存在"})
                return
            self._send_json(200, public_session(session, with_transcript=True))
            return
        self._send_json(404, {"message": "没有这个地址"})

    def do_POST(self) -> None:  # noqa: N802
        path = self._path()
        if path == "/api/sessions":
            session = create_session()
            self._send_json(201, public_session(session))
            return
        if path == "/api/chat/stop":
            self._stop()
            return
        if path == "/api/chat":
            self._chat()
            return
        self._send_json(404, {"message": "没有这个地址"})

    def _chat(self) -> None:
        try:
            body = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"message": str(exc)})
            return

        session = get_session(str(body.get("session_id") or ""))
        if session is None:
            self._send_json(404, {"message": "会话不存在"})
            return

        retry = bool(body.get("retry"))
        message = str(body.get("message") or "").strip()
        if not retry and not message:
            self._send_json(400, {"message": "请输入评估需求"})
            return
        if not retry and len(message) > 4000:
            self._send_json(400, {"message": "输入超过 4000 字"})
            return

        with session.lock:
            if session.busy:
                self._send_json(409, {"message": "上一条请求还在处理"})
                return
            session.busy = True
            session.cancel.clear()
            if retry and session.transcript and session.transcript[-1].get("type") == "error":
                session.transcript.pop()

        if retry and not _can_retry(session):
            with session.lock:
                session.busy = False
            self._send_json(400, {"message": "没有可以重试的请求"})
            return

        if not retry:
            session.messages.append({"role": "user", "content": message})
            _append_transcript(session, {"type": "user", "content": message})
            self._maybe_title(session, message)

        started = False
        try:
            self._start_sse()
            started = True
            self._emit({
                "type": "session",
                "id": session.id,
                "title": session.title,
            })
            run_turn(session, lambda payload: self._emit(payload))
        except Cancelled:
            pass
        finally:
            if started:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass
            with session.lock:
                session.busy = False

    def _stop(self) -> None:
        try:
            body = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"message": str(exc)})
            return
        session = get_session(str(body.get("session_id") or ""))
        if session is None:
            self._send_json(404, {"message": "会话不存在"})
            return
        session.cancel.set()
        self._send_json(200, {"ok": True})

    def _maybe_title(self, session: Session, message: str) -> None:
        if session.title != "新对话":
            return
        compact = " ".join(message.split())
        session.title = compact[:18] + ("…" if len(compact) > 18 else "")

    def _start_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _emit(self, payload: dict) -> None:
        try:
            data = b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n"
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise Cancelled() from exc

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 1_000_000:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("请求不是 JSON 对象")
        return data

    def _send_html(self) -> None:
        body = (ROOT / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _path(self) -> str:
        path = self.path.split("?", 1)[0]
        if path != "/":
            path = path.rstrip("/")
        return path

    def log_message(self, fmt: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


def main() -> None:
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"估值助手已启动：http://{HOST}:{PORT}")
    print(f"模型：{MODEL} @ {OLLAMA_HOST}")
    if using_sample_data():
        print("查询案例、估值、出报告目前使用示例数据。在 tools.py 顶部填入地址后改为真实请求。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.server_close()


if __name__ == "__main__":
    main()
