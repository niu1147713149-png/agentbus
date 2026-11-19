#!/usr/bin/env python3
"""agentbus：本机 agent 之间派活、传话、记账的小总线。

  agentbus ask codex "任务"            派活给 codex，等它做完，打印回复（同一项目自动接着上次的会话）
       --new 开新会话  --file 任务书  --tag 封面（记到这个岗位下，以后好比较谁干得好）
  agentbus ask 封面 "任务"             不写 agent 写岗位：按偏好自动挑首选的 agent
  agentbus post claude "消息"          给某个 agent 留言（--from 写自己是谁）
  agentbus inbox codex                 看给 codex 的未读留言
  agentbus board [-n 30]               看项目任务板最近记录
  agentbus agents                      列出所有 agent、装没装、在办公室还是休息室
  agentbus who 封面                    这个岗位谁来干（按偏好排）
  agentbus suggest 封面                各 agent 干这类活的历史：次数、用时、花费，给调度做参考
  agentbus assign codex 视觉设计 prefer  给 agent 挂岗位（大类或词条；prefer 首选 / ok 可用 / avoid 不用 / clear 去掉）
  agentbus move zcode lounge           把 agent 挪到休息室（office 办公室）
  agentbus quota                       现在的真实额度：GPT 百分比、DeepSeek 余额
  agentbus bill [--batch 名]           本项目的账单（每个 agent 真实花了多少）
  agentbus batch 项目-1111             设定当前批次，之后派的活都记在这个批次下
  agentbus project list|use 名字|new 名字   项目：每个项目有自己的阵容、职能和项目说明；use 无 = 通用

项目登记在 ~/.agentbus/projects.json：{名字: {"root": 项目目录, "bus": 总线目录}}。
总线目录里放 brief.md（所有 agent 共用的项目说明）、roles/<agent>.md（各自分工）、
board.jsonl（任务板）、sessions.json（各 agent 的会话 id）、runs/（每次派活的完整记录）。
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quota  # noqa: E402
from catalog import CATEGORIES, LEVELS  # noqa: E402
from config import (HOME, update_json, agents_dict, bill, doubao_remote, installed, ledger_add, ledger_read, load_config,  # noqa: E402
                    load_json, now, project, resolve_tag, save_config, save_json, set_batch, who)

MAX_DEPTH = 2  # agent 再派活给别的 agent 最多两层，防止互相踢皮球
RUNNING = HOME / "running"


def conv_id():
    """协同会话：哪一个主位窗口在派活。Claude 每个对话有自己的 CLAUDE_CODE_SESSION_ID；
    agentbus 派出去的 agent 再派活时会带上 AGENTBUS_CONV，归到同一个协同会话。"""
    e = os.environ
    return e.get("AGENTBUS_CONV") or e.get("CLAUDE_CODE_SESSION_ID") or e.get("CODEX_THREAD_ID") or e.get("CODEX_SESSION_ID") or ""


def sess_key(agent_id, conv):
    # 同一个 agent 在不同窗口里各续各的会话；没有窗口信息时退回老规矩（按 agent）
    return f"{agent_id}@{conv[:12]}" if conv else agent_id


def short_title(task, limit=26):
    """从任务原文提炼一句短标题（悬浮窗胶囊用）：去掉「按附带任务书…」这类开头和 markdown，取第一句，按显示宽度截短（中文算 2）。"""
    import re as _re
    t = str(task or "").strip()
    t = _re.sub(r"^【[^】]{0,40}】", "", t)  # 【agentbus 聊天室 · 频道…】
    t = _re.sub(r"^.{0,24}?(在群里找你|在群里 @ 了你)[：:]\s*", "", t)  # 聊天室派活：标题用消息本身
    t = _re.split(r"\n\s*#|\n\n", t, maxsplit=1)[0]  # 后面附的任务书正文不要
    t = _re.sub(r"^(这是[^：:]{0,20}[：:]\s*)", "", t)
    t = _re.sub(r"^(请|麻烦)?(你)?(按|根据|照)(附带|附件|附上)?(的)?(最新)?(的)?(任务书|说明|要求)(里的要求)?[，,：:、\s]*", "", t)
    t = _re.sub(r"^(帮我|请|麻烦你?)\s*", "", t)
    t = _re.sub(r"[`*#>]+", "", t).replace("\n", " ").strip()
    first = _re.split(r"[。；;！!？?\n]", t, maxsplit=1)[0].strip() or t
    if sum(2 if ord(c) > 0x2e80 else 1 for c in first) > limit:  # 还太长：先试第一个逗号前
        head = _re.split(r"[，,：:]", first, maxsplit=1)[0].strip()
        if sum(2 if ord(c) > 0x2e80 else 1 for c in head) >= 8:
            first = head
    out, w = "", 0
    for c in first:
        w += 2 if ord(c) > 0x2e80 else 1
        if w > limit:
            return out.rstrip("，,、 ") + "…"
        out += c
    return out


def board_add(bus, **rec):
    rec = {"t": now(), **rec}
    with open(bus / "board.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def board_read(bus):
    p = bus / "board.jsonl"
    if not p.exists():
        return []
    out = []
    for l in p.read_text("utf-8").splitlines():
        try:
            out.append(json.loads(l))
        except json.JSONDecodeError:
            pass
    return out


def build_prompt(agent, name, root, bus, task, sender, pb_block=""):
    parts = [f"# 你是 {agent}，在「{name}」项目里接活，派活的是 {sender}"]
    for f in (bus / "brief.md", bus / "roles" / f"{agent}.md"):
        if f.exists():
            parts.append(f.read_text("utf-8").strip())
    if pb_block:
        parts.append(pb_block)
    unread = inbox(bus, agent, mark=True)
    if unread:
        parts.append("## 给你的留言\n" + "\n".join(f"- [{m['t']}] {m['frm']}：{m['text']}" for m in unread))
    team_cfg = load_config(name)
    team = agents_dict(team_cfg)
    others = "、".join(a for a in team_cfg["roster"]["office"] if a != agent and team.get(a, {}).get("run") and not team[a].get("hidden"))
    import playbook as _pb
    pb_on = (bus / "playbook" / "manifest.json").exists() and _pb.equip_of(load_config(), name)["enabled"]
    lesson_hint = ("\n- 干活中发现以后值得照做的经验（踩过的坑、主人新定的规矩）：`agentbus lesson propose \"一句话经验\" --tags 岗位 --from "
                   + agent + "`，主人在「经验」页采纳后才会生效") if pb_on else ""
    parts.append(f"""## 协作方式
- 项目目录：{root}
- 卡住了、要别人拍板，或者想把一块杂活转给别的 agent：`agentbus ask <agent> "..."`（agent 可选：{others}）；只留言不等回复：`agentbus post <agent> "..." --from {agent}`
- 要用户本人做的事（登录、付款、手机实拍）不要硬做，在最终回复里写清楚{lesson_hint}
- 最终回复只写：做了什么、产出文件的完整路径、没做完/有疑问的地方。不要客套""")
    parts.append("## 这次的任务\n" + task)
    return "\n\n".join(parts)


def _events(stdout):
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                yield event
        except json.JSONDecodeError:
            continue


def load_json_str(s):
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return next((e for e in reversed(list(_events(s))) if e.get("type") == "result"), {})


def _event_progress(event):
    """只展示可核验的阶段和工具名，不展示模型的内部推理内容或工具参数。"""
    kind = event.get("type")
    if kind == "thread.started" or (kind == "system" and event.get("subtype") == "init"):
        return "会话已连接，等待模型响应"
    if kind == "turn.started":
        return "模型已开始处理任务"
    if kind == "stream_event":
        inner = event.get("event") or {}
        if inner.get("type") == "content_block_start":
            block = inner.get("content_block") or {}
            if block.get("type") == "tool_use":
                return "正在使用工具：" + str(block.get("name") or "工具")[:60]
            return "模型正在生成回复" if block.get("type") == "text" else "模型正在处理任务"
        if inner.get("type") == "content_block_delta":
            return "模型正在生成回复" if (inner.get("delta") or {}).get("type") == "text_delta" else "模型正在处理任务"
    if kind == "assistant":
        blocks = (event.get("message") or {}).get("content") or []
        tools = [str(b.get("name") or "工具")[:60] for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        return "正在使用工具：" + "、".join(tools) if tools else "模型已返回一段回复"
    if kind == "user" and any(b.get("type") == "tool_result" for b in (event.get("message") or {}).get("content", []) if isinstance(b, dict)):
        return "工具已返回，继续处理任务"
    if kind in ("item.started", "item.completed"):
        item = event.get("item") or {}
        labels = {"command_execution": "执行命令", "file_change": "修改文件", "mcp_tool_call": "调用 MCP 工具", "web_search": "搜索网页"}
        if item.get("type") in labels:
            return labels[item["type"]] + ("完成" if kind == "item.completed" else "中")
        return "模型已返回一段回复" if item.get("type") == "agent_message" else "模型正在处理任务"
    if kind in ("result", "turn.completed"):
        return "模型已返回，正在保存结果"
    if kind in ("error", "turn.failed"):
        return "执行器报告错误，正在收尾"
    return None


def _save_progress(detail):
    if detail and _CUR.get("running"):
        try:
            update_json(_CUR["running"], lambda rec: rec.update(progress={"detail": detail, "updated": time.time()}))
        except (OSError, TimeoutError):
            pass  # 进度只是观测信息，短时文件占用不能终止正在执行的 Agent。


class Stalled(Exception):
    """agent 太久没动静（没有新输出、自己的会话日志也不再更新）。"""


# 只在结束时才输出结果的 agent，用它自己的会话日志判断还活着没
# 看门狗除了看 stdout，还看这次派活自己的会话日志在不在动（只认这次开工后新建的、或者续的那个会话的文件，
# 不能用整个目录——主位自己的对话也在往那里写，会把卡死的活一直“续命”）
ACTIVITY_LOGS = {"zcode": "~/.zcode/cli/rollout/model-io-*.jsonl", "codex": "~/.codex/sessions/*/*/*/rollout-*.jsonl"}


def watchdog_cfg():
    w = load_config().get("watchdog") or {}
    return float(os.environ.get("AGENTBUS_IDLE_MIN") or w.get("idle_min", 10)), int(os.environ.get("AGENTBUS_RETRIES") or w.get("retries", 1))


def _own_log_probe(kind, session, t0):
    """返回一个函数：这次派活自己的会话日志最近一次更新的时间（没有就 0）。"""
    import glob
    pattern = ACTIVITY_LOGS.get(kind)
    if not pattern:
        return lambda: 0
    mine = set()

    def probe():
        for f in glob.glob(os.path.expanduser(pattern)):
            if f in mine:
                continue
            try:
                st = os.stat(f)
            except OSError:
                continue
            born = getattr(st, "st_birthtime", st.st_ctime)
            if (session and session in os.path.basename(f)) or born >= t0 - 2:
                mine.add(f)
        return max((os.path.getmtime(f) for f in mine if os.path.exists(f)), default=0)
    return probe


def pf_is_win():
    import pf
    return pf.IS_WIN


class _NoStart(Exception):
    """排完队发现不能开工（比如额度用完会扣付费 credits）：不跑，直接收尾写回复。"""


_CUR = {}  # 这次派活的 running 记录路径（_run 往里登记执行器进程）


def _sigterm(*_):
    raise KeyboardInterrupt  # 被喊停（SIGTERM）：走和 Ctrl+C 一样的收尾，停掉执行器、写“被停掉了”的回复


def _run(kind, cmd, input=None, cwd=None, env=None, capture_output=True, text=True, session=None):
    """像 subprocess.run 一样跑 agent，但边跑边看它有没有动静：超过 idle_min 分钟没有任何新输出，
    自己的会话日志也没更新，就整组杀掉、抛 Stalled，由 cmd_ask 开新会话重试。"""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="agentbus-run-") as directory:
        with open(Path(directory) / "stdout", "w+", encoding="utf-8") as out, open(Path(directory) / "stderr", "w+", encoding="utf-8") as err:
            return _run_captured(kind, cmd, input, cwd, env, session, out, err)


def _run_captured(kind, cmd, input, cwd, env, session, out, err):
    import pf
    idle_min, _ = watchdog_cfg()
    cmd = pf.resolve(cmd)
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                         stdout=out, stderr=err, text=True, encoding="utf-8", **pf.group_kwargs())
    try:  # 从起进程那一刻就在保护里：登记途中被喊停也会把执行器整组停掉
        if _CUR.get("running"):  # 登记实际执行器的进程组：停止时连它一起停（它和外层不在同一组）
            try:
                rec = update_json(_CUR["running"], lambda d: d.update(worker=p.pid, progress={"detail": "执行器已启动，等待连接", "updated": time.time()}))
                if rec.get("cancel"):  # 登记前主人已经点了停止
                    raise KeyboardInterrupt
            except OSError:
                pass
    except BaseException:
        pf.kill_tree(p)
        p.wait()
        raise
    if input is not None:  # 另开线程写提示：执行器暂时不读 stdin 时也不会卡住，看门狗照常计时
        import threading

        def feed():
            try:
                p.stdin.write(input)
                p.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        threading.Thread(target=feed, daemon=True).start()
    last, sizes = time.time(), (0, 0)
    own_log = _own_log_probe(kind, session, last)
    offset, pending = 0, b""

    def progress():
        nonlocal offset, pending
        # 独立文件句柄：读取进度不能改变子进程写 stdout 的文件位置。
        with open(out.name, "rb") as stream:
            stream.seek(offset)
            chunk = stream.read()
        offset += len(chunk)
        lines = (pending + chunk).split(b"\n")
        pending = lines.pop()
        details = [_event_progress(e) for e in _events(b"\n".join(lines).decode("utf-8", "replace"))]
        detail = next((d for d in reversed(details) if d), None)
        _save_progress(detail)
    try:
        while p.poll() is None:
            time.sleep(2 if time.time() - last < 60 else 10)
            now_sizes = (os.fstat(out.fileno()).st_size, os.fstat(err.fileno()).st_size)
            if now_sizes != sizes:
                sizes, last = now_sizes, time.time()
                progress()
            last = max(last, own_log())
            if time.time() - last > idle_min * 60:
                raise Stalled(f"{max(1, round((time.time() - last) / 60))} 分钟没有任何动静")
    except BaseException:  # 卡住、被喊停、任何意外：先把执行器整组停掉再往外抛，不留野进程
        pf.kill_tree(p)
        p.wait()
        raise
    out.seek(0)
    err.seek(0)
    progress()
    return subprocess.CompletedProcess(cmd, p.returncode, out.read(), err.read())


def run_agent(agent_id, a, root, prompt, session, run_file):
    """跑一次 agent，返回 (回复文本, 新会话 id, 原始输出, token 用量)。"""
    import brains
    brain = brains.chosen(load_config(), agent_id)  # 主人给这个队员（分身）选的模型和思考强度
    if a["run"] in ("claude", "codex") and brain.get("model") and not brain.get("effort"):
        brain = brains.for_run(brain, brains.options(a))
    env = dict(os.environ, PWD=str(root), AGENTBUS_SELF=agent_id, AGENTBUS_DEPTH=str(int(os.environ.get("AGENTBUS_DEPTH", "0")) + 1), AGENTBUS_CONV=conv_id())
    env.pop("AGENTBUS_JOB_ID", None)  # 执行器再派别的活时不能覆盖本单的状态
    env.pop("AGENTBUS_CHAT_FILES", None)  # Only the dispatcher inherits the original upload references on handoff.
    kind = a["run"]
    import pf
    if len(prompt) > pf.ARG_LIMIT and kind not in ("codex", "claude", "codebuddy", "api"):
        # Windows 上命令行参数有长度上限：长任务书写成文件，参数里只放一句“去读它”
        pf_file = run_file.with_suffix(".prompt.md")
        pf_file.parent.mkdir(parents=True, exist_ok=True)
        pf_file.write_text(prompt, "utf-8")
        prompt = f"完整任务书在 {pf_file}，先完整读一遍，再按里面的要求开工。"
    if kind == "codex":
        out = run_file.with_suffix(".last.md")
        base = ["--skip-git-repo-check", "--json", "-o", str(out)] + brains.flags("codex", brain)
        codex_bin = a.get("bin") or "codex"
        prefix = [codex_bin, "--sandbox", "workspace-write", "exec"]
        cmd = [*prefix, "resume", session, *base, "-"] if session else [*prefix, *base, "-"]
        p = _run(kind, cmd, input=prompt, cwd=root, env=env, capture_output=True, text=True, session=session)
        sid, tok, errs = session, {"input": 0, "output": 0, "cache": 0}, []
        for ev in _events(p.stdout):
            if ev.get("type") in ("error", "turn.failed"):  # 额度、网络、工具被拦这类：把原因带回给主位
                er = ev.get("error") if isinstance(ev.get("error"), dict) else {}
                errs.append(er.get("message") or ev.get("message") or str(ev)[:200])
            if ev.get("type") == "thread.started":
                sid = ev.get("thread_id") or sid
            u = ev.get("usage") if ev.get("type") == "turn.completed" else None
            if u:
                tok["input"] += u.get("input_tokens", 0)
                tok["cache"] += u.get("cached_input_tokens", 0)
                tok["output"] += u.get("output_tokens", 0)
        text = out.read_text("utf-8").strip() if out.exists() else ""
        if not text and errs:
            return f"（codex 出错：{'；'.join(dict.fromkeys(errs))[:600]}）", sid, p.stdout, tok
        return text or f"（codex 没有回复，退出码 {p.returncode}）\n{p.stderr[-2000:]}", sid, p.stdout, tok
    if kind == "agy":  # 反重力 CLI：-p 单次执行，--output-format json 一次性返回 {conversation_id, status, response, usage}
        cmd = ["agy", "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions"]
        if session:
            cmd += ["--conversation", session]
        p = _run(kind, cmd, cwd=root, env=env, capture_output=True, text=True)
        try:
            d = json.loads((p.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return f"（反重力没有回复，退出码 {p.returncode}）\n{(p.stderr or '')[-1500:]}", session, p.stdout, None
        u = d.get("usage") or {}
        tok = {"input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0) + int(u.get("thinking_tokens") or 0),
               "cache": int(u.get("cache_read_tokens") or 0)}
        text = (d.get("response") or "").strip()
        if d.get("status") not in (None, "SUCCESS") and not text:
            text = f"（反重力出错：{d.get('status')}）"
        return text or "（反重力没有回复）", d.get("conversation_id") or session, p.stdout, tok
    if kind == "opencode":
        model = brains.opencode_model(brain, os.environ.get("AGENTBUS_DEEPSEEK_MODEL", "deepseek/deepseek-flash"))
        cmd = ["opencode", "run", "-m", model, "--format", "json", "--auto"]
        if session:
            cmd += ["--session", session]
        p = _run(kind, cmd + [prompt], cwd=root, env=env, capture_output=True, text=True)
        sid, texts, tok, errs = session, [], {"input": 0, "output": 0, "cache": 0}, []
        for ev in _events(p.stdout):
            sid = ev.get("sessionID") or sid
            if ev.get("type") == "error":  # 比如模型下线：provider.no-route
                er = ev.get("error") or {}
                errs.append(er.get("message") or er.get("type") or str(er)[:200])
            if ev.get("type") == "text":
                texts.append(ev["part"].get("text", ""))
            if ev.get("type") == "step_finish":
                t = (ev.get("part") or {}).get("tokens") or {}
                tok["input"] += t.get("input", 0)
                tok["output"] += t.get("output", 0) + t.get("reasoning", 0)
                tok["cache"] += (t.get("cache") or {}).get("read", 0)
        # 只要最后一段文字，前面是它边干活边念叨的
        text = texts[-1].strip() if texts else ""
        if not text and errs:
            return f"（opencode 出错：{'；'.join(errs)[:500]}）", sid, p.stdout, tok
        return text or f"（opencode 没有回复，退出码 {p.returncode}）\n{p.stderr[-2000:]}", sid, p.stdout, tok
    if kind == "zcode":
        # zcode 没有 --model：主人在 agentbus 里选了模型的话，开工那几秒临时换它的默认模型（brains.zcode_brain）
        cmd = ["zcode", "-p", prompt, "--cwd", str(root), "--json", "--mode", "yolo"]
        if session:
            cmd += ["--resume", session]
        with brains.zcode_brain(brain):
            p = _run(kind, cmd, cwd=root, env=env, capture_output=True, text=True, session=session)
        whole = load_json_str(p.stdout)  # --json 输出是一整个 JSON：{sessionId, response, usage, ...}
        if isinstance(whole, dict) and whole.get("response"):
            u = whole.get("usage") or {}
            tok = {"input": u.get("inputTokens", 0) - u.get("cacheReadTokens", 0), "output": u.get("outputTokens", 0),
                   "cache": u.get("cacheReadTokens", 0)}
            return str(whole["response"]).strip(), whole.get("sessionId") or session, p.stdout, tok
        return (p.stdout.strip()[-3000:] or f"（zcode 没有回复，退出码 {p.returncode}）\n{p.stderr[-2000:]}"), session, p.stdout, None
    if kind == "claude":
        cmd = [a.get("bin") or "claude", "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--permission-mode", "acceptEdits"] + brains.flags("claude", brain)
        if session:
            cmd += ["--resume", session]
        p = _run(kind, cmd, input=prompt, cwd=root, env=env, capture_output=True, text=True)
        res = load_json_str(p.stdout)
        if res.get("is_error"):
            return f"（claude 出错：{res.get('result') or res.get('subtype')}）", session, p.stdout, None
        u = res.get("usage") or {}
        tok = {"input": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
               "output": u.get("output_tokens", 0), "cache": u.get("cache_read_input_tokens", 0),
               "usd_equiv": res.get("total_cost_usd")}
        return (res.get("result") or f"（claude 没有回复，退出码 {p.returncode}）\n{p.stderr[-2000:]}"), \
            res.get("session_id", session), p.stdout, tok
    if kind == "api":
        import api_agent
        try:
            text, sid, raw, tok = api_agent.run(a["account"], brain.get("model") or a["model"], prompt, root, session, brain.get("effort"),
                                                timeout=watchdog_cfg()[0] * 60 * 3, bus=run_file.parent.parent, me=agent_id)
        except api_agent.ApiError as e:
            return f"（{a['name']} 调用出错：{e}）", session, "", None
        return text, sid, raw, tok
    if kind == "dsh":
        p = _run(kind, ["dsh", "--profile", "headless", prompt], cwd=root, env=env, capture_output=True, text=True)
        return (p.stdout.strip() or f"（DeepSeek Harness 没有回复，退出码 {p.returncode}）\n{p.stderr[-2000:]}"), session, p.stdout, None
    if kind == "codebuddy":
        cmd = [a.get("bin") or "codebuddy", *(a.get("bin_args") or []), "-p", "--permission-mode", "acceptEdits",
               "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        if session:
            cmd += ["--resume", session]
        p = _run(kind, cmd, input=prompt, cwd=root, env=env, capture_output=True, text=True)
        res = load_json_str(p.stdout)
        if res.get("is_error"):
            return f"（WorkBuddy 出错：{res.get('result') or res.get('subtype')}）", session, p.stdout, None
        if isinstance(res, dict) and res.get("result"):
            return res["result"], res.get("session_id", session), p.stdout, None
        return f"（WorkBuddy 没有回复，退出码 {p.returncode}）\n{p.stderr[-2000:]}", session, p.stdout, None
    if kind == "doubao":
        # 豆包没有官方 CLI，用我们的豆包遥控（CDP）：填进输入框 → 发送 → 等它答完 → 读回复
        js = doubao_remote()
        log = []
        for step in (["输入", prompt], ["发送"], ["等完", "900"], ["读", "6000"]):
            p = _run(kind, ["node", js, *step], cwd=root, env=env, capture_output=True, text=True)
            log.append(f"$ 豆包遥控 {step[0]}\n{p.stdout}{p.stderr}")
            if p.returncode:
                return f"（豆包没接上：{p.stdout.strip() or p.stderr.strip()}）", session, "\n".join(log), None
        return p.stdout.strip(), session, "\n".join(log), None
    if kind == "generic":
        cmd = [x.replace("{prompt}", prompt) for x in a["cmd"]]
        p = _run(kind, cmd, cwd=root, env=env, capture_output=True, text=True)
        return (p.stdout.strip() or f"（{a['name']} 没有回复，退出码 {p.returncode}）\n{p.stderr[-2000:]}"), session, p.stdout, None
    sys.exit(f"{a['name']} 不能用命令行派活（{a.get('desc', '')}）")


def inbox(bus, agent, mark=False):
    read = load_json(bus / "read.json", {})
    recs = board_read(bus)
    start = read.get(agent, 0)
    msgs = [r for r in recs[start:] if r.get("kind") == "msg" and r.get("to") == agent]
    if mark:
        from config import update_json
        update_json(bus / "read.json", lambda d: d.__setitem__(agent, max(d.get(agent, 0), len(recs))))
    return msgs


def overlapping(billing, me):
    """同一种计费、同时在跑的其他派活（它们的消耗会混进余额差里）。"""
    out = []
    for f in RUNNING.glob("*.json"):
        r = load_json(f, {})
        if r.get("billing") == billing and r.get("id") != me:
            out.append(r.get("id"))
    return out


def fmt_cost(billing, c):
    if not c or not c.get("ok"):
        return "额度：" + ((c or {}).get("note") or "没有数据")
    if billing == "codex":
        s = f"GPT 额度：5 小时 +{c.get('five_hour') or 0:g}%、本周 +{c.get('weekly') or 0:g}%"
        if c.get("credits"):
            s += f"、credits -{c['credits']:g}"
        return s + ("（" + c["note"] + "）" if c.get("note") else "")
    if billing in ("deepseek", "free"):
        return f"花费：¥{c.get('cny') or 0:.2f}" + ("（DeepSeek 扣费可能晚几分钟入账，几分钟后会自动补查）" if billing == "deepseek" else "")
    return ""


# 额度读数超过这么久就不算“现在的”了：opencodex 不通时退回的缓存可能是几天前的。
CREDITS_FRESH_SECS = 600


def _credits_block(billing, q, allow, now=None):
    """GPT 额度已经用完，或者读数已过期，就拒绝派活（再派会扣付费 credits）。
    读不到额度（没有 opencodex 也没有缓存）不拦：没有数据就无从判断，这种情况放行，由 _credits_unknown() 提示。
    --allow-credits 明确放行一切。"""
    if billing != "codex" or allow:
        return None
    now = time.time() if now is None else now
    if not q.get("ok"):
        return None
    stale = q.get("stale") or now - (q.get("updated") or 0) > CREDITS_FRESH_SECS
    if stale:
        return ("GPT 额度读数已过期（" + time.strftime("%m-%d %H:%M", time.localtime(q.get("updated") or 0)) + "），"
                "不能确认还有额度，为免扣付费 credits 先不派。确定要用就加 --allow-credits，或者换别的 agent。")
    if max(q.get("weekly") or 0, q.get("five_hour") or 0) >= 100:
        return (f"GPT 额度用完了（本周 {q.get('weekly')}%、5 小时 {q.get('five_hour')}%），再派会扣付费 credits"
                f"（现在 {q.get('credits')}）。确定要用就加 --allow-credits，或者换别的 agent。")
    return None


def _credits_unknown(billing, q):
    """读不到 GPT 额度时给主人的提示文字；能读到额度就返回 None。"""
    if billing != "codex" or q.get("ok"):
        return None
    return "（没有读到 GPT 额度，" + (q.get("error") or "来源不可用") + "；这单可能扣付费 credits）"


def cmd_ask(a):
    if int(os.environ.get("AGENTBUS_DEPTH", "0")) >= MAX_DEPTH:
        sys.exit("派活层数到顶了，这件事请自己做完或在回复里交给总指挥。")
    if a.project is None and not os.environ.get("AGENTBUS_PROJECT"):
        from config import project_of_dir
        a.project = project_of_dir()  # 从哪个项目目录派的就记在哪个项目下
    name, root, bus, batch = project(a.project)
    cfg = load_config(name)  # 明确指定了项目就用那个项目的阵容和职能，不用全局“当前项目”的
    agents = agents_dict(cfg)
    from config import whoami as _who
    me = a.sender or _who() or lead_of_cfg(cfg) or "claude"
    own = (cfg.get("office_lineups") or {}).get(me)
    if own:  # 这位主位的办公室调过战备预设组：按岗位挑人时用它的阵容和主力
        cfg["roster"]["office"] = [x for x in own.get("office", []) if x in agents]
        cfg["jobs"] = own.get("jobs") or cfg["jobs"]
    # 职能：--tag，或者环境变量 AGENTBUS_TAG（派活脚本、主位在 Bash 里这样传）
    agent_id, tag = a.agent, a.tag or os.environ.get("AGENTBUS_TAG") or None
    if agent_id not in agents:  # 写的是岗位：按偏好挑人
        key, label = resolve_tag(agent_id)
        from config import sealed
        cands = [c for c in who(cfg, label) if agents[c["id"]].get("run") and not sealed(agents[c["id"]], cfg) and installed(agents[c["id"]])] if key.startswith("tag:") else []
        if not cands:
            sys.exit(f"没有能干「{label}」的 agent。先用 agentbus assign 给谁挂上这个岗位，或者直接写 agent 名字。")
        agent_id, tag = cands[0]["id"], tag or label
        print(f"[agentbus] 「{label}」按偏好派给 {agents[agent_id]['name']}"
              + ("" if cands[0]["in_office"] else "（办公室战备里没人能干，从休息室调的）"), file=sys.stderr)
    ag = agents[agent_id]
    from config import sealed
    if sealed(ag, cfg):  # Claude 默认封印：只当主位、不接派活；主人在队员页解封后才能派
        sys.exit("Claude 已封印，不接派活：这件事主位自己做，或者派给别的队员（要让 Claude 当队员，先在 agentbus 队员页给它解封）。")
    if ag.get("billing") == "manual":
        sys.exit(f"{ag['name']} 现在是网页版（没装命令行），没法派活；请你自己把任务贴给它，或者先在 agentbus 里招募安装。")
    task = sys.stdin.read() if a.task == "-" else a.task
    if a.file:
        task += "\n\n" + Path(a.file).read_text("utf-8")
    from config import touch_lead, whoami
    sender = a.sender or whoami() or lead_of_cfg(cfg) or "claude"
    if not os.environ.get("AGENTBUS_CHAT"):  # 消息窗口里的接力（队员 @ 队员）不算谁在统筹，不改主位
        touch_lead(sender, "ask", conv_id())
    sessions = load_json(bus / "sessions.json", {})
    conv = conv_id()
    session = None if a.new else sessions.get(sess_key(agent_id, conv))
    stamp = dt.datetime.now().strftime("%m%d-%H%M%S")
    import uuid
    while True:  # 同一秒派给同一个队员的两单也各有各的记录、输出文件和账本 id
        run_file = bus / "runs" / f"{stamp}-{uuid.uuid4().hex[:4]}-{agent_id}"
        if not run_file.with_suffix(".md").exists() and not (RUNNING / (run_file.name + ".json")).exists():
            break
    run_id = f"{name}/{run_file.name}"
    # 项目经验包：按这次的岗位挑出那一步的做法和相关经验（项目选了“不用”就不带）
    import playbook
    # 经验包：装在这位主位办公室上的、装在这个文件夹上的优先；都没有就看项目自己的经验包开没开
    from config import mounted_packs
    mounts = mounted_packs(cfg, sender, str(root))
    pb_bus = Path(mounts[0]) if mounts else bus
    pb = playbook.load(pb_bus)
    equip = dict(playbook.equip_of(cfg, name), enabled=True) if mounts else playbook.equip_of(cfg, name)
    pb_step, pb_lessons = None, []
    if pb and equip["enabled"]:
        pb_step, pb_lessons = playbook.pick(pb, equip, tag, task, head=a.task if a.task != "-" else "")
        if not tag or not pb_step:
            print(f"[agentbus] 经验包：{playbook.pick.why}" + ("（建议加 --tag 职能）" if not tag else ""), file=sys.stderr)
    pb_personal = playbook.personal_of(pb_bus) if pb else {}
    pb_mine = playbook.pick_personal(pb_personal, pb_step, tag, task) if pb and equip["enabled"] else []
    block = playbook.prompt_block(pb, pb_step, pb_lessons, pb_personal, pb_mine) if pb and equip["enabled"] else ""
    from config import mounted_skills
    sk = mounted_skills(cfg, sender, str(root))
    if sk:  # 主人给这间办公室 / 这个文件夹装的 skill：只给目录和说明，用到哪个再读（不把整份塞进来）
        block += "\n\n## 这里装了这些 skill（用得上就先完整读它的 SKILL.md 再照做）\n" + "\n".join(f"- {n}：{d[:160]}（{p}/SKILL.md）" for n, d, p in sk)
    prompt = build_prompt(agent_id, name, root, bus, task, sender, block)
    # Expand in the worker, not in the command line, job metadata, or shared channel history.
    attachment_material = ""
    if os.environ.get("AGENTBUS_CHAT") and os.environ.get("AGENTBUS_CHAT_FILES"):
        import chat
        from chat_attachments import material
        attachment_material = material(json.loads(os.environ["AGENTBUS_CHAT_FILES"]), chat.FILES, root)
    if pb_step or pb_lessons:
        playbook.bump(pb_bus, pb_step, pb_lessons)
        print(f"[agentbus] 带上经验包：{pb_step['name'] if pb_step else '（无对应步骤）'} + {len(pb_lessons)} 条经验"
              + (f"（{playbook.pick.why}）" if tag and pb_step else ""), file=sys.stderr)
    billing = ag.get("billing", "none")
    RUNNING.mkdir(parents=True, exist_ok=True)
    running = RUNNING / (run_file.name + ".json")
    _CUR["running"] = running
    if not pf_is_win():
        import signal
        signal.signal(signal.SIGTERM, _sigterm)
    t0 = time.time()
    first = task.split("\n")[0][:80]
    # 先登记“在跑”（界面、agentbus wait 马上能看到），再去查额度（查余额要几秒）
    save_json(running, {"id": run_id, "agent": agent_id, "billing": billing, "project": name, "start": t0, "conv": conv[:12],
                        "task": first, "title": (getattr(a, "title", None) or "").strip()[:40] or short_title(a.task if a.task != "-" else task), "pid": os.getpid(), "frm": sender, "tag": tag, "batch": batch, "run": run_file.name,
                        "job": os.environ.get("AGENTBUS_JOB_ID"), "channel": os.environ.get("AGENTBUS_CHAT"), "cwd": str(root)})
    try:
        before = quota.snapshot(billing)
    except Exception as e:
        before = {"ok": False, "error": str(e)}
    # 额度用完后 opencodex 会转去扣付费 credits：没有明确允许就不派
    if why := _credits_block(billing, before, getattr(a, "allow_credits", False)):
        running.unlink(missing_ok=True)
        sys.exit(why)
    save_json(running, dict(load_json(running, {}), before=_slim(before)))
    board_add(bus, kind="task", frm=sender, to=agent_id, text=task, run=run_file.name, tag=tag, batch=batch, conv=conv[:12])
    print(f"[agentbus] {sender} → {agent_id}（{'续上次会话' if session else '新会话'}）开始 {now()}", file=sys.stderr, flush=True)
    _, retries = watchdog_cfg()
    import scheduler
    text, sid, raw, tok, ok, status = None, session, "", None, False, "error"
    sched = scheduler.acquire(agent_id, ag, tag, getattr(a, "screen", False), running, first)
    try:
        sched.__enter__()  # 先排队：屏幕锁、钱包并发
        if sched.waited > 1:  # 等过队：开工时间和额度起点从现在算
            t0 = time.time()
            before = quota.snapshot(billing, refresh=True)
            save_json(running, dict(load_json(running, {}), start=t0, before=_slim(before)))

        def admit(attempt):
            """每次真正启动前按主人现在的设置再查一遍：排队 / 重试期间重新封印了、额度用完了，就不开工。"""
            from config import sealed as _sealed
            cfg2 = load_config(name)
            if _sealed(agents_dict(cfg2).get(agent_id) or ag, cfg2):
                return f"{ag['name']} 已被封印，这单没开工"
            if billing == "codex":
                q = before if attempt == 0 else quota.snapshot(billing, refresh=True)
                return _credits_block(billing, q, getattr(a, "allow_credits", False))
        for attempt in range(retries + 1):
            why = admit(attempt)
            if why:
                text = "（没开工：" + ("排队期间" if attempt == 0 else "重试前") + why + "）"
                raise _NoStart()
            try:
                text, sid, raw, tok = run_agent(agent_id, ag, root, prompt + attachment_material, session, run_file)
                ok = not _looks_failed(text)
                status = "ok" if ok else "failed"
                break
            except Stalled as e:
                note = f"{ag['name']} {e}，已停掉"
                if attempt < retries:
                    note += f"，开新会话重试（第 {attempt + 1} 次）"
                    prompt += ("\n\n## 注意：上一次接这个活中途卡住、被 agentbus 停掉了\n"
                               "先检查项目里已经产出的文件，合格的不要重做，把剩下的做完。回复里说明哪些是这次补的。")
                    session = None
                    save_json(running, dict(load_json(running, {}), retry=attempt + 1, retry_t=time.time(), stalled=str(e)))
                print(f"[agentbus] {note}", file=sys.stderr, flush=True)
                board_add(bus, kind="msg", frm="agentbus", to=sender, text=note, run=run_file.name)
        else:
            text, status = f"（{ag['name']} 连续卡住 {retries + 1} 次，已停止。可能已经产出了一部分文件，请检查后再派。）", "stalled"
    except _NoStart:
        status = "not_started"
    except KeyboardInterrupt:
        text, status = f"（这单被手动停掉了：{ag['name']} 没做完）", "stopped"
    except BaseException as e:  # 任何意外都要留下回复，不能让主位干等
        import traceback
        text = f"（agentbus 派活出错，{ag['name']} 没做完：{type(e).__name__}: {e}）"
        raw = raw or traceback.format_exc()
    finally:
        if not pf_is_win():
            import signal
            signal.signal(signal.SIGTERM, signal.SIG_IGN)  # 收尾（记账、写回复）不能再被打断
        try:
            sched.__exit__(None, None, None)
        except Exception:
            pass
        others = overlapping(billing, run_id)
        # 先收尾（记账、写回复），最后才撤掉“在跑”记录：收尾哪一步出错，界面和 wait 也还看得到这单、会补一条失败回复
        try:
            _finish(locals())
        except Exception as e:
            try:
                board_add(bus, kind="reply", frm=agent_id, to=sender, text=f"（{ag['name']} 这单收尾时出错：{type(e).__name__}: {e}。回复可能不全，去产出目录看看。）\n\n{(text or '')[:1500]}",
                          run=run_file.name, tag=tag, batch=batch, ok=False, conv=conv[:12])
            except Exception:
                pass
            print(f"[agentbus] 收尾出错：{e}", file=sys.stderr)
        running.unlink(missing_ok=True)
    sys.exit(0 if ok else 1)


def _looks_failed(text):
    """回复是 agentbus 自己补的“没回复 / 出错 / 卡住”就算失败（队员正常交的回复里说没做成，也照常算交了回复）。"""
    t = (text or "").strip()
    return not t or (t.startswith("（") and any(k in t[:60] for k in ("没有回复", "出错", "卡住", "没接上", "调用出错", "停掉", "已停止", "超时", "没做完", "没开工")))


def _finish(v):
    """派活收尾：记账、写回复、提醒派活的人。无论成功、失败、出错都会走到这里。"""
    a, ag, bus, name, batch, billing = v["a"], v["ag"], v["bus"], v["name"], v["batch"], v["billing"]
    agent_id, sender, conv, tag, run_file, run_id = v["agent_id"], v["sender"], v["conv"], v["tag"], v["run_file"], v["run_id"]
    text, sid, raw, tok, ok, t0 = v["text"], v["sid"], v["raw"], v["tok"], v["ok"], v["t0"]
    text = text or f"（{ag['name']} 没有回复）"
    t1 = time.time()
    try:
        after = quota.snapshot(billing, refresh=True)
        cost = quota.delta(billing, v["before"], after)
    except Exception as e:
        after, cost = {"ok": False}, {"ok": False, "note": f"额度没查到：{e}"}
    if v["others"] and cost.get("ok"):
        cost["note"] = "同时还有别的活在用同一个额度，单条数字偏大；账单里会合并计算"
    if not cost.get("ok") and _credits_unknown(billing, v["before"]) and _credits_unknown(billing, after):
        text = text + "\n\n" + _credits_unknown(billing, after)
    if tok:
        tok["total"] = sum(x for k, x in tok.items() if k in ("input", "output", "cache") and isinstance(x, int))
    if sid:
        import pf
        lock = HOME / "locks" / "sessions.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock, "a+")
        try:
            pf.lock(fh, wait=30)  # 几单同时收尾时别互相覆盖会话记录
            sessions = load_json(bus / "sessions.json", {})
            sessions[sess_key(agent_id, conv)] = sid
            save_json(bus / "sessions.json", sessions)
        except TimeoutError:
            pass
        finally:
            pf.unlock(fh)
            fh.close()
    stamp = run_file.name.split("-" + agent_id)[0]
    run_file.with_suffix(".md").write_text(
        f"# {sender} → {agent_id}  {stamp}\n会话：{sid}\n\n## 发出的完整提示\n\n{v['prompt']}\n\n## 回复\n\n{text}\n", "utf-8")
    run_file.with_suffix(".jsonl").write_text(raw or "", "utf-8")
    ledger_add({"id": run_id, "project": name, "batch": batch, "agent": agent_id, "billing": billing, "tag": tag,
                "task": v["first"], "start": t0, "end": t1, "seconds": round(t1 - t0), "ok": ok, "status": v.get("status") or ("ok" if ok else "failed"), "frm": sender,
                "reply": text[:600], "before": _slim(v["before"]), "after": _slim(after), "cost": cost, "tokens": tok, "overlap": v["others"],
                "settle_at": t1 + 240 if billing == "deepseek" else None, "conv": conv[:12]})
    board_add(bus, kind="reply", frm=agent_id, to=sender, text=text, run=run_file.name, tag=tag, batch=batch,
              cost=cost, seconds=round(t1 - t0), ok=ok, status=v.get("status"), conv=conv[:12])
    if os.environ.get("AGENTBUS_CHAT") and not os.environ.get("AGENTBUS_TALK"):  # 群聊由轮次进程统一发言，避免 @ 递归派单
        try:
            import chat
            chat.send(os.environ["AGENTBUS_CHAT"], text if ok else f"❌ {text}", frm=agent_id, hops=int(os.environ.get("AGENTBUS_CHAT_HOPS", "0")), cwd=str(v["root"]))
        except Exception as e:
            print(f"[agentbus] 回复发回群里失败：{e}", file=sys.stderr)
    if os.environ.get("AGENTBUS_JOB_ID"):
        import mcp_server
        mcp_server.finish_job(os.environ["AGENTBUS_JOB_ID"], v.get("status") or ("ok" if ok else "failed"), text, run_file.name)
    print(text)
    print(f"\n[agentbus] {'做完了' if ok else '没做成'}，用时 {round((t1 - t0) / 60, 1)} 分钟；{fmt_cost(billing, cost)}", file=sys.stderr)


def lead_of_cfg(cfg):
    from config import active_lead
    return active_lead(cfg)


def _slim(q):
    keep = ("ok", "five_hour", "weekly", "credits", "total", "by_key", "error", "stale")
    return {k: q[k] for k in keep if k in q}


# ---------- 回音：派出去的活做完 / 失败，派活的人一定能知道 ----------
SEEN = HOME / "seen.json"


def _me():
    """看结果的人是谁：有协同会话按会话分（两个窗口各看各的），没有就按身份。"""
    from config import whoami
    c = conv_id()
    return ("conv:" + c[:12]) if c else ("who:" + (whoami() or "claude"))


def _mine(r, me):
    return (me.startswith("conv:") and r.get("conv") == me[5:]) or (me.startswith("who:") and r.get("frm", "claude") == me[4:] and not r.get("conv"))


def _result_text(r, full=False):
    c = r.get("cost") or {}
    head = f"{'✅' if r.get('ok', True) else '❌'} {r['agent']}｜{r.get('task') or ''}（{round((r.get('seconds') or 0) / 60, 1)} 分钟"
    head += f"，{fmt_cost(r.get('billing'), c)}）" if c.get("ok") else "）"
    body = (r.get("reply") or "（回复见任务板）").strip()
    return head + "\n" + (body if full else body[:400] + ("…" if len(body) > 400 else ""))


def unseen_results(mark=True, me=None):
    """这个窗口（或这位主位）派出去、做完了、还没看过的活。"""
    from config import _settled
    me = me or _me()
    seen = load_json(SEEN, {})
    since = seen.get(me, time.time() - 6 * 3600)
    out = [r for r in _settled() if r.get("end", 0) > since and _mine(r, me)]
    if mark and out:
        from config import update_json
        t = max(r["end"] for r in out)
        update_json(SEEN, lambda d: d.__setitem__(me, max(d.get(me, 0), t)))
    return out


def cmd_news(a):
    rs = unseen_results(mark=not a.peek)
    if not rs:
        if not a.hook:
            print("没有新结果。")
        return
    print(("[agentbus 回音] 你派出去的活有新结果，先处理再继续：\n" if a.hook else "") + "\n\n".join(_result_text(r) for r in rs))


def _my_running(me):
    return {r.get("id"): r for r in (load_json(f, {}) for f in RUNNING.glob("*.json")) if _mine(r, me)}


def cmd_wait(a):
    """等派出去的活：默认等到“我”派出去的任意一单做完（或失败）就返回，用在 Bash 后台跑，结束时会叫醒主位。
    --all 等全部做完；也可以写 MCP 派活的 job id。"""
    me = _me()
    jobs = []
    for jid in a.jobs:
        jf = HOME / "jobs" / f"{jid}.json"
        if jf.exists():
            jobs.append(load_json(jf, {}).get("pid"))
    deadline = time.time() + a.timeout * 60
    grace = time.time() + 20
    waiting, recs = None, {}
    def all_running():
        return {r.get("id"): r for r in (load_json(f, {}) for f in RUNNING.glob("*.json")) if r.get("id")}
    while True:
        cur = _my_running(me)
        if jobs:  # 指定了 job：按派活进程认，不管是哪个窗口派的
            if waiting is None:
                time.sleep(3)  # 后台派活刚起，running 记录可能还没写
            cur = {k: r for k, r in all_running().items() if r.get("pid") in jobs}
        if waiting is None and not cur and time.time() < grace:
            time.sleep(2)  # 刚派出去的活登记要一两秒：先等一会儿再下结论
            continue
        if waiting is None:
            waiting = set(cur)
            if not waiting:
                if jobs:  # 指定的 job 已经结束（或者没起来）：直接给它的输出，启动失败的原因也在里面
                    import mcp_server
                    print("\n\n".join(mcp_server.job({"id": j}) for j in a.jobs if (HOME / "jobs" / f"{j}.json").exists()))
                    return
                rs = unseen_results()
                print("\n\n".join(_result_text(r, True) for r in rs) if rs else "现在没有你派出去、还在跑的活。")
                return
            print(f"[agentbus] 在等 {len(waiting)} 单：" + "；".join(f"{r['agent']}：{r.get('task')}" for r in cur.values()), file=sys.stderr, flush=True)
        for k, r in cur.items():
            recs.setdefault(k, r)
        done = waiting - set(cur)
        if done and (not a.all or not (waiting & set(cur))):
            # 进程结束前还要查一次额度、记账，最多再等 90 秒；进程还活着就不算“没了”
            end_by, got = time.time() + 90, {}
            while True:
                got = {r["id"]: r for r in load_ledger_ids(done)}
                busy = [i for i in done if i not in got and _pid_alive(recs[i].get("pid"))]
                if not busy or time.time() > end_by:
                    break
                time.sleep(2)
            others = [r for r in unseen_results() if r["id"] not in done]  # 顺便标成已读
            for i in done:
                if i in got:
                    print(_result_text(got[i], True) + "\n")
                else:
                    print(f"❌ {recs[i].get('agent')}｜{recs[i].get('task')}：派活进程没了，没留下回复（可能被杀掉或电脑睡眠）。去任务板 / 产出目录看看做到哪了。\n")
            if others:
                print("另外还有这些新结果：\n" + "\n\n".join(_result_text(r) for r in others) + "\n")
            left = waiting & set(_my_running(me))
            if left:
                print(f"[agentbus] 还有 {len(left)} 单在跑；要继续等就再后台跑一次 agentbus wait。")
            sys.exit(0 if all(i in got and got[i].get("ok", True) for i in done) else 1)
        if time.time() > deadline:
            print(f"[agentbus] 等了 {a.timeout:g} 分钟还没做完：" + "；".join(f"{r['agent']}：{r.get('task')}" for r in cur.values()))
            sys.exit(2)
        time.sleep(5)


def _pid_alive(pid):
    import pf
    return pf.pid_alive(pid)


def load_ledger_ids(ids):
    from config import ledger_read
    return [r for r in ledger_read() if r.get("id") in ids]


# ---------- 新对话自动接入：Claude / GPT 的钩子调这里 ----------
# 会话开始（SessionStart）→ 告诉悬浮窗“有新对话，接不接入 bus？”；主人点了接入 → 这个窗口下一次发消息（UserPromptSubmit）时
# 把 bus 的上下文塞进对话，只塞一次；之后每次只附上派出去的活的新结果。设置 auto_join：ask（默认）/ always / never。
JOINED = HOME / "joined.json"


def _hook_input():
    try:
        return json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return {}


def _post(path, body):
    import urllib.request
    try:
        from config import URL as _URL
        req = urllib.request.Request(_URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


def join_brief(agent):
    """接入 bus 时塞给这个窗口的上下文：和 MCP 握手（agentbus_hello）同一份。"""
    try:
        import mcp_server
        os.environ.setdefault("AGENTBUS_SELF", agent)
        return mcp_server.hello({})  # 文件夹由 cmd_hook 设好的 AGENTBUS_CWD 给
    except Exception as e:
        print(f"agentbus 握手信息没取到：{e}", file=sys.stderr)
        return None


def cmd_hook(a):
    d = _hook_input()
    # 被派活起来的 agent（队员）：它的新会话不是主人开的窗口——不问接入、不塞握手、不报回音（派活时 agentbus 会设这些环境变量）
    if int(os.environ.get("AGENTBUS_DEPTH", "0") or 0) > 0 or os.environ.get("AGENTBUS_CHAT"):
        return
    # 各家钩子的输入字段名不一样：Claude/Codex/ZCode/CodeBuddy 是 session_id，反重力是 camelCase 的 conversationId
    sid = d.get("session_id") or d.get("thread_id") or d.get("conversationId") or d.get("sessionId") or d.get("sessionID") or ""
    d.setdefault("session_id", sid)
    if not d.get("cwd") and d.get("workspacePaths"):
        d["cwd"] = (d["workspacePaths"] or [None])[0]
    conv = sid[:12]
    agent = a.agent
    if conv:
        os.environ["AGENTBUS_CONV"] = d.get("session_id") or conv
    if d.get("cwd"):
        os.environ["AGENTBUS_CWD"] = d["cwd"]
    joined = load_json(JOINED, {})
    mode = (load_config().get("settings") or {}).get("auto_join", "ask")
    out = []
    if a.event == "session":
        if conv and conv not in joined and d.get("source", "startup") in ("startup", "clear", None):  # 点过“不了”的也算决定过
            if mode == "always":
                rec = {"agent": agent, "cwd": d.get("cwd"), "t": time.time(), "briefed": False}
                joined = update_json(JOINED, lambda j: j.setdefault(conv, rec) and None)
            elif mode == "ask" and agent != "gemini":  # 反重力的子对话也会触发钩子、文件夹也报不准：它的新对话交给被动检测（能认出子对话）
                _post("/api/session/new", {"agent": agent, "conv": conv, "cwd": d.get("cwd") or os.getcwd()})
        if conv in joined and joined[conv].get("briefed"):  # 续上以前接入过的窗口：提醒一句
            out.append("（这个窗口已接入 agentbus，按 bus 的规矩派活、等回音。）")
    elif a.event == "prompt":
        j = joined.get(conv)
        if j and not j.get("briefed") and not j.get("declined"):
            brief = join_brief(agent)
            if brief:  # 握手生成成功才算送到；失败这次只提示一句，下一句话再试
                out.append("[agentbus] 主人在悬浮窗里让这个窗口接入了 agentbus，你是这个窗口的主位，下面是握手信息，照它的规矩干活：\n" + brief)
                update_json(JOINED, lambda jj: jj.get(conv, {}).__setitem__("briefed", True) if conv in jj else None)
                from config import touch_lead
                touch_lead(agent, "mcp", conv)
            else:
                out.append("（这个窗口已接入 agentbus，但握手信息这次没取到，下一句话会再试；也可以直接调 agentbus_hello。）")
        rs = unseen_results()
        if rs:
            out.append("[agentbus 回音] 你派出去的活有新结果，先处理再继续：\n" + "\n\n".join(_result_text(r) for r in rs))
    if out:
        print(hook_output(agent, a.event, "\n\n".join(out)))


def hook_output(agent, event, text):
    """按各家钩子要的格式输出，才会真正进模型上下文：
    Claude 纯文本；Codex / ZCode / CodeBuddy(WorkBuddy) 要 hookSpecificOutput JSON；反重力要 injectSteps。"""
    ev = "SessionStart" if event == "session" else "UserPromptSubmit"
    if agent in ("antigravity", "gemini"):  # 队员 gemini 就是反重力
        return json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": text}}]}, ensure_ascii=False)
    if agent in ("codex", "zcode", "codebuddy"):
        return json.dumps({"hookSpecificOutput": {"hookEventName": ev, "additionalContext": text}}, ensure_ascii=False)
    return text


def cmd_hello(a):
    """兜底的接入方式：主人把接入口令贴进对话框，agent 自己跑这条命令拿握手（不需要钩子，也不需要 MCP）。"""
    import mcp_server
    if a.agent:
        os.environ["AGENTBUS_SELF"] = a.agent
    os.environ.setdefault("AGENTBUS_CWD", os.getcwd())
    print(mcp_server.hello({"explicit": True, "project": a.project}))


def cmd_post(a):
    _, _, bus, _ = project(a.project)
    board_add(bus, kind="msg", frm=a.sender or os.environ.get("AGENTBUS_SELF", "user"), to=a.agent, text=a.text)
    print("已留言")


def cmd_inbox(a):
    _, _, bus, _ = project(a.project)
    msgs = inbox(bus, a.agent, mark=not a.peek)
    for m in msgs:
        print(f"[{m['t']}] {m['frm']}：{m['text']}")
    if not msgs:
        print("没有未读留言")


def cmd_board(a):
    _, _, bus, _ = project(a.project)
    for r in board_read(bus)[-a.n:]:
        arrow = {"task": "派活", "reply": "回复", "msg": "留言"}.get(r["kind"], r["kind"])
        text = r["text"] if a.full else r["text"].replace("\n", " ")[:160]
        print(f"[{r['t']}] {r.get('frm', r.get('from'))} → {r['to']} {arrow}：{text}")


def cmd_agents(a):
    cfg = load_config()
    office = cfg["roster"]["office"]
    for k, v in agents_dict(cfg).items():
        where = "办公室" if k in office else "休息室"
        ok = "已装" if installed(v) else "未装"
        print(f"{k:11s} {v['name']:16s} {where}  {ok}  {v.get('desc', '')}")


def cmd_who(a):
    cfg = load_config()
    key, label = resolve_tag(a.tag)
    if key.startswith("cat:"):
        cid = key[4:]
        tags = next(t for c, _, t in CATEGORIES if c == cid)
        for t in tags:
            names = "、".join(f"{r['name']}（{LEVELS[r['level']]}）" for r in who(cfg, t)) or "没人"
            print(f"{t}：{names}")
        return
    rows = who(cfg, label)
    if not rows:
        print(f"「{label}」还没分配给谁")
    for r in rows:
        print(f"{r['name']:16s} {LEVELS[r['level']]}（按{r['via']}）{'' if r['in_office'] else '  在休息室'}")


def cmd_suggest(a):
    _, label = resolve_tag(a.tag)
    recs = [r for r in ledger_read() if r.get("tag") == label and not r.get("type")]
    if not recs:
        print(f"「{label}」还没有派活记录。派活时加 --tag {label}，以后就能比较。")
    per = {}
    for r in recs:
        s = per.setdefault(r["agent"], {"n": 0, "sec": 0, "cny": 0.0, "weekly": 0.0})
        s["n"] += 1
        s["sec"] += r.get("seconds") or 0
        c = r.get("cost") or {}
        s["cny"] += c.get("cny") or 0
        s["weekly"] += c.get("weekly") or 0
    names = agents_dict()
    for aid, s in sorted(per.items(), key=lambda x: -x[1]["n"]):
        extra = f"平均 ¥{s['cny'] / s['n']:.2f}" if s["cny"] else (f"平均周额度 {s['weekly'] / s['n']:.1f}%" if s["weekly"] else "")
        print(f"{names.get(aid, {}).get('name', aid):16s} {s['n']} 次，平均 {s['sec'] / s['n'] / 60:.1f} 分钟 {extra}")
    cfg = load_config()
    cur = who(cfg, label)
    if cur:
        print("当前偏好：" + "、".join(f"{r['name']}（{LEVELS[r['level']]}）" for r in cur))


def _cfg_txn(fn):
    """命令行改配置：读→改→写整段在配置锁里，不会把别处（界面、钩子）刚改的设置用旧快照盖回去。"""
    import functools

    @functools.wraps(fn)
    def run(a):
        from config import locked
        with locked("config"):
            return fn(a)
    return run


@_cfg_txn
def cmd_assign(a):
    cfg = load_config()
    if a.agent not in agents_dict(cfg):
        sys.exit(f"不认识的 agent：{a.agent}")
    key, label = resolve_tag(a.tag)
    jobs = cfg["jobs"].setdefault(a.agent, {})
    if a.level == "clear":
        jobs.pop(key, None)
    else:
        jobs[key] = a.level
    save_config(cfg)
    print(f"{a.agent}：{label} → {LEVELS.get(a.level, '去掉')}")


@_cfg_txn
def cmd_move(a):
    cfg = load_config()
    if a.agent not in agents_dict(cfg):
        sys.exit(f"不认识的 agent：{a.agent}")
    for k in ("office", "lounge"):
        if a.agent in cfg["roster"][k]:
            cfg["roster"][k].remove(a.agent)
    cfg["roster"][a.place].append(a.agent)
    save_config(cfg)
    print(f"{a.agent} 去了{'办公室' if a.place == 'office' else '休息室'}")


def cmd_quota(a):
    c = quota.codex(refresh=a.refresh)
    if c.get("ok"):
        print(f"GPT（{c.get('plan') or 'Codex'}）：5 小时已用 {c.get('five_hour')}%，本周已用 {c.get('weekly')}%，credits {c.get('credits')}")
    else:
        print("GPT：" + c.get("error", "读不到"))
    d = quota.deepseek()
    for acc in d.get("accounts") or []:
        print(f"DeepSeek 余额：¥{acc['balance']:.2f}（{'、'.join(sorted(set(acc['used_by'])))} 在用）")
    for e in d.get("errors") or []:
        print("DeepSeek 读取失败：" + e)
    print("Claude：" + quota.claude()["error"])


def cmd_bill(a):
    name, _, _, batch = project(a.project)
    b = bill(name, a.batch if a.batch is not None else (batch or None))
    names = agents_dict()
    print(f"项目「{name}」{'批次 ' + (a.batch or batch) if (a.batch or batch) else '全部'}：")
    for r in b["per_agent"]:
        parts = [f"{r['runs']} 次", f"{r['seconds'] / 60:.0f} 分钟"]
        if r["cny"]:
            parts.append(f"¥{r['cny']:.2f}")
        if r["weekly"] or r["five_hour"]:
            parts.append(f"GPT 周额度 {r['weekly']:g}% / 5 小时 {r['five_hour']:g}%")
        if r["unknown"]:
            parts.append(f"{r['unknown']} 次没有额度数据")
        print(f"  {names.get(r['agent'], {}).get('name', r['agent']):16s} " + "，".join(parts))
    print(f"  合计花费 ¥{b['total_cny']:.2f}")


def cmd_batch(a):
    name, _, _, _ = project(a.project)
    set_batch(name, a.name)
    print(f"项目「{name}」当前批次：{a.name or '（不分批）'}")


def cmd_project(a):
    from config import create_project, current_project, projects, set_current
    if a.action == "list":
        cur = current_project()
        print(("* " if not cur else "  ") + "无（通用）")
        for k, v in projects().items():
            if not k.startswith("_"):
                print(("* " if k == cur else "  ") + f"{k}  {v['root']}")
    elif a.action == "use":
        set_current("" if a.name in (None, "无", "通用") else a.name)
        print(f"当前项目：{a.name or '无'}")
    elif a.action == "new":
        create_project(a.name, a.root)
        set_current(a.name)
        print(f"已新建并切到项目「{a.name}」，项目说明在 ~/.agentbus/projects/{a.name}/brief.md")


def cmd_lesson(a):
    import playbook
    _, _, bus, _ = project(a.project)
    if a.action == "propose":
        pid = playbook.propose(bus, a.text, [t.strip() for t in (a.tags or "").replace("，", ",").split(",") if t.strip()], a.scope,
                               a.sender or os.environ.get("AGENTBUS_SELF", "user"))
        print(f"已提议（{pid}），等主人在「经验」页采纳")
    else:
        for p in playbook.proposals(bus):
            print(f"{p['pid']}  [{p['from']}] {p['text']}  ({'、'.join(p['tags'])})")


@_cfg_txn
def cmd_playbook(a):
    import playbook
    name, root, bus, _ = project(a.project)
    cfg = load_config()
    pb = playbook.load(bus)
    if a.action == "show":
        if not pb:
            print(f"项目「{name}」还没有经验包。用 agentbus playbook draft 让主位从现有文档生成一份草稿。")
            return
        eq = playbook.equip_of(cfg, name)
        m = pb["manifest"]
        print(f"经验包「{m['name']}」v{m['version']}（{'在用' if eq['enabled'] else '这个项目不用，留着分享时导出'}）：{len(pb['steps'])} 步、{len(pb['lessons'])} 条经验")
        for st in pb["steps"]:
            print(f"  {st['step']}. {st['name']}  [{'、'.join(st['tags'])}]  需要：{', '.join(st['caps'])}{'  （已卸下）' if st['id'] in eq['steps_off'] else ''}")
        if a.name:  # 预览这个岗位派活时会带上什么
            step, lessons = playbook.pick(pb, eq, a.name, a.name)
            print("\n" + playbook.prompt_block(pb, step, lessons))
    elif a.action in ("on", "off"):
        cfg.setdefault("lineups", {}).setdefault(name, {"roster": cfg["roster"], "jobs": cfg["jobs"]}).setdefault("playbook", {})["enabled"] = a.action == "on"
        save_config(cfg)
        print(f"项目「{name}」{'使用经验包' if a.action == 'on' else '不用经验包（按项目文件夹里的文档干活）'}")


def main():
    ap = argparse.ArgumentParser(prog="agentbus")
    ap.add_argument("-p", "--project")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ask"); s.add_argument("agent", help="agent 名字，或者岗位词条"); s.add_argument("task")
    s.add_argument("--screen", action="store_true", help="这活要占屏幕/鼠标（录屏、操控电脑）；岗位是录屏这类时会自动判断")
    s.add_argument("--new", action="store_true", help="开新会话"); s.add_argument("--file", help="把文件内容附在任务后面")
    s.add_argument("--tag", help="这活属于哪个岗位词条"); s.add_argument("--from", dest="sender")
    s.add_argument("--title", help="十来个字的短标题，悬浮窗和任务列表显示它（不写就从任务里自动提炼）")
    s.add_argument("--allow-credits", action="store_true", help="GPT 额度用完后仍然派（会扣付费 credits）"); s.set_defaults(f=cmd_ask)
    s = sub.add_parser("wait", help="等派出去的活做完：默认任意一单做完就返回（Bash 后台跑，做完会叫醒主位）")
    s.add_argument("jobs", nargs="*", help="MCP 派活返回的 job id，不写就等这个窗口派出去的全部")
    s.add_argument("--all", action="store_true", help="等全部做完才返回"); s.add_argument("--timeout", type=float, default=240, help="最多等几分钟")
    s.set_defaults(f=cmd_wait)
    s = sub.add_parser("news", help="看派出去的活有没有新结果（看过的不再显示）")
    s.add_argument("--peek", action="store_true"); s.add_argument("--hook", action="store_true", help="给 Claude Code 钩子用：没有就什么都不打印")
    s.set_defaults(f=cmd_news)
    s = sub.add_parser("chat-talk", help="聊天室群聊的主循环（服务在后台起，不用手动跑）")
    s.add_argument("channel"); s.add_argument("msg"); s.set_defaults(f=lambda a: __import__("chat").run_talk(a.channel, a.msg))
    s = sub.add_parser("hook", help="给 Claude / GPT 的钩子用：session = 新对话开始，prompt = 主人发了一条消息")
    s.add_argument("event", choices=["session", "prompt"]); s.add_argument("--agent", default="claude"); s.set_defaults(f=cmd_hook)
    s = sub.add_parser("hello", help="接入 agentbus：输出握手信息（主人把接入口令贴进对话框时，agent 跑这条）")
    s.add_argument("--agent", help="你是哪个 agent（codex / zcode / opencode …）"); s.set_defaults(f=cmd_hello)
    s = sub.add_parser("post"); s.add_argument("agent"); s.add_argument("text")
    s.add_argument("--from", dest="sender"); s.set_defaults(f=cmd_post)
    s = sub.add_parser("inbox"); s.add_argument("agent"); s.add_argument("--peek", action="store_true"); s.set_defaults(f=cmd_inbox)
    s = sub.add_parser("board"); s.add_argument("-n", type=int, default=30); s.add_argument("--full", action="store_true"); s.set_defaults(f=cmd_board)
    s = sub.add_parser("agents"); s.set_defaults(f=cmd_agents)
    s = sub.add_parser("who"); s.add_argument("tag"); s.set_defaults(f=cmd_who)
    s = sub.add_parser("suggest"); s.add_argument("tag"); s.set_defaults(f=cmd_suggest)
    s = sub.add_parser("assign"); s.add_argument("agent"); s.add_argument("tag")
    s.add_argument("level", choices=["prefer", "ok", "avoid", "clear"]); s.set_defaults(f=cmd_assign)
    s = sub.add_parser("move"); s.add_argument("agent"); s.add_argument("place", choices=["office", "lounge"]); s.set_defaults(f=cmd_move)
    s = sub.add_parser("quota"); s.add_argument("--refresh", action="store_true"); s.set_defaults(f=cmd_quota)
    s = sub.add_parser("bill"); s.add_argument("--batch"); s.set_defaults(f=cmd_bill)
    s = sub.add_parser("project", help="项目：list / use <名字或 无> / new <名字> [--root 目录]")
    s.add_argument("action", choices=["list", "use", "new"]); s.add_argument("name", nargs="?"); s.add_argument("--root")
    s.set_defaults(f=cmd_project)
    s = sub.add_parser("playbook", help="经验包：show [岗位] 看内容（带岗位就预览派活时注入什么）/ on / off")
    s.add_argument("action", choices=["show", "on", "off"]); s.add_argument("name", nargs="?"); s.set_defaults(f=cmd_playbook)
    s = sub.add_parser("lesson", help="经验提议：propose \"一句话\" --tags 岗位 / list")
    s.add_argument("action", choices=["propose", "list"]); s.add_argument("text", nargs="?", default="")
    s.add_argument("--tags"); s.add_argument("--scope", default="本项目"); s.add_argument("--from", dest="sender"); s.set_defaults(f=cmd_lesson)
    s = sub.add_parser("batch"); s.add_argument("name", nargs="?", default=""); s.set_defaults(f=cmd_batch)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
