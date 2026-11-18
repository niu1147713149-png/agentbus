#!/usr/bin/env python3
"""agentbus 的 MCP 服务（stdio）：让调度 agent（Claude 桌面端等）不用敲命令就能看办公室、查额度、调岗位、派活。

注册：claude mcp add --scope user agentbus -- python3 ~/agentbus/core/mcp_server.py
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import quota  # noqa: E402
from catalog import LEVELS  # noqa: E402
from config import HOME, agent_of_client, agents_dict, bill, load_config, load_json, project, resolve_tag, save_config, save_json, update_json, who, whoami  # noqa: E402

JOBS = HOME / "jobs"
CLI = HERE / "agentbus.py"
URL = "http://127.0.0.1:" + (os.environ.get("AGENTBUS_PORT") or "8790")


def _ui_hint(agent):
    if agent == "codex":
        return "不要自动打开浏览器或应用窗口；只有用户明确要求打开界面时才打开。"
    return (f"界面：{URL}——如果你有内置浏览器（Claude 桌面端：preview_start 传 url），现在就打开它给主人看进度。"
            "视口用跟随面板宽度的响应式（不要固定成 1280×720 之类的桌面尺寸再缩放），页面会按面板实际宽度自己排版。")


TOOLS = [
    {"name": "agentbus_status", "description": "看办公室：谁在岗、谁在休息室、谁在干活、手上什么活、挂了什么岗位。",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string", "description": "项目名，默认当前默认项目"}}}},
    {"name": "agentbus_quota", "description": "现在的真实额度：GPT 的 5 小时/本周已用百分比和 credits，DeepSeek 账户余额（人民币）。",
     "inputSchema": {"type": "object", "properties": {"refresh": {"type": "boolean"}}}},
    {"name": "agentbus_bill", "description": "项目账单：每个 agent 派活次数、用时、真实消耗（GPT 额度百分比、DeepSeek 人民币）。batch 不填就用项目当前批次，填空字符串看全部。",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}, "batch": {"type": "string"}}}},
    {"name": "agentbus_who", "description": "某个岗位（词条或大类，如“封面”“视觉设计”）该派给谁：按主人偏好排序，首选在前，办公室里的优先。",
     "inputSchema": {"type": "object", "properties": {"tag": {"type": "string"}}, "required": ["tag"]}},
    {"name": "agentbus_assign", "description": "给 agent 挂岗位或改偏好：tag 是词条或大类名，level = prefer 首选 / ok 可用 / avoid 不用 / clear 去掉。只在主人要求时改。",
     "inputSchema": {"type": "object", "properties": {"agent": {"type": "string"}, "tag": {"type": "string"},
                                                       "level": {"type": "string", "enum": ["prefer", "ok", "avoid", "clear"]}},
                     "required": ["agent", "tag", "level"]}},
    {"name": "agentbus_move", "description": "把 agent 调进办公室（office，上岗）或休息室（lounge）。",
     "inputSchema": {"type": "object", "properties": {"agent": {"type": "string"}, "place": {"type": "string", "enum": ["office", "lounge"]}},
                     "required": ["agent", "place"]}},
    {"name": "agentbus_dispatch", "description": "派活：agent 填名字或岗位词条（按偏好自动挑人；处于封印状态的队员不能接活）。后台运行，立刻返回 job id。"
                                                   "要在做完/失败时被叫醒：接着用 Bash 后台（run_in_background）跑返回里给的 `agentbus wait <job id>`，它一结束你就会收到结果，不用主人提醒。",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string"}, "task": {"type": "string", "description": "一句话交代"},
         "file": {"type": "string", "description": "任务书文件路径，内容附在任务后面"},
         "tag": {"type": "string", "description": "这活属于哪个岗位词条，用来记账和比较"},
         "title": {"type": "string", "description": "十来个字的短标题（比如「做 42 号封面」），悬浮窗显示它；不写就从任务里自动提炼"},
         "new": {"type": "boolean", "description": "开新会话，默认 true"}, "project": {"type": "string"}},
         "required": ["agent", "task"]}},
    {"name": "agentbus_job", "description": "查 agentbus_dispatch 派出去的活：还在跑 / 做完了，做完的给出回复和消耗。",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "agentbus_playbook_fit", "description": "主人说“把经验包融入我的习惯 / 帮我装配经验包”时用：返回分析材料（经验包要点、主人习惯文件的位置、阵容和派活记录）和要写的建议格式。你读完材料和习惯文件后，把装配建议写成 JSON 到返回的路径，主人会在 agentbus「经验」页逐项确认。",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string", "description": "项目名，默认当前项目"}}}},
    {"name": "agentbus_hello", "description": "开工握手：每个新对话一开始先调一次（不用主人说）。它会告诉你这个窗口接没接入 agentbus：接入了就返回握手信息（你是哪间办公室的主位、战备、经验包、派活规则）；主人没接入就告诉你当没有 agentbus、正常干活。主人明确说了“接入 / 用 agentbus”时传 explicit=true，直接接入不再问。",
     "inputSchema": {"type": "object", "properties": {"explicit": {"type": "boolean", "description": "主人在对话里明确要用 agentbus"}, "project": {"type": "string"}}}},
    {"name": "agentbus_playbook_step", "description": "主位自己动手做某一步之前用：返回项目经验包里这一步的做法和相关经验（派给队员时 agentbus 会自动带上，主位自己做时要主动查）。tag 填职能词条或步骤名，比如「回填与打包」「批量整理文件」「审稿改稿」。",
     "inputSchema": {"type": "object", "properties": {"tag": {"type": "string"}, "project": {"type": "string"}}, "required": ["tag"]}},
    {"name": "agentbus_playbook_refresh", "description": "主人说「按项目文档重新整理经验包」时用（一般是要分享给别人之前）：返回要读的项目文档和经验包格式，你读完后把新版写到返回的目录，主人在「经验」页点替换。",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}}},
    {"name": "agentbus_playbook_merge", "description": "主人导入了别人的经验包、说「把它融合进本地文档 / 融合经验」时用：返回经验包和本地工作流、复盘文档的位置，以及融合步骤。你对比后先把建议列给主人，主人同意再改本地文件；经验包本身不用来派活。",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}}},
    {"name": "agentbus_post", "description": "给某个队员留言（不等回复）。它下次接活时会在提示里看到。",
     "inputSchema": {"type": "object", "properties": {"agent": {"type": "string"}, "text": {"type": "string"}, "project": {"type": "string"}}, "required": ["agent", "text"]}},
    {"name": "agentbus_inbox", "description": "看给主位（或某个队员）的未读留言，比如队员之间转来的问题、看门狗的提醒。",
     "inputSchema": {"type": "object", "properties": {"agent": {"type": "string", "description": "默认是你自己（当前主位）"}, "project": {"type": "string"}}}},
    {"name": "agentbus_news", "description": "看你派出去的活有没有新结果（做完 / 失败），看过的不再显示。其他 agentbus 工具的返回里也会自动附上新结果。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "agentbus_open", "description": "应用户明确要求打开 agentbus 办公室界面（应用窗口或浏览器）。不是连接或健康检查工具。",
     "inputSchema": {"type": "object", "properties": {}}},
]


def status(args):
    import service  # 复用本地服务的状态拼装
    s = service.state(args.get("project"))
    lines = [f"项目：{s['project']}" + (f"（批次 {s['batch']}）" if s["batch"] else "")]
    names = {c["id"]: c["name"] for c in service.catalog()["categories"]}
    for place, title in (("office", "办公室"), ("lounge", "休息室")):
        lines.append(f"\n{title}：")
        for a in s["agents"]:
            if a["place"] != place or (place == "lounge" and not a["installed"]):
                continue
            jobs = "、".join(f"{names.get(k[4:], k[4:])}{'' if v == 'prefer' else '（' + LEVELS[v] + '）'}" for k, v in a["jobs"].items())
            lines.append(f"- {a['name']}（{a['id']}）：{service_state_text(a['state'])}"
                         + (f"，在做：{a['task']}" if a["task"] else "") + (f"；岗位：{jobs}" if jobs else ""))
    missing = [a["name"] for a in s["agents"] if not a["installed"]]
    if missing:
        lines.append("\n没安装：" + "、".join(missing))
    return "\n".join(lines)


def playbook_step(args):
    import playbook
    pname, _, bus, _ = project(args.get("project"))
    pb = playbook.load(bus)
    if not pb:
        return f"项目「{pname}」没有经验包。"
    cfg = load_config()
    eq = playbook.equip_of(cfg, pname)
    if not eq["enabled"]:
        return f"项目「{pname}」不用经验包：做法以项目文件夹里的文档为准（项目说明、CLAUDE.md、复盘经验这类），动手前先读对应的那份。"
    step, lessons = playbook.pick(pb, eq, args["tag"], args["tag"], head=args["tag"])
    block = playbook.prompt_block(pb, step, lessons, playbook.personal_of(bus), playbook.pick_personal(playbook.personal_of(bus), step, args["tag"], args["tag"]))
    return (block or f"经验包里没有和「{args['tag']}」相关的内容。") + "\n\n（照这一步的做法来；能交给队员的活就派出去，主位只统筹。）"


JOINED = Path(os.environ.get("AGENTBUS_HOME") or Path.home() / ".agentbus") / "joined.json"  # 和服务端写的是同一份


_FALLBACK_CONV = "mcp-" + __import__("uuid").uuid4().hex[:8]  # 拿不到窗口 id 时：这个 MCP 进程自己的随机编号（不截 PID，不会撞）


def _conv_full():
    """这个对话窗口的完整编号：Claude 有会话 id、GPT 有 thread id；都没有就用本进程的随机编号。握手、主位登记、派活记录都用它。"""
    return (os.environ.get("AGENTBUS_CONV") or os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")
            or os.environ.get("CODEX_SESSION_ID") or _FALLBACK_CONV)


def _conv_key():
    """接入记录里的键（前 12 位）。"""
    return _conv_full()[:12]


def gate(args):
    """新对话要不要接入 bus：explicit / 设成总是 → 直接接入；设成从不 → 不接；否则在悬浮窗问主人，最多等 25 秒。
    返回 True = 接入了。结果记在 ~/.agentbus/joined.json（钩子和 MCP 共用）。"""
    from config import load_config as _lc, load_json as _lj, update_json as _uj, whoami
    conv, me = _conv_key(), whoami() or "claude"
    joined = _lj(JOINED, {})
    rec = joined.get(conv)
    if args.get("explicit") or (rec and not rec.get("declined")):
        if not rec or rec.get("declined"):
            _uj(JOINED, lambda j: j.__setitem__(conv, {"agent": me, "cwd": os.getcwd(), "t": time.time(), "briefed": False}))
        return True  # briefed 等握手真的生成好了再标（hello 末尾），失败时钩子下一句还会再送
    if rec and rec.get("declined"):
        return False
    mode = (_lc().get("settings") or {}).get("auto_join", "ask")
    if mode == "always":
        _uj(JOINED, lambda j: j.__setitem__(conv, {"agent": me, "cwd": os.getcwd(), "t": time.time(), "briefed": False}))
        return True
    if mode == "never":
        return False
    import urllib.request
    try:
        body = json.dumps({"agent": me, "conv": conv, "cwd": os.environ.get("AGENTBUS_CWD") or os.getcwd()}).encode()
        urllib.request.urlopen(urllib.request.Request(URL + "/api/session/new", data=body, headers={"Content-Type": "application/json"}), timeout=2).read()
    except Exception:
        return False
    end = time.time() + 25
    while time.time() < end:  # 等主人在悬浮窗里点
        rec = _lj(JOINED, {}).get(conv)
        if rec:
            return not rec.get("declined")
        time.sleep(1)
    return False


NOT_JOINED = ("这个窗口没有接入 agentbus（主人没点接入，或者设置了不自动接入）：当没有 agentbus，正常干活，不要再调别的 agentbus 工具。"
              "主人之后说“接入 agentbus / 用 bus”时，再调 agentbus_hello 并传 explicit=true。")


def hello(args):
    if not gate(args):
        return NOT_JOINED
    from config import touch_lead as _tl, whoami as _w
    _tl(_w(), "mcp", _conv_full())  # 接入了才登记成主位；拒绝接入的窗口不改主位
    import service
    s = service.state(args.get("project"))
    conv = os.environ.get("AGENTBUS_CONV") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    from config import mounted_packs
    me = whoami() or s["lead"] or "claude"
    cwd = os.environ.get("AGENTBUS_CWD") or os.getcwd()
    office = next((o for o in s.get("offices") or [] if o["id"] == me), None)
    lineup = office["lineup"]["office"] if office else [a["id"] for a in s["agents"] if a["place"] == "office" and a["id"] != me]
    jobs_of = (office["lineup"]["jobs"] if office else {})
    lines = [_ui_hint(me),
             f"你是「{(office or {}).get('name') or (next((a['name'] for a in s['agents'] if a['id'] == me), me) + ' 办公室')}」的主位；这个窗口的项目文件夹：{cwd}（派活、记账都跟着这个文件夹走）；协同会话：{conv[:12] or '（没有窗口信息，会话按 agent 共用）'}"]
    names = {c["id"]: c["name"] for c in service.catalog()["categories"]}
    agents = {a["id"]: a for a in s["agents"]}
    lines.append("这间办公室的战备（按岗位挑人先从这里挑；这里没人能干时才从休息室调，派活结果里会写明）：")
    for aid in lineup:
        a = agents.get(aid)
        if not a:
            continue
        j = jobs_of.get(aid) or a["jobs"]
        jobs = "、".join(names.get(k[4:], k[4:]) for k, v in j.items() if v == "prefer")
        lines.append(f"- {a['name']}（{a['id']}）{service_state_text(a['state'])}；大脑 {(a.get('brain') or {}).get('label') or '默认'}"
                     + (f"；主力：{jobs}" if jobs else "") + ("；分身" if a.get("clone_of") else "") + ("；API 队员" if a.get("api") else ""))
    from config import mounted_skills
    sk = mounted_skills(load_config(), me, cwd)
    if sk:
        lines.append("装在这里的 skill（主位自己干活、派活时都可以用，用到先读它的 SKILL.md）：" + "；".join(f"{n}（{p}/SKILL.md）" for n, d, p in sk))
    packs = mounted_packs(load_config(), me, cwd)
    if packs:
        lib = {x["id"]: x for x in s.get("packs") or []}
        lines.append("装在这里的经验包（派活时自动带上那一步的做法）：" + "、".join(f"{lib.get(x, {}).get('name', x)} v{lib.get(x, {}).get('version', '')}" for x in packs)
                     + "。主位自己动手前用 agentbus_playbook_step 查那一步的做法。")
    pb = (s.get("playbook") or {}) if not packs else {}
    if pb:
        lines.append(f"经验包：{pb.get('name')} v{pb.get('version')}（{pb.get('steps')} 步、{pb.get('lessons')} 条经验，"
                     + ("用着）" if pb.get("enabled") else "这个项目不用，只在分享时导出）。做法以项目文件夹里的文档为准（项目说明、CLAUDE.md、复盘经验），新规矩写进这些文档。"))
    sc = s.get("scheduler") or {}
    lines.append(f"调度：屏幕锁{'开' if sc.get('screen_lock') else '关'}" + (f"（{('、'.join(sc['screen']))} 在用屏幕）" if sc.get("screen") else "")
                 + "；并发上限 " + "、".join(f"{k} {v}" for k, v in (sc.get("limits") or {}).items() if k in ("codex", "deepseek", "claude")))
    if s["running"]:
        lines.append("正在跑：" + "；".join(f"{r['name']}：{r['task']}" + (f"（{r['queued']}）" if r.get("queued") else "") for r in s["running"]))
    if pb and pb.get("enabled"):
        try:
            st = service.playbook_state()
            mine = [x["name"] for x in st.get("steps") or [] if x.get("on") and (not x.get("who") or x["who"][0]["id"] == s["lead"])]
            if mine:
                lines.append(f"经验包里这几步现在落在主位（或没人负责）：{'、'.join(mine)}。自己动手前先调 agentbus_playbook_step 看这一步的做法"
                             "（命令行：`agentbus -p 项目 playbook show <职能>`）；能派给队员的优先派出去。")
        except Exception:
            pass
        lines.append("学到新规矩（主人新定的、踩坑总结的）：用 `agentbus lesson propose \"一句话\" --tags 职能` 提议进经验包，主人在「经验」页采纳；只写进文档的话，下次派活队员拿不到。")
    lines.append("派活规则：用 agentbus_dispatch 或 `agentbus ask`，带上 --tag 职能（或环境变量 AGENTBUS_TAG）——会自动注入经验包那一步的做法；录屏、操控电脑的活会自动排队等屏幕。Claude 是否接活取决于队员页的封印设置。")
    lines.append("回音规则：派出去的活一定要能叫醒你——要么 Bash 后台跑 `agentbus ask …`（做完就结束），要么 agentbus_dispatch 之后 Bash 后台跑 `agentbus wait <job id>`。"
                 "不要干等、也不要等主人提醒；新结果也会附在之后任何一个 agentbus 工具的返回里。")
    from config import update_json as _uj
    conv_k = _conv_key()
    _uj(JOINED, lambda j: j[conv_k].__setitem__("briefed", True) if conv_k in j else None)  # 握手生成好了：钩子不用再塞一遍
    return "\n".join(lines)


def service_state_text(st):
    return {"working": "干活中", "queued": "排队中", "break": "刚忙完", "off": "休息", "manual": "等传话（网页版）", "absent": "没安装"}.get(st, st)


def run_cli(*args):
    import pf
    p = subprocess.run([*pf.cli_cmd(), *args], capture_output=True, text=True, timeout=60)
    return (p.stdout + p.stderr).strip()


def dispatch(args):
    agents = agents_dict(load_config())
    from config import sealed
    if sealed(agents.get(args.get("agent")) or {}):
        return "Claude 只当主位，不接派活：这件事你自己做，或者派给别的队员。"
    jid = start_job(args)
    return (f"已派出，job id：{jid}。\n"
            f"现在就用 Bash 后台（run_in_background）跑：agentbus wait {jid}\n"
            "——它在这单做完或失败时结束，你会被自动叫醒拿到回复，不要等主人提醒；同时可以接着干别的。")


def start_job(args, env_extra=None, cwd=None):
    """后台派活，返回 job id。MCP 和 A2A 接口共用。"""
    JOBS.mkdir(parents=True, exist_ok=True)
    jid = time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    import pf
    from config import project_of_dir
    cmd = pf.cli_cmd()
    args = dict(args)
    if not args.get("project"):  # 从哪个目录的对话里派的，就记在哪个项目
        args["project"] = project_of_dir(cwd or os.getcwd())
    if args.get("project"):
        cmd += ["-p", args["project"]]
    cmd += ["ask", args["agent"], args["task"]]
    if args.get("new", True):
        cmd.append("--new")
    if args.get("file"):
        cmd += ["--file", args["file"]]
    if args.get("title"):
        cmd += ["--title", str(args["title"])[:40]]
    if args.get("tag"):
        cmd += ["--tag", args["tag"]]
    log = JOBS / f"{jid}.log"
    meta = JOBS / f"{jid}.json"
    root = str(project(args.get("project"))[1])
    save_json(meta, {"pid": None, "cmd": cmd[2:], "start": time.time(), "agent": args["agent"], "status": "starting", "project": args.get("project"), "cwd": root})
    try:
        with open(log, "w", encoding="utf-8") as f:
            p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, **pf.group_kwargs(),
                                 cwd=root, env={**os.environ, "AGENTBUS_SELF": os.environ.get("AGENTBUS_SELF", "claude"), "AGENTBUS_CONV": _conv_full(), **(env_extra or {}), "AGENTBUS_JOB_ID": jid, "PYTHONIOENCODING": "utf-8"})
        update_json(meta, lambda d: d.update(pid=p.pid))  # 保留子进程可能已经写好的结束状态
    except Exception as e:
        finish_job(jid, "failed", f"任务启动失败：{e}")
        raise
    return jid


JOB_LABELS = {"starting": "启动中", "queued": "排队中", "running": "执行中", "ok": "已完成",
              "failed": "失败", "error": "失败", "stopped": "已停止", "stalled": "超时停止",
              "not_started": "未开工", "interrupted": "意外中断", "unknown": "记录缺失"}
TERMINAL_JOBS = {"ok", "failed", "error", "stopped", "stalled", "not_started", "interrupted"}


def finish_job(jid, status, reply, run=None):
    """保存最终状态，服务重启或进程退出后仍可查询。"""
    if not jid:
        return
    update_json(JOBS / f"{jid}.json", lambda d: d.update(status=status, reply=reply, run=run, end=time.time()))


def job_snapshot(jid):
    """同一份状态供 MCP 和聊天室使用；兼容尚未保存最终状态的旧任务。"""
    import pf
    meta = load_json(JOBS / f"{jid}.json", {})
    status, reason, progress = meta.get("status"), "", None
    if not meta:
        status = "unknown"
    elif status not in TERMINAL_JOBS:
        records = (load_json(f, {}) for f in (HOME / "running").glob("*.json"))
        record = next((r for r in records if meta.get("pid") and r.get("pid") == meta["pid"]), {})
        if pf.pid_alive(meta.get("pid")):
            status = "queued" if record.get("queued") else "running" if record.get("worker") else "starting"
            reason = record.get("queued") or (f"第 {record['retry']} 次重试" if record.get("retry") else "")
            progress = record.get("progress")
        elif not meta.get("pid") and time.time() - meta.get("start", 0) < 15:
            status = "starting"
        else:
            log = JOBS / f"{jid}.log"
            tail = log.read_text("utf-8", "replace")[-1000:] if log.exists() else ""
            status = ("ok" if "[agentbus] 做完了" in tail else "stopped" if record.get("cancel") else
                      "failed" if "[agentbus] 没做成" in tail else "interrupted")
    return {"status": status, "label": JOB_LABELS.get(status, status), "reason": reason,
            "reply": meta.get("reply"), "run": meta.get("run"), "cwd": meta.get("cwd"), "progress": progress}


def job_state(jid):
    """(还在跑吗, 输出全文, 开始时间)"""
    import pf
    meta = json.loads((JOBS / f"{jid}.json").read_text("utf-8"))
    alive = pf.pid_alive(meta["pid"])
    if alive and not pf.IS_WIN:
        try:
            st = subprocess.run(["ps", "-o", "stat=", "-p", str(meta["pid"])], capture_output=True, text=True).stdout.strip()
            alive = bool(st) and not st.startswith("Z")
        except OSError:
            alive = False
    return alive, (JOBS / f"{jid}.log").read_text("utf-8", "ignore"), meta["start"]


def job(args):
    alive, out, start = job_state(args["id"])
    snap = job_snapshot(args["id"])
    mins = (time.time() - start) / 60
    if snap["status"] in TERMINAL_JOBS:
        return f"{snap['label']}：\n{snap.get('reply') or out[-6000:]}"
    if alive or snap["status"] == "starting":
        return f"{snap['label']}（{mins:.0f} 分钟）{('：' + snap['reason']) if snap['reason'] else ''}。目前输出：\n{out[-800:]}"
    # 收尾时 agentbus ask 会在输出末尾写一行「做完了 / 没做成」：按它判断，不按有没有文字猜
    tail = out[-600:]
    if "[agentbus] 做完了" in tail:
        return f"做完了：\n{out[-6000:]}"
    if "[agentbus] 没做成" in tail:
        return f"没做成（失败、被停掉或卡住）：\n{out[-6000:]}"
    return f"派活进程已经结束，但没走到收尾（可能被强杀）：\n{out[-6000:]}"


def call(name, args):
    if name == "agentbus_dispatch":  # 在统筹的就是主位（hello 要等主人同意接入后才算，见 hello）
        from config import touch_lead, whoami
        touch_lead(whoami(), "mcp", _conv_full())
    if name == "agentbus_status":
        return status(args)
    if name == "agentbus_quota":
        return run_cli("quota", *(["--refresh"] if args.get("refresh") else []))
    if name == "agentbus_bill":
        cli = (["-p", args["project"]] if args.get("project") else []) + ["bill"]
        if "batch" in args:
            cli += ["--batch", args["batch"]]
        return run_cli(*cli)
    if name == "agentbus_who":
        return run_cli("who", args["tag"])
    if name == "agentbus_assign":
        return run_cli("assign", args["agent"], args["tag"], args["level"])
    if name == "agentbus_move":
        return run_cli("move", args["agent"], args["place"])
    if name == "agentbus_dispatch":
        return dispatch(args)
    if name == "agentbus_job":
        return job(args)
    if name == "agentbus_playbook_fit":
        import playbook
        from config import ledger_read, set_current, current_project
        if args.get("project"):
            set_current(args["project"])
        pname, root, bus, _ = project()
        pb = playbook.load(bus)
        if not pb:
            return f"项目「{pname}」还没有经验包。"
        cfg = load_config()
        d = playbook.pb_dir(bus)
        ctx = d / ".fit-context.md"
        ctx.write_text(playbook.fit_context(pb, root, bus, cfg, agents_dict(cfg), ledger_read()), "utf-8")
        return (f"分析材料：{ctx}（先读它，再逐个读里面列出的主人习惯文件）\n"
                f"把装配建议写成 JSON 到：{d / 'proposal.json'}\n格式：\n{playbook.FIT_SCHEMA}\n"
                "每一步、每条经验都要给出判断；理由用一句大白话写给主人看。写完告诉主人去 agentbus「经验」页确认装配。")
    if name == "agentbus_hello":
        return hello(args)
    if name == "agentbus_playbook_step":
        return playbook_step(args)
    if name == "agentbus_playbook_merge":
        import playbook
        pname, root, bus, _ = project(args.get("project"))
        pb = playbook.load(bus)
        if not pb:
            return f"项目「{pname}」还没有导入经验包。先在 agentbus「经验」页点「导入」。"
        ctx = playbook.pb_dir(bus) / ".merge-context.md"
        ctx.write_text(playbook.merge_context(pb, root, bus), "utf-8")
        return f"融合材料写好了：{ctx}\n先读它，再按里面的步骤来。\n\n" + playbook.merge_context(pb, root, bus)
    if name == "agentbus_playbook_refresh":
        import service
        pname, root, bus, _ = project(args.get("project"))
        brief, target, _docs = service.draft_brief(pname, root, bus)
        return brief + f"\n\n写完告诉主人：去 agentbus「经验」页点「换成新版」，旧版会整个留档。"
    if name == "agentbus_post":
        from config import whoami
        return run_cli(*(["-p", args["project"]] if args.get("project") else []), "post", args["agent"], args["text"], "--from", whoami() or "claude")
    if name == "agentbus_inbox":
        from config import whoami
        return run_cli(*(["-p", args["project"]] if args.get("project") else []), "inbox", args.get("agent") or whoami() or "claude")
    if name == "agentbus_news":
        return run_cli("news")
    if name == "agentbus_open":
        import pf
        return pf.open_agentbus()
    raise ValueError(f"没有这个工具：{name}")


def _announce(client):
    """通知本地服务有客户端接入；Codex 连接失败时不启动应用或浏览器。"""
    from config import is_working_as_member
    import urllib.request
    me = agent_of_client(client) or whoami()
    if me and is_working_as_member(me):  # 被派活的队员干活时连上来的，不打招呼、不抢主位
        return
    if me and int(os.environ.get("AGENTBUS_DEPTH", "0")) == 0:
        os.environ.setdefault("AGENTBUS_SELF", me)  # 主位要等 agentbus_hello 接入后才登记，连上来还不算
    body = json.dumps({"agent": me, "client": client}).encode()
    for attempt in range(2):
        try:
            req = urllib.request.Request(URL + "/api/hello", data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=2).read()
            return
        except Exception:
            if me == "codex":
                return
            if attempt == 0:
                import pf
                pf.open_agentbus()
                time.sleep(4)


def _with_news(name, text):
    """任何工具的返回都顺带附上“你派出去的活的新结果”，主位不用专门去查。"""
    if name in ("agentbus_news", "agentbus_job"):
        return text
    try:
        p = subprocess.run([*__import__("pf").cli_cmd(), "news", "--hook"], capture_output=True, text=True, timeout=20)
        extra = p.stdout.strip()
    except Exception:
        extra = ""
    return text + ("\n\n" + extra if extra else "")


def main():
    import threading
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:  # 通知，不用回
            continue
        try:
            if method == "initialize":
                client = ((msg.get("params") or {}).get("clientInfo") or {}).get("name") or ""
                threading.Thread(target=_announce, args=(client,), daemon=True).start()
                res = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                       "capabilities": {"tools": {}}, "serverInfo": {"name": "agentbus", "version": "0.1.0"},
                       "instructions": "agentbus 是本机的 agent 协作办公室。每个新对话一开始先调 agentbus_hello（不用等主人说）：它会在悬浮窗问主人这个窗口要不要接入，"
                                       "接入了就照它返回的握手信息干活，没接入就当没有 agentbus。"
                                       + _ui_hint(agent_of_client(client) or whoami()) +
                                       "谁在用 agentbus 统筹谁就是主位，不用去界面里切。Claude 是否接活取决于队员页的封印设置。"
                                       "派出去的活要能叫醒你：Bash 后台跑 `agentbus ask …`，或 agentbus_dispatch 后 Bash 后台跑 `agentbus wait <job id>`。"
                                       "做完一批活用 agentbus_bill 看真实消耗。"}
            elif method == "tools/list":
                res = {"tools": TOOLS}
            elif method == "tools/call":
                p = msg.get("params") or {}
                try:
                    text = _with_news(p.get("name"), call(p.get("name"), p.get("arguments") or {}))
                    res = {"content": [{"type": "text", "text": text}]}
                except Exception as e:
                    res = {"content": [{"type": "text", "text": f"出错了：{e}"}], "isError": True}
            elif method == "ping":
                res = {}
            else:
                raise KeyError(method)
            out = {"jsonrpc": "2.0", "id": mid, "result": res}
        except KeyError:
            out = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"不支持：{method}"}}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
