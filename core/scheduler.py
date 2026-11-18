"""调度器：派出去的活在真正开工前先排队拿“资源”。

  - 屏幕锁：录屏、操控电脑这类活要占屏幕和鼠标，同一时间只放一个，其余排队。
  - 钱包并发：同一个额度 / 账户同时最多几个活（GPT、DeepSeek、Claude 各一档；API 账户按钱包里设的上限）。
用文件锁（~/.agentbus/locks/），进程挂了锁自动释放；排队时把原因写进 running 记录，界面显示“排队中”。
"""
import json
import os
import sys
import time
from pathlib import Path

import pf
from config import HOME, load_config, load_json, save_json

LOCKS = HOME / "locks"
SCREEN_TAGS = {"录屏", "操控桌面应用", "操控浏览器", "填写表单", "手机 / 模拟器操作"}
DEFAULT_LIMITS = {"codex": 3, "deepseek": 6, "claude": 3, "antigravity": 3, "free": 1, "none": 4}
BILLING_NAME = {"codex": "GPT 额度", "deepseek": "DeepSeek 账户", "claude": "Claude 额度", "antigravity": "反重力额度", "free": "豆包", "none": "这个工具"}


def settings(cfg=None):
    s = (cfg or load_config()).get("scheduler") or {}
    return {"screen_lock": s.get("screen_lock", True), "limits": dict(DEFAULT_LIMITS, **(s.get("limits") or {}))}


def needs_screen(agent, tag, flag=False):
    if flag:
        return True
    return bool(tag) and tag in SCREEN_TAGS and "computer" in (agent.get("caps") or [])


def wallet_slot(agent, st):
    """这个活占哪一档并发：API 队员按钱包账户，其余按计费方式。"""
    if agent.get("run") == "api":
        import wallet
        acc = wallet.accounts().get(agent.get("account")) or {}
        return "api-" + agent.get("account", ""), int(acc.get("limit") or 4), acc.get("name") or "API 账户"
    b = agent.get("billing") or "none"
    return b, int(st["limits"].get(b, 4)), BILLING_NAME.get(b, b)


def _try(name, n, who):
    """在 name 的 n 个座位里抢一个，抢到返回打开的文件（拿着就是占着）。"""
    LOCKS.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        f = open(LOCKS / f"{name}.{i}.lock", "a+")
        if not pf.try_lock(f):
            f.close()
            continue
        if not pf.IS_WIN:  # Windows 上锁住的文件别处读不了，占用者信息另存
            f.seek(0)
            f.truncate()
            f.write(json.dumps(who, ensure_ascii=False))
            f.flush()
        (LOCKS / f"{name}.{i}.who").write_text(json.dumps(who, ensure_ascii=False), "utf-8")
        return f
    return None


def holders(name, n):
    out = []
    for i in range(n):
        p = LOCKS / f"{name}.{i}.who"
        try:
            out.append(json.loads(p.read_text("utf-8") or "{}"))
        except Exception:
            pass
    return [h for h in out if h]


class acquire:
    """with acquire(...)：拿到屏幕锁（需要时）和钱包座位才往下走。"""

    def __init__(self, agent_id, agent, tag, screen_flag, running_file, task=""):
        self.aid, self.agent, self.tag, self.running = agent_id, agent, tag, running_file
        st = settings()
        self.screen = st["screen_lock"] and needs_screen(agent, tag, screen_flag)
        self.slot, self.limit, self.slot_name = wallet_slot(agent, st)
        self.who = {"agent": agent_id, "name": agent.get("name"), "task": task[:60], "pid": os.getpid(), "since": time.time()}
        self.held, self.waited = [], 0.0

    def _mark(self, why):
        if self.running and self.running.exists():
            r = load_json(self.running, {})
            if why:
                r["queued"] = why
            else:
                r.pop("queued", None)
            save_json(self.running, r)

    def __enter__(self):
        t0, said = time.time(), None
        need = ([("screen", 1, "屏幕")] if self.screen else []) + [(self.slot, self.limit, self.slot_name)]
        while True:
            got, why = [], None
            for name, n, label in need:
                f = _try(name, n, self.who)
                if not f:
                    hs = holders(name, n)
                    who = "、".join(h.get("name") or h.get("agent", "") for h in hs[:3])
                    why = f"等{label}：{who} 在用" if label == "屏幕" else f"等{label}空位（同时最多 {n} 个{('：' + who) if who else ''}）"
                    break
                got.append(f)
            if not why:
                self.held = got
                break
            for f in got:
                f.close()
            if why != said:
                print(f"[agentbus] 排队中，{why}", file=sys.stderr, flush=True)
                self._mark(why)
                said = why
            time.sleep(3)
        self.waited = time.time() - t0
        if said:
            self._mark(None)
            print(f"[agentbus] 排到了，等了 {round(self.waited)} 秒", file=sys.stderr, flush=True)
        return self

    def __exit__(self, *a):
        for f in self.held:
            try:
                w = Path(f.name).with_suffix(".who")
                w.write_text("")
                pf.unlock(f)
                f.close()
            except Exception:
                pass
        self.held = []


def status():
    """界面用：现在谁占着屏幕、各档并发用了几个。"""
    st = settings()
    out = {"screen": holders("screen", 1) if LOCKS.exists() else [], "slots": {}}
    for b, n in st["limits"].items():
        out["slots"][b] = {"limit": n, "used": len([h for h in holders(b, n) if _alive(h.get("pid"))])}
    out["screen"] = [h for h in out["screen"] if _alive(h.get("pid"))]
    return out


def _alive(pid):
    return pf.pid_alive(pid)
