"""API 队员：不用装任何命令行工具，直接拿钱包里的账户调模型干活。

一个很小的 agent 循环：模型可以列目录、读文件、写文件（只限项目文件夹），最多 30 轮。
适合文案、改稿、翻译、总结、审稿这类文字活；不能操控电脑、不能跑命令。
会话存在 ~/.agentbus/api-sessions/<会话>.json，下次派活能续上。
"""
import http.client
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import wallet
from config import HOME

SESS = HOME / "api-sessions"
MAX_STEPS = 30
MAX_READ = 60_000

TOOLS = [
    {"type": "function", "function": {"name": "list_dir", "description": "列出项目里某个文件夹的内容（路径相对项目根目录，空字符串 = 根目录）",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "读项目里的一个文本文件（相对路径）。很长的文件用 offset 分段读",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}},
                                                     "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "在项目里写一个文本文件（相对路径，会覆盖同名文件，文件夹不存在会自动建）",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                                                     "required": ["path", "content"]}}},
]
PROPOSE = {"type": "function", "function": {"name": "propose_lesson", "description": "干活中发现以后值得照做的经验（踩过的坑、主人新定的规矩），提议写进项目经验包；主人采纳后才生效",
                                       "parameters": {"type": "object", "properties": {"text": {"type": "string", "description": "一句话经验"},
                                                                                       "tags": {"type": "array", "items": {"type": "string"}, "description": "相关岗位词条"}},
                                                      "required": ["text"]}}}
SYSTEM = ("你是 agentbus 里的 API 队员，通过工具读写项目文件夹里的文件来完成任务。"
          "只能用给你的工具；不能运行命令、不能上网、不能操控电脑，需要这些就在最终回复里说明。"
          "提示里说的 `agentbus lesson propose` 命令你用 propose_lesson 工具代替。"
          "做完后直接给出最终回复（不再调用工具）。")


class ApiError(Exception):
    pass


def _events(response, deadline):
    """SSE data can span lines; keepalives and other SSE fields are not model output."""
    data = []
    for line in response:
        if time.monotonic() >= deadline:
            raise TimeoutError()
        line = line.rstrip(b"\r\n")
        if line.startswith(b"data:"):
            data.append(line[5:].lstrip(b" "))
        elif not line and data:
            yield b"\n".join(data)
            data = []
    # An event is only committed on a blank line; EOF can cut a tool argument in half.


def _stream_reply(response, deadline):
    content, reasoning, calls, usage = [], [], {}, {}
    finished, done = None, False
    for payload in _events(response, deadline):
        if payload == b"[DONE]":
            done = True
            break
        frame = json.loads(payload)
        if frame.get("error"):
            raise ApiError("中转在返回流中报告错误，回复未完整；未自动重发。")
        if frame.get("usage"):
            usage = frame["usage"]
        for choice in frame.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
            for chunk in delta.get("tool_calls") or []:
                i = chunk.get("index", 0)
                call = calls.setdefault(i, {"type": "function", "function": {"name": "", "arguments": ""}})
                if chunk.get("id"):
                    call["id"] = chunk["id"]
                if chunk.get("extra_content"):  # Keep Google's per-call thought signature on the next request.
                    call.setdefault("extra_content", {}).update(chunk["extra_content"])
                for key in ("name", "arguments"):
                    call["function"][key] += (chunk.get("function") or {}).get(key) or ""
            if choice.get("finish_reason"):
                finished = choice["finish_reason"]
    if finished not in ("stop", "tool_calls") and not (finished is None and done and not calls):
        raise ApiError("中转回复未完整（未收到正常结束标记），已停止，未自动重发。")
    msg = {"role": "assistant", "content": "".join(content)}
    if reasoning and calls:
        msg["reasoning_content"] = "".join(reasoning)
    if calls:
        msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        for call in calls.values():
            try:
                valid = call.get("id") and call["function"]["name"] and isinstance(json.loads(call["function"]["arguments"]), dict)
            except ValueError:
                valid = False
            if not valid:
                raise ApiError("中转返回的工具调用未完整，未执行工具，未自动重发。")
    if not calls and not msg["content"].strip():
        raise ApiError("中转结束了回复，但没有返回正文或工具调用。")
    return {"choices": [{"message": msg}], "usage": usage}


def _safe(root, rel):
    p = (Path(root) / (rel or "")).resolve()
    r = Path(root).resolve()
    if p != r and r not in p.parents:
        raise ValueError("只能访问项目文件夹里的文件")
    return p


def _tool(root, name, args, bus=None, me=""):
    try:
        if name == "propose_lesson" and bus:
            import playbook
            return "已提议（" + playbook.propose(bus, args.get("text", ""), args.get("tags") or [], "本项目", me) + "），等主人采纳"
        if name == "list_dir":
            p = _safe(root, args.get("path", ""))
            items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name))[:300]
            return "\n".join(("📁 " if x.is_dir() else "") + x.name for x in items if not x.name.startswith(".")) or "（空）"
        if name == "read_file":
            p = _safe(root, args["path"])
            t = p.read_text("utf-8", "ignore")
            off = int(args.get("offset") or 0)
            chunk = t[off:off + MAX_READ]
            more = f"\n\n……（还有 {len(t) - off - MAX_READ} 字，用 offset={off + MAX_READ} 接着读）" if len(t) > off + MAX_READ else ""
            return chunk + more
        if name == "write_file":
            p = _safe(root, args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args.get("content", ""), "utf-8")
            return f"已写入 {p}（{len(args.get('content', ''))} 字）"
    except Exception as e:
        return f"出错：{e}"
    return "没有这个工具"


def _post(base, key, body, timeout):
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "agentbus"}
    if body.get("stream"):
        headers["Accept"] = "text/event-stream, application/json"
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers=headers)
    phase = "等待响应头"
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            phase = "接收回复"
            if r.headers.get_content_type() == "text/event-stream":
                return _stream_reply(r, started + timeout)
            return json.load(r)
    except urllib.error.HTTPError as e:
        with e:
            raise ApiError(f"HTTP {e.code}：{e.read(400).decode('utf-8', 'ignore')[:400]}")
    except (OSError, http.client.HTTPException) as e:
        raise ApiError(f"{phase}时连接失败（{type(e).__name__}，{time.monotonic() - started:.0f} 秒）。"
                       "本轮未获得完整回包，中转可能已计费；未自动重发。") from e
    except (UnicodeError, ValueError) as e:
        raise ApiError("中转回包不是有效的对话数据；未自动重发。") from e


def run(account, model, prompt, root, session=None, effort=None, timeout=600, on_step=None, bus=None, me=""):
    """返回 (回复, 会话 id, 原始记录, token 用量)。"""
    key = wallet.key_of(account)
    acc = wallet.accounts().get(account)
    if not key or not acc:
        raise ApiError(f"钱包里的账户 {account} 读不到 key")
    SESS.mkdir(parents=True, exist_ok=True)
    sid = session or "api-" + uuid.uuid4().hex[:12]
    sf = SESS / f"{sid}.json"
    msgs = json.loads(sf.read_text("utf-8")) if session and sf.exists() else [{"role": "system", "content": SYSTEM}]
    msgs.append({"role": "user", "content": prompt})
    tok = {"input": 0, "output": 0, "cache": 0}
    log = []
    t_end = time.time() + timeout
    text = ""
    for step in range(MAX_STEPS):
        has_pb = bool(bus) and (Path(bus) / "playbook" / "manifest.json").exists()
        body = {"model": model, "messages": msgs, "tools": TOOLS + ([PROPOSE] if has_pb else [])}
        if model.rsplit("/", 1)[-1].lower().startswith("gemini-"):
            body.update(stream=True, stream_options={"include_usage": True})
        if effort:
            body["reasoning_effort"] = effort
        res = _post(acc["base"], key, body, max(30, t_end - time.time()))
        u = res.get("usage") or {}
        cached = u.get("prompt_cache_hit_tokens") or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        tok["input"] += (u.get("prompt_tokens") or 0) - cached
        tok["cache"] += cached
        tok["output"] += u.get("completion_tokens") or 0
        m = ((res.get("choices") or [{}])[0]).get("message") or {}
        keep = {"role": "assistant", "content": m.get("content") or ""}
        if m.get("tool_calls"):
            keep["tool_calls"] = m["tool_calls"]
        if m.get("reasoning_content") and m.get("tool_calls"):  # 有的模型要求带回思考内容
            keep["reasoning_content"] = m["reasoning_content"]
        msgs.append(keep)
        log.append({"step": step, "content": keep["content"][:2000], "tools": [c["function"]["name"] for c in m.get("tool_calls") or []]})
        if not m.get("tool_calls"):
            text = keep["content"].strip()
            break
        for c in m["tool_calls"]:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            out = _tool(root, c["function"]["name"], args, bus, me)
            if on_step:
                on_step(c["function"]["name"], args.get("path", ""))
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out[:MAX_READ]})
        if time.time() > t_end:
            text = "（超时，已停止；可能已经写了一部分文件）"
            break
    else:
        text = text or f"（{MAX_STEPS} 轮还没做完，已停止）"
    # 存会话：工具结果太长的截短，免得越滚越大
    for x in msgs:
        if x.get("role") == "tool" and len(x.get("content", "")) > 4000:
            x["content"] = x["content"][:4000] + "……（已截短）"
    sf.write_text(json.dumps(msgs, ensure_ascii=False), "utf-8")
    return text, sid, "\n".join(json.dumps(x, ensure_ascii=False) for x in log), tok
