#!/usr/bin/env python3
"""agentbus 本地服务：给办公室界面出数据（应用窗口和 Claude 右侧浏览器打开的是同一个地址，数据和操作实时共享）。

  python3 service.py [端口，默认 8790]   然后打开 http://127.0.0.1:8790

只读各 agent 的日志和总线文件；界面上的拖动、挂岗位写进 ~/.agentbus/config.json，和 CLI / MCP 用的是同一份。
"""
import hashlib
import re
import json
import os
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import activity  # noqa: E402
import brains  # noqa: E402
import pf  # noqa: E402
import quota  # noqa: E402
from catalog import CATEGORIES, INSTALL, INSTALL_GUIDE, LEVELS, OPENERS, install_cmd, install_needs  # noqa: E402
from config import (GENERAL, HOME, LEDGER, agents_dict, bill, pack_library, pack_mounts, skill_library, create_project, current_project, installed,  # noqa: E402
                    ledger_add, load_config, load_json, project, projects, resolve_tag, save_config, save_json, set_batch, set_current)

# 打包成 exe（PyInstaller）后界面文件在解包目录里；源码运行时在 core 旁边的 ui/
UI = Path(os.environ["AGENTBUS_UI"]) if os.environ.get("AGENTBUS_UI") else \
    Path(getattr(sys, "_MEIPASS", "")) / "ui" if getattr(sys, "frozen", False) else HERE.parent / "ui"
WORKING_SEC = 40       # 这么多秒内有新动作就算“在干活”
BREAK_SEC = 900        # 15 分钟内算“刚忙完”，再久就是“休息”

_hello = {}  # 最近一次有 agent 接入（MCP 握手）：悬浮窗看到就弹出来打招呼
JOIN_ASK_TTL = 15  # 接入询问弹出 15 秒没人理就自己收起（不记成“不了”，也不再问）
_join_ask = {}  # 新开的对话窗口，等主人在悬浮窗里点“接入 / 不了”：conv → {agent, cwd, t}
_ASK_FILE = HOME / "join_ask.json"  # 存一份：服务重启时还没来得及回答的询问不丢


def _ask_save():
    try:
        save_json(_ASK_FILE, {k: v for k, v in _join_ask.items() if time.time() - v["t"] < JOIN_ASK_TTL})
    except OSError:
        pass


def _ask_restore():
    """重启前两分钟内弹出、还没人回答的询问：重启后再弹一次（重新计 15 秒）。"""
    try:
        joined = load_json(HOME / "joined.json", {})
        for k, v in load_json(_ASK_FILE, {}).items():
            if k not in joined and time.time() - v.get("t", 0) < 120:
                _join_ask[k] = dict(v, t=time.time())
    except Exception:
        pass
_quota = {"codex": {}, "deepseek": {}, "claude": quota.claude(), "t_codex": 0, "t_deepseek": 0, "api": {}, "t_api": 0, "balance": {}, "t_balance": 0}
_lock = threading.Lock()


_buswin = []  # 近 7 天接入过 agentbus 的窗口（后台扫，见 activity.bus_windows）
_compressing = set()
_lock_compress = threading.Lock()
_upd = {}  # 最近一次查到的新版本信息


_loop_beat = {"t": 0}
_compress_fail = {}  # 频道 → 上次压缩失败的时间；失败后 10 分钟内不再自动试


def _compress_one(ch):
    """压一个频道；自动、手动共用同一个占用标记，失败也一定释放。返回 False = 这个频道正在压。"""
    import chat
    with _lock_compress:
        if ch in _compressing:
            return False
        _compressing.add(ch)
    try:
        chat.compress(ch, force=True)
        _compress_fail.pop(ch, None)
    except Exception as e:
        _compress_fail[ch] = time.time()
        print(f"压缩「{ch}」失败：", e, file=sys.stderr)
    finally:
        with _lock_compress:
            _compressing.discard(ch)
    return True


def _auto_compress():
    """聊天室：最近有动静的频道，没压缩的部分超过设置里的阈值就后台压一次（同一个频道同时只压一次）。
    任何一个频道出错都只跳过它，不影响别的频道，也不会让后台刷新循环停掉。"""
    import chat
    if not chat.CHAT.exists():
        return
    at = chat.lim()["compress_at"]
    for f in chat.CHAT.glob("*.jsonl"):
        ch = f.stem
        try:
            if (not chat.valid(ch) or time.time() - f.stat().st_mtime > 900 or ch in _compressing
                    or time.time() - _compress_fail.get(ch, 0) < 600 or chat.usage(ch)["chars"] < at):
                continue
            threading.Thread(target=_compress_one, args=(ch,), daemon=True).start()
        except Exception as e:
            print(f"检查「{ch}」要不要压缩时出错：", e, file=sys.stderr)


def detect_loop():
    """被动发现新开的对话（不靠 agent 自己跑钩子或调 agentbus_hello）：每 5 秒扫一遍各家的会话记录，
    新对话没接入、没拒绝、没问过 → 进悬浮窗的接入询问。第一次启动时把已有的都记成“见过”，不补问旧窗口。"""
    from config import load_json as _lj, update_json as _uj
    seen_f, joined_f = HOME / "sessions_seen.json", HOME / "joined.json"
    first = not seen_f.exists()
    while True:
        try:
            convs = activity.new_conversations(600)
            seen = _lj(seen_f, {})
            mode = ((load_config().get("settings") or {}).get("auto_join") or "ask")
            now = time.time()
            # 第一句还没落盘的先等（最多 60 秒）：不提前记成“见过”，免得漏掉；也给派活识别留时间
            fresh = [c for c in convs if c["conv"][:12] not in seen and not (c.get("first") is None and now - c["t"] < 60 and c["agent"] not in ("claude", "codex"))]
            if fresh:
                joined = _lj(joined_f, {})
                for c in fresh:
                    key = c["conv"][:12]
                    if first or key in joined or key in _join_ask or mode == "never":
                        continue
                    if mode == "always":
                        _uj(joined_f, lambda j, c=c, key=key: j.setdefault(key, {"agent": c["agent"], "cwd": c["cwd"], "t": time.time(), "briefed": False, "via": "detect"}))
                        continue
                    _join_ask[key] = {"conv": key, "agent": c["agent"], "name": c.get("name"), "cwd": c["cwd"], "t": time.time(), "via": "detect",
                                      "first": (c.get("first") or "")[:60]}
                    _ask_save()
                    _cache.clear()
                _uj(seen_f, lambda d: d.update({c["conv"][:12]: now for c in fresh}) or {k: v for k, v in d.items() if now - v < 30 * 86400})
            elif first:
                _uj(seen_f, lambda d: d)
            first = False
        except Exception as e:
            print("新对话检测出错：", e, file=sys.stderr)
        time.sleep(5)


_DEMO = None  # 演示模式（录宣传片用）：AGENTBUS_DEMO=额度样例文件，额度读它、不联网不读任何真实凭证


def quota_loop():
    global _buswin, _DEMO
    if os.environ.get("AGENTBUS_DEMO"):
        _DEMO = json.load(open(os.environ["AGENTBUS_DEMO"], encoding="utf-8"))
    while True:
        if _DEMO:
            _quota.update(_DEMO)
            _quota.update({k: 9e18 for k in ("t_codex", "t_deepseek", "t_api", "t_balance", "t_local")})
        _loop_beat["t"] = time.time()  # 后台循环还活着（/api/health 里能看到）
        now = time.time()
        try:
            _buswin = activity.bus_windows()
        except Exception as e:
            print("扫窗口失败：", e, file=sys.stderr)
        try:
            _auto_compress()
        except Exception as e:
            print("自动压缩出错：", e, file=sys.stderr)
        try:
            import update
            if update.repo() and time.time() - _upd.get("t", 0) > update.EVERY:  # 每 6 小时查一次新版本
                _upd.update(update.check(), t=time.time())
                _cache.clear()
        except Exception as e:
            print("查更新失败：", e, file=sys.stderr)
        try:
            if now - _quota["t_codex"] > 60:
                _quota["codex"], _quota["t_codex"] = quota.codex(), now
            if now - _quota["t_deepseek"] > 120:
                _quota["deepseek"], _quota["t_deepseek"] = quota.deepseek(), now
            from config import sealed as _sealed
            if now - _quota["t_balance"] > 300:  # 「接入余额」打开了的队员：用它自己的登录凭证查剩余
                import balance
                for aid in _balance_links():
                    _quota["balance"][aid] = balance.check(aid)
                _quota["t_balance"] = now
            if now - _quota.get("t_local", 0) > 300:  # 读不到剩余的队员：数本机用量记录（封印的 Claude 不数）
                loc = {}
                for k, fn in quota.LOCAL_USAGE.items():
                    if k == "claude" and _sealed("claude"):
                        continue
                    try:
                        loc[k] = fn()
                    except Exception as e:
                        loc[k] = {"ok": False, "error": str(e)[:120]}
                _quota["local"], _quota["t_local"] = loc, now
            if now - _quota["t_api"] > 300:  # API 队员的钱包账户余额（支持查余额的供应商），5 分钟一次
                import wallet
                accs = {a.get("account") for a in agents_dict(load_config()).values() if a.get("api") and a.get("account")}
                _quota["api"] = {acc: wallet.balance(acc) for acc in accs}
                _quota["t_api"] = now
            settle()
        except Exception as e:  # 网络断了下次再试
            print("额度刷新失败：", e, file=sys.stderr)
        time.sleep(15)


def settle():
    """DeepSeek 扣费可能晚几分钟入账：派活结束 4 分钟后再查一次余额，补一条 settle 记录。"""
    from config import ledger_read
    recs = ledger_read()
    done = {r["id"] for r in recs if r.get("type") == "settle"}
    runs = [r for r in recs if not r.get("type")]
    busy = any(load_json(f, {}).get("billing") == "deepseek" for f in (HOME / "running").glob("*.json"))
    for r in runs:
        if r.get("billing") != "deepseek" or r["id"] in done or not r.get("settle_at"):
            continue
        if time.time() < r["settle_at"] or time.time() - r["end"] > 3600 or busy:
            continue
        # 结束后又有别的 DeepSeek 活开始了，余额里混了它的钱，不补查
        if any(o is not r and o.get("billing") == "deepseek" and o["start"] > r["end"] for o in runs):
            ledger_add({"type": "settle", "id": r["id"], "after": r["after"], "cost": r["cost"], "note": "后面紧接着有别的活，沿用结束时的余额"})
            continue
        after = quota.deepseek()
        if not after.get("ok"):
            return
        c = quota.delta("deepseek", r["before"], after)
        ledger_add({"type": "settle", "id": r["id"], "after": {"ok": True, "total": after["total"], "by_key": after.get("by_key")}, "cost": c})
        _quota["deepseek"], _quota["t_deepseek"] = after, time.time()


def desk_art(a):
    """GPT 画的“坐在桌前”三格图：有专属形象的用自己的，没有的用默认小机器人。"""
    name = (a.get("sprite") or "robot.png").replace(".png", "")
    f = UI / "assets" / "art" / f"desk-{name}.png"
    return f"assets/art/{f.name}?v={int(f.stat().st_mtime)}" if f.exists() else None


ART2 = UI / "assets" / "art" / "v2"
PROVIDER_ART = {"deepseek": "deepseek", "zhipu": "zcode", "moonshot": "kimi", "qwen": "qwen", "openai": "gpt", "xai": "grok"}
CHAR_FILE = {"codex": "gpt", "dsh": "deepseek"}  # DSH 用经典的 DeepSeek 鲸鱼娘形象  # agent id → 美术文件名（GPT 的立绘叫 gpt）


def _art(name):
    f = ART2 / name
    return f"assets/art/v2/{name}?v={int(f.stat().st_mtime)}" if f.exists() else None


def lihui_of(art):
    """全身立绘（ui/assets/art/lihui/，从 美术素材/01_DeepSeek.png… 处理来的）。"""
    art = {"gemini-cli": "gemini"}.get(art, art)
    f = UI / "assets" / "art" / "lihui" / f"{art}.png"
    return f"assets/art/lihui/{art}.png?v={int(f.stat().st_mtime)}" if f.exists() else None


def anim_info(art):
    """角色的动画帧表和姿态（art/scripts 处理好放在 assets/art/v2/ 里）：有就给界面用，没有就用三格静态图。"""
    out = {}
    for key, name in (("work", f"anim-{art}-work.png"), ("poses", f"poses-{art}.png"), ("acts", f"acts-{art}.png"), ("sofa", f"sofa-{art}.png")):
        f = UI / "assets" / "art" / "v2" / name
        if f.exists():
            out[key] = f"assets/art/v2/{name}?v={int(f.stat().st_mtime)}"
    meta = UI / "assets" / "art" / "v2" / f"acts-{art}.json"
    if meta.exists():  # 6 个办公动作：0 歇着 / 1 接活 / 2 交成果 / 3 喝茶发呆 / 4 专心干 / 5 卡住想；desk = 该配哪张桌子
        out["acts_meta"] = load_json(meta, [])
    fit = UI / "assets" / "art" / "v2" / f"acts-{art}.fit.json"
    if fit.exists():  # 动作表里的人比工位那格小一圈：显示时按这个放大、平移，大小才一致
        out["acts_fit"] = load_json(fit, {})
    return out or None


THEME_DIR = UI / "assets" / "art" / "themes"
OFFICE_THEME_DEFAULT = {"claude": "纸本编辑部", "codex": "温室工作室"}


def themes():
    """办公室主题（art/待接入/扩展素材-1112/办公室主题 → assets/art/themes/<名>/）：全景背景 + 长条共用桌。"""
    out = []
    for d in sorted(THEME_DIR.glob("*/theme.json")) if THEME_DIR.exists() else []:
        t = load_json(d, {})
        v = int(d.stat().st_mtime)
        out.append({"name": t.get("name") or d.parent.name, "bg": f"assets/art/themes/{d.parent.name}/bg.png?v={v}",
                    "desk": f"assets/art/themes/{d.parent.name}/desk.png?v={v}", "desk_view": t.get("desk_view"), "desk_dim": t.get("desk_dim")})
    return out


def art_info():
    """第二批美术（分层）：桌子层、沙发层、休息室和悬浮窗背景。没画好的是 None，界面会用旧画法顶上。"""
    return {"themes": themes(), "desk": _art("desk-front.png"), "desk_nomug": _art("desk-front-nomug.png"), "sofa": _art("sofa-front.png"),
            "desk_work": _art("desk-work.png"), "desk_sleep": _art("desk-sleep.png"),
            "lounge": _art("lounge-bg.png"), "hud_wide": _art("hud-wide.png"), "hud_tall": _art("hud-tall.png")}


# 各家命令行怎么注册 MCP：(检查是否已接入的参数, 判断方式, 添加的参数)；没有的就给一段配置让主人自己粘
MCP_CONNECT = {
    "claude": (["mcp", "get", "agentbus"], "rc", ["mcp", "add", "--scope", "user", "agentbus", "--"]),
    "codex": (["mcp", "get", "agentbus"], "rc", ["mcp", "add", "agentbus", "--"]),
    "codebuddy": (["mcp", "list"], "grep", ["mcp", "add", "-s", "user", "agentbus", "--"]),
}


def connect_lead(aid=None):
    """把 agentbus 接进主位 agent：支持一键的直接跑它自己的 mcp add；不支持的返回一段配置和说明。"""
    import subprocess
    cfg = load_config()
    aid = aid or lead_of(cfg)
    a = agents_dict(cfg).get(aid)
    if not a:
        raise ValueError("还没有主位：先到队员页给一位点「设为主位」")
    cmd = pf.mcp_cmd()
    kind = a.get("run")
    spec = MCP_CONNECT.get(kind)
    exe = pf.which((a.get("bin") or kind or "")) if spec else None
    if spec and exe:
        chk, how, add = spec
        r = subprocess.run([exe, *chk], capture_output=True, text=True, timeout=30)
        if (how == "rc" and r.returncode == 0) or (how == "grep" and "agentbus" in (r.stdout + r.stderr)):
            return {"ok": True, "already": True, "name": a["name"]}
        r = subprocess.run([exe, *add, *cmd], capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise ValueError(f"{a['name']} 没接上：" + (r.stderr or r.stdout).strip()[:300])
        return {"ok": True, "name": a["name"]}
    # 不能一键：给通用的 MCP 配置（大多数支持 MCP 的工具都认这个格式）
    snippet = json.dumps({"mcpServers": {"agentbus": {"command": cmd[0], "args": cmd[1:]}}}, ensure_ascii=False, indent=2)
    if kind == "opencode":
        snippet = json.dumps({"mcp": {"agentbus": {"type": "local", "command": cmd}}}, ensure_ascii=False, indent=2)
    where = {"opencode": "把下面这段合并进 ~/.config/opencode/opencode.jsonc", "zcode": "在 zcode 里打开 MCP 设置，按下面的命令添加一个本地服务"}.get(
        kind, f"在 {a['name']} 的 MCP 设置里添加下面这个本地服务")
    return {"ok": True, "manual": True, "name": a["name"], "where": where, "snippet": snippet,
            "cli": f'{" ".join(pf.cli_cmd()) if getattr(sys, "frozen", False) else "agentbus"} ask <队员> "任务" --tag <职能>'}


def live_costs(runs):
    """正在跑的活从开工到现在一共消耗了多少（累加，活结束后下一个活从 0 重新算）：开工时记下的额度 / 余额
    对比最新一次读数（GPT 每分钟、DeepSeek 每 2 分钟刷新），不是等做完才算。同一个账户同时有几个活在跑时，花费混在一起，标出是和谁一起算的。"""
    by = {}
    for r in runs:
        by.setdefault(r.get("billing"), []).append(r)
    now = time.time()
    for b, rs in by.items():
        cur = _quota.get(b) if b in ("codex", "deepseek") else None
        if not cur or not cur.get("ok"):
            continue
        for r in rs:
            before, since = r.get("before") or {}, None
            if not before.get("ok"):  # 老版本派出去的活没记起点：从服务第一次看到它时的读数算起，并写回记录，重启也不丢
                if r.get("before_seen"):
                    before = r["before_seen"]
                else:
                    before = {k: cur[k] for k in ("ok", "five_hour", "weekly", "credits", "total") if k in cur}
                    rf = HOME / "running" / (str(r.get("id", "")).split("/")[-1] + ".json")
                    if rf.exists():
                        save_json(rf, dict(load_json(rf, {}), before_seen=before, before_seen_t=now))
                    r["before_seen_t"] = now
                since = time.strftime("%H:%M", time.localtime(r.get("before_seen_t") or now))
            c = quota.delta(b, before, cur)
            if not c.get("ok"):
                continue
            if b == "deepseek":
                cny = max(0.0, c.get("cny") or 0)
                live = {"cny": round(cny, 2), "text": f"消耗 ¥{cny:.2f}"}
            else:
                wk, fh = max(0, c.get("weekly") or 0), max(0, c.get("five_hour") or 0)
                live = {"weekly": wk, "five_hour": fh, "text": f"消耗额度 {fh:g}%" + (f"（周 {wk:g}%）" if wk else "")}
            others = [o["name"] for o in rs if o is not r]
            if others:
                live["shared"] = others
                live["text"] += f"（和{others[0]}{'等' if len(others) > 1 else ''}共用）"
            if since:
                live["since"] = since
                live["text"] += f"（{since} 起）"
            live["at"] = _quota.get("t_" + b)
            r["live"] = live
    return runs


def sched_brief(cfg):
    import scheduler
    st = scheduler.settings(cfg)
    try:
        now = scheduler.status()
    except Exception:
        now = {"screen": [], "slots": {}}
    return {"screen_lock": st["screen_lock"], "limits": st["limits"], "screen": [h.get("name") for h in now["screen"]],
            "used": {b: v["used"] for b, v in now["slots"].items()}}


# ---------- A2A（Agent2Agent）接口：别的 agent / 平台按 A2A 协议把活交给 agentbus 这支队伍 ----------
def a2a_card():
    cfg = load_config()
    team = [a for aid, a in agents_dict(cfg).items() if not a.get("hidden") and a.get("run") and a.get("billing") != "manual" and installed(a)]
    return {"protocolVersion": "0.3.0", "name": "agentbus", "url": "http://127.0.0.1:8790/a2a", "preferredTransport": "JSONRPC", "version": "0.4",
            "description": "本机多 agent 协作队伍。队员：" + "、".join(a["name"] for a in team) + "。按职能自动挑人，带项目经验包干活。",
            "capabilities": {"streaming": False, "pushNotifications": False}, "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
            "skills": [{"id": cid, "name": name, "description": "、".join(tags), "tags": tags} for cid, name, tags in CATEGORIES]}


def a2a_rpc(req):
    import mcp_server
    mid, method, p = req.get("id"), req.get("method"), req.get("params") or {}
    ok = lambda r: {"jsonrpc": "2.0", "id": mid, "result": r}
    err = lambda c, m: {"jsonrpc": "2.0", "id": mid, "error": {"code": c, "message": m}}
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if method == "message/send":
        msg = p.get("message") or {}
        text = "\n".join(x.get("text", "") for x in msg.get("parts") or [] if x.get("kind", x.get("type")) == "text").strip()
        if not text:
            return err(-32602, "message 里没有文字")
        meta = dict(p.get("metadata") or {}, **(msg.get("metadata") or {}))
        cfg = load_config()
        target = meta.get("agent") or meta.get("skill") or meta.get("tag") or lead_of(cfg)
        ctx = msg.get("contextId") or "ctx-" + time.strftime("%m%d%H%M%S")
        jid = mcp_server.start_job({"agent": target, "task": text, "tag": meta.get("tag") or meta.get("skill"), "project": meta.get("project"),
                                    "new": not msg.get("contextId")}, env_extra={"AGENTBUS_CONV": "a2a-" + ctx, "AGENTBUS_SELF": "a2a"})
        return ok({"kind": "task", "id": jid, "contextId": ctx, "status": {"state": "submitted", "timestamp": now}})
    if method == "tasks/get":
        try:
            alive, out, _ = mcp_server.job_state(p.get("id", ""))
        except FileNotFoundError:
            return err(-32001, "没有这个任务")
        if alive:
            return ok({"kind": "task", "id": p["id"], "status": {"state": "working", "timestamp": now}})
        reply = "\n".join(l for l in out.splitlines() if not l.startswith("[agentbus]")).strip()
        failed = not reply or "[agentbus] 做完了" not in out[-600:]  # 只认收尾写的「做完了」，失败、被停、没走到收尾都算失败
        return ok({"kind": "task", "id": p["id"], "status": {"state": "failed" if failed else "completed", "timestamp": now},
                   "artifacts": [{"artifactId": "reply", "parts": [{"kind": "text", "text": reply or out[-2000:]}]}]})
    return err(-32601, f"不支持：{method}")


def brain_brief(cfg, aid, a):
    """名牌上的大脑：用的模型 + 思考强度；能不能在这里换。"""
    opts = brains.options(a)
    mine = brains.chosen(cfg, aid)
    if not opts["models"] and not opts.get("current") and not opts.get("custom_model"):
        return None
    m, e, info = brains.effective(mine, opts)
    return {"label": brains.label(mine, opts), "set": bool(mine), "switch": opts["switch"], "model": m, "effort": e, "custom_model": bool(opts.get("custom_model")),
            "name": info.get("name") or m.split("/")[-1], "effort_name": brains.EFFORT_NAME.get(e, e or "")}


def brain_options(aid, fresh=False):
    cfg = load_config()
    a = agents_dict(cfg).get(aid)
    if not a:
        raise ValueError("不认识的 agent")
    o = dict(brains.options(a, fresh))
    o["chosen"] = brains.chosen(cfg, aid)
    o["names"] = brains.EFFORT_NAME
    return o


def set_brain(aid, data):
    from config import CONFIG, update_json
    a = agents_dict(load_config()).get(aid)
    if not a:
        raise ValueError("不认识的 agent")
    choice = brains.validate_choice(data, brains.options(a))

    def change(cfg):
        if choice:
            cfg.setdefault("brains", {})[aid] = choice
        else:
            cfg.setdefault("brains", {}).pop(aid, None)

    update_json(CONFIG, change)
    _cache.clear()
    return {"ok": True, "chosen": choice}


def make_clone(base, name=None):
    """开个分身：同一个外壳换个大脑，单独占工位、职能和会话。返回新 id。"""
    cfg = load_config()
    ag = agents_dict(cfg)
    b = ag.get(base)
    if not b or b.get("clone_of"):
        raise ValueError("只能给本体开分身")
    if b.get("billing") == "manual":
        raise ValueError("网页版没法开分身")
    n = 2
    while f"{base}-{n}" in ag:
        n += 1
    cid = f"{base}-{n}"
    cfg.setdefault("clones", {})[cid] = {"base": base, "name": (name or "").strip() or f"{b['name']} {n}号"}
    # 职能先照抄本体，主人再改；进休息室，主人拖上岗
    cfg["jobs"][cid] = dict(cfg["jobs"].get(base) or {})
    office = cfg["roster"]["office"]
    (office if len(office) < 6 else cfg["roster"]["lounge"]).append(cid)
    save_config(cfg)
    return cid


def new_api_agent(account, model, name=None):
    import wallet
    acc = wallet.accounts().get(account)
    if not acc or not model:
        raise ValueError("选一个钱包账户和模型")
    from config import load_json as _lj, CONFIG, save_json
    raw = _lj(CONFIG, {})
    api = raw.setdefault("api_agents", {})
    n = 1
    while f"api-{n}" in api:
        n += 1
    aid = f"api-{n}"
    p = wallet.PRESETS.get(acc["provider"]) or wallet.PRESETS["custom"]
    api[aid] = {"name": (name or "").strip() or f"{p['name'].split('（')[0]} {model.split('/')[-1]}", "account": account, "model": model,
                "base": acc["base"], "vendor": f"{p['name']} · API", "color": p["color"]}
    save_json(CONFIG, raw)
    return aid


def remove_api_agent(aid):
    from config import load_json as _lj, CONFIG, save_json
    raw = _lj(CONFIG, {})
    if aid not in (raw.get("api_agents") or {}):
        raise ValueError("这不是 API 队员")
    raw["api_agents"].pop(aid)
    (raw.get("brains") or {}).pop(aid, None)
    for cid in [c for c, v in (raw.get("clones") or {}).items() if v.get("base") == aid]:
        raw["clones"].pop(cid)
    for lu in [raw] + list((raw.get("lineups") or {}).values()):
        for k in ("office", "lounge"):
            lst = (lu.get("roster") or {}).get(k) or []
            lu.get("roster", {})[k] = [x for x in lst if x != aid and not x.startswith(aid + "-")] if lst else lst
        (lu.get("jobs") or {}).pop(aid, None)
    save_json(CONFIG, raw)


def remove_clone(cid):
    from config import load_json as _lj, CONFIG, save_json
    raw = _lj(CONFIG, {})
    if cid not in (raw.get("clones") or {}):
        raise ValueError("这不是分身")
    raw["clones"].pop(cid)
    (raw.get("brains") or {}).pop(cid, None)
    # 所有项目的阵容里都撤掉
    for lu in [raw] + list((raw.get("lineups") or {}).values()):
        for k in ("office", "lounge"):
            if cid in ((lu.get("roster") or {}).get(k) or []):
                lu["roster"][k].remove(cid)
        (lu.get("jobs") or {}).pop(cid, None)
        if (lu.get("roster") or {}).get("lead") == cid:
            lu["roster"]["lead"] = cid.rsplit("-", 1)[0]
    save_json(CONFIG, raw)


def _alive(pid):
    return pf.pid_alive(pid)


def _record_lost(r):
    """派活进程意外没了、没走到收尾：补一条失败回复和账单记录（没有消耗数据）。"""
    if not r.get("id"):
        return
    from config import ledger_read, locked
    with locked("lost"):  # 几个进程同时发现同一单没了：只补记一次
        if any(x.get("id") == r["id"] for x in ledger_read()[-500:]):
            return
        _record_lost_now(r)


def _record_lost_now(r):
    try:
        _, _, bus, _ = project(r.get("project"))
    except SystemExit:
        return
    now = time.time()
    status = "stopped" if r.get("cancel") else "interrupted"
    text = (f"（这单已被手动停止：{r.get('agent')} 没做完。）" if r.get("cancel") else
            f"（派活进程意外没了，{r.get('agent')} 没交回复：可能被杀掉、或电脑睡眠/重启。去产出目录看看做到哪了。）")
    ledger_add({"id": r["id"], "project": r.get("project"), "batch": r.get("batch") or "", "agent": r.get("agent"), "billing": r.get("billing"),
                "tag": r.get("tag"), "task": r.get("task"), "start": r.get("start") or now, "end": now, "seconds": round(now - (r.get("start") or now)),
                "ok": False, "lost": True, "status": status, "frm": r.get("frm") or "claude", "reply": text, "cost": {"ok": False, "note": "进程没了，没查额度"}, "conv": r.get("conv") or ""})
    with open(Path(bus) / "board.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "reply", "frm": r.get("agent"), "to": r.get("frm") or "claude",
                             "text": text, "run": r.get("run"), "tag": r.get("tag"), "batch": r.get("batch"), "ok": False, "conv": r.get("conv") or ""},
                            ensure_ascii=False) + "\n")
    import chat
    import mcp_server
    jid = r.get("job")
    if not jid:  # 旧版 running 记录没有 job id，按派活进程关联
        jid = next((f.stem for f in (HOME / "jobs").glob("*.json")
                    if load_json(f, {}).get("pid") == r.get("pid")), None)
    mcp_server.finish_job(jid, status, text, r.get("run"))
    channel = r.get("channel") or next((c["name"] for c in chat.channels() if chat.conv_of(c["name"]) == r.get("conv")), None)
    if channel:
        chat.append(channel, "agentbus", text, system=True, run=r.get("run"))


def build_rooms(running, sess, agents, cfg):
    """单间：一个主位窗口一间。窗口来自 Claude / GPT 自己的会话日志（正在干活的），也来自派活记录里的协同会话；
    派出去的队员坐进派活那个窗口的单间；没有窗口、被直接使用的 agent 单独一间。running 里每条补上 room。"""
    from config import ROOMS_FILE
    reg = load_json(ROOMS_FILE, {})
    names = {a["id"]: a["name"] for a in agents}
    rooms = {}

    def room(rid, lead, title=None, since=None):
        r = rooms.get(rid)
        if not r:
            info = reg.get(rid) or {}
            r = rooms[rid] = {"id": rid, "lead": lead or info.get("lead"), "live": False, "members": [], "jobs": [],
                              "title": title or (os.path.basename((info.get("cwd") or "").rstrip("/")) or None), "since": since or time.time()}
        if title and not r["title"]:
            r["title"] = title
        if since:
            r["since"] = min(r["since"], since)
        return r

    def add(r, aid):
        if aid and aid in names and aid not in r["members"]:
            r["members"].append(aid)

    for x in sorted(sess, key=lambda x: x["t"]):
        r = room(x["conv"], x["agent"], x["title"], x["t"])
        r["live"] = True
        add(r, x["agent"])
    for job in sorted(running.values(), key=lambda j: j.get("start", 0)):
        frm, conv = job.get("frm") or lead_of(cfg), (job.get("conv") or "")[:12]
        rid = conv
        if not rid:  # 没有窗口信息（比如 GPT 的 MCP）：并进这位主位正在干活的那间，没有就按主位开一间
            rid = next((k for k, r in rooms.items() if r["lead"] == frm and r["live"]), "agent:" + str(frm))
        r = room(rid, frm, job.get("project"), job.get("start"))
        add(r, frm)
        add(r, job.get("agent"))
        r["jobs"].append(job.get("id"))
        job["room"] = rid
    seated = {m for r in rooms.values() for m in r["members"]}
    for a in agents:  # 直接在用、又不在任何单间里的（zcode、opencode 这类没有窗口信息的）
        if a["state"] == "working" and a["id"] not in seated and all(j.get("agent") != a["id"] for j in running.values()):
            add(room("direct:" + a["id"], a["id"], a["name"], time.time()), a["id"])
    out = sorted(rooms.values(), key=lambda r: r["since"])
    for r in out:
        r["label"] = "直接在用" if r["id"].startswith("direct:") else f"{names.get(r['lead'], r['lead'] or '')} · {r['title']}" if r["title"] else names.get(r["lead"], r["lead"] or "")
    return out


_titles = {"codex": (0, {}), "claude": {}}


def window_title(agent, conv):
    """窗口名和最后使用时间：Claude 读对话文件里的标题（customTitle），GPT 读 ~/.codex/session_index.jsonl 的 thread_name。"""
    import glob
    if not conv or conv.startswith(("direct:", "agent:", "hello:")):
        return None, None
    if agent == "claude":
        c = _titles["claude"].get(conv)
        fs = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{conv}*.jsonl"))
        if not fs:
            return None, None
        f = max(fs, key=os.path.getmtime)
        m = os.path.getmtime(f)
        if c and c[0] == m:
            return c[1], c[2] or m
        title, last = None, None
        for l in activity.tail_lines(f, 400_000):
            if '"customTitle":"' in l:
                mm = re.search(r'"customTitle":"([^"]*)"', l)
                title = mm.group(1) if mm else title
            if '"timestamp":"' in l and ('"type":"user"' in l or '"type":"assistant"' in l):
                mm = re.search(r'"timestamp":"([^"]*)"', l)
                last = activity.iso_ts(mm.group(1)) if mm else last  # 文件修改时间不可靠（会被别的程序碰到），用最后一条消息的时间
        _titles["claude"][conv] = (m, title, last)
        return title, last or m
    if agent == "codex":
        idx = os.path.expanduser("~/.codex/session_index.jsonl")
        try:
            m = os.path.getmtime(idx)
        except OSError:
            return None, None
        if _titles["codex"][0] != m:
            names = {}
            with open(idx, encoding="utf-8", errors="ignore") as fh:
                for l in fh:
                    try:
                        d = json.loads(l)
                    except json.JSONDecodeError:
                        continue
                    names[(d.get("id") or "")[:12]] = (d.get("thread_name"), d.get("updated_at"))
            _titles["codex"] = (m, names)
        name, upd = _titles["codex"][1].get(conv[:12], (None, None))
        try:
            t = activity.iso_ts(upd)
        except ValueError:
            t = None
        return name, t
    return None, None


def office_lineup(cfg, lead):
    """办公室的战备预设组：这位主位派活时从哪些队员里挑、各自主力干什么。没调过就跟项目阵容一样。"""
    own = (cfg.get("office_lineups") or {}).get(lead)
    from config import sealed as _sealed
    member = lambda a: a in agents_dict(cfg) and a != lead and not _sealed(a, cfg)  # 主位自己、封印的 Claude 不算队员
    if own:
        return {"office": [a for a in own.get("office", []) if member(a)], "jobs": own.get("jobs") or {}, "custom": True}
    return {"office": [a for a in cfg["roster"]["office"] if member(a)], "jobs": cfg.get("jobs") or {}, "custom": False}


def build_offices(rooms, agents, cfg=None):
    """办公室：每个在用 bus 的 agent 一间（Claude 办公室、GPT 办公室…），里面一个窗口一组。
    组名 = 那个 agent 里的对话窗口名，带最后使用时间；近 7 天用 bus 派过活 / 统筹过的窗口都列出来，正在干活的排前面。
    直接在用、没走 bus 的 agent 也给一间，组名“直接在用”。"""
    from config import ROOMS_FILE, ledger_read
    cfg = cfg or load_config()
    now = time.time()
    names = {a["id"]: a["name"] for a in agents}
    week = now - 7 * 86400
    today = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    hist, jobs_today = {}, {}
    for x in ledger_read():  # 每个窗口派过活给谁、几单、最后一次什么时候
        if x.get("type") or (x.get("start") or 0) < week:
            continue
        lead = x.get("frm") or "claude"
        if (x.get("start") or 0) >= today:
            jobs_today[lead] = jobs_today.get(lead, 0) + 1
        conv = (x.get("conv") or "")[:12]
        if not conv:
            continue
        h = hist.setdefault((lead, conv), {"workers": [], "jobs": 0, "last": 0, "project": x.get("project")})
        h["jobs"] += 1
        h["last"] = max(h["last"], x.get("end") or 0)
        if x.get("agent") not in h["workers"]:
            h["workers"].append(x.get("agent"))
    for conv, r in load_json(ROOMS_FILE, {}).items():  # 统筹过（握手 / 派活）的窗口
        if r.get("t", 0) >= week and r.get("via") in ("mcp", "ask") and r.get("lead"):
            h = hist.setdefault((r["lead"], conv[:12]), {"workers": [], "jobs": 0, "last": 0, "project": None})
            h["last"] = max(h["last"], r["t"])
            h.setdefault("cwd", r.get("cwd"))
    for kind, conv, cwd, mtime in _buswin:  # 接入过 agentbus 的窗口：不干活也列出来
        if kind in names:
            h = hist.setdefault((kind, conv), {"workers": [], "jobs": 0, "last": 0, "project": None})
            h["cwd"] = h.get("cwd") or cwd
            h.setdefault("seen_mtime", mtime)
    live = {r["id"]: r for r in rooms}
    offices = {}

    def office(lead):
        return offices.setdefault(lead, {"id": lead, "agent": lead, "name": f"{names.get(lead, lead)} 办公室", "groups": [], "active": False,
                                         "working": [], "today": jobs_today.get(lead, 0), "lineup": office_lineup(cfg, lead),
                                         "theme": (cfg.get("office_themes") or {}).get(lead) or OFFICE_THEME_DEFAULT.get(lead)})
    seen = set()
    for r in rooms:
        lead = r.get("lead") or "claude"
        if lead not in names:  # 主人从消息窗口直接派的活：不是哪个 agent 的办公室
            continue
        title, last = window_title(lead, r["id"])
        h = hist.get((lead, r["id"][:12]), {})
        g = {"id": r["id"], "title": title or r.get("title") or names.get(lead, lead), "active": True, "live": bool(r.get("live")),
             "direct": r["id"].startswith("direct:"), "members": list(r.get("members") or []), "workers": h.get("workers", []),
             "jobs": h.get("jobs", 0), "last": now, "cwd": h.get("cwd") or r.get("cwd")}
        o = office(lead)
        o["groups"].append(g)
        o["active"] = True
        for m in g["members"]:
            if m not in o["working"]:
                o["working"].append(m)
        seen.add((lead, r["id"][:12]))
    for (lead, conv), h in hist.items():
        if (lead, conv) in seen or lead not in names:
            continue
        title, last = window_title(lead, conv)
        o = office(lead)
        o["groups"].append({"id": conv, "title": title or os.path.basename((h.get("cwd") or "").rstrip("/")) or h.get("project") or "未命名窗口",
                            "active": False, "live": False, "direct": False, "members": [lead], "workers": h["workers"], "jobs": h["jobs"],
                            "cwd": h.get("cwd"), "last": max(h["last"], last or h.get("seen_mtime") or 0)})
    lead = lead_of(cfg)
    if lead in names and lead not in offices:  # 主位总有一间办公室（还没接入过窗口时基地也能显示它的战备）
        office(lead)
    for o in offices.values():
        o["groups"] = [g for g in o["groups"] if g["active"] or g["last"] >= week]  # 最后一条消息超过 7 天的不列
        o["groups"].sort(key=lambda g: (not g["active"], -g["last"]))
        o["groups"] = o["groups"][:30]
    return sorted(offices.values(), key=lambda o: (not o["active"], -max((g["last"] for g in o["groups"]), default=0)))


def board_tail(bus, n=300_000):
    p = Path(bus) / "board.jsonl"
    if not p.exists():
        return []
    recs = []
    for l in activity.tail_lines(str(p), n):
        try:
            recs.append(json.loads(l))
        except json.JSONDecodeError:
            pass
    return recs


def open_tasks(recs):
    out = {}
    day_ago = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 86400))
    for r in recs:
        if r.get("kind") == "task" and (r.get("t") or "") >= day_ago:  # 一天前没回的当作已放弃
            out[r.get("to")] = r
        elif r.get("kind") == "reply":
            out.pop(r.get("frm"), None)
    return out


def state(project_name=None):
    project_name, _root, bus, batch = project(project_name)  # 不传就是当前项目（界面就是这么用的）
    cfg = load_config(project_name)
    ps = projects()
    recs = board_tail(bus)
    tasks = open_tasks(recs)
    running = {}
    for f in (HOME / "running").glob("*.json"):
        r = load_json(f, {})
        if not pf.pid_alive(r.get("pid")):  # 派活的进程已经没了（被杀、机器重启）：记一条“没做成”，派活的人才知道
            _record_lost(r)
            f.unlink(missing_ok=True)
            continue
        if not r.get("title"):  # 老记录没有短标题：现场提炼一个（悬浮窗胶囊只显示它）
            from agentbus import short_title
            r["title"] = short_title(r.get("task"))
        running[r.get("id") or f.stem] = r  # 一单一条（同一位队员并行的几单都保留）
    # 名牌上一位队员只显示一单：有在干的显示最早开工的那单，全在排队才显示排队
    by_agent = {}
    for r in sorted(running.values(), key=lambda r: (bool(r.get("queued")), r.get("start", 0))):
        by_agent.setdefault(r.get("agent"), r)
    now = time.time()
    sess = activity.live_sessions()
    live_by = {x["agent"] for x in sess}
    agents = []
    for aid, a in agents_dict(cfg).items():
        if a.get("hidden"):
            continue
        inst = installed(a)
        acts = activity.recent(a["acts"]) if a.get("acts") and inst else []
        # 分身和本体共用一份日志：只让本体（或在跑派活的那位）亮“在干活”，免得一人干活亮两盏灯
        live = bool(a.get("acts") and inst and not a.get("clone_of") and
                    (a["acts"] in live_by if a["acts"] in activity.SESSIONS else activity.live(a["acts"], acts)))
        last = acts[0]["ts"] if acts else None
        idle = now - last if last else None
        if a.get("billing") == "manual":
            st = "manual"
        elif not inst:
            st = "absent"
        elif aid in by_agent and by_agent[aid].get("queued"):
            st = "queued"
        elif aid in by_agent or live or (idle is not None and idle < WORKING_SEC):
            st = "working"
        elif idle is not None and idle < BREAK_SEC:
            st = "break"
        else:
            st = "off"
        look = a.get("clone_of") or aid  # 分身穿本体的衣服
        art = "api" if a.get("api") else CHAR_FILE.get(look, look)  # API 队员一律用通用小助手形象，厂商靠图标区分
        av2 = UI / "assets" / "avatars" / f"v2-{art}.png"
        av = av2 if av2.exists() else UI / "assets" / "avatars" / f"{(a.get('sprite') or '').replace('.png', '')}.png"
        brand = prov_name = None
        if a.get("api"):  # API 队员：形象用厂商图标（assets/brands/<供应商>.png，从本机对应 App 的图标取的）
            import wallet
            acc = wallet.accounts().get(a.get("account")) or {}
            pv = acc.get("provider") or "custom"
            prov_name = (wallet.PRESETS.get(pv) or {}).get("name")
            bf = UI / "assets" / "brands" / f"{pv}.png"
            brand = f"assets/brands/{pv}.png?v={int(bf.stat().st_mtime)}" if bf.exists() else None
        task = by_agent.get(aid, {}).get("task") or (tasks.get(aid) or {}).get("text", "").split("\n")[0][:80] or None
        agents.append({"id": aid, "name": a["name"], "abbr": a.get("abbr") or a["name"][:1].upper(), "vendor": a.get("vendor"), "desc": a.get("desc"), "color": a.get("color"),
                       "sprite": a.get("sprite"), "billing": a.get("billing"), "installed": inst, "state": st,
                       "place": "office" if aid in cfg["roster"]["office"] else "lounge",
                       "jobs": cfg["jobs"].get(aid) or {}, "task": task, "acts": acts[:8],
                       "avatar": f"assets/avatars/{av.name}" if a.get("api") and av.exists() else brand if a.get("api") else f"assets/avatars/{av.name}" if (a.get("sprite") or av2.exists()) and av.exists() else
                                 ("assets/avatars/robot.png" if (UI / "assets/avatars/robot.png").exists() else None),
                       "since": by_agent.get(aid, {}).get("start"), "desk": desk_art(a), "direct": st == "working" and aid not in by_agent,
                       "frames": anim_info(art), "lihui": lihui_of(art),
                       "install": install_cmd(look, (cfg.get("settings") or {}).get("install_dir")) if (not inst or a.get("billing") == "manual") else None, "get_url": INSTALL_GUIDE.get(look) if not inst else None, "open": openers(look), "char": _art(f"char-{art}.png"), "lounge_art": _art(f"lounge-{art}.png"), "quota_text": quota_text(a), "meter": meter(a), "sealed": _sealed_of(a, cfg), "balance": _balance_ui(a, cfg), "login": _login_ui(a) if inst else None,
                       "clone_of": a.get("clone_of"), "brain": brain_brief(cfg, aid, a), "caps": a.get("caps") or [], "api": bool(a.get("api")), "generic": art == "api", "provider_name": prov_name, "brand": brand,
                       "cloneable": inst and not a.get("clone_of") and a.get("billing") != "manual" and a.get("run") not in (None, "doubao")})
    order = {aid: i for i, aid in enumerate(cfg["roster"]["office"] + cfg["roster"]["lounge"])}
    agents.sort(key=lambda x: order.get(x["id"], 99))
    rooms = build_rooms(running, sess, agents, cfg)
    offices = build_offices(rooms, agents, cfg)
    # 刚完成：所有项目的回复按时间合在一起（悬浮窗要看全局）
    replies = []
    buses = {name: pp["bus"] for name, pp in ps.items() if not name.startswith("_")}
    buses[GENERAL] = str(HOME / "general")
    for name, b in buses.items():
        rs = recs if name == project_name else board_tail(b, 200_000)
        replies += [dict(r, project=name) for r in rs if r.get("kind") == "reply"]
    replies = sorted(replies, key=lambda r: r.get("t") or "")[-8:][::-1]
    return {
        "settings": settings(cfg), "art": art_info(),
        "names": {k: v["name"] for k, v in agents_dict(cfg).items()},  # 包括隐藏的，用来显示旧记录
        "running": live_costs(sorted([dict(r, name=agents_dict(cfg).get(r.get("agent"), {}).get("name", r.get("agent")))
                                      for r in running.values()], key=lambda r: r.get("start", 0))),
        "recent": [{"t": r.get("t"), "agent": r.get("frm"), "to": r.get("to"), "ok": r.get("ok", True), "project": r.get("project"), "tag": r.get("tag"), "text": activity.short(r.get("text"), 120),
                    "cost": r.get("cost"), "seconds": r.get("seconds"), "run": r.get("run")} for r in replies],
        "batches": batches(project_name),
        "project": project_name, "project_key": current_project(), "batch": batch,
        "projects": [k for k in ps if not k.startswith("_")],
        "own_lineup": bool(cfg.get("_lineup_of")), "lead": lead_of(cfg), "playbook": playbook_brief(cfg, project_name, bus),
        "agents": agents, "rooms": rooms, "offices": offices, "packs": pack_library() + skill_library(), "pack_mounts": pack_mounts(cfg), "scheduler": sched_brief(cfg), "hello": dict(_hello), "update": {k: _upd.get(k) for k in ("available", "latest", "current", "url", "notes", "name")} if _upd.get("available") else None, "chat_rev": max((f.stat().st_mtime for f in (HOME / "chat").glob("*.jsonl")), default=0) if (HOME / "chat").exists() else 0,
        "join_ask": sorted([x for x in _join_ask.values() if now - x["t"] < JOIN_ASK_TTL], key=lambda x: -x["t"])[:3], "platform": {"os": pf.NAME, "keystore": pf.KEYSTORE, "files": pf.FILE_MANAGER},
        "board": [{"t": r.get("t"), "kind": r.get("kind"), "frm": r.get("frm"), "to": r.get("to"), "tag": r.get("tag"),
                   "text": activity.short(r.get("text"), 90), "cost": r.get("cost"), "seconds": r.get("seconds")}
                  for r in recs[-20:]][::-1],
        "quota": {"codex": _quota["codex"], "deepseek": _quota["deepseek"], "claude": _quota["claude"]},
        "bill": bill(project_name, batch or None) if project_name else None,
        "bill_all": bill(project_name) if project_name else None,
    }


DEFAULT_SETTINGS = {"hud": {"dock": "top", "autopop": True, "stay": 6, "notify_done": True, "enabled": True, "view": "scene", "pop_direct": True, "focus": False, "ball_min": 3, "hide_min": 20},
                    "theme": "system", "login": False, "lang": "system"}  # 跟随系统语言（中文系统显示中文，其他显示英文）；设置 → 通用 里可以固定


def settings(cfg=None):
    cfg = cfg or load_config()
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    st = cfg.get("settings") or {}
    out["hud"].update(st.get("hud") or {})
    import chat as _chat
    out["chat"] = {**_chat.DEFAULTS, **(st.get("chat") or {})}
    import update as _u
    out["update_repo"] = st.get("update_repo") or _u.DEFAULT_REPO
    out["version"] = _u.VERSION
    for k in ("theme", "login", "install_dir", "tour_done", "setup_done", "auto_join", "lang"):
        if k in st:
            out[k] = st[k]
    out["claude_seal"] = st.get("claude_seal", True) is not False  # 默认封印
    if pf.IS_WIN and not (st.get("hud") or {}).get("dock"):
        out["hud"]["dock"] = "right"  # Windows 没有刘海和菜单栏，默认贴右边的窄条，不挡窗口标题栏
    return out


def set_login(on):
    """开机自动打开 agentbus（Mac 写 LaunchAgent，Windows 写注册表 Run）。"""
    pf.set_login(on)


def batches(project_name):
    from config import ledger_read
    seen = []
    for r in ledger_read():
        if r.get("project") == project_name and not r.get("type") and r.get("batch") and r["batch"] not in seen:
            seen.append(r["batch"])
    return seen[::-1]


def suggest(tag):
    from config import ledger_read
    _, label = resolve_tag(tag)
    per = {}
    for r in ledger_read():
        if r.get("type") or r.get("tag") != label:
            continue
        s = per.setdefault(r["agent"], {"agent": r["agent"], "runs": 0, "seconds": 0, "cny": 0.0, "weekly": 0.0})
        s["runs"] += 1
        s["seconds"] += r.get("seconds") or 0
        c = r.get("cost") or {}
        s["cny"] += c.get("cny") or 0
        s["weekly"] += c.get("weekly") or 0
    return {"tag": label, "stats": sorted(per.values(), key=lambda x: -x["runs"])}


def quests(project_name, limit=120):
    """任务记录：把派活（task）和回复（reply）按 run 配成一对。"""
    _, _, bus, _ = project()
    recs = board_tail(bus, 1_500_000)
    by_run, order = {}, []
    for r in recs:
        run = r.get("run")
        if not run or r.get("kind") not in ("task", "reply"):
            continue
        q = by_run.setdefault(run, {"run": run})
        if run not in order:
            order.append(run)
        if r["kind"] == "task":
            q.update(t=r.get("t"), frm=r.get("frm"), agent=r.get("to"), task=activity.short(r.get("text"), 600),
                     tag=r.get("tag"), batch=r.get("batch"))
        else:
            q.update(done=r.get("t"), reply=activity.short(r.get("text"), 1200), cost=r.get("cost"), seconds=r.get("seconds"))
    return [by_run[k] for k in order[::-1][:limit] if by_run[k].get("agent")]


def openers(aid):
    """这个 agent 能一键打开什么：本机装了的桌面 App / 网页版 / 命令行。"""
    o = OPENERS.get(aid) or {}
    return {"app": pf.app_exists(o) or bool(o.get("web")), "cli": bool(o.get("cli") and pf.which(o["cli"]))}


def open_agent(aid, how):
    """主位一键打开：桌面端（Mac open -a / Windows 找 exe）；命令行开一个终端窗口，进项目文件夹再启动。"""
    o = OPENERS.get(aid) or {}
    if how == "app":
        if pf.app_exists(o):
            pf.open_app(o)
        elif o.get("web"):
            pf.open_url(o["web"])
        else:
            raise ValueError("本机没有它的桌面端")
    elif how == "cli":
        if not o.get("cli"):
            raise ValueError("它没有命令行版")
        pf.open_terminal(project()[1], o["cli"])
    else:
        raise ValueError("how 只能是 app 或 cli")


def playbook_brief(cfg, name, bus):
    import playbook
    pb = playbook.load(bus)
    if not pb:
        return None
    return {"name": pb["manifest"].get("name"), "version": pb["manifest"].get("version"), "steps": len(pb["steps"]),
            "lessons": len(pb["lessons"]), "enabled": playbook.equip_of(cfg, name)["enabled"]}


def project_docs(root):
    """项目里现成的经验文档：生成经验包时交给主位读。"""
    root = Path(root)
    pats = ["CLAUDE.md", "AGENTS.md", "README.md", "复盘*/*.md", "*经验*/*.md", "docs/*.md"]
    seen = []
    for pat in pats:
        for f in sorted(root.glob(pat)):
            if f.is_file() and not f.name.startswith("._") and f not in seen and f.stat().st_size < 400_000:
                seen.append(f)
    return [str(f) for f in seen[:20]]


def playbook_state():
    import playbook
    from config import who
    cfg = load_config()
    name, root, bus, _ = project()
    pb = playbook.load(bus)
    eq = playbook.equip_of(cfg, name)
    out = {"project": name, "has": bool(pb), "docs": project_docs(root) if name != GENERAL else [],
           "drafting": (HOME / "jobs" / f"playbook-{name}.json").exists() and _job_alive(HOME / "jobs" / f"playbook-{name}.json")}
    if pb:
        out.update(playbook.summary(pb, eq, agents_dict(cfg), lambda t: who(cfg, t)))
        out["stepBodies"] = {st["id"]: st["body"] for st in pb["steps"]}
        import playbook as _pb
        d = _pb.pb_dir(bus)
        out["personal"] = _pb.personal_of(bus)
        out["proposals"] = _pb.proposals(bus)
        # 项目文档比经验包新：分享前提醒先按文档更新；有没有一份待替换的新版
        core_files = [d / "manifest.json", d / "workflow.md", d / "lessons.jsonl"] + list((d / "steps").glob("*/SKILL.md"))
        pbt = max((f.stat().st_mtime for f in core_files if f.exists()), default=0)  # 只看经验包正文，不算提议、用量这些
        newer = [Path(x).name for x in project_docs(root) if Path(x).stat().st_mtime > pbt + 60]
        out["stale"] = {"newer": newer[:6], "pb_date": time.strftime("%m-%d", time.localtime(pbt))} if newer else None
        dd = Path(bus) / "playbook.draft"
        out["draft"] = (dd / "manifest.json").exists()
        out["fitting"] = (HOME / "jobs" / f"fit-{name}.json").exists() and _job_alive(HOME / "jobs" / f"fit-{name}.json")
        try:
            out["proposal"] = json.loads((d / "proposal.json").read_text("utf-8")) if (d / "proposal.json").exists() else None
        except json.JSONDecodeError:
            out["proposal"] = {"error": "主位写的建议格式不对，可以重新分析"}
    return out


def _job_alive(f):
    try:
        return pf.pid_alive(json.loads(f.read_text())["pid"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False


def draft_playbook():
    """让主位读项目里现成的文档，按经验包格式整理一份（已有经验包就写到 playbook.draft/ 等主人确认）。"""
    import subprocess
    cfg = load_config()
    name, root, bus, _ = project()
    if name == GENERAL:
        raise ValueError("先选一个项目")
    lead = lead_of(cfg) or "claude"
    brief, target, docs = draft_brief(name, root, bus)
    if agents_dict(cfg).get(lead, {}).get("run") == "claude":  # 桌面端主位：在它自己的对话里做（后台 claude -p 没登录）
        return {"lead": lead, "mode": "chat", "say": f"按项目文档重新整理「{name}」的 agentbus 经验包", "target": str(target), "docs": docs}
    JOBS = HOME / "jobs"
    JOBS.mkdir(parents=True, exist_ok=True)
    log = open(JOBS / f"playbook-{name}.log", "w")
    p = subprocess.Popen([*pf.cli_cmd(), "-p", name, "ask", lead, brief, "--new", "--tag", "复盘总结"],
                         stdout=log, stderr=subprocess.STDOUT, cwd=str(root), **pf.group_kwargs())
    (JOBS / f"playbook-{name}.json").write_text(json.dumps({"pid": p.pid, "target": str(target)}))
    return {"lead": lead, "target": str(target), "docs": docs}


def draft_brief(name, root, bus):
    """整理经验包的任务书：已有经验包就写到 playbook.draft/（主人确认后替换），没有就直接写 playbook/。"""
    docs = project_docs(root)
    target = Path(bus) / ("playbook.draft" if (Path(bus) / "playbook" / "manifest.json").exists() else "playbook")
    brief = f"""请把这个项目已有的工作经验整理成 agentbus 经验包，写到 `{target}/`。
要读的文档：
{chr(10).join('- ' + d for d in docs) or '- （项目里没找到现成文档，就根据任务板 ' + str(Path(bus) / 'board.jsonl') + ' 里的历史任务总结）'}

格式：
- manifest.json：name、version（"1.0"）、author、updated、description、caps_needed、source、changelog
- workflow.md：整条工作流一张表（步、做什么、需要的能力、岗位词条）
- steps/<两位序号-英文id>/SKILL.md：每一步一个，frontmatter 写 name / description / step / tags（只用 agentbus 职能词条）/ caps（只用 {', '.join(sorted(__import__('catalog').CAPS))}）/ scope（通用 或 本项目），正文写这一步怎么做
- lessons.jsonl：一行一条 {{"id": "L001", "text": …, "tags": [词条], "scope": "通用|本项目", "source": …, "date": …}}；写给岗位和能力，不写给具体哪个 agent；只给主位看的规矩不要标“全流程”
不要改这些原始文档，只新建经验包文件。做完回复：几步、几条经验、哪些内容拿不准。"""
    return brief, target, docs


def fit_playbook():
    """融入主人习惯：把经验包要点 + 习惯出处 + 派活记录整理成材料，交给主位写装配建议 proposal.json。"""
    import subprocess
    import playbook
    from config import ledger_read
    cfg = load_config()
    name, root, bus, _ = project()
    pb = playbook.load(bus)
    if not pb:
        raise ValueError("这个项目还没有经验包")
    d = playbook.pb_dir(bus)
    ctx = d / ".fit-context.md"
    ctx.write_text(playbook.fit_context(pb, root, bus, cfg, agents_dict(cfg), ledger_read()), "utf-8")
    out = d / "proposal.json"
    out.unlink(missing_ok=True)
    lead = lead_of(cfg) or "claude"
    if agents_dict(cfg).get(lead, {}).get("run") == "claude":
        # 主位是桌面端的 Claude：在它自己的对话里做（它通过 MCP 工具 agentbus_playbook_fit 拿材料），不另起后台进程
        return {"lead": lead, "mode": "chat", "say": f"把「{name}」的经验包融入我的习惯"}
    brief = f"""你是主位。请帮主人把项目经验包「{pb['manifest'].get('name')}」融入他自己的工作习惯，给出一份装配建议。
1. 读分析材料：{ctx}（经验包每一步、每条经验，主人习惯的出处，主人的阵容和派活记录）
2. 逐个读材料里列出的“主人的习惯”文件，弄清主人自己的做法和偏好
3. 对照着判断：每一步是直接用（use）、按主人习惯改（adapt，写清怎么改）还是跳过（skip）；每条经验用不用；哪里冲突；主人习惯里有、经验包里没有的规矩提炼成新条目；每一步交给哪个 agent 最合主人的习惯
4. 只输出一个 JSON 文件到 {out}，格式：
{playbook.FIT_SCHEMA}
要求：每一步、每条经验都要给出判断；理由用一句大白话写给主人看；不确定的倾向 use；不要改任何其他文件。写完回复一句话：建议跳过几步、改几步、新增几条习惯。"""
    JOBS = HOME / "jobs"
    JOBS.mkdir(parents=True, exist_ok=True)
    log = open(JOBS / f"fit-{name}.log", "w")
    p = subprocess.Popen([*pf.cli_cmd(), "-p", name, "ask", lead, brief, "--new", "--tag", "决策建议"],
                         stdout=log, stderr=subprocess.STDOUT, cwd=str(root), **pf.group_kwargs())
    (JOBS / f"fit-{name}.json").write_text(json.dumps({"pid": p.pid, "start": time.time()}))
    return {"lead": lead}


def lead_of(cfg):
    """主位（负责统筹的 agent）：最近在用 agentbus 统筹的那位（自动主位）> 阵容里指定的 > 第一个挂了“统筹管理”首选的。"""
    from config import active_lead
    lead = active_lead(cfg)
    if lead:
        return lead
    for aid, jobs in (cfg.get("jobs") or {}).items():
        if jobs.get("cat:coord") == "prefer":
            return aid
    return None


_installs = {}


def start_install(aid, install_dir=None):
    """一键招募：只跑 catalog 里定好的安装命令（可以换安装位置），日志在 ~/.agentbus/install/<agent>.log。"""
    import subprocess
    if install_dir is not None:  # 记住这次选的位置，以后找命令行工具也去那里找
        cfg = load_config()
        cfg.setdefault("settings", {})["install_dir"] = install_dir.strip() or None
        save_config(cfg)
    from catalog import install_argv
    plan = install_argv(aid, (load_config().get("settings") or {}).get("install_dir"))
    if not plan:
        raise ValueError("这个 agent 在这台电脑上没有一键安装")
    need = install_needs(aid)
    if need:
        raise ValueError(f"要先装 {need['name']}（{need['url']}），装好再来招募")
    if aid in _installs and _installs[aid].poll() is None:
        return
    d = HOME / "install"
    d.mkdir(parents=True, exist_ok=True)
    log = open(d / f"{aid}.log", "w")
    argv, extra = plan
    if argv is None:  # catalog 里写死的官方安装脚本，不含任何主人输入
        argv, extra = pf.shell(extra), {}
    _installs[aid] = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env={**os.environ, **extra}, **pf.group_kwargs())


def install_status(aid):
    p = _installs.get(aid)
    f = HOME / "install" / f"{aid}.log"
    tail = f.read_text("utf-8", "ignore")[-1500:] if f.exists() else ""
    return {"agent": aid, "cmd": install_cmd(aid, (load_config().get("settings") or {}).get("install_dir")), "needs": install_needs(aid),
            "install_dir": (load_config().get("settings") or {}).get("install_dir") or "", "running": bool(p and p.poll() is None),
            "done": bool(p and p.poll() is not None), "ok": bool(p and p.poll() == 0), "log": tail}


_login_cache = {}


def _login_ui(a):
    """队员页的登录按钮要的：怎么登、登没登（判断要读钥匙串，20 秒缓存一次）。"""
    import logins
    key = a.get("id")
    c = _login_cache.get(key)
    if c and time.time() - c[0] < 20:
        return c[1]
    st = logins.status(a)
    _login_cache[key] = (time.time(), st)
    return st


def _balance_ui(a, cfg):
    """队员页「接入余额」开关要的：这家支不支持、开没开、上次链路每一步的结果。"""
    import balance
    key = a.get("clone_of") or a.get("id")
    return {"supported": balance.supported(key), "on": key in _balance_links(cfg), "result": _quota["balance"].get(key), "why": balance.why_not(key),
            "can_login": balance.can_login(key)}


def _balance_links(cfg=None):
    """打开了「接入余额」的队员 id（分身跟本体走，所以存的是本体 id）。"""
    st = ((cfg or load_config()).get("settings") or {}).get("balance_link") or {}
    return [k for k, v in st.items() if v]


def _sealed_of(a, cfg):
    from config import sealed
    return sealed(a, cfg)


def _pool_windows(wins):
    """额度条合并（对所有队员通用）：剩余比例和恢复时间都一样的算同一个额度池，只留一条；
    名字用模型家族的短名（去掉 Low / Medium / High 这些档位）。返回 [{id, label, short, full, remain}]，按剩余从少到多。"""
    import re as _re
    def fam(lbl):
        return _re.sub(r"\s*\((?:low|medium|high|xhigh|max|minimal)\)\s*$", "", str(lbl), flags=_re.I).strip()
    def short(f):
        f = _re.sub(r"\s*\(([^)]*)\)", r" \1", f)
        words = [w for w in f.split() if not _re.fullmatch(r"[\d.]+[a-z]?|\d+b", w, _re.I) and w.lower() not in ("claude", "gemini", "gpt", "model")]
        return words[0] if words else f.split()[0]  # 「Orchestrator (Argon)」→ Orchestrator
    pools = {}
    for w in wins:
        key = (float(w.get("remain") or 0), w.get("reset") or "")  # 精确值相同才合（不再四舍五入，免得把相近的两个池合成一个）
        p = pools.setdefault(key, {"remain": float(w.get("remain") or 0), "fams": []})
        f = fam(w.get("label"))
        if f not in p["fams"]:
            p["fams"].append(f)
    def vendor(f):
        return re.split(r"[\s(]", f, maxsplit=1)[0]
    plist = list(pools.values())
    for p in plist:
        p["vendors"] = list(dict.fromkeys(vendor(f) for f in p["fams"]))
    out = []
    for p in plist:
        clash = any(q is not p and set(q["vendors"]) & set(p["vendors"]) for q in plist)  # 和别的池同一家：用型号名区分
        names = list(dict.fromkeys(short(f) for f in p["fams"])) if clash else p["vendors"]
        label = "/".join(names[:2]) + (f"+{len(names) - 2}" if len(names) > 2 else "")
        first = names[0]
        out.append({"id": "|".join(sorted(p["fams"])), "label": label, "full": "、".join(p["fams"]), "remain": p["remain"],
                    "short": first if len(first) <= 6 else first[:5]})  # 悬浮窗卡片窄：最多 6 个字符（Claude、Argon、Hydro）
    return sorted(out, key=lambda x: x["remain"])


def _meter_pick(agent_id, ws):
    """悬浮窗显示哪几条：主人在队员页勾选过就按勾选；没选过默认最多 2 条，超过 2 条只显示剩得最少的那条。"""
    pick = ((load_config().get("settings") or {}).get("meter_pick") or {}).get(agent_id)
    if pick:  # 按模型家族认：额度池拆开 / 重新合并（id 从 A|B 变成 A、B）后，勾过的那几个模型还是选中的
        fams = {f for p in pick for f in str(p).split("|")}
        chosen = [w for w in ws if set(w["id"].split("|")) & fams]
        if chosen:
            return chosen
    return ws if len(ws) <= 2 else ws[:1]


def meter(a):
    """每位队员名下的「还剩多少」，对所有计费方式通用：
    pct  订阅额度：账号有哪些窗口（5 小时 / 每周）就各给一条剩余百分比，界面每个窗口画一条进度条
    money 余额：只给金额，界面不画条
    none 看不到或不计费：只给一句说明，不画条。"""
    b = a.get("billing")
    def tone(r):
        return "empty" if r <= 0.02 else "low" if r < 0.2 else "ok"
    key = a.get("clone_of") or a.get("id")
    if key in _balance_links():  # 主人打开了「接入余额」：用它自己的登录凭证查到的剩余，优先显示
        res = _quota["balance"].get(key)
        if not res:
            return {"kind": "none", "main": "余额读取中…"}
        if not res.get("ok"):
            return {"kind": "none", "main": "余额接入失败", "sub": res.get("error") or ""}
        allw = [dict(w, tone=tone(w["remain"]), text=f"{round(w['remain'] * 100)}%") for w in _pool_windows(res["windows"])]
        ws = _meter_pick(key, allw)
        tight = allw[0]
        return {"kind": "pct", "windows": ws, "all": allw, "remain": tight["remain"], "tone": tight["tone"], "main": f"剩 {tight['text']}",
                "sub": " · ".join(f"{w['label']} 剩 {w['text']}" for w in allw)}
    if b == "codex":
        c = _quota["codex"]
        if not c:
            return {"kind": "none", "main": "读取中…"}
        if not c.get("ok"):
            return {"kind": "none", "main": "额度读不到"}
        # 账号有哪些额度窗口就显示哪些：有的账号没有 5 小时限制（读数是 None），只显示每周
        wins = [(label, 100 - c[k]) for k, label in (("five_hour", "5 小时"), ("weekly", "每周")) if c.get(k) is not None]
        if not wins:
            return {"kind": "none", "main": "额度读不到"}
        allw = [{"id": label, "label": label, "short": {"5 小时": "5h", "每周": "周"}.get(label, label), "full": label, "remain": max(0, r) / 100, "tone": tone(max(0, r) / 100), "text": f"{round(r)}%"} for label, r in wins]
        ws = _meter_pick(a.get("clone_of") or a.get("id"), allw)
        tight = min(allw, key=lambda x: x["remain"])
        return {"kind": "pct", "windows": ws, "all": allw, "remain": tight["remain"], "tone": tight["tone"], "main": f"剩 {tight['text']}",
                "sub": " · ".join(f"{x['label']}剩 {x['text']}" for x in allw), "stale": bool(c.get("stale"))}
    if a.get("api") and a.get("account"):
        q = _quota["api"].get(a["account"]) or {}
        if q.get("ok"):
            bal = q.get("cny") if q.get("cny") is not None else q.get("usd")
            cur = "¥" if q.get("cny") is not None else "$"
            return {"kind": "money", "main": f"{cur}{bal:.2f}", "sub": "账户余额"}
    if b == "deepseek":
        d = _quota["deepseek"]
        if not d:
            return {"kind": "none", "main": "读取中…"}
        if not d.get("ok"):
            return {"kind": "none", "main": "余额读不到"}
        run = a.get("run")
        acc = next((x for x in d.get("accounts") or [] if run in (x.get("used_by") or [])), None)
        bal = acc["balance"] if acc else d.get("total") or 0
        return {"kind": "money", "main": f"¥{bal:.1f}", "sub": "DeepSeek 余额" + ("" if acc else "（合计）")}
    if b == "free":
        return {"kind": "none", "main": "免费"}
    if b == "manual":
        return {"kind": "none", "main": "网页版"}
    from config import sealed as _sealed
    if _sealed(a):
        return {"kind": "none", "main": "🔒 已封印"}  # 封印的 Claude 连用量也不去读
    # 剩余额度读不到的：按身份找本机用量记录（Claude / Gemini CLI / Qwen Code 数 token，WorkBuddy 数积分）
    key = a.get("clone_of") or a.get("id")
    if key in quota.LOCAL_USAGE:
        u = (_quota.get("local") or {}).get(key)
        if u is None:
            return {"kind": "none", "main": "读取中…"}
        if not u.get("ok"):
            return {"kind": "none", "main": "额度看不到"}
        if not (u.get("h5") or u.get("d7")):
            return {"kind": "none", "main": "暂无用量记录", "sub": "本机最近 7 天没有它的用量记录；剩余额度读不到"}
        tok = u.get("unit") == "tokens"
        fmt = (lambda n: f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else str(int(n))) if tok else (lambda n: f"{n:g}")
        if u.get("unit") == "积分":  # 单位写进固定文案：英文界面能整句翻译
            return {"kind": "usage", "main": f"7 天用 {fmt(u['d7'])} 积分", "sub": f"最近 5 小时 {fmt(u['h5'])} 积分 · 7 天 {fmt(u['d7'])} 积分（本机记录数的，剩余读不到）"}
        unit = "" if tok else " " + u["unit"]
        return {"kind": "usage", "main": f"7 天用 {fmt(u['d7'])}{unit}", "sub": f"最近 5 小时 {fmt(u['h5'])}{unit} · 7 天 {fmt(u['d7'])}{unit}（本机记录数的，剩余读不到）"}
    return {"kind": "none", "main": "额度看不到"}  # 读不到不等于免费，别写“不计费”


def quota_text(a):
    """每个队员名下显示的额度：GPT 体力剩多少、DeepSeek 金币余额。"""
    b = a.get("billing")
    if b == "codex":
        c = _quota["codex"]  # 两个窗口都可能缺（有的账号只有周额度）：有哪个写哪个
        if not c.get("ok"):
            return "体力 —"
        parts = [f"{lab} {100 - c[k]}%" for k, lab in (("five_hour", "体力"), ("weekly", "周")) if isinstance(c.get(k), (int, float))]
        return " · ".join(parts) or "体力 —"
    if b == "deepseek":
        d = _quota["deepseek"]
        return f"¥{d['total']:.1f}" if d.get("ok") else "金币 —"
    if b == "claude":  # 封印时写清楚；解封后显示从本机记录数的用量
        m = meter(a)
        return m["main"]
    return {"free": "免费", "manual": "网页版"}.get(b, "")


def catalog():
    return {"categories": [{"id": c, "name": n, "tags": t} for c, n, t in CATEGORIES], "levels": LEVELS}


_cache = {}


def cached_state(project_name=None):
    project_name = current_project()  # 所有界面看同一个“当前项目”
    with _lock:
        c = _cache.get(project_name)
        if c and time.time() - c[0] < 1.5:
            return c[1]
        body = json.dumps(state(project_name), ensure_ascii=False)
        _cache[project_name] = (time.time(), body)
        return body


# 只认本机自己：浏览器里别的网站（跨站请求、DNS 重绑定）一律拒绝，不然随便一个网页就能借 agentbus 派活、装东西
_PORT = 8790
def _own_origins():
    return {f"http://127.0.0.1:{_PORT}", f"http://localhost:{_PORT}"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(UI), **kw)

    def end_headers(self):
        # 界面文件（js / css / html）每次都问一下有没有更新：改了代码刷新就生效，不会继续跑浏览器缓存里的旧版
        if self.command == "GET" and not urlparse(self.path).path.startswith("/api/") and "Cache-Control" not in "".join(
                h.decode("latin-1") if isinstance(h, bytes) else str(h) for h in getattr(self, "_headers_buffer", [])):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _allowed(self):
        """Host 必须是本机地址；带 Origin 的必须是 8790 自己的页面；浏览器标了跨站的也拒绝。
        命令行、MCP、应用壳（Rust）发的请求不带 Origin，照常放行。"""
        host = (self.headers.get("Host") or "").lower()
        if host not in {f"127.0.0.1:{_PORT}", f"localhost:{_PORT}"}:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in _own_origins():
            return False
        return self.headers.get("Sec-Fetch-Site") not in ("cross-site", "same-site")

    def _page(self, path):
        f = UI / ("index.html" if path == "/" else path.lstrip("/"))
        html = f.read_text("utf-8")

        def ver(m):
            src = UI / m.group(2)
            v = int(src.stat().st_mtime) if src.exists() else 0
            return f'{m.group(1)}="{m.group(2)}?v={v}"'
        html = re.sub(r'(src|href)="([\w./-]+\.(?:js|css))"', ver, html)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _oauth_callback(self):
        import balance
        host = (self.headers.get("Host") or "").lower()
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        if host not in {f"127.0.0.1:{_PORT}", f"localhost:{_PORT}"} or not balance.login_pending(q.get("state", "")):
            return self._deny()
        def page(title, text, ok):
            import html as _h
            title, text = _h.escape(str(title)), _h.escape(str(text))
            body = (f'<!doctype html><meta charset="utf-8"><title>agentbus</title><body style="font:15px -apple-system,system-ui,sans-serif;'
                    f'display:grid;place-items:center;height:90vh;margin:0;background:#f6f3ee;color:#2b2622"><div style="max-width:440px;text-align:center">'
                    f'<div style="font-size:42px">{"✅" if ok else "⚠️"}</div><h2>{title}</h2><p style="color:#6b625b;line-height:1.6">{text}</p></div>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        if q.get("error") or not q.get("code"):
            balance._login.pop(q.get("state", ""), None)
            return page("没有登录", "你取消了授权或者登录没完成。回到 agentbus 再点一次「用 Google 登录」就行。", False)
        try:
            email = balance.login_finish(q["state"], q["code"])
        except Exception as e:
            return page("登录没成功", f"{e}", False)
        from config import update_json, CONFIG
        update_json(CONFIG, lambda c: c.setdefault("settings", {}).setdefault("balance_link", {}).__setitem__("gemini", True))
        _quota["balance"]["gemini"] = balance.check("gemini")  # 登录完直接把余额链路跑一遍
        _cache.clear()
        page("登录好了", f"已用 {email or 'Google 账号'} 登录 Gemini，agentbus 正在读它的剩余额度。可以关掉这个页面回到 agentbus。", True)

    def _deny(self):
        body = b'{"error": "forbidden"}'
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        super().end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        body = (obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _guard(self, fn):
        """请求总边界：任何没接住的异常都回 500 和一个错误编号（日志里能对上），不直接断开连接。"""
        try:
            return fn()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            import traceback
            import uuid
            eid = uuid.uuid4().hex[:6]
            print(f"[{eid}] {self.command} {self.path} 出错：", file=sys.stderr)
            traceback.print_exc()
            try:
                status = 400 if isinstance(e, (ValueError, KeyError, TypeError)) else 500
                self._json({"error": f"{type(e).__name__}: {e}"[:300], "id": eid}, status)
            except Exception:
                pass

    def do_GET(self):
        return self._guard(self._do_get)

    # 读主人凭证 / 改东西的 GET 也要确认是本机同一个用户发的
    _PEER_GET = ("/api/wallet", "/api/connectors", "/api/chat/file", "/api/shutdown", "/api/hudlog", "/api/brain", "/api/setup")

    def _peer_ok(self):
        """连接对面必须是同一个系统用户的程序（挡住同一台电脑上的其他用户）。查不了的平台照常放行。"""
        uid = pf.peer_uid(self.client_address[1], _PORT)
        return uid == -1 or uid == os.getuid()

    def do_POST(self):
        # 改数据的请求排队过同一把配置锁：悬浮窗和主界面同时改设置不会互相覆盖；CLI 的 save_config 也走这把锁。
        # 来源检查和读请求体都在拿锁之前做完（限大小、限时），慢连接占不住锁
        from config import locked
        path = urlparse(self.path).path

        def go():
            if not self._allowed() or not self._peer_ok():
                return self._deny()
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json({"error": "Content-Length 不对"}, 400)
            if n < 0 or n > (50 if path == "/api/chat/upload" else 4) * 1024 * 1024:
                return self._json({"error": "内容太大" + ("（单个附件上限 50MB）" if path == "/api/chat/upload" else "")}, 413)
            self.connection.settimeout(30)
            try:
                self._body = self.rfile.read(n)
            finally:
                self.connection.settimeout(None)
            if len(self._body) < n:
                return
            if path in ("/api/wallet/test", "/api/chat/upload", "/api/balance/link", "/api/login"):  # 要联网很久 / 只存附件：不占全局配置锁（自己用 update_json 的锁）
                return self._do_post()
            with locked("config"):
                return self._do_post()
        return self._guard(go)

    def _do_get(self):
        if urlparse(self.path).path in ("/", "/index.html", "/hud.html"):  # 页面里的 js / css 带上版本号：更新后所有窗口都会拿到新版，不吃缓存
            if not self._allowed():
                return self._deny()
            return self._page(urlparse(self.path).path)
        if urlparse(self.path).path == "/oauth2callback":  # Google 登录完跳回来（来自 Google 网站，所以单独放行：只认本次登录的一次性 state）
            return self._oauth_callback()
        u = urlparse(self.path)
        if u.path.startswith(self._PEER_GET) and not self._peer_ok():
            return self._deny()
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/state":
            return self._json(cached_state(q.get("project")))
        if u.path == "/api/catalog":
            return self._json(catalog())
        if u.path == "/api/events":
            return self.sse(q.get("project"))
        if u.path == "/api/bill":
            b = q.get("batch")
            return self._json(bill(q.get("project") or project()[0], b if b else None))
        if u.path == "/api/project":
            name = q.get("name") or ""
            ps = projects()
            if name not in ps:
                return self._json({"error": "没有这个项目"}, 404)
            brief = Path(ps[name]["bus"]) / "brief.md"
            return self._json({"name": name, "root": ps[name]["root"], "bus": ps[name]["bus"],
                               "brief": brief.read_text("utf-8") if brief.exists() else ""})
        if u.path == "/api/install/preview":  # 换了安装位置，命令长什么样
            return self._json({"cmd": install_cmd(q.get("agent", ""), q.get("dir") or None), "needs": install_needs(q.get("agent", ""))})
        if u.path == "/api/install":
            return self._json(install_status(q.get("agent", "")))
        if u.path in ("/.well-known/agent.json", "/.well-known/agent-card.json"):
            return self._json(a2a_card())
        if u.path == "/api/wallet":
            import wallet
            return self._json({"accounts": wallet.public(), "found": [{k: d[k] for k in ("fp", "provider", "name", "from", "base", "added")} | {"also": d.get("also"), "models": len(d["models"])}
                                                                      for d in wallet.discover()],
                               "presets": {k: {"name": v["name"], "base": v["base"]} for k, v in wallet.PRESETS.items()}})
        if u.path == "/api/wallet/balance":
            import wallet
            return self._json(wallet.balance(q.get("id", "")))
        if u.path == "/api/wallet/models":
            import wallet
            return self._json({"models": wallet.models(q.get("id", ""), q.get("fresh") == "1")})
        if u.path == "/api/setup":  # 首次装配：评估本机的 agent 和模型
            import setup
            return self._json(setup.evaluate())
        if u.path == "/api/brain":
            try:
                return self._json(brain_options(q.get("agent", ""), q.get("fresh") == "1"))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
        if u.path == "/api/playbook":
            return self._json(playbook_state())
        if u.path == "/api/messages":  # 留言：给这位 / 这位发出的，最近 30 条，带“对方读没读”
            aid = q.get("agent", "")
            _, _, bus, _ = project()
            bf = Path(bus) / "board.jsonl"  # 读全文：已读位置是按整份任务板的行号记的
            recs = [json.loads(l) for l in bf.read_text("utf-8").splitlines() if l.strip()] if bf.exists() else []
            read = json.loads((Path(bus) / "read.json").read_text()) if (Path(bus) / "read.json").exists() else {}
            out = []
            for i, r in enumerate(recs):
                if r.get("kind") == "msg" and aid in (r.get("to"), r.get("frm")):
                    out.append({"t": r.get("t"), "frm": r.get("frm"), "to": r.get("to"), "text": r.get("text"),
                                "read": r.get("to") != aid or i < read.get(aid, 0)})
            return self._json({"messages": out[-30:]})
        if u.path == "/api/chat":  # 消息：频道列表 + 某个频道最近的消息
            import chat
            ch = q.get("channel") or "大厅"
            if not chat.valid(ch):
                return self._json({"error": "频道名不对"}, 400)
            mt = chat.meta(ch)
            tasks = chat.running(ch)
            return self._json({"channels": chat.channels(), "channel": ch, "messages": chat.with_job_status(chat.read(ch, int(q.get("n") or 200))),
                               "meta": {k: mt.get(k) for k in ("mode", "duty", "members", "rounds", "compressions", "project", "handoff_mode", "relay_hops")}, "cwd": chat.workspace(ch)[1], "talking": bool(mt.get("talk_pid") and _alive(mt["talk_pid"])),
                               "usage": chat.usage(ch), "talk_members": chat.talk_members(ch), "running": len(tasks),
                               "running_tasks": [{k: r.get(k) for k in ("agent", "start", "queued", "title", "progress", "cwd", "job")} for r in tasks],
                               "duty_now": chat.resolve_duty(mt.get("duty")), "limits": chat.channel_limits(ch), "max_rounds": chat.TALK_ROUNDS})
        if u.path == "/api/connectors":  # 各家 agent 的接入方式：装没装、钩子挂没挂、手动配置、接入口令
            import connectors
            return self._json(connectors.status())
        if u.path == "/api/update":  # 有没有新版本（GitHub Releases）
            import update
            return self._json(update.check(q.get("fresh") == "1"))
        if u.path == "/api/chat/file":  # 附件预览：只给附件目录里的文件
            import chat
            from chat_attachments import managed_upload
            try:
                f = managed_upload(q.get("p") or "", chat.FILES)
            except (OSError, ValueError, RuntimeError):
                return self._json({"error": "不行"}, 403)
            # 只有普通位图在页面里直接显示；HTML、SVG 等能带脚本的一律当下载，并用沙箱 CSP 兜底，免得它以本服务的身份调接口
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}.get(f.suffix.lower())
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self'")
            if not ctype:
                from urllib.parse import quote
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(f.name))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/quests":
            return self._json(quests(q.get("project")))
        if u.path == "/api/suggest":
            return self._json(suggest(q.get("tag", "")))
        if u.path == "/api/settings":
            return self._json(settings())
        if u.path == "/api/hudlog":  # 悬浮窗调试用：把它报上来的信息写进服务日志
            print("hud:", q, file=sys.stderr, flush=True)
            try:  # 应用拉起的服务没有终端，另记一份到 hud.log（只留最近 200 行）
                f = HOME / "hud.log"
                lines = (f.read_text(encoding="utf-8").splitlines() if f.exists() else [])[-199:]
                f.write_text("\n".join(lines + [time.strftime("%m-%d %H:%M:%S ") + str(q.get("m", ""))[:300]]) + "\n", encoding="utf-8")
            except OSError:
                pass
            return self._json({"ok": True})
        if u.path == "/api/shutdown":  # 应用菜单“重启本地服务”用（只听本机）
            self._json({"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), _server and _server.shutdown()), daemon=True).start()
            return
        if u.path == "/api/health":
            return self._json({"ok": True, "app": "agentbus", "loop_age": round(time.time() - _loop_beat["t"]) if _loop_beat["t"] else None})
        return super().do_GET()

    def sse(self, project_name):
        """有变化就推一份完整状态；两个界面（应用窗口、Claude 内置浏览器）同时连着也是同一份数据。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last, beat = None, time.time()
        try:
            while True:
                body = cached_state(project_name)
                h = hashlib.md5(body.encode()).hexdigest()
                if h != last:
                    self.wfile.write(b"data: " + body.encode() + b"\n\n")
                    self.wfile.flush()
                    last, beat = h, time.time()
                elif time.time() - beat > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    beat = time.time()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _do_post(self):
        if not self._allowed():
            return self._deny()
        u = urlparse(self.path)
        if u.path == "/api/chat/upload":  # 保留原始字节；发送时用此引用绑定附件
            import chat
            from urllib.parse import unquote
            raw = self.headers.get("X-Name") or "file"
            try:  # 没编码直接发的中文名（http.server 按 latin-1 读进来）还原回去
                raw = raw.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", unquote(raw))[-80:].rstrip(" .") or "file"
            chat.FILES.mkdir(parents=True, exist_ok=True)
            import uuid
            f = chat.FILES / f"{time.strftime('%m%d-%H%M%S')}-{uuid.uuid4().hex}-{name}"
            with f.open("xb") as upload:
                upload.write(self._body)
            return self._json({"path": str(f), "name": name, "uploaded": True, "image": f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic")})
        try:
            data = json.loads(self._body or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "不是 JSON"}, 400)
        if not isinstance(data, dict):
            return self._json({"error": "要传一个 JSON 对象"}, 400)
        if u.path.startswith("/api/chat") and data.get("channel") is not None:
            import chat
            if not chat.valid(data["channel"]):
                return self._json({"error": "频道名不对"}, 400)
        if u.path == "/api/pack/mount":  # 经验包装到办公室 / 窗口的项目文件夹，或者卸下
            import config as _c
            raw = _c.load_json(_c.CONFIG, {})
            ms = [m for m in raw.get("pack_mounts") or [] if not (m.get("pack") == data.get("pack") and m.get("office") == data.get("office") and m.get("dir") == data.get("dir"))]
            if data.get("on") and data.get("pack") and (data.get("office") or data.get("dir")):
                ms.append({k: data[k] for k in ("pack", "office", "dir") if data.get(k)})
            raw["pack_mounts"] = ms
            _c.save_json(_c.CONFIG, raw)
            _cache.clear()
            return self._json({"ok": True})
        if u.path in ("/api/office/lineup", "/api/office/theme"):  # 办公室：调战备预设组 / 换主题
            cfg = load_config()
            lead = data.get("office")
            if lead not in agents_dict(cfg):
                return self._json({"error": "没有这间办公室"}, 400)
            import config as _c  # 这个函数后面别处 import 过 save_json，直接用名字会被当成局部变量
            raw = _c.load_json(_c.CONFIG, {})
            if u.path == "/api/office/theme":
                raw.setdefault("office_themes", {})[lead] = data.get("theme") or None
            elif data.get("reset"):
                (raw.get("office_lineups") or {}).pop(lead, None)
            else:
                cur = office_lineup(cfg, lead)
                office = [a for a in (data.get("members") if data.get("members") is not None else cur["office"]) if a in agents_dict(cfg)][:6]
                jobs = dict(cur["jobs"])
                for aid, cat in (data.get("prefer") or {}).items():  # 换某人的主力：这间办公室里只改它自己的那一条
                    j = {k: v for k, v in (jobs.get(aid) or {}).items() if v != "prefer"}
                    if cat:
                        j[cat if cat.startswith(("cat:", "tag:")) else "cat:" + cat] = "prefer"
                    jobs[aid] = j
                raw.setdefault("office_lineups", {})[lead] = {"office": office, "jobs": jobs}
            _c.save_json(_c.CONFIG, raw)
            _cache.clear()
            return self._json({"ok": True})
        if u.path == "/api/chat/send":  # 在消息窗口里发言：@ 到的队员各派一单活，回复自动发回群里
            import chat
            text = (data.get("text") or "").strip()
            if not text:
                return self._json({"error": "写点内容"}, 400)
            from chat_attachments import managed_upload
            files = data.get("files", [])
            if not isinstance(files, list) or len(files) > 10:
                return self._json({"error": "每条消息最多可附 10 个文件"}, 400)
            try:
                for x in files:
                    managed_upload(x["path"], chat.FILES)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                return self._json({"error": "附件不可用，请用回形针重新选择文件"}, 400)
            msg, jobs = chat.send(data.get("channel") or "大厅", text, cwd=data.get("cwd") or None, files=files)
            _cache.clear()
            return self._json({"ok": True, "message": msg, "jobs": jobs})
        if u.path == "/api/chat/settings":  # 频道设置：模式（派活 / 群聊）、值班的人、群聊成员和轮数
            import chat
            ch = data.get("channel") or "大厅"
            kw = {k: data[k] for k in ("mode", "duty", "members", "rounds", "project") if k in data}
            if kw.get("project") and kw["project"] not in projects():
                return self._json({"error": "没有这个项目，请先登记项目文件夹"}, 400)
            if "rounds" in kw:
                kw["rounds"] = max(1, min(chat.TALK_ROUNDS, int(kw["rounds"] or 1)))
            old = chat.meta(ch)
            if "project" in kw and kw["project"] != (old.get("project") or "") and (chat.running(ch) or (old.get("talk_pid") and _alive(old["talk_pid"]))):
                return self._json({"error": "本频道仍在执行，请结束后再切换项目文件夹"}, 409)
            chat.set_meta(ch, **kw)
            if kw.get("mode") and kw["mode"] != old.get("mode"):  # 切了模式：在群里说清楚之后会怎样
                names = {k: v.get("name", k) for k, v in agents_dict(load_config()).items()}
                if kw["mode"] == "talk":
                    who = "、".join(names.get(m, m) for m in chat.talk_members(ch))
                    chat.append(ch, "agentbus", f"🔀 这个频道改成了「群聊」：你说一句，{who} 会按顺序轮流接着聊（只聊不动手）。想让某人干活就 @ 他。", system=True)
                else:
                    d = chat.resolve_duty(chat.meta(ch).get("duty"))
                    chat.append(ch, "agentbus", f"🔀 这个频道改成了「派活」：@ 谁谁干活，干完把结果发回群里；没 @ 人时交给 {names.get(d, '没人（只记录）') if d else '没人（只记录）'}。", system=True)
            return self._json({"ok": True})
        if u.path == "/api/chat/stop":
            import chat
            chat.stop(data.get("channel") or "大厅")
            _cache.clear()
            return self._json({"ok": True})
        if u.path == "/api/chat/compress":  # 手动压缩上下文（后台跑，要一会儿）
            import chat
            ch = data.get("channel") or "大厅"
            if ch in _compressing:
                return self._json({"ok": True, "busy": True})
            threading.Thread(target=_compress_one, args=(ch,), daemon=True).start()
            return self._json({"ok": True})
        if u.path == "/api/chat/channel":  # 新建频道
            import chat
            name = (data.get("name") or "").strip().replace("/", "／").replace("\\", "＼")[:30]
            if not name:
                return self._json({"error": "起个名字"}, 400)
            if not chat.valid(name):
                return self._json({"error": "名字里不能有 : 或开头的点"}, 400)
            chat.append(name, "agentbus", f"频道「{name}」建好了，@ 队员就能派活。", system=True)
            chat.set_meta(name, project=current_project())
            return self._json({"ok": True})
        if u.path == "/api/session/new":  # 钩子报来的新对话窗口：悬浮窗问主人要不要接入 bus
            if data.get("conv"):
                _join_ask[data["conv"]] = {"conv": data["conv"], "agent": data.get("agent"), "cwd": data.get("cwd"), "t": time.time()}
                _ask_save()
                _cache.clear()
            return self._json({"ok": True})
        if u.path == "/api/join":  # 主人点了“接入”或“不了”（always = 以后新对话都自动接入）
            conv = data.get("conv")
            ask = _join_ask.pop(conv, None) or {}
            _ask_save()
            import config as _c  # 这个函数后面别处 import 过 save_json，直接用名字会被当成局部变量
            if conv:  # 接入 / 不了都记下来：MCP 握手（agentbus_hello）在等这个结果
                rec = ({"agent": ask.get("agent") or data.get("agent"), "cwd": ask.get("cwd"), "t": time.time(), "briefed": False}
                       if data.get("accept") else {"declined": True, "t": time.time()})
                _c.update_json(HOME / "joined.json", lambda j: j.__setitem__(conv, rec))
            if data.get("always") is not None:
                raw = _c.load_json(_c.CONFIG, {})
                raw.setdefault("settings", {})["auto_join"] = "always" if data["always"] else "ask"
                _c.save_json(_c.CONFIG, raw)
            _cache.clear()
            if data.get("accept"):  # 接入了：告诉悬浮窗这个窗口怎么拿到握手（装了钩子就自动；没装给兜底选项）
                import connectors
                return self._json({"ok": True, "delivery": connectors.delivery(ask.get("agent") or data.get("agent") or ""), "name": ask.get("name")})
            return self._json({"ok": True})
        if u.path == "/api/login":  # 一键登录（通用）：内置授权 / 后台跑它自带的登录命令 / 打开它的应用 / 兜底开终端
            import logins
            a = agents_dict(load_config()).get(data.get("agent") or "")
            if not a:
                return self._json({"error": "没有这个队员"}, 400)
            try:
                r = logins.start(a, _PORT)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            _login_cache.clear()
            return self._json(r)
        if u.path == "/api/balance/link":  # 队员页「接入余额」：打开就立刻用登录凭证跑一遍链路，把每一步的结果带回去
            import balance
            from config import update_json, CONFIG
            aid = data.get("agent") or ""
            base = (agents_dict(load_config()).get(aid) or {}).get("clone_of") or aid
            if not balance.supported(base):
                return self._json({"error": "这家还没有接入余额的方式"}, 400)
            on = bool(data.get("on"))
            update_json(CONFIG, lambda c: c.setdefault("settings", {}).setdefault("balance_link", {}).__setitem__(base, on))
            res = balance.check(base) if on else None
            if on:
                _quota["balance"][base] = res
            else:
                _quota["balance"].pop(base, None)
            _cache.clear()
            return self._json({"ok": True, "on": on, "result": res})
        if u.path in ("/api/connectors/install", "/api/connectors/remove"):  # 一键装 / 卸 接入钩子（改别家配置前先备份）
            import connectors
            fn = connectors.install if u.path.endswith("install") else connectors.remove
            try:
                return self._json(fn(data.get("id", "")))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
        if u.path == "/api/hello":  # 有 agent 通过 MCP 接入了
            _hello.clear()
            _hello.update(agent=data.get("agent"), client=(data.get("client") or "")[:40], t=time.time())
            _cache.clear()
            return self._json({"ok": True})
        if u.path == "/api/post":  # 主人给队员留言：它下次接活时会在提示里看到
            if not (data.get("agent") and (data.get("text") or "").strip()):
                return self._json({"error": "写点内容"}, 400)
            _, _, bus, _ = project()
            with open(Path(bus) / "board.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "msg", "frm": "user", "to": data["agent"],
                                    "text": data["text"].strip()}, ensure_ascii=False) + "\n")
            _cache.clear()
            return self._json({"ok": True})
        if u.path in ("/api/connect-lead", "/api/connect-claude"):  # 把 agentbus 的 MCP 工具接进主位（谁当主位就接谁）
            try:
                return self._json(connect_lead(data.get("agent")))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
        if u.path in ("/api/setup/apply", "/api/setup/skip"):
            import setup
            if u.path == "/api/setup/apply":
                setup.apply(data)
            else:
                cfg = load_config(); cfg.setdefault("settings", {})["setup_done"] = True; save_config(cfg)
            _cache.clear()
            return self._json({"ok": True})
        if u.path == "/a2a":
            return self._json(a2a_rpc(data))
        if u.path == "/api/open-url":  # 打开更新下载页（只放 GitHub 的链接）
            url = str(data.get("url") or "")
            if not url.startswith("https://github.com/") and url not in INSTALL_GUIDE.values():
                return self._json({"error": "只能打开 GitHub 和官方下载页的链接"}, 400)
            pf.open_url(url)
            return self._json({"ok": True})
        if u.path == "/api/open":
            try:
                open_agent(data.get("agent", ""), data.get("how", "app"))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True})
        if u.path == "/api/playbook/equip":
            cfg = load_config()
            name = project()[0]
            if name == GENERAL:
                return self._json({"error": "无项目时没有经验包"}, 400)
            lu = cfg.setdefault("lineups", {}).setdefault(name, {"roster": cfg["roster"], "jobs": cfg["jobs"]})
            eq = lu.setdefault("playbook", {"enabled": True, "steps_off": [], "lessons_off": []})
            eq.setdefault("steps_off", []); eq.setdefault("lessons_off", [])
            if "enabled" in data:
                eq["enabled"] = bool(data["enabled"])
            for kind, key in (("step", "steps_off"), ("lesson", "lessons_off")):
                if kind in data:
                    off = set(eq[key])
                    (off.discard if data.get("on") else off.add)(data[kind])
                    eq[key] = sorted(off)
            save_config(cfg)
            _cache.clear()
            return self._json({"ok": True})
        if u.path == "/api/playbook/fit":
            try:
                return self._json(fit_playbook())
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
        if u.path == "/api/playbook/apply":
            import playbook
            cfg = load_config()
            name, _, bus, _ = project()
            if data.get("drop"):  # 先不装：把建议收起来，什么都不改
                prop = playbook.pb_dir(bus) / "proposal.json"
                if prop.exists():
                    prop.rename(prop.with_name("proposal.dropped.json"))
                return self._json({"ok": True})
            personal = playbook.apply_fit(bus, cfg, name, data)
            save_config(cfg)
            _cache.clear()
            return self._json({"ok": True, "personal": len(personal["lessons"]), "adapt": len(personal["steps"])})
        if u.path == "/api/playbook/merge":  # 把导入的经验包融合进本地文档：交给主位（桌面端在它自己的对话里做）
            import playbook
            cfg = load_config()
            name, root, bus, _ = project()
            pb = playbook.load(bus)
            if not pb:
                return self._json({"error": "还没有经验包，先导入"}, 400)
            lead = lead_of(cfg) or "claude"
            ctx = playbook.merge_context(pb, root, bus)
            (playbook.pb_dir(bus) / ".merge-context.md").write_text(ctx, "utf-8")
            if agents_dict(cfg).get(lead, {}).get("run") == "claude":
                return self._json({"lead": lead, "mode": "chat", "say": f"把导入的「{pb['manifest'].get('name')}」经验包融合进本地文档"})
            import subprocess
            subprocess.Popen([*pf.cli_cmd(), "-p", name, "ask", lead, ctx + "\n\n先只写出融合建议（三类），不要直接改文件，等主人确认。", "--new", "--tag", "复盘总结"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(root), **pf.group_kwargs())
            return self._json({"lead": lead, "mode": "job"})
        if u.path == "/api/playbook/adopt-draft":  # 用按文档整理的新版替换经验包（旧版整个改名留着）
            _, _, bus, _ = project()
            d, dd = Path(bus) / "playbook", Path(bus) / "playbook.draft"
            if not (dd / "manifest.json").exists():
                return self._json({"error": "没有待替换的新版"}, 400)
            if d.exists():
                d.rename(d.with_name("playbook.bak-" + time.strftime("%m%d-%H%M%S")))
            dd.rename(d)
            _cache.clear()
            return self._json({"ok": True})
        if u.path in ("/api/playbook/export", "/api/playbook/import", "/api/playbook/lesson", "/api/playbook/archive"):
            import base64
            import playbook
            name, root, bus, _ = project()
            try:
                if u.path == "/api/playbook/export":
                    out, rep = playbook.export_pack(bus, root, data.get("hide_names", True))
                    pf.reveal(out)  # 在访达 / 资源管理器里选中导出的文件
                    return self._json({"ok": True, "path": str(out), "report": rep})
                if u.path == "/api/playbook/import":
                    if name == GENERAL:
                        raise ValueError("先选一个项目再导入")
                    r = playbook.import_pack(bus, root, base64.b64decode(data.get("data", "")))
                    cfg = load_config()  # 导入的经验包不直接用来派活，等融合进本地文档
                    cfg.setdefault("lineups", {}).setdefault(name, {"roster": cfg["roster"], "jobs": cfg["jobs"]}).setdefault("playbook", {})["enabled"] = False
                    save_config(cfg)
                elif u.path == "/api/playbook/lesson":
                    r = {"id": playbook.decide(bus, data.get("pid", ""), bool(data.get("adopt")), data.get("text"))}
                else:
                    playbook.archive(bus, data.get("id", ""))
                    r = {}
            except (ValueError, KeyError) as e:
                return self._json({"error": str(e).strip("'")}, 400)
            _cache.clear()
            return self._json(dict(r or {}, ok=True))
        if u.path == "/api/playbook/draft":
            try:
                return self._json(draft_playbook())
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
        if u.path.startswith("/api/wallet/") or u.path.startswith("/api/api-agent/"):
            import wallet
            try:
                r = {"ok": True}
                if u.path == "/api/wallet/test":  # 只试连接，不保存
                    r = wallet.test_found(data["fp"]) if data.get("fp") else wallet.test(data.get("base"), (data.get("key") or "").strip())
                    return self._json(r)
                if u.path == "/api/wallet/link":
                    added = next((k for k, v in wallet.accounts().items() if v.get("fp") == data.get("fp")), None)
                    r["id"] = added or wallet.link(data.get("fp", ""), data.get("base"), data.get("models"))
                elif u.path == "/api/wallet/add":
                    r["id"] = wallet.add(data.get("provider", "custom"), (data.get("key") or "").strip(), data.get("base"), data.get("name"), data.get("models"))
                elif u.path == "/api/wallet/remove":
                    used = [k for k, v in (load_config().get("api_agents") or {}).items() if v.get("account") == data.get("id")]
                    if used:
                        raise ValueError("还有 API 队员在用这个账户，先解雇它们")
                    wallet.remove(data.get("id", ""))
                elif u.path == "/api/wallet/update":
                    wallet.update(data.get("id", ""), limit=data.get("limit"), name=data.get("name"))
                elif u.path == "/api/api-agent/new":
                    r["id"] = new_api_agent(data.get("account", ""), data.get("model", ""), data.get("name"))
                elif u.path == "/api/api-agent/remove":
                    remove_api_agent(data.get("agent", ""))
                else:
                    return self._json({"error": "没有这个接口"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            _cache.clear()
            return self._json(r)
        if u.path in ("/api/clone", "/api/clone/remove"):
            try:
                if u.path == "/api/clone":
                    cid = make_clone(data.get("base", ""), data.get("name"))
                else:
                    remove_clone(data.get("agent", ""))
                    cid = None
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            _cache.clear()
            return self._json({"ok": True, "id": cid})
        if u.path == "/api/install":
            try:
                start_install(data.get("agent", ""), data.get("install_dir"))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            _cache.clear()
            return self._json({"ok": True})
        if u.path.startswith("/api/project/"):
            try:
                if u.path == "/api/project/new":
                    create_project(data.get("name", ""), data.get("root") or None, data.get("brief", ""), data.get("copy", True))
                    set_current(data["name"].strip())
                elif u.path == "/api/project/remove":
                    from config import remove_project
                    remove_project(data.get("name", ""))
                elif u.path == "/api/project/use":
                    set_current(data.get("name") or "")
                elif u.path == "/api/project/brief":
                    ps = projects()
                    (Path(ps[data["name"]]["bus"]) / "brief.md").write_text(data.get("brief", ""), "utf-8")
                    if data.get("root"):
                        ps[data["name"]]["root"] = os.path.expanduser(data["root"])
                        from config import PROJECTS, save_json
                        save_json(PROJECTS, ps)
                else:
                    return self._json({"error": "没有这个接口"}, 404)
            except (ValueError, KeyError) as e:
                return self._json({"error": str(e).strip("'")}, 400)
            _cache.clear()
            return self._json({"ok": True})
        cfg = load_config()
        if u.path == "/api/move":
            aid, place = data.get("agent"), data.get("place")
            if aid not in agents_dict(cfg) or place not in ("office", "lounge"):
                return self._json({"error": "参数不对"}, 400)
            for k in ("office", "lounge"):
                if aid in cfg["roster"][k]:
                    cfg["roster"][k].remove(aid)
            idx = data.get("index")
            lst = cfg["roster"][place]
            lst.insert(idx if isinstance(idx, int) else len(lst), aid)
        elif u.path == "/api/brain":
            try:
                return self._json(set_brain(data.get("agent"), data))
            except ValueError as error:
                return self._json({"error": str(error)}, 400)
        elif u.path == "/api/lead":
            if data.get("agent") not in agents_dict(cfg):
                return self._json({"error": "不认识的 agent"}, 400)
            cfg["roster"]["lead"] = data["agent"]
            from config import LEAD_FILE
            LEAD_FILE.unlink(missing_ok=True)  # 手动指定的马上生效；之后谁再来统筹，自动主位会再跟过去
        elif u.path == "/api/assign":
            aid, level = data.get("agent"), data.get("level")
            if aid not in agents_dict(cfg):
                return self._json({"error": "不认识的 agent"}, 400)
            key = data.get("key") or resolve_tag(data.get("tag", ""))[0]
            jobs = cfg["jobs"].setdefault(aid, {})
            if level in LEVELS:
                jobs[key] = level
            else:
                jobs.pop(key, None)
        elif u.path == "/api/settings":
            st = cfg.setdefault("settings", {})
            if "hud" in data:
                st.setdefault("hud", {}).update(data["hud"])
            if isinstance(data.get("chat"), dict):  # 聊天室默认规则
                import chat as _chat
                cur = st.setdefault("chat", {})
                for k, v in data["chat"].items():
                    if k in _chat.DEFAULTS:
                        cur[k] = v if k == "duty" else max(1, int(v))
            if "update_repo" in data:
                st["update_repo"] = str(data["update_repo"] or "").strip()[:100]
                _upd.clear()
            for k in ("theme", "login", "tour_done", "auto_join"):
                if k in data:
                    st[k] = data[k]
            if data.get("lang") in ("system", "en", "zh"):
                st["lang"] = data["lang"]
            if isinstance(data.get("meter_pick"), dict):  # 队员页勾选：悬浮窗显示哪几条额度
                mp = st.setdefault("meter_pick", {})
                for k, v in data["meter_pick"].items():
                    if isinstance(v, list) and v:
                        mp[k] = [str(x) for x in v][:4]
                    else:
                        mp.pop(k, None)
            if "claude_seal" in data:  # 解封要在界面上确认过风险；重新封印随时可以
                st["claude_seal"] = bool(data["claude_seal"])
            if "scheduler" in data:  # {"screen_lock": bool, "limits": {计费: n}}
                sc = cfg.setdefault("scheduler", {})
                if "screen_lock" in data["scheduler"]:
                    sc["screen_lock"] = bool(data["scheduler"]["screen_lock"])
                for b, n in (data["scheduler"].get("limits") or {}).items():
                    sc.setdefault("limits", {})[b] = max(1, min(20, int(n)))
            if "login" in data:
                set_login(bool(data["login"]))
        elif u.path == "/api/batch":
            set_batch(data.get("project") or project()[0], data.get("batch", ""))
            _cache.clear()
            return self._json({"ok": True})
        else:
            return self._json({"error": "没有这个接口"}, 404)
        save_config(cfg)
        _cache.clear()
        return self._json({"ok": True})

    def do_OPTIONS(self):
        if not self._allowed():
            return self._deny()
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def end_headers(self):
        # 只给 8790 自己的页面（应用窗口、Claude 内置浏览器打开的都是它）
        origin = self.headers.get("Origin") if hasattr(self, "headers") else None
        if origin in _own_origins():
            self.send_header("Access-Control-Allow-Origin", origin)
        if not getattr(self, "path", "/api/").startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, *a):
        pass


_server = None


def serve(port=8790):
    global _server, _PORT
    _PORT = int(port)
    os.environ["AGENTBUS_PORT"] = str(_PORT)  # 派活、群聊这些子进程回调本服务时用同一个端口
    # 应用拉起服务时环境里没有中文编码：派活的子进程（opencode 等）拿到中文路径会变乱码、切不进目录
    if "UTF-8" not in (os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE") or os.environ.get("LANG") or "").upper():
        os.environ["LANG"] = os.environ["LC_ALL"] = "en_US.UTF-8"
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.pop("PWD", None)  # 继承来的 PWD 可能是乱码（中文路径），opencode 会照它切目录然后失败
    # 老用户（已经有配置、但还没有装配标记）：当作已经装配过，不弹首次装配向导
    from config import CONFIG
    if CONFIG.exists():
        raw = load_json(CONFIG, {})
        st = raw.setdefault("settings", {})
        if "setup_done" not in st and (raw.get("jobs") or (raw.get("roster") or {}).get("office")):
            st["setup_done"] = True
            save_json(CONFIG, raw)
    threading.Thread(target=quota_loop, daemon=True).start()
    _ask_restore()
    brains.zcode_recover()  # 上次换 ZCode 模型时被强杀、没换回的，补换回
    threading.Thread(target=detect_loop, daemon=True).start()
    ThreadingHTTPServer.daemon_threads = True
    _server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"agentbus：http://127.0.0.1:{port}", flush=True)
    _server.serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8790)
