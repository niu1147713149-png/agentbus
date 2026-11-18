"""~/.agentbus 下的用户配置：项目登记、办公室/休息室名单、岗位和偏好、账单。CLI、本地服务、MCP 共用这一份。"""
import copy
from contextlib import contextmanager
import datetime as dt
import json
import os
import shutil
import time
from pathlib import Path

from catalog import CATEGORIES, DEFAULT_CONFIG, KNOWN_AGENTS, category_of

HOME = Path(os.environ.get("AGENTBUS_HOME", Path.home() / ".agentbus"))  # agentbus 自己的数据只认这一处（各模块都从这里取）
PORT = int(os.environ.get("AGENTBUS_PORT") or 8790)
URL = f"http://127.0.0.1:{PORT}"
CONFIG = HOME / "config.json"
PROJECTS = HOME / "projects.json"
LEDGER = HOME / "ledger.jsonl"

# 从应用或 launchd 启动时 PATH 很短（/usr/bin:/bin…），各家命令行装在用户自己的目录里就找不到（显示“未招募”）。
# 通用办法：PATH 里没有用户目录时，读一次主人登录 shell 的 PATH（缓存一天，钩子这类频繁调用不会每次都起 shell）；
# 再补上常见的安装位置（官方安装脚本的默认目录）。
def _login_path():
    import subprocess
    cache = HOME / "shell_path.txt"
    try:
        if time.time() - cache.stat().st_mtime < 86400:
            return cache.read_text("utf-8").strip()
    except OSError:
        pass
    sh = os.environ.get("SHELL") or "/bin/zsh"
    try:
        out = subprocess.run([sh, "-ilc", "printf '__P__%s' \"$PATH\""], capture_output=True, text=True, timeout=4, stdin=subprocess.DEVNULL).stdout
        p = out.rsplit("__P__", 1)[1].strip() if "__P__" in out else ""
    except Exception:
        p = ""
    if p:
        try:
            HOME.mkdir(parents=True, exist_ok=True)
            cache.write_text(p, "utf-8")
        except OSError:
            pass
    return p


if os.name != "nt":
    _cur = os.environ.get("PATH", "").split(":")
    if not any(x.startswith(os.path.expanduser("~")) for x in _cur):
        for x in _login_path().split(":"):
            if x and x not in _cur:
                _cur.append(x)
    for p in ("~/.local/bin", "~/.opencode/bin", "~/.bun/bin", "~/.npm-global/bin", "~/.volta/bin", "~/.deno/bin", "~/.hermes/node/bin", "~/.cargo/bin",
              "/opt/homebrew/bin", "/usr/local/bin"):
        p = os.path.expanduser(p)
        if p not in _cur:
            _cur.append(p)
    os.environ["PATH"] = ":".join(x for x in _cur if x)


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(p, default):
    try:
        return json.loads(Path(p).read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return copy.deepcopy(default)


def save_json(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    import threading
    tmp = p.with_suffix(f"{p.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")  # 几个进程 / 线程同时写同一个文件不会互相踩
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    try:
        for attempt in range(7):
            try:
                os.replace(tmp, p)
                break
            except PermissionError as error:
                # Windows 的短时读取可能未共享删除权限；保留原文件并稍后重试原子替换。
                if os.name != "nt" or getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 6:
                    raise
                time.sleep(.02 * 2 ** attempt)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


_held = {}  # 本线程已经拿着的锁（同一线程里嵌套用不会自己卡死自己）


@contextmanager
def locked(name):
    """跨进程锁住一份数据（~/.agentbus/locks/<name>.lock）。读→改→写整段放在锁里，几个窗口 / 进程同时改不会互相覆盖。"""
    import threading
    key = (threading.get_ident(), name)
    if _held.get(key):
        _held[key] += 1
        try:
            yield
        finally:
            _held[key] -= 1
        return
    import pf
    f = HOME / "locks" / f"{name}.lock"
    f.parent.mkdir(parents=True, exist_ok=True)
    fh = open(f, "a+")
    try:
        pf.lock(fh, wait=20)
        _held[key] = 1
        try:
            yield
        finally:
            _held.pop(key, None)
            pf.unlock(fh)
    finally:
        fh.close()


def update_json(p, fn, default=None):
    """在锁里重新读一遍、交给 fn 改（原地改或返回新值）、再写回。返回写进去的数据。"""
    with locked(Path(p).stem):
        d = load_json(p, {} if default is None else default)
        r = fn(d)
        d = d if r is None else r
        save_json(p, d)
        return d


def load_config(project_name=None):
    """读配置。cfg["roster"] / cfg["jobs"] 是**当前项目**的阵容和职能：项目有自己的就用自己的，没有（或项目为“无”）就用通用的。"""
    cfg = load_json(CONFIG, DEFAULT_CONFIG)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, copy.deepcopy(v))
    name = current_project() if project_name is None else project_name
    own = (cfg.get("lineups") or {}).get(name)
    cfg["_lineup_of"] = name if own else ""
    if own:
        cfg["_global"] = {"roster": cfg["roster"], "jobs": cfg["jobs"]}
        cfg["roster"], cfg["jobs"] = own["roster"], own["jobs"]
    # 新加进目录的 agent 默认进休息室
    placed = set(cfg["roster"]["office"]) | set(cfg["roster"]["lounge"])
    for aid in agents_dict(cfg):
        if aid not in placed:
            cfg["roster"]["lounge"].append(aid)
    return cfg


def save_config(cfg):
    cfg = dict(cfg)
    name = cfg.pop("_lineup_of", "")
    g = cfg.pop("_global", None)
    if name and g:
        # 只更新阵容和职能，项目里别的设置（经验包装配等）原样保留
        lu = dict((cfg.get("lineups") or {}).get(name) or {})
        lu.update(roster=cfg["roster"], jobs=cfg["jobs"])
        cfg.setdefault("lineups", {})[name] = lu
        cfg["roster"], cfg["jobs"] = g["roster"], g["jobs"]
    with locked("config"):
        save_json(CONFIG, cfg)


def agents_dict(cfg=None):
    cfg = cfg if cfg is not None else load_json(CONFIG, DEFAULT_CONFIG)
    out = {k: dict(v, id=k) for k, v in KNOWN_AGENTS.items()}
    for a in out.values():  # 网页版 + 命令行二合一（Gemini）：装了 CLI 就按命令行算
        if a.get("web_fallback") and cli_ready(a):
            a.update(billing="none", desc=a["name"] + " CLI")
    for k, v in (cfg.get("custom_agents") or {}).items():
        out[k] = dict({"run": "generic", "acts": None, "billing": "none", "sprite": None, "color": "#888888"}, **v, id=k)
    # API 队员：钱包里的账户 + 一个模型，用 agentbus 内置的轻量 agent 干活（不用装命令行）
    for k, v in (cfg.get("api_agents") or {}).items():
        out[k] = {"id": k, "name": v.get("name") or k, "run": "api", "account": v["account"], "model": v["model"], "abbr": (v.get("name") or "A")[:1],
                  "vendor": v.get("vendor") or "API 队员", "desc": f"API 队员 · {v['model']}", "caps": ["files", "cn", "long", "cheap"],
                  "billing": "deepseek" if "deepseek.com" in (v.get("base") or "") else "api", "sprite": None, "acts": None,
                  "color": v.get("color") or "#7a8da8", "api": True}
    # 分身：同一个外壳，换个大脑，单独占工位、单独配职能、单独记战绩
    for cid, c in (cfg.get("clones") or {}).items():
        base = out.get(c.get("base"))
        if base:
            out[cid] = dict(base, id=cid, name=c.get("name") or base["name"] + "·分身", clone_of=c["base"], acts=None, hidden=False,
                            desc=f"{base['name']} 的分身", abbr=(base.get("abbr") or base["name"])[:1] + cid.rsplit("-", 1)[-1])
    # 计费跟着大脑走：换成别家模型后不再按原来的额度记账
    for aid, a in out.items():
        m = ((cfg.get("brains") or {}).get(aid) or {}).get("model") or ""
        if a.get("billing") == "codex" and "/" in m:
            a["billing"] = "none"
        if a.get("run") == "opencode" and m and not m.startswith("deepseek"):
            a["billing"] = "none"
    return out


def cli_ready(agent):
    return bool(agent.get("bin") and shutil.which(agent["bin"]))


def installed(agent):
    if agent.get("run") == "api":
        import wallet
        return agent.get("account") in wallet.accounts()
    if agent["run"] == "doubao":
        return Path(doubao_remote()).exists()
    if agent.get("billing") == "manual" or agent.get("web_fallback"):
        return True
    return bool(agent.get("bin") and shutil.which(agent["bin"]))


def doubao_remote():
    return load_json(CONFIG, {}).get("doubao_remote") or str(HOME / "doubao-remote.mjs")  # 豆包遥控脚本（CDP），config.json 里可以改位置


# ---------- 项目 ----------

def projects():
    return load_json(PROJECTS, {})


GENERAL = "通用"   # 项目选“无”时用的通用工作区


def current_project():
    """当前项目（悬浮窗、主界面、命令行共用）。空字符串 = 无。"""
    return os.environ.get("AGENTBUS_PROJECT") or projects().get("_default") or ""


def project_of_dir(d=None):
    """派活时在哪个目录：落在哪个已登记项目的目录里就算哪个项目（取最深的那个）；
    不在任何项目里、又不是家目录 → 通用（以免把别的事记进当前项目的账）；家目录或根目录 → None（用当前默认项目）。"""
    try:
        d = Path(d or os.getcwd()).resolve()
    except OSError:
        return None
    best, depth = None, -1
    for name, p in projects().items():
        if name.startswith("_") or not isinstance(p, dict) or not p.get("root"):
            continue
        r = Path(p["root"]).resolve()
        if r != Path.home() and (d == r or r in d.parents) and len(r.parts) > depth:
            best, depth = name, len(r.parts)
    if best:
        return best
    if d in (Path.home(), Path("/")) or HOME in (d, *d.parents) or d.parent in (Path("/"), Path("/Volumes")):
        return None
    # 没登记过的文件夹：自动当成一个项目（名字用文件夹名），不用在 agentbus 里手动建
    name, ps = d.name, projects()
    while name in ps and Path(ps[name].get("root", "")).resolve() != d:
        name += "·"
    if name not in ps:
        try:
            create_project(name, str(d), copy_lineup=False)
        except (ValueError, OSError):
            return GENERAL
    return name


def project(name=None):
    """返回 (项目名, 项目目录, 总线目录, 当前批次)。项目为“无”时用通用工作区，目录是当前目录。"""
    ps = projects()
    name = name if name is not None else current_project()
    if not name or name == GENERAL:
        bus = HOME / "general"
        (bus / "runs").mkdir(parents=True, exist_ok=True)
        return GENERAL, Path.cwd(), bus, (ps.get("_general") or {}).get("batch") or ""
    if name not in ps or name.startswith("_"):
        raise SystemExit(f"没有这个项目：{name}。已登记：{[k for k in ps if not k.startswith('_')]}")
    p = ps[name]
    bus = Path(p["bus"])
    (bus / "runs").mkdir(parents=True, exist_ok=True)
    return name, Path(p["root"]), bus, p.get("batch") or ""


def create_project(name, root=None, brief="", copy_lineup=True):
    """新建项目：总线目录放在 ~/.agentbus/projects/<名字>/，可以带一份项目说明；阵容默认复制当前的。"""
    name = name.strip()
    ps = projects()
    if not name or name.startswith("_") or name == GENERAL or "/" in name:
        raise ValueError("项目名不能为空，也不能叫“通用”或带斜杠")
    if name in ps:
        raise ValueError(f"已经有项目「{name}」了")
    bus = HOME / "projects" / name
    (bus / "runs").mkdir(parents=True, exist_ok=True)
    (bus / "roles").mkdir(exist_ok=True)
    (bus / "brief.md").write_text(brief.strip() + "\n" if brief.strip() else f"## 项目说明：{name}\n\n（写给所有 agent 看的共同规矩：目标、文件夹、红线。）\n", "utf-8")
    ps[name] = {"root": str(Path(os.path.expanduser(root or "~")).resolve()), "bus": str(bus)}
    update_json(PROJECTS, lambda d: d.__setitem__(name, ps[name]))
    base = load_config()  # 当前项目的阵容
    lu = {"roster": copy.deepcopy(base["roster"]), "jobs": copy.deepcopy(base["jobs"])} if copy_lineup else \
        {"roster": {"office": [], "lounge": list(agents_dict())}, "jobs": {}}
    update_json(CONFIG, lambda c: c.setdefault("lineups", {}).__setitem__(name, lu), DEFAULT_CONFIG)
    return ps[name]


def remove_project(name):
    """从列表里移除项目（不删任何文件，总线目录和记录都留着；以后同名新建不会覆盖）。"""
    ps = projects()
    if name not in ps or name.startswith("_"):
        raise ValueError(f"没有这个项目：{name}")
    ps.pop(name)
    if ps.get("_default") == name:
        ps["_default"] = ""
    save_json(PROJECTS, ps)
    cfg = load_json(CONFIG, DEFAULT_CONFIG)
    (cfg.get("lineups") or {}).pop(name, None)
    save_json(CONFIG, cfg)


def set_current(name):
    ps = projects()
    if name and name not in ps:
        raise ValueError(f"没有这个项目：{name}")
    ps["_default"] = name or ""
    save_json(PROJECTS, ps)


def set_batch(name, batch):
    ps = projects()
    key = "_general" if not name or name == GENERAL else name
    ps.setdefault(key, {})["batch"] = batch
    save_json(PROJECTS, ps)


# ---------- 岗位 ----------

def level_for(cfg, agent_id, tag):
    """某 agent 对某词条的偏好：单独配的词条优先，其次是大类。"""
    jobs = cfg["jobs"].get(agent_id) or {}
    if "tag:" + tag in jobs:
        return jobs["tag:" + tag], "词条"
    cid = category_of(tag)
    if cid and "cat:" + cid in jobs:
        return jobs["cat:" + cid], "大类"
    return None, None


def who(cfg, tag):
    """谁能干这个词条的活：先看办公室战备（里面再按首选 / 可以排），办公室没人能干才轮到休息室；标了“不用”的去掉。"""
    rank = {"prefer": 0, "ok": 1}
    office = cfg["roster"]["office"]
    out = []
    for aid, a in agents_dict(cfg).items():
        lv, via = level_for(cfg, aid, tag)
        if lv in rank:
            out.append({"id": aid, "name": a["name"], "level": lv, "via": via, "in_office": aid in office})
    out.sort(key=lambda r: (not r["in_office"], rank[r["level"]]))
    return out


def resolve_tag(text):
    """“封面”“视觉设计”“cat:visual” 都认。返回 (key, 显示名)。"""
    text = text.strip()
    if text.startswith(("tag:", "cat:")):
        return text, text[4:]
    for cid, cname, tags in CATEGORIES:
        if text in (cid, cname):
            return "cat:" + cid, cname
        if text in tags:
            return "tag:" + text, text
    return "tag:" + text, text


# ---------- 账单 ----------

def ledger_add(rec):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def ledger_read():
    if not LEDGER.exists():
        return []
    out = []
    for l in LEDGER.read_text("utf-8").splitlines():
        try:
            out.append(json.loads(l))
        except json.JSONDecodeError:
            pass
    return out


def _settled():
    """派活记录 + 之后补查的余额（type=settle）合在一起。"""
    recs, settles, fixes = [], {}, {}
    for r in ledger_read():
        if r.get("type") == "settle":
            settles[r["id"]] = r
        elif r.get("type") == "fix":  # 事后更正（记错项目、算错钱）：账本只追加，不改旧行
            fixes.setdefault(r["id"], {}).update(r.get("set") or {})
        else:
            recs.append(r)
    for r in recs:
        s = settles.get(r["id"])
        if s:
            r["after"], r["cost"], r["settled"] = s["after"], s["cost"], True
        r.update(fixes.get(r["id"], {}))
    return recs


def _merge_overlaps(recs):
    """同一种额度上时间重叠的几次派活，余额差会互相混进去：合成一组，组的总消耗 = 第一单开始前 − 最后一单结束后，
    再按 token 比例分给组里每一单（没有 token 就平分）。"""
    # (额度字段, 账单字段, 是否“已用百分比”：百分比越用越大，余额越用越小)
    for billing, fields in (("deepseek", (("total", "cny", False),)),
                            ("codex", (("five_hour", "five_hour", True), ("weekly", "weekly", True), ("credits", "credits", False)))):
        rs = sorted([r for r in recs if r.get("billing") == billing and (r.get("before") or {}).get("ok")
                     and (r.get("after") or {}).get("ok")], key=lambda r: r["start"])
        groups, cur, cur_end = [], [], 0
        for r in rs:
            if cur and r["start"] < cur_end:
                cur.append(r)
            else:
                cur and groups.append(cur)
                cur = [r]
            cur_end = max(cur_end, r["end"]) if len(cur) > 1 else r["end"]
        cur and groups.append(cur)
        for g in groups:
            if len(g) < 2:
                continue
            last = max(g, key=lambda r: r["end"])
            weights = [((r.get("tokens") or {}).get("total") or 0) for r in g]
            if not sum(weights):
                weights = [1] * len(g)
            for r, w in zip(g, weights):
                c = dict(r.get("cost") or {}, ok=True, merged=len(g))
                for qf, cf, used in fields:
                    try:
                        diff = float(g[0]["before"][qf]) - float(last["after"][qf])
                        diff = -diff if used else diff
                        c[cf] = round(diff * w / sum(weights), 4)
                    except (KeyError, TypeError, ValueError):
                        pass
                r["cost"] = c
    return recs


def bill(project_name=None, batch=None):
    """按 agent 汇总真实消耗。"""
    recs = _merge_overlaps(_settled())
    recs = [r for r in recs if (not project_name or r.get("project") == project_name)
            and (batch is None or r.get("batch") == batch)]
    per = {}
    for r in recs:
        a = per.setdefault(r["agent"], {"agent": r["agent"], "runs": 0, "seconds": 0, "cny": 0.0,
                                        "five_hour": 0.0, "weekly": 0.0, "credits": 0.0, "tokens": 0, "unknown": 0})
        a["runs"] += 1
        a["seconds"] += r.get("seconds") or 0
        a["tokens"] += (r.get("tokens") or {}).get("total") or 0
        d = r.get("cost") or {}
        if not d.get("ok"):
            a["unknown"] += 1
        for k in ("cny", "five_hour", "weekly", "credits"):
            if isinstance(d.get(k), (int, float)):
                a[k] = round(a[k] + d[k], 4)
    return {"records": recs[-200:], "per_agent": list(per.values()),
            "total_cny": round(sum(a["cny"] for a in per.values()), 4)}


# ---------- 主位自适应：谁在用 agentbus 派活 / 接入，谁就是主位，不用先去界面里切 ----------
LEAD_FILE = HOME / "lead.json"
ROOMS_FILE = HOME / "rooms.json"  # 单间：一个主位窗口（协同会话）一间
LEAD_FRESH = 12 * 3600  # 这么久没动静的“自动主位”就不算了，退回阵容里指定的

# MCP 握手时客户端报的名字 → agentbus 里的 agent id
CLIENTS = {"claude": "claude", "codex": "codex", "opencode": "opencode", "zcode": "zcode", "gemini": "gemini",
           "codebuddy": "codebuddy", "workbuddy": "codebuddy", "qwen": "qwen", "kimi": "kimi", "cursor": "cursor", "dsh": "dsh"}


def agent_of_client(name):
    n = (name or "").lower()
    return next((aid for key, aid in CLIENTS.items() if key in n), None)


def whoami():
    """当前这个进程是谁在用 agentbus：队员身份（派活时 agentbus 设的）> 从外壳的环境变量认 > None。"""
    env = os.environ
    if env.get("AGENTBUS_SELF"):
        return env["AGENTBUS_SELF"]
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if any(k.startswith("CODEX_") for k in env):
        return "codex"
    if env.get("OPENCODE") or any(k.startswith("OPENCODE_") for k in env):
        return "opencode"
    if any(k.startswith("ZCODE") for k in env):
        return "zcode"
    if any(k.startswith(("GEMINI_CLI", "GEMINI_CLI_")) for k in env):
        return "gemini"
    if any(k.startswith("CODEBUDDY") for k in env):
        return "codebuddy"
    return None


def sealed(agent, cfg=None):
    """Claude 封印（默认开）：Claude 和它的分身不当队员——不接派活、不进聊天室、不查额度，任何能把它当队员唤醒的路都关着。
    当主位不受影响。主人在队员页确认风险后可以解封（settings.claude_seal = False）。agent 可以是 id 或 agent 字典。"""
    cfg = cfg if cfg is not None else load_json(CONFIG, DEFAULT_CONFIG)
    a = agent if isinstance(agent, dict) else agents_dict(cfg).get(agent) or {}
    if a.get("run") != "claude":
        return False
    return (cfg.get("settings") or {}).get("claude_seal", True) is not False


def is_working_as_member(agent):
    """这位现在手上有派来的活（它是被派活的队员）：它干活时连上 agentbus 不算“它来统筹了”。"""
    for f in (HOME / "running").glob("*.json"):
        if load_json(f, {}).get("agent") == agent:
            return True
    return False


def touch_lead(agent, via="", conv=""):
    """有 agent 在用 agentbus 统筹（MCP 接入、握手、派活）：记成当前主位。队员（派活层数 ≥ 1）不算。"""
    if not agent or int(os.environ.get("AGENTBUS_DEPTH", "0")) > 0 or is_working_as_member(agent):
        return
    if conv:  # 单间登记：哪个窗口、谁在统筹、在哪个文件夹（悬浮窗给单间起名用）
        r = load_json(ROOMS_FILE, {}).get(conv[:12]) or {}
        if r.get("lead") != agent or time.time() - r.get("t", 0) > 60:
            def put(rooms):
                rooms[conv[:12]] = {"lead": agent, "via": via, "cwd": os.getcwd(), "t": time.time()}
                cutoff = time.time() - 7 * 86400
                return {k: v for k, v in rooms.items() if v.get("t", 0) > cutoff}
            update_json(ROOMS_FILE, put)
    cur = load_json(LEAD_FILE, {})
    if cur.get("agent") == agent and time.time() - cur.get("t", 0) < 60:
        return  # 一分钟内重复的不写
    save_json(LEAD_FILE, {"agent": agent, "via": via, "conv": conv[:12], "t": time.time()})


def active_lead(cfg=None):
    """现在的主位：开着“自动主位”（默认开）时，取最近在用 agentbus 统筹的那位；否则 / 没有就用阵容里指定的。"""
    cfg = cfg or load_config()
    auto = (cfg.get("settings") or {}).get("lead_auto", True)
    cur = load_json(LEAD_FILE, {})
    if auto and cur.get("agent") in agents_dict(cfg) and time.time() - cur.get("t", 0) < LEAD_FRESH:
        return cur["agent"]
    return cfg["roster"].get("lead")


# ---------- 经验包：装到办公室 / 窗口的项目文件夹 ----------
# 经验包本身放在某个总线目录的 playbook/ 里（项目自带的，或导入到 ~/.agentbus/packs/<名字>/ 的）。
# 装到办公室：这位主位派活时都带上；装到文件夹：在这个文件夹（及子文件夹）里派活都带上。记在 config.json 的 pack_mounts。
PACKS = HOME / "packs"


def pack_library():
    out = []
    seen = set()
    buses = [(n, Path(p["bus"])) for n, p in projects().items() if not n.startswith("_") and isinstance(p, dict) and p.get("bus")]
    buses += [(d.name, d) for d in sorted(PACKS.glob("*")) if d.is_dir()] if PACKS.exists() else []
    for name, bus in buses:
        m = load_json(bus / "playbook" / "manifest.json", None)
        if not m or str(bus) in seen:
            continue
        seen.add(str(bus))
        out.append({"id": str(bus), "kind": "pack", "name": m.get("name") or name, "version": m.get("version"), "project": name, "desc": m.get("description", "")})
    return out


SKILL_DIRS = [("Claude", Path.home() / ".claude" / "skills"), ("GPT", Path.home() / ".codex" / "skills"), ("通用", Path.home() / ".agents" / "skills")]


def _skill_meta(f):
    """SKILL.md 开头的 name / description。"""
    meta = {}
    try:
        text = f.read_text("utf-8", "ignore")
    except OSError:
        return meta
    if text.startswith("---"):
        for l in text.split("---", 2)[1].splitlines():
            if ":" in l:
                k, v = l.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


def skill_library():
    """本机装着的 skill（Claude / GPT / 通用目录），和经验包一样可以装到办公室或文件夹。"""
    out = []
    for owner, d in SKILL_DIRS:
        for f in sorted(d.glob("*/SKILL.md")) if d.exists() else []:
            m = _skill_meta(f)
            out.append({"id": str(f.parent), "kind": "skill", "owner": owner, "name": m.get("name") or f.parent.name, "desc": (m.get("description") or "")[:240]})
    return out


def pack_mounts(cfg=None):
    return list(((cfg or load_config()).get("pack_mounts")) or [])


def mounted_packs(cfg, office=None, folder=None):
    """派活时该带哪些经验包：装在这位主位办公室上的 + 装在这个文件夹（或它的上级）上的。返回总线目录列表。"""
    out = []
    f = Path(folder).resolve() if folder else None
    for m in pack_mounts(cfg):
        hit = (office and m.get("office") == office) or (f and m.get("dir") and (f == Path(m["dir"]) or Path(m["dir"]) in f.parents))
        if hit and m.get("pack") not in out and (Path(m["pack"]) / "playbook" / "manifest.json").exists():
            out.append(m["pack"])
    return out


def mounted_skills(cfg, office=None, folder=None):
    """装在这位主位办公室 / 这个文件夹上的 skill：[(名字, 说明, 目录)]。"""
    out, seen = [], set()
    f = Path(folder).resolve() if folder else None
    for m in pack_mounts(cfg):
        p = m.get("pack") or ""
        hit = (office and m.get("office") == office) or (f and m.get("dir") and (f == Path(m["dir"]) or Path(m["dir"]) in f.parents))
        if hit and p not in seen and (Path(p) / "SKILL.md").exists():
            seen.add(p)
            meta = _skill_meta(Path(p) / "SKILL.md")
            out.append((meta.get("name") or Path(p).name, meta.get("description") or "", p))
    return out
