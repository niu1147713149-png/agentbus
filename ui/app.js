// agentbus 主界面。数据来自本地服务 /api/events（SSE）；应用窗口和 Claude 右侧浏览器是同一个页面、同一份数据。
(() => {
  const { U, icon, robot, esc, avatar, brandMark, STATE, LEVEL, BILLING, money, mins, clock, costText, resetText } = AB;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  const PAGES = [["base", "基地", "base"], ["chat", "聊天室", "chat"], ["office", "办公室", "office"], ["team", "队员", "team"], ["playbook", "经验", "skills"], ["quests", "任务", "quests"], ["report", "战报", "report"], ["settings", "设置", "settings"]];
  const CAT_IC = { coord: "🧭", writing: "✍️", editing: "📝", lang: "🌐", visual: "🎨", av: "🎬", computer: "🖱️", research: "🔎",
    assets: "🧺", dev: "💻", office: "📊", ops: "📣", edu: "🎓", ideation: "💡" };
  const SEATS = [[20, 62], [50, 62], [80, 62], [20, 97.5], [50, 97.5], [80, 97.5]]; // 办公室 6 个工位（x%, 底边 y%）
  const MAX_SEATS = SEATS.length;

  let S = null, CAT = null, es = null, dragging = null;
  let route = { page: "base", id: null }, painted = {};
  const prevState = new Map();
  const ui = { q: "", qAgent: "", openRun: null, batch: null, openCat: null };
  let questsCache = { t: 0, key: "", list: [] };
  const catName = Object.create(null);

  // ---------- 数据 ----------
  // 页面藏起来 20 秒（比如右侧浏览器收起）就换到空页面 sleep.html，省掉立绘和动画图层的内存；再显示时它会跳回来。
  // 应用里的主窗口关掉就直接销毁了（见 app/src-tauri/src/main.rs），用不到这个
  let sleepT = null;
  const sleepSoon = () => { clearTimeout(sleepT); sleepT = setTimeout(() => { es && es.close(); location.replace("/sleep.html" + (location.hash || "#base")); }, 20000); };
  document.addEventListener("visibilitychange", () => (document.hidden ? sleepSoon() : clearTimeout(sleepT)));
  if (document.hidden) sleepSoon(); // 应用启动 / 重新加载时主窗口本来就藏着：不会再收到“藏起来”的事件
  function connect() {
    es && es.close();
    es = new EventSource("/api/events");
    es.onmessage = e => { $("#live").classList.remove("down"); onState(JSON.parse(e.data)); };
    es.onerror = () => $("#live").classList.add("down");
  }
  const api = async (path, body) => {
    const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) toast(j.error || "没成功，再试一次", "close");
    return r.ok ? j : null;
  };
  async function quests(force) {
    const key = S ? S.project : "";
    if (force || key !== questsCache.key || Date.now() - questsCache.t > 8000) questsCache = { t: Date.now(), key, list: (await api("/api/quests")) || [] };
    return questsCache.list;
  }
  const A = id => (S.agents.find(a => a.id === id) || { id, name: (S.names || {})[id] || id, color: "#999", jobs: {}, acts: [], state: "off" });
  const office = () => S.agents.filter(a => a.place === "office");
  // 基地显示哪间办公室：主人点选的；没选（自动）就是正在干活的那间，没人干活就是主位那间
  ui.baseOffice = (() => { try { return localStorage.getItem("baseOffice") || null; } catch { return null; } })();
  const baseOffice = () => {
    const L = (S && S.offices) || [];
    return (ui.baseOffice && L.find(o => o.id === ui.baseOffice)) || L.find(o => o.active) || L.find(o => o.id === S.lead) || L[0] || null;
  };
  const seatsOf = o => o ? o.lineup.office.map(A) : office();                     // 工位上的：这间办公室的战备
  const loungeOf = o => o ? S.agents.filter(a => a.installed && a.billing !== "manual" && a.id !== o.agent && a.id !== "claude" && !o.lineup.office.includes(a.id)) : lounge();
  const lounge = () => S.agents.filter(a => a.place === "lounge" && a.installed);
  const recruits = () => S.agents.filter(a => !a.installed);

  function toast(text, ic = "check") {
    $$(".toast").forEach(t => t.remove());
    const t = document.createElement("div"); t.className = "toast"; t.innerHTML = icon(ic) + esc(text);
    document.body.appendChild(t); setTimeout(() => t.remove(), 2400);
  }  // 基地场景里谁戴「主位」的帽子：看的是哪间办公室，就是那间的主人（不是全局最近在统筹的那位）
  const viewLead = () => { const bo = baseOffice(); return bo ? bo.agent : S.lead; };
  const leadSeated = () => { const bo = baseOffice(); return !!(bo && A(bo.agent).installed); }; // 主位坐第一个工位
  const lineupMax = () => MAX_SEATS - (leadSeated() ? 1 : 0); // 战备能坐几个人


  function onState(s) {
    if (ui.lock && Date.now() < ui.lock.until) { const x = s.agents.find(a => a.id === ui.lock.id); if (x) x.jobs = { ...ui.lock.jobs }; }
    const first = !S, projChanged = S && S.project !== s.project;
    const channel = ui.chat.channel;
    const endedHere = S?.running.some(r => r.channel === channel && r.job && !s.running.some(n => n.job === r.job));
    S = s;
    applyTheme(s.settings.theme);
    I18N.sync(s.settings.lang); // 语言和服务端存的不一样就重新加载
    // 第一次打开：先自动装配（看本机有哪些 agent、各自适合干什么），再接新手引导
    if (first && !s.settings.setup_done) setTimeout(() => setupWizard(), 600);
    else if (first && !s.settings.tour_done) setTimeout(() => { if (tourI < 0) tourStart(); }, 900);
    // 左下角：现在是谁在当主位（项目不用手动管了，跟着窗口所在的文件夹走），点一下进它的办公室
    const bo = baseOffice(), ld = bo ? A(bo.agent) : s.lead ? A(s.lead) : null; // 跟着基地当前显示的办公室走
    $("#proj").innerHTML = ld ? `${avatar(ld, 30)}<b>${esc(ld.name)}</b><small>主位 · 办公室</small>` : `<span class="proj-ic">${icon("office")}</span><b>办公室</b><small>还没人接入</small>`;
    $("#proj").title = "进主位的办公室";
    const qn = $(`.nav[data-page="quests"]`), b = qn.querySelector(".badge");
    if (s.running.length) (b || qn.appendChild(Object.assign(document.createElement("span"), { className: "badge" }))).textContent = s.running.length; else b && b.remove();
    // 回复可能先入频道，运行记录稍后才结束。退出时再取一次最终状态，不把消失当作成功。
    if (s.chat_rev !== ui.chatRev || endedHere) {
      ui.chatRev = s.chat_rev;
      if (route.page === "chat" || route.page === "base") loadChat();
      else ui.chat.data = null;
    }
    if (first || projChanged) go(); else if (!dragging) paint();
  }

  // ---------- 路由 ----------
  function go() {
    const [p, id] = (location.hash || "#base").slice(1).split("/");
    route = { page: PAGES.some(x => x[0] === p) || p === "hero" ? p : "base", id: id ? decodeURIComponent(id) : null };
    $$(".nav").forEach(n => n.classList.toggle("on", n.dataset.page === (route.page === "hero" ? "team" : route.page)));
    painted = {};
    $("#page").innerHTML = "";
    $("#main").scrollTop = 0;
    if (S) paint();
  }
  window.addEventListener("hashchange", () => { ui.openCat = null; closePop(); go(); });
  for (const [id, label, ic] of PAGES) { const n = $(`.nav[data-page="${id}"]`); n.innerHTML = icon(ic) + `<span>${label}</span>`; }

  function paint() { Promise.resolve(({ base: pBase, office: pOffice, chat: pChat, team: pTeam, hero: pHero, playbook: pPlaybook, quests: pQuests, report: pReport, settings: pSettings })[route.page]()).then(() => tick()); }
  function part(key, el, html) { if (el && painted[key] !== html) { painted[key] = html; el.innerHTML = html; return true; } return false; }
  function shell(html) { if (!painted.__shell) { $("#page").innerHTML = html; painted.__shell = true; } }

  // ---------- 小组件 ----------
  const ringAv = (a, size) => `<span class="ring ${a.state === "working" ? "working" : ""}">${avatar(a, size)}<i class="sdot" style="background:${STATE[a.state].c}"></i></span>`;
  const statePill = a => `<span class="pill"><i class="dot" style="background:${STATE[a.state].c}"></i>${STATE[a.state].t}</span>`;
  const quotaPill = a => a.quota_text ? `<span class="pill ${a.billing === "deepseek" ? "gold" : a.billing === "codex" ? "hp" : ""}">${a.billing === "deepseek" ? icon("coin") : a.billing === "codex" ? icon("bolt") : ""}${esc(a.quota_text)}</span>` : "";
  const label = k => k.startsWith("cat:") ? (catName[k.slice(4)] || k.slice(4)) : k.slice(4);
  function skillPills(a, n = 3) {
    return Object.entries(a.jobs || {}).filter(([, v]) => v !== "avoid").sort((x, y) => (x[1] === "prefer" ? -1 : 1) - (y[1] === "prefer" ? -1 : 1)).slice(0, n)
      .map(([k, v]) => `<span class="pill ${v === "prefer" ? "accent" : ""}">${v === "prefer" ? icon("star") : ""}${esc(label(k))}${k.startsWith("cat:") ? "·整类" : ""}</span>`).join("");
  }
  const hpClass = left => left <= 15 ? "low" : left <= 40 ? "mid" : "";
  const pose = st => ({ working: 0, manual: 0, break: 1, queued: 1 }[st] ?? 2);

  // 一个人坐在工位/沙发上：角色层在后（会动），桌子/沙发层在前（不动）
  // 休息室里每人隔一阵换个沙发动作（同一时段同一个人动作不变，重绘不会乱跳）
  const sofaPose = a => (Math.floor(Date.now() / 14000) + [...a.id].reduce((n, c) => n + c.charCodeAt(0), 0)) % 5;
  function figureHTML(a, where) {
    const p = where === "sofa" ? (a.state === "break" ? 1 : 2) : pose(a.state);
    if (where === "sofa" && a.frames && a.frames.sofa) { // 沙发 5 个动作（扩展素材），休息室里轮着换
      const i = sofaPose(a);
      return `<div class="lz s5" data-sofa="${i}" style="background-image:url(${esc(a.frames.sofa)});background-size:500% 100%;background-position:${i * 25}% 0"></div>`;
    }
    if (where === "sofa" && a.lounge_art) { // 休息室专属躺姿：玩手机 / 睡觉
      const f = a.state === "break" || a.state === "manual" ? 0 : 1;
      return `<div class="lz f${f}" style="background-image:url(${esc(a.lounge_art)})">${a.api && a.generic ? brandMark(a, "bd-lz") : ""}${f ? `<span class="lz-z"><i>z</i><i>z</i><i>Z</i></span>` : `<span class="lz-glow"></span>`}</div>`;
    }
    if (a.char && S.art.desk) {
      const front = where === "sofa" ? S.art.sofa : p === 1 ? (S.art.desk_nomug || S.art.desk) : p === 2 ? (S.art.desk_sleep || S.art.desk) : (S.art.desk_work || S.art.desk);
      // API 队员：耳机圆牌上和电脑背面叠厂商图标（随角色一起动）
      const fit = (a.frames && a.frames.acts_fit) || {}; // 动作表里的人比工位那格小一圈，按角色放大、平移对齐
      const acts = where !== "sofa" && a.frames && a.frames.acts ? `<div class="act-l" style="background-image:url(${esc(a.frames.acts)});scale:${fit.scale || 1};translate:${((fit.dx || 0) / 5.12).toFixed(2)}% 0"></div>` : "";
      return `<div class="lay v2">${acts}<div class="ch p${p}" style="background-image:url(${esc(a.char)})">${a.api && a.generic ? brandMark(a, "bd-ch") : ""}</div>${front ? `<img class="fr" src="${esc(front)}" alt="" draggable="false">` : ""}${a.api && p === 0 && where !== "sofa" ? brandMark(a, "bd-lid") : ""}</div>`;
    }
    if (a.desk && where !== "sofa") return `<div class="lay old"><div class="ch p${p}" style="background-image:url(${esc(a.desk)})"></div></div>`;
    return `<div class="lay bare">${a.avatar ? `<img class="bust" src="${esc(a.avatar)}" alt="" draggable="false">` : robot(a.color)}</div>`;
  }

  // ---------- 基地 ----------
  function mateHTML(a) {
    const typing = a.state === "working" ? `<div class="typing" title="${esc((a.acts[0] && a.acts[0].plain) || a.task || "")}"><i></i><i></i><i></i></div>` : "";
    return `<div class="mate ${a.state}" data-drag="1" data-agent="${esc(a.id)}" title="${esc(a.name)} · ${STATE[a.state].t}${a.task ? "\n" + esc(a.task) : ""}">
      ${typing}${figureHTML(a, "desk")}
      ${a.state === "off" ? `<div class="zzz"><i>z</i><i>z</i><i>Z</i></div>` : ""}
      <div class="tag ${a.id === viewLead() ? "lead" : ""}"><b>${a.id === viewLead() ? `<span class="crown">👑</span>` : `<i class="dot" style="background:${STATE[a.state].c}"></i>`}${esc(a.name)}${a.id === viewLead() ? `<em>主位</em>` : ""}</b>${a.quota_text ? `<small>${esc(a.quota_text)}</small>` : ""}</div></div>`;
  }
  const openBtns = (a, cls = "") => `${a.open && a.open.app ? `<button class="btn ${cls} primary" data-open="${esc(a.id)}" data-how="app">${icon("open")}打开桌面端</button>` : ""}${a.open && a.open.cli ? `<button class="btn ${cls}" data-open="${esc(a.id)}" data-how="cli">${icon("quests")}打开 CLI</button>` : ""}`;
  function leadHTML() {
    const bo = baseOffice(), lid = bo ? bo.agent : S.lead; // 基地显示的那间办公室的主位
    const a = lid && S.agents.find(x => x.id === lid);
    if (!a) return `<div class="lead-card empty"><div><b>还没有主位</b><p>主位是负责统筹派活的 agent。到队员的战备页点“设为主位”。</p></div><a class="btn sm" href="#team">去设置</a></div>`;
    return `<div class="lead-card" style="--c:${esc(a.color)}"><a class="lead-av" href="#hero/${encodeURIComponent(a.id)}">${ringAv(a, 56)}<span class="crown">👑</span></a>
      <div class="lead-main"><div class="lead-t"><b>${esc(a.name)}</b><span class="pill accent">主位 · 统筹</span></div>
        <p>${STATE[a.state].t}${a.task ? " · " + U(a.task) : ""}</p><div class="lead-act">${openBtns(a, "sm") || `<span class="small muted">本机没有它的桌面端或命令行</span>`}<button class="btn sm ghost" data-tour="lead">怎么派活</button></div></div></div>`;
  }
  function pBase() {
    shell(`<div class="head"><div><h1>基地</h1><p id="base-sub"></p></div>
        <div class="act"><div class="offpick" id="offpick"></div><button class="btn ghost" data-tour>新手引导</button><a class="btn" href="#team">${icon("team")}队员与职能</a></div></div>
      <div class="upd-banner" id="upd-banner"></div>
      <div class="base">
        <div class="scene-wrap"><div class="scene" id="scene"><div class="board" id="board"></div>
          ${SEATS.map(([x, y], i) => `<div class="seat ${i >= 3 ? "front" : ""}" data-seat="${i}" style="left:${x}%;top:${y}%"></div>`).join("")}</div></div>
        <div class="base-top"><div class="card" id="lead"></div><div class="card minichat" id="mc"></div></div>
        <div class="lounge-card" id="lounge"></div>
        <div class="base-side"><div class="card" id="res"></div><div class="card" id="run"></div></div></div>`);
    const bo = baseOffice(), off = seatsOf(bo);
    $("#base-sub").innerHTML = bo ? `<b>${esc(bo.name)}</b> · 战备 ${off.length}/${lineupMax()} · 拖动队员换人 · <a class="lnk" href="#office/${encodeURIComponent(bo.id)}">数据和设置</a>` : `默认阵容 · ${off.length}/${MAX_SEATS} 位上阵`;
    part("offpick", $("#offpick"), (S.offices || []).length > 0 ? `<span class="op-q" tabindex="0">?<span class="op-tip"><b>选办公室</b>每个在用 agentbus 的 agent 一间办公室（Claude 办公室、GPT 办公室…）。基地显示选中那间的战备：工位上是它派活时会用的队员，拖动就能调。<br><br><b>自动</b>：哪间有窗口在干活就显示哪间；带绿点的就是正在干活的。</span></span>`
      + `<button class="op ${!ui.baseOffice ? "on" : ""}" data-bo="" title="自动：显示正在干活的办公室">自动</button>` + (S.offices || []).map(o =>
      `<button class="op tile ${bo && bo.id === o.id ? "cur" : ""}" data-bo="${esc(o.id)}" title="${esc(o.name)}${o.active ? " · 有窗口在干活" : ""}">${avatar(A(o.agent), 24)}${o.active ? "<i></i>" : ""}</button>`).join("") : "");
    // 看某间办公室时：第一个工位坐这间的主人（Q 版小人戴皇冠，不能拖、不能被换下），战备从第二个工位往后坐
    const lead = leadSeated() ? A(bo.agent) : null, shown = lead ? [lead, ...off] : off;
    $$(".seat[data-seat]").forEach((seat, i) => {
      const a = shown[i];
      seat.classList.toggle("lead-desk", !!lead && i === 0);
      const key = a ? JSON.stringify([a.id, a.state, a.quota_text, a.char, a.desk, S.art.desk, S.art.desk_work, (a.acts[0] || {}).plain, viewLead() === a.id]) : "empty";
      if (seat.dataset.key === key) return;
      const was = seat.dataset.agent;
      seat.dataset.key = key; seat.dataset.agent = a ? a.id : "";
      seat.innerHTML = a ? mateHTML(a) : `<div class="slot">${icon("plus")}<span>空工位</span></div>`;
      if (lead && i === 0) seat.querySelector(".mate")?.removeAttribute("data-drag"); // 主位的座位不能拖
      if (a && was !== undefined && was !== a.id) seat.querySelector(".mate").classList.add("drop-in");
    });
    for (const a of S.agents) { const prev = prevState.get(a.id); if (prev === "working" && a.state !== "working") cheer(a.id); prevState.set(a.id, a.state); }
    const doing = S.running.map(r => ({ n: A(r.agent).name, t: r.task }));
    part("board", $("#board"), `<h3>今日任务</h3>` + (doing.length ? `<ul>${doing.slice(0, 4).map(d => `<li><b>${esc(d.n)}</b> ${esc(d.t)}</li>`).join("")}</ul>` : `<div class="none">还没有进行中的任务</div>`));
    const lo = loungeOf(bo);
    part("lounge", $("#lounge"), `<div class="lounge-head"><h2>${icon("sofa")}休息室</h2><span>拖进办公室上阵 · 拖回来休息</span><a class="more" href="#team">全部队员 ${icon("chev")}</a></div>
      <div class="lounge-room ${S.art.lounge ? "art" : ""}" ${S.art.lounge ? `style="background-image:url(${esc(S.art.lounge)})"` : ""}>
        <div class="sofas">${lo.map(a => `<div class="couch ${a.state}" data-drag="1" data-agent="${esc(a.id)}" title="${esc(a.name)} · ${STATE[a.state].t}">${figureHTML(a, "sofa")}
          <div class="tag"><b><i class="dot" style="background:${STATE[a.state].c}"></i>${esc(a.name)}</b></div></div>`).join("")}
          ${recruits().length ? `<a class="couch recruit" href="#team" data-recruit-list title="还没安装的 agent，点一下招募"><span class="rc">${icon("plus")}</span><div class="tag"><b>招募</b><small>${recruits().length} 位</small></div></a>` : ""}
          ${!lo.length ? `<div class="empty">休息室空空的</div>` : ""}</div></div>`);
    part("lead", $("#lead"), leadHTML());
    const up = S.update; // 有新版本：基地顶上提示
    part("updb", $("#upd-banner"), up && up.available && !ui.updHide ? `<div class="upd">🎉 <b>有新版本 ${esc(up.latest)}</b><span>${esc(String(up.notes || up.name || "").split("\n").filter(Boolean)[0] || "")}</span>
      <button class="btn sm primary" data-updgo="${esc(up.url || "")}">去下载</button><button class="ib" data-updhide title="先不更新">${icon("close")}</button></div>` : "");
    if (!(document.activeElement && document.activeElement.id === "mc-text")) paintMiniChat(); // 正在小聊天框里打字时不重画
    part("res", $("#res"), resourcesHTML());
    part("run", $("#run"), runningHTML());
  }
  function resourcesHTML() {
    const c = S.quota.codex || {}, d = S.quota.deepseek || {};
    const meter = (lbl, used, reset) => { const left = used == null ? null : Math.max(0, 100 - used);
      return `<div class="meter"><div class="meter-top">${icon("bolt")}<span>${lbl}</span><span class="muted">${resetText(reset)}</span><b>${left == null ? "—" : left + "%"}</b></div><div class="bar"><i class="${hpClass(left ?? 100)}" style="width:${left ?? 0}%"></i></div></div>`; };
    return `<div class="card-h"><h2>资源</h2><a class="more" href="#report">战报 ${icon("chev")}</a></div><div class="card-b">
      ${c.ok ? meter("GPT 体力 · 5 小时", c.five_hour, c.five_hour_reset) + meter("GPT 体力 · 本周", c.weekly, c.weekly_reset) : `<p class="muted small">GPT 体力读不到：${esc(c.error || "opencodex 没开")}</p>`}
      <div class="coin"><span class="ci">${icon("coin")}</span><div><b>${d.ok ? money(d.total) : "—"}</b><small>DeepSeek 金币${d.ok ? " · " + esc([...new Set((d.accounts || []).flatMap(x => x.used_by))].join("、")) + " 在用" : ""}</small></div></div>
      ${S.agents.some(a => a.billing === "claude" && a.installed) ? `<p class="small muted" style="margin:10px 0 0">Claude 额度看不到（订阅用量不对外开放）</p>` : ""}</div>`;
  }
  function runningHTML() {
    const rows = S.running.map(r => { const a = A(r.agent); return `<div class="q">${ringAv({ ...a, state: "working" }, 34)}<div class="q-main">
        <div class="q-top"><b>${esc(a.name)}</b><span class="t" data-since="${r.start}"></span></div>
        <div class="q-task" data-notr>${esc(r.task)}</div>${r.queued ? `<span class="pill gold">⏳ ${esc(r.queued)}</span>` : a.acts && a.acts[0] ? `<div class="q-act">${esc(a.acts[0].plain)}</div>` : ""}${r.retry ? `<span class="pill gold">卡住过，第 ${r.retry} 次重试</span>` : ""}</div></div>`; }).join("");
    const recent = S.recent.slice(0, 3).map(r => { const a = A(r.agent); return `<div class="q">${avatar(a, 34)}<div class="q-main">
        <div class="q-top"><b>${esc(a.name)}</b><span class="t">${esc((r.t || "").slice(11, 16))}</span></div>
        <div class="q-task" data-notr>${esc(r.text)}</div>${costText(r.cost) ? `<span class="pill ${r.cost.cny != null ? "gold" : "hp"}">${costText(r.cost)}</span>` : ""}</div></div>`; }).join("");
    return `<div class="card-h"><h2>进行中</h2>${S.running.length ? `<span class="pill hp">${S.running.length}</span>` : ""}<a class="more" href="#quests">全部 ${icon("chev")}</a></div>
      <div class="card-b">${rows || `<div class="empty"><div class="big">☕</div>暂时没有派出去的任务<br><span class="small">在主位里派活，这里和悬浮窗会实时出现</span></div>`}
      ${recent ? `<div class="small muted" style="margin:10px 0 2px">刚完成</div>${recent}` : ""}</div>`;
  }
  function cheer(id) {
    const m = $(`.mate[data-agent="${CSS.escape(id)}"]`); if (!m) return;
    m.classList.add("cheer");
    const p = document.createElement("div"); p.className = "done-pop"; p.innerHTML = icon("check"); m.appendChild(p);
    setTimeout(() => { m.classList.remove("cheer"); p.remove(); }, 2300);
  }
  // 活人感：办公室里的人隔一阵换个动作（办公动作表 6 格：0 看邻座 1 接活 2 交成果 3 喝茶 4 埋头干 5 卡住想）——
  // 干活的偶尔埋头苦干 / 卡住想一想，歇着的喝茶、看看邻座；沙发上的人轮着换 5 个动作
  setInterval(() => {
    if (!S || route.page !== "base" || document.hidden) return;
    for (const m of $$(".mate")) {
      const a = A(m.dataset.agent), l = m.querySelector(".act-l"), fr = m.querySelector(".fr");
      if (!l || m.dataset.act || Math.random() > .35) continue;
      const pick = a.state === "working" ? [4, 5] : a.state === "break" || a.state === "queued" ? [3, 0, 3] : [];
      if (!pick.length) continue;
      const i = pick[Math.floor(Math.random() * pick.length)], meta = ((a.frames || {}).acts_meta || [])[i] || {};
      const old = fr && fr.getAttribute("src");
      m.dataset.act = i;
      l.style.backgroundPosition = `${i * 20}% 0`;
      if (fr) fr.src = meta.desk === "clear" ? (S.art.desk_nomug || S.art.desk) : (S.art.desk_sleep || S.art.desk);
      setTimeout(() => { delete m.dataset.act; if (fr && old) fr.src = old; }, 5000 + Math.random() * 4000);
    }
    for (const c of $$(".lz.s5")) { const a = A(c.closest("[data-agent]").dataset.agent), i = sofaPose(a); if (c.dataset.sofa !== String(i)) { c.dataset.sofa = i; c.style.backgroundPosition = `${i * 25}% 0`; } }
  }, 4000);
  setInterval(() => { const idle = $$(".mate.off, .mate.break, .couch.off, .couch.break"); if (!idle.length) return;
    const m = idle[Math.floor(Math.random() * idle.length)]; m.classList.add("stretch"); setTimeout(() => m.classList.remove("stretch"), 1700); }, 7000);
  const tick = () => document.querySelectorAll("[data-since]").forEach(e => (e.textContent = clock(Date.now() / 1000 - Number(e.dataset.since))));
  setInterval(tick, 1000);

  // 拖动：休息室 ↔ 工位，工位之间互换位置。用指针事件自己实现：应用窗口里 HTML5 拖放会被外壳（接管文件拖入）拦掉，拖不动
  let drag = null;
  document.addEventListener("pointerdown", e => {
    if (e.button !== 0) return;
    const el = e.target.closest("[data-agent][data-drag]");
    if (el) drag = { el, id: el.dataset.agent, x: e.clientX, y: e.clientY, moved: false };
  });
  const dropTarget = e => { const t = document.elementFromPoint(e.clientX, e.clientY); return t && t.closest(".seat, .lounge-room"); };
  document.addEventListener("pointermove", e => {
    if (!drag) return;
    if (!drag.moved) {
      if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 6) return;
      drag.moved = true; dragging = drag.id; closePop();
      const r = drag.el.getBoundingClientRect(), g = drag.el.cloneNode(true);
      g.classList.add("drag-ghost"); g.style.width = r.width + "px"; g.style.height = r.height + "px";
      drag.dx = e.clientX - r.left; drag.dy = e.clientY - r.top; drag.ghost = g;
      document.body.appendChild(g); drag.el.classList.add("dragging"); document.body.classList.add("is-dragging");
    }
    e.preventDefault();
    drag.ghost.style.transform = `translate(${e.clientX - drag.dx}px, ${e.clientY - drag.dy}px) rotate(-3deg) scale(1.04)`;
    const t = dropTarget(e);
    $$(".over").forEach(x => x !== t && x.classList.remove("over"));
    if (t) t.classList.add("over");
  });
  document.addEventListener("pointerup", async e => {
    const d = drag; drag = null;
    if (!d || !d.moved) return;
    const t = dropTarget(e);
    d.ghost.remove(); d.el.classList.remove("dragging"); document.body.classList.remove("is-dragging");
    $$(".over").forEach(x => x.classList.remove("over"));
    setTimeout(() => (dragging = null), 60); // 松手后紧跟着的 click 不当成点角色
    if (t) await dropOn(t, d.id);
    setTimeout(paint, 80);
  });
  async function dropOn(t, id) {
    const a = A(id), bo = baseOffice();
    if (bo) return dropOffice(bo, t, id);
    if (t.classList.contains("lounge-room")) { if (a.place === "office" && await api("/api/move", { agent: id, place: "lounge" })) toast(`${a.name} 去休息室了`, "sofa"); return; }
    const i = Number(t.dataset.seat), off = office(), occupant = off[i], from = off.findIndex(x => x.id === id);
    if (occupant && occupant.id === id) return;
    if (a.place === "office" && from >= 0) {
      await api("/api/move", { agent: id, place: "office", index: Math.min(i, off.length - 1) });
      if (occupant) await api("/api/move", { agent: occupant.id, place: "office", index: from });
      toast(occupant ? `${a.name} 和 ${occupant.name} 换了位置` : `${a.name} 换了个工位`, "star");
      return;
    }
    if (occupant) {
      await api("/api/move", { agent: occupant.id, place: "lounge" });
      await api("/api/move", { agent: id, place: "office", index: i });
      toast(`${a.name} 换下了 ${occupant.name}`, "star");
      return;
    }
    if (off.length >= MAX_SEATS) { toast("工位满了，拖到某个人身上就能换下他", "team"); return; }
    await api("/api/move", { agent: id, place: "office", index: Math.min(i, off.length) });
    toast(`${a.name} 上阵！`, "star");
  }
  // 基地上拖动 = 调这间办公室的战备（上场、换下、互换位置、回休息室）
  async function dropOffice(bo, t, id) {
    const a = A(id), m = [...bo.lineup.office], from = m.indexOf(id);
    if (t.classList.contains("lounge-room")) {
      if (from < 0) return;
      m.splice(from, 1); if (await api("/api/office/lineup", { office: bo.id, members: m })) toast(`${a.name} 去休息室了`, "sofa");
      return;
    }
    const shift = leadSeated() ? 1 : 0;
    if (shift && Number(t.dataset.seat) === 0) { toast("第一个工位是主位的，换不了", "team"); return; }
    const i = Number(t.dataset.seat) - shift, occupant = m[i];
    if (occupant === id) return;
    if (from >= 0) { // 战备里换位置
      if (occupant) { m[i] = id; m[from] = occupant; } else { m.splice(from, 1); m.splice(Math.min(i, m.length), 0, id); }
      if (await api("/api/office/lineup", { office: bo.id, members: m })) toast(occupant ? `${a.name} 和 ${A(occupant).name} 换了位置` : `${a.name} 换了个工位`, "star");
      return;
    }
    if (occupant) m[i] = id;
    else if (m.length >= lineupMax()) { toast("工位满了，拖到某个人身上就能换下他", "team"); return; }
    else m.splice(Math.min(i, m.length), 0, id);
    if (await api("/api/office/lineup", { office: bo.id, members: m })) toast(occupant ? `${a.name} 换下了 ${A(occupant).name}` : `${a.name} 进了 ${bo.name} 的战备`, "star");
  }
  document.addEventListener("click", e => { // 基地顶上选办公室（空 = 自动跟着正在干活的那间）
    const b = e.target.closest("[data-bo]"); if (!b) return;
    ui.baseOffice = b.dataset.bo || null;
    try { ui.baseOffice ? localStorage.setItem("baseOffice", ui.baseOffice) : localStorage.removeItem("baseOffice"); } catch {}
    painted = { __shell: painted.__shell }; $$(".seat").forEach(x => (x.dataset.key = "")); paint(); onState(S);
  });
  // 休息室能横着滑：鼠标滚轮也能左右翻
  document.addEventListener("wheel", e => {
    const so = e.target.closest(".sofas"); if (!so || so.scrollWidth <= so.clientWidth || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
    so.scrollLeft += e.deltaY; e.preventDefault();
  }, { passive: false });


  // ---------- 队员 ----------
  function brainPill(a) { return a.brain && a.brain.label ? `<span class="pill brainp ${a.brain.set ? "set" : ""}" title="模型 · 思考强度">${esc(a.brain.label)}</span>` : ""; }
  // 特性小标注：主位 / 分身 / 可分身 / 网页版 / 没招募，再带一两个关键能力
  const CAP_TAG = { computer: "操控电脑", image: "生图", browser: "浏览器", vision: "看图" };
  function traits(a, n = 2) {
    const t = [];
    if (S.lead === a.id) t.push(`<span class="tr lead">👑 主位</span>`);
    if (a.clone_of) t.push(`<span class="tr clone">分身 · ${esc(A(a.clone_of).name)}</span>`);
    else if (a.cloneable) t.push(`<span class="tr can">可分身</span>`);
    if (a.billing === "manual") t.push(`<span class="tr web">网页版</span>`);
    if (a.api) t.push(`<span class="tr web">API</span>`);
    if (!a.installed) t.push(`<span class="tr off">未招募</span>`);
    (a.caps || []).filter(c => CAP_TAG[c]).slice(0, n).forEach(c => t.push(`<span class="tr cap">${CAP_TAG[c]}</span>`));
    return t.join("");
  }
  function cloneAct(a, sm = "sm") {
    if (a.clone_of) return `<button class="btn ${sm} ghost danger" data-clone-x="${esc(a.id)}">解散</button>`;
    if (a.cloneable) return `<button class="btn ${sm} ghost" data-clone-new="${esc(a.id)}">${icon("plus")}分身</button>`;
    return "";
  }
  // 全身立绘（美术素材里那套 01_DeepSeek…）：卡片用小图，战备页用大图
  const lihui = (a, big) => a.lihui ? `<img class="lh" src="${esc(big ? a.lihui : a.lihui.replace("/lihui/", "/lihui/sm/"))}" alt="" draggable="false" loading="lazy">` : "";
  function memberCard(a) {
    const act = !a.installed ? (a.install ? `<button class="btn sm primary" data-recruit="${esc(a.id)}">${icon("plus")}招募</button>` : a.get_url ? `<button class="btn sm primary" data-geturl="${esc(a.get_url)}">${icon("plus")}去官网下载</button>` : "")
      : a.place === "office" ? `<button class="btn sm" data-move="lounge" data-id="${esc(a.id)}">去休息</button>` : `<button class="btn sm primary" data-move="office" data-id="${esc(a.id)}">上阵</button>`;
    return `<a class="mc ${a.installed ? "" : "absent"} ${a.lihui ? "has-lh" : ""}" style="--c:${esc(a.color)}" href="#hero/${encodeURIComponent(a.id)}">
      ${a.lihui ? `<div class="mc-lh">${lihui(a)}</div>` : ""}<div class="mc-art">${figureHTML(a, "desk")}</div>
      <div class="mc-info"><div class="mc-top"><h3>${esc(a.name)}</h3>${a.sealed ? `<span class="pill seal" title="不接派活、不进聊天室、不查额度">🔒 封印</span>` : ""}${statePill(a)}</div><p>${esc(a.vendor || "")}${a.installed ? brainPill(a) : ""}</p>
        <div class="trs">${traits(a)}</div>
        <div class="mc-tags">${a.installed ? skillPills(a) || `<span class="small muted">还没装配职能</span>` : `<span class="small muted">${esc(a.desc || "")}</span>`}</div>
        <div class="mc-foot">${quotaPill(a) || `<span></span>`}<span class="mc-btns">${cloneAct(a)}${act}</span></div></div></a>`;
  }
  // 卡片视图：一张张角色卡，头像 + 名字 + 标注，操作放角上
  function charCard(a) {
    const act = !a.installed ? (a.install ? `<button class="cc-b primary" data-recruit="${esc(a.id)}" title="招募">${icon("plus")}</button>` : a.get_url ? `<button class="cc-b primary" data-geturl="${esc(a.get_url)}" title="去官网下载">${icon("plus")}</button>` : "")
      : a.place === "office" ? `<button class="cc-b" data-move="lounge" data-id="${esc(a.id)}" title="去休息">${icon("sofa")}</button>` : `<button class="cc-b primary" data-move="office" data-id="${esc(a.id)}" title="上阵">${icon("star")}</button>`;
    const cl = a.clone_of ? `<button class="cc-b danger" data-clone-x="${esc(a.id)}" title="解散分身">${icon("close")}</button>`
      : a.cloneable ? `<button class="cc-b" data-clone-new="${esc(a.id)}" title="开分身">${icon("copy")}</button>` : "";
    return `<a class="cc ${a.installed ? "" : "absent"} ${a.clone_of ? "is-clone" : ""}" style="--c:${esc(a.color)}" href="#hero/${encodeURIComponent(a.id)}">
      ${a.lihui ? `<div class="cc-lh">${lihui(a)}<span class="cc-q">${avatar(a, 44)}</span></div>` : ""}<div class="cc-art ${a.lihui ? "mini" : ""}">${a.lihui ? "" : avatar(a, 96)}<i class="cc-dot" style="background:${STATE[a.state].c}" title="${STATE[a.state].t}"></i>${a.clone_of ? `<i class="cc-n">${esc(a.abbr)}</i>` : ""}</div>
      <b class="cc-name">${esc(a.name)}</b>
      <span class="cc-brain">${a.installed && a.brain ? esc(a.brain.label) : esc(a.vendor || "")}</span>
      <div class="trs">${traits(a, 1)}</div>
      <div class="cc-act">${cl}${act}</div></a>`;
  }
  function pTeam() {
    ui.teamView ??= (() => { try { return localStorage.getItem("teamView") || "list"; } catch { return "list"; } })();
    shell(`<div class="head"><div><h1>队员</h1><p>点角色进战备。可分身的能开分身，各配模型并行干活。</p></div><div class="act" id="team-act"></div></div><div id="team"></div>`);
    part("teamact", $("#team-act"), `<button class="btn sm" data-apiwiz>🔌 接入 API</button><button class="btn sm" data-recruit-list>${icon("plus")}招募</button><div class="seg">${[["list", "列表"], ["cards", "卡片"]].map(([k, t]) => `<button class="${ui.teamView === k ? "on" : ""}" data-tview="${k}">${t}</button>`).join("")}</div>`);
    const off = office(), lo = lounge(), r = recruits(), card = ui.teamView === "cards" ? charCard : memberCard, g = ui.teamView === "cards" ? "cgrid" : "grid";
    // agent 队员（有自己的外壳：能读写文件、跑命令、操控电脑）和 API 队员（只接了模型的 key，用内置的轻量 agent，适合写写改改）分开放
    const ag = x => x.filter(a => !a.api), ap = [...off, ...lo].filter(a => a.api);
    part("team", $("#team"), `
      <div class="section-t"><h2>Agent 队员 · 阵容</h2><span>${ag(off).length}/${MAX_SEATS} 个工位 · 有自己的外壳，能读写文件、跑命令、操控电脑</span></div><div class="${g}">${ag(off).map(card).join("") || `<div class="empty">还没人上阵</div>`}</div>
      <div class="section-t"><h2>Agent 队员 · 休息室</h2><span>${ag(lo).length} 位</span></div><div class="${g}">${ag(lo).map(card).join("") || `<div class="empty">休息室没人</div>`}</div>
      <div class="section-t"><h2>API 队员</h2><span>${ap.length} 位 · 只接了模型 key，用 agentbus 内置的轻量 agent，适合写文案、改稿、翻译</span><button class="btn sm" data-apiwiz style="margin-left:auto">🔌 接入 API</button></div><div class="${g}">${ap.map(card).join("") || `<div class="empty">还没有 API 队员</div>`}</div>
      ${r.length ? `<div class="section-t"><h2>可招募</h2><span>装好对应的命令行工具，就会自动来休息室报到</span></div><div class="${g}">${r.map(card).join("")}</div>` : ""}`);
  }
  document.addEventListener("click", e => {
    const v = e.target.closest("[data-tview]"); if (!v) return;
    ui.teamView = v.dataset.tview; try { localStorage.setItem("teamView", ui.teamView); } catch {}
    painted.team = painted.teamact = null; pTeam();
  });
  document.addEventListener("click", async e => {
    const mv = e.target.closest("[data-move]"); if (!mv) return;
    e.preventDefault(); e.stopPropagation();
    const a = A(mv.dataset.id);
    if (mv.dataset.move === "office" && office().length >= MAX_SEATS) { toast("工位满了，先让一位去休息", "team"); return; }
    if (await api("/api/move", { agent: a.id, place: mv.dataset.move })) toast(mv.dataset.move === "office" ? `${a.name} 上阵！` : `${a.name} 去休息室了`, "star");
  }, true);
  document.addEventListener("click", e => { // 点办公室/休息室里的人 → 快查卡（和悬浮窗同款）
    const m = e.target.closest(".mate, .couch[data-agent]"); if (!m || dragging) return;
    // 再点一次同一个角色 = 收起（按下时弹窗已经被“点外面收起”关掉了，这里就别再打开）
    if (ui.qcClosed && ui.qcClosed.id === m.dataset.agent && Date.now() - ui.qcClosed.t < 500) return (ui.qcClosed = null);
    quickCard(m.dataset.agent, m);
  });
  async function quickCard(id, anchor) {
    closePop();
    const a = A(id);
    const pop = document.createElement("div"); pop.className = "pop qc"; pop.id = "pop"; pop.dataset.agent = id;
    document.body.appendChild(pop);
    const place = () => {
      // 右边放得下放右边，再试左边，都不行就放上面或下面——不盖住角色，这样再点一次角色能收起
      const r = anchor.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
      let left, top;
      if (r.right + 10 + w <= innerWidth - 12) left = r.right + 10;
      else if (r.left - 10 - w >= 12) left = r.left - w - 10;
      if (left != null) top = Math.max(12, Math.min(r.top + r.height / 2 - h / 2, innerHeight - h - 12));
      else {
        left = Math.max(12, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 12));
        const below = innerHeight - r.bottom - 10, above = r.top - 10;
        top = below >= h || below >= above ? r.bottom + 8 : Math.max(12, r.top - h - 8);
        pop.style.maxHeight = Math.max(below, above) - 16 + "px";
      }
      pop.style.left = left + "px"; pop.style.top = top + "px";
    };
    const draw = (list, bill) => {
      const run = S.running.find(r => r.agent === a.id);
      const day = new Date().toISOString().slice(0, 10);
      const today = (list || []).filter(q => q.agent === a.id && q.done && q.done.slice(0, 10) === day);
      const b = ((bill && bill.per_agent) || []).find(x => x.agent === a.id) || {};
      const used = a.billing === "deepseek" ? money(b.cny || 0) : a.billing === "codex" ? (b.weekly ? "-" + b.weekly.toFixed(0) + "%" : b.runs ? "<1%" : "—") : "—";
      pop.innerHTML = `<div class="qc-h">${ringAv(a, 48)}<div><b>${a.id === S.lead ? "👑 " : ""}${esc(a.name)}</b><span>${esc(a.vendor || "")} · ${a.place === "office" ? "上阵中" : "休息室"}</span><span class="row-pills">${quotaPill(a)}</span></div>
          <button class="ib" data-qc-x aria-label="关闭">${icon("close")}</button></div>
        <div class="qc-now">${run ? `<span class="t" data-since="${run.start}"></span><b>${esc(run.task)}</b>` : `<b>${STATE[a.state].t}</b>`}${a.acts[0] ? `<span>${run ? "" : "上一步："}${esc(a.acts[0].plain)}</span>` : ""}</div>
        <div class="qc-stats"><div><span>今天</span><b>${list ? today.length + " 个" : "…"}</b></div><div><span>用时</span><b>${list ? mins(today.reduce((s, q) => s + (q.seconds || 0), 0)) : "…"}</b></div><div><span>${S.batch ? "本批消耗" : "项目消耗"}</span><b>${bill ? used : "…"}</b></div></div>
        ${skillPills(a) ? `<div class="qc-skills">${skillPills(a)}</div>` : ""}
        ${a.installed && a.billing !== "manual" && a.brain ? `<div class="qc-brain">${brainBtn(a)}</div>` : ""}
        <div class="qc-act">${openBtns(a, "sm")}${a.installed ? (a.place === "office" ? `<button class="btn sm" data-move="lounge" data-id="${esc(a.id)}">${icon("sofa")}去休息</button>` : `<button class="btn sm primary" data-move="office" data-id="${esc(a.id)}">${icon("star")}上阵</button>`) : a.install ? `<button class="btn sm primary" data-recruit="${esc(a.id)}">${icon("plus")}招募</button>` : a.get_url ? `<button class="btn sm primary" data-geturl="${esc(a.get_url)}">${icon("plus")}去官网下载</button>` : ""}
          ${a.installed && S.lead !== a.id ? `<button class="btn sm" data-lead="${esc(a.id)}">👑 设为主位</button>` : ""}${a.installed && a.billing !== "manual" ? `<button class="btn sm ghost" data-msg="${esc(a.id)}">💬</button>` : ""}<a class="btn sm ghost" href="#hero/${encodeURIComponent(a.id)}">完整战备 ${icon("chev")}</a></div>`;
      tick(); place();
    };
    draw(null, null);
    pop.addEventListener("click", e => { if (e.target.closest("[data-qc-x], a")) closePop(); });
    const [list, bill] = await Promise.all([quests(), api("/api/bill?batch=" + encodeURIComponent(S.batch || ""))]);
    if ($("#pop") === pop) draw(list, bill);
  }

  // ---------- 留言：给队员留句话，它下次接活时会看到 ----------
  async function msgBox(id) {
    closePop();
    const a = A(id);
    const draw = async () => {
      const r = await api("/api/messages?agent=" + encodeURIComponent(id)), ms = (r && r.messages) || [];
      const who = x => x === "user" ? "我" : x === id ? a.name : (A(x).name || x);
      const list = $("#mg-list"); if (!list) return;
      list.innerHTML = ms.map(m => `<div class="mg ${m.frm === "user" ? "me" : ""}"><span>${esc(who(m.frm))} → ${esc(who(m.to))} · ${esc((m.t || "").slice(5, 16))}${m.to === id ? (m.read ? " · 已看到" : " · 未读") : ""}</span><p>${esc(m.text)}</p></div>`).join("")
        || `<p class="small muted mg-empty">还没有留言。写一句，${esc(a.name)} 下次接活时会在任务开头看到。</p>`;
      list.scrollTop = list.scrollHeight;
    };
    $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><div class="modal mgbox"><div class="mg-h">${avatar(a, 36)}<div><h2>给 ${esc(a.name)} 留言</h2><p class="muted small">不会马上叫醒它；下次派活时会附在任务前面</p></div></div>
      <div class="mg-list" id="mg-list"><p class="small muted">…</p></div>
      <div class="mg-in"><textarea class="inp" id="mg-t" rows="2" placeholder="比如：封面字号再大一点；下次录屏先关掉通知"></textarea><button class="btn primary" id="mg-go">发送</button></div></div></div>`;
    $("#sheet-bg").onclick = e => { if (e.target.id === "sheet-bg") $("#layer").innerHTML = ""; };
    const send = async () => { const t = $("#mg-t").value.trim(); if (!t) return; if (await api("/api/post", { agent: id, text: t })) { $("#mg-t").value = ""; toast("留好了", "check"); draw(); } };
    $("#mg-go").onclick = send;
    $("#mg-t").onkeydown = e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send(); };
    $("#mg-t").focus(); draw();
  }
  document.addEventListener("click", e => { const m = e.target.closest("[data-msg]"); if (!m) return; e.preventDefault(); e.stopPropagation(); msgBox(m.dataset.msg); }, true);

  // ---------- 主位：一键打开、设为主位 ----------
  document.addEventListener("click", async e => {
    const o = e.target.closest("[data-open]"), l = e.target.closest("[data-lead]");
    if (o) { e.preventDefault(); e.stopPropagation(); if (await api("/api/open", { agent: o.dataset.open, how: o.dataset.how })) toast(`正在打开 ${A(o.dataset.open).name}${o.dataset.how === "cli" ? " 命令行" : ""}`, "open"); }
    if (l) { e.preventDefault(); e.stopPropagation(); if (await api("/api/lead", { agent: l.dataset.lead })) toast(`${A(l.dataset.lead).name} 坐上主位`, "star"); }
  }, true);

  // ---------- 招募（一键安装）----------
  document.addEventListener("click", e => {  // 没有一键安装命令的（反重力）：打开官网下载页，装好桌面端就会来报到
    const g = e.target.closest("[data-geturl]");
    if (!g) return;
    e.preventDefault(); e.stopPropagation();
    api("/api/open-url", { url: g.dataset.geturl }).then(r => r && toast("已打开官网下载页，装好后会自动来休息室报到", "open"));
  }, true);
  document.addEventListener("click", e => {
    const r = e.target.closest("[data-recruit]"), l = e.target.closest("[data-recruit-list]");
    if (!r && !l) return;
    e.preventDefault(); e.stopPropagation();
    if (r) return recruit(r.dataset.recruit);
    const list = recruits().filter(a => a.install);
    $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><div class="modal"><h2>招募新队员</h2>
      <button class="rc-api" data-apiwiz><span class="rc-api-ic">🔌</span><span><b>接入 API</b><small>官方 API 或中转都行：填 key → 选模型 → 直接上岗，不用安装</small></span>${icon("chev")}</button>
      <p class="muted small" style="margin-top:14px">或者一键安装命令行工具，装好就会来休息室报到：</p>
      <div class="rc-list">${list.map(a => `<button class="pa" data-recruit="${esc(a.id)}">${avatar(a, 40)}<span class="nm">${esc(a.name)}<small>${esc(a.vendor || "")}</small></span>${icon("chev")}</button>`).join("") || `<p class="muted">没有可以一键安装的了</p>`}</div>
      <div class="modal-act"><button class="btn ghost" id="rc-x">关闭</button></div></div></div>`;
    $("#rc-x").onclick = () => ($("#layer").innerHTML = "");
    $("#sheet-bg").onclick = ev => { if (ev.target.id === "sheet-bg") $("#layer").innerHTML = ""; };
  }, true);
  // ---------- 接入 API：一步步来 ①选来源 ②填 key 测连接 ③选模型上岗 ----------
  const BRAND_IMG = ["deepseek", "moonshot", "zhipu", "openai", "opencode-go"];
  const brandAv = (pid, name, color, size = 34) => BRAND_IMG.includes(pid) ? `<span class="av brand" style="--s:${size}px"><img src="assets/brands/${pid}.png" alt=""></span>`
    : `<span class="av letter" style="--s:${size}px;--c:${esc(color || "#888")}"><b>${esc((name || "?").slice(0, 1))}</b></span>`;
  async function apiWizard(opt = {}) {
    const w = await walletData(true);
    const st = { tab: "official", step: 1, provider: null, base: "", name: "", key: "", fp: null, test: null, model: "", aname: "", q: "" };
    const COLORS = { deepseek: "#4d6bfe", zhipu: "#3a5cf5", moonshot: "#16191e", qwen: "#615ced", siliconflow: "#7c3aed", openrouter: "#6467f2", openai: "#10a37f", xai: "#111", "opencode-go": "#2b2b2b" };
    const found = w.found.filter(f => !f.added).concat(w.found.filter(f => f.added));
    const close = () => ($("#layer").innerHTML = "");
    const body = () => {
      if (st.step === 1) return `<div class="seg wz-tabs">${[["official", "官方 API"], ["relay", "中转"], ["local", `本机已有${found.length ? " " + found.length : ""}`]].map(([k, t]) => `<button class="${st.tab === k ? "on" : ""}" data-wt="${k}">${t}</button>`).join("")}</div>
        ${st.tab === "official" ? `<div class="pv-grid">${Object.entries(w.presets).filter(([k]) => k !== "custom").map(([k, v]) => `<button class="pv" data-pv="${esc(k)}">${brandAv(k, v.name, COLORS[k])}<b>${esc(v.name)}</b></button>`).join("")}</div>`
        : st.tab === "relay" ? `<p class="small muted">中转站会给你一个地址和一个 key，填进来就行。</p>
          <label class="fld"><span>中转地址</span><input class="inp" id="wz-base" placeholder="https://某某中转.com/v1" value="${esc(st.base)}"></label>
          <label class="fld"><span>起个名字</span><input class="inp" id="wz-name" placeholder="比如：MatrixAll 中转" value="${esc(st.name)}"></label>
          <div class="wz-act"><span></span><button class="btn primary" data-wrelay>下一步</button></div>`
        : found.length ? `<p class="small muted">这台电脑上别的工具里已经有的 key，选一个直接用（不复制、不显示 key）。</p><div class="lk-list">${found.map(f => `<button class="lk" data-wfp="${esc(f.fp)}">${brandAv(f.provider, f.name, COLORS[f.provider], 30)}<span><b>${esc(f.name)}</b><small>${esc(f.from)}${f.models ? " · " + f.models + " 个模型" : ""}${f.added ? " · 已在供应商" : ""}</small></span>${icon("chev")}</button>`).join("")}</div>`
        : `<p class="small muted wz-empty">这台电脑上没找到现成的 key，换「官方 API」或「中转」自己填一个。</p>`}`;
      if (st.step === 2) {
        const t = st.test;
        return `<div class="wz-who">${brandAv(st.provider, st.name, COLORS[st.provider], 40)}<div><b>${esc(st.name)}</b><small>${esc(st.base)}</small></div><button class="lnk small" data-wback>改</button></div>
          <label class="fld"><span>API key</span><input class="inp" id="wz-key" type="password" autocomplete="off" placeholder="sk-…" value="${esc(st.key)}"></label>
          <p class="small muted">key 会存进这台电脑的${esc(((S.platform || {}).keystore) || "钥匙串")}，不会写进任何文件。</p>
          ${t ? `<div class="wz-test ${t.ok ? "ok" : "bad"}">${t.ok ? `✓ 连上了${t.models.length ? `，有 ${t.models.length} 个模型` : "（这个地址不给列模型，下一步手动填）"}` : "✗ " + esc(t.error || "连不上")}</div>` : ""}
          <div class="wz-act"><button class="btn ghost" data-wback>上一步</button><button class="btn ${t && t.ok ? "" : "primary"}" data-wtest>${t ? "再测一次" : "测试连接"}</button>${t && t.ok ? `<button class="btn primary" data-wnext>下一步</button>` : ""}</div>`;
      }
      const ms = (st.test.models || []).filter(m => !st.q || m.toLowerCase().includes(st.q));
      return `<div class="wz-who">${brandAv(st.provider, st.name, COLORS[st.provider], 40)}<div><b>${esc(st.name)}</b><small>✓ 已连上</small></div></div>
        <label class="fld"><span>选一个模型</span>${(st.test.models || []).length ? `${st.test.models.length > 8 ? `<label class="search">${icon("search")}<input id="wz-q" placeholder="搜模型" value="${esc(st.q)}"></label>` : ""}
          <div class="md-list">${ms.map(m => `<button class="dd-it ${st.model === m ? "on" : ""}" data-wm="${esc(m)}"><b>${esc(m)}</b></button>`).join("") || `<p class="small muted">没找到</p>`}</div>`
          : `<input class="inp" id="wz-model" placeholder="模型名，比如 deepseek-chat" value="${esc(st.model)}">`}</label>
        <label class="fld"><span>队员名字（可选）</span><input class="inp" id="wz-aname" placeholder="${esc(st.name)} ${esc((st.model || "").split("/").pop())}" value="${esc(st.aname)}"></label>
        <div class="wz-act"><button class="btn ghost" data-wsave>只存进供应商</button><button class="btn primary" data-wgo ${st.model ? "" : "disabled"}>${icon("star")}上岗</button></div>`;
    };
    const draw = () => {
      const m = $("#wz-m"); if (!m) return;
      m.querySelector(".wz-steps").innerHTML = ["选来源", "填 key", "选模型"].map((t, i) => `<span class="${st.step === i + 1 ? "on" : st.step > i + 1 ? "done" : ""}"><i>${st.step > i + 1 ? "✓" : i + 1}</i>${t}</span>`).join("<em></em>");
      m.querySelector(".wz-body").innerHTML = body();
      const k = $("#wz-key"); if (k) { k.oninput = () => { st.key = k.value; }; k.onkeydown = e => { if (e.key === "Enter") m.querySelector("[data-wtest]").click(); }; if (!st.key) k.focus(); }
      const q = $("#wz-q"); if (q) q.oninput = () => { st.q = q.value.trim().toLowerCase(); const pos = q.value.length; draw(); const n = $("#wz-q"); n.focus(); n.setSelectionRange(pos, pos); };
      const mi = $("#wz-model"); if (mi) mi.oninput = () => { st.model = mi.value.trim(); m.querySelector("[data-wgo]").disabled = !st.model; };
      const an = $("#wz-aname"); if (an) an.oninput = () => { st.aname = an.value; };
    };
    const save = async () => { // 存进供应商，返回账户 id
      if (st.fp) return (await api("/api/wallet/link", { fp: st.fp, base: st.test.base, models: st.test.models })) || {};
      return (await api("/api/wallet/add", { provider: st.provider, base: st.test.base, name: st.name, key: st.key, models: st.test.models })) || {};
    };
    $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><div class="modal wz" id="wz-m"><div class="wz-h"><h2>🔌 接入 API</h2><button class="ib" data-wx>${icon("close")}</button></div><div class="wz-steps"></div><div class="wz-body"></div></div></div>`;
    $("#sheet-bg").onclick = e => { if (e.target.id === "sheet-bg") close(); };
    $("#wz-m").addEventListener("click", async e => {
      const g = sel => e.target.closest(sel);
      if (g("[data-wx]")) return close();
      if (g("[data-wt]")) { st.tab = g("[data-wt]").dataset.wt; return draw(); }
      if (g("[data-pv]")) { const k = g("[data-pv]").dataset.pv; Object.assign(st, { provider: k, base: w.presets[k].base, name: w.presets[k].name, fp: null, test: null, step: 2 }); return draw(); }
      if (g("[data-wrelay]")) {
        st.base = $("#wz-base").value.trim(); st.name = $("#wz-name").value.trim() || "中转";
        if (!/^https?:\/\//.test(st.base)) return toast("地址要以 https:// 开头", "close");
        Object.assign(st, { provider: "custom", fp: null, test: null, step: 2 }); return draw();
      }
      if (g("[data-wfp]")) {
        const f = found.find(x => x.fp === g("[data-wfp]").dataset.wfp);
        Object.assign(st, { provider: f.provider, base: f.base, name: f.name, fp: f.fp });
        g("[data-wfp]").querySelector("small").textContent = "测试连接中…";
        st.test = await api("/api/wallet/test", { fp: f.fp });
        if (st.test && st.test.ok) { st.step = 3; st.model = st.test.models.length === 1 ? st.test.models[0] : ""; } else { toast("这个 key 连不上：" + ((st.test || {}).error || ""), "close"); st.test = null; }
        return draw();
      }
      if (g("[data-wback]")) { st.step = 1; st.test = null; return draw(); }
      if (g("[data-wtest]")) {
        const b = g("[data-wtest]"); b.textContent = "测试中…"; b.disabled = true;
        st.test = await api("/api/wallet/test", { base: st.base, key: st.key }); return draw();
      }
      if (g("[data-wnext]")) { st.step = 3; st.model = st.test.models.length === 1 ? st.test.models[0] : ""; return draw(); }
      if (g("[data-wm]")) { st.model = g("[data-wm]").dataset.wm; return draw(); }
      if (g("[data-wsave]")) { const r = await save(); if (r.id) { close(); toast(`「${st.name}」存进供应商了`, "coin"); painted.wallet = null; ui.wallet = null; if (route.page === "settings") paintWallet(true); } return; }
      if (g("[data-wgo]")) {
        const r = await save(); if (!r.id) return;
        const q = await api("/api/api-agent/new", { account: r.id, model: st.model, name: st.aname.trim() });
        if (q && q.id) { close(); ui.wallet = null; toast("API 队员来休息室报到了", "star"); setTimeout(() => (location.hash = "#hero/" + encodeURIComponent(q.id)), 300); }
      }
    });
    draw();
  }
  document.addEventListener("click", e => { const b = e.target.closest("[data-apiwiz]"); if (!b) return; e.preventDefault(); e.stopPropagation(); apiWizard(); }, true);
  function recruit(id) {
    const a = A(id);
    $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><div class="modal"><div style="display:flex;gap:12px;align-items:center">${avatar(a, 56)}<div><h2>招募 ${esc(a.name)}？</h2><p class="muted small" style="margin:2px 0 0">${esc(a.vendor || "")} · ${esc(a.desc || "")}</p></div></div>
      <label class="fld"><span>安装位置</span><input class="inp" id="rc-dir" placeholder="默认位置（不用改）" value="${esc((S.settings || {}).install_dir || "")}"></label>
      <p class="small">会在这台电脑上执行下面这条命令（需要联网下载，通常一两分钟）：</p><pre class="cmd" id="rc-cmd">${esc(a.install)}</pre>
      <p class="small warn" id="rc-need" hidden></p>
      <pre class="cmd log" id="rc-log" hidden></pre>
      <div class="modal-act"><button class="btn ghost" id="rc-x">取消</button><button class="btn primary" id="rc-go">${icon("plus")}一键安装</button></div></div></div>`;
    const close = () => { clearInterval(recruit.t); $("#layer").innerHTML = ""; };
    $("#rc-x").onclick = close;
    $("#sheet-bg").onclick = ev => { if (ev.target.id === "sheet-bg") close(); };
    const preview = async () => {
      const r = await api(`/api/install/preview?agent=${encodeURIComponent(id)}&dir=${encodeURIComponent($("#rc-dir").value.trim())}`); if (!r) return;
      $("#rc-cmd").textContent = r.cmd || "这台电脑上没有一键安装"; const n = $("#rc-need");
      n.hidden = !r.needs; if (r.needs) n.innerHTML = `要先装 ${esc(r.needs.name)}：<a class="lnk" href="${esc(r.needs.url)}" target="_blank">${esc(r.needs.url)}</a>`;
      $("#rc-go").disabled = !r.cmd || !!r.needs;
    };
    $("#rc-dir").onchange = preview; preview();
    $("#rc-go").onclick = async () => {
      if (!(await api("/api/install", { agent: id, install_dir: $("#rc-dir").value.trim() }))) return;
      const go = $("#rc-go"); go.disabled = true; go.textContent = "安装中…"; $("#rc-x").textContent = "后台继续，关闭";
      const log = $("#rc-log"); log.hidden = false;
      recruit.t = setInterval(async () => {
        const st = await api("/api/install?agent=" + encodeURIComponent(id)); if (!st || !$("#rc-log")) return clearInterval(recruit.t);
        log.textContent = st.log || "开始安装…"; log.scrollTop = log.scrollHeight;
        if (st.done) { clearInterval(recruit.t); go.textContent = st.ok ? "装好了！" : "没装上，看上面的日志"; toast(st.ok ? `${a.name} 来休息室报到了` : `${a.name} 没装上`, st.ok ? "star" : "close"); }
      }, 2000);
    };
  }

  // ---------- 战备（角色 → 职能 → 词条）----------
  // 账号登录（通用）：新手不用开终端。内置授权 / 后台跑它自带的登录命令 / 打开它的应用 / 兜底开终端
  function loginPanel(a) {
    const L = a.login;
    if (!L || a.sealed) return "";
    const st = L.logged_in === true ? `<span class="pill hp">✓ 已登录</span>` : L.logged_in === false ? `<span class="pill gold">还没登录</span>` : "";
    const btn = L.busy || ui.loginBusy === a.id ? `<button class="btn sm" disabled>登录中…</button>`
      : `<button class="btn sm ${L.logged_in === true ? "ghost" : "primary"}" data-login="${esc(a.id)}">${L.logged_in === true ? "重新登录" : esc(L.label)}</button>`;
    return `<div class="bal-box login-box"><div class="bal-top"><b>账号 ${st}</b>${btn}</div>${ui.loginNote && ui.loginNote.id === a.id ? `<p class="bal-desc">${esc(ui.loginNote.text)}</p>` : L.note && L.logged_in !== true ? `<p class="bal-desc">${esc(L.note)}</p>` : ""}</div>`;
  }
  document.addEventListener("click", async e => {
    const b = e.target.closest("[data-login]"); if (!b) return;
    e.preventDefault();
    const id = b.dataset.login;
    ui.loginBusy = id; painted.hero = null; if (route.page === "hero") pHero();
    const r = await api("/api/login", { agent: id });
    ui.loginBusy = null;
    if (r) ui.loginNote = { id, text: r.note || "" };
    painted.hero = null; if (route.page === "hero") pHero();
  });
  // 悬浮窗显示哪几条额度（通用：GPT 的 5 小时 / 每周、Antigravity 的各额度池…），勾哪条显示哪条，至少留一条
  function meterPickPanel(a) {
    const m = a.meter;
    if (!m || !m.all || m.all.length < 2) return "";
    const on = new Set((m.windows || []).map(w => w.id));
    return `<div class="bal-box pick-box"><div class="bal-top"><b>悬浮窗显示</b></div><p class="bal-desc">勾哪几条额度，悬浮窗里 ${esc(a.name)} 名下就显示哪几条（同一个额度池的模型已经合成一条）。</p>
      <div class="pick-list">${m.all.map(w => `<label title="${esc(w.full || w.label)}"><input type="checkbox" data-mpick="${esc(a.id)}" value="${esc(w.id)}" ${on.has(w.id) ? "checked" : ""}>
        <span>${esc(w.label)}</span><i><b style="width:${Math.round(w.remain * 100)}%"></b></i><em>${esc(w.text)}</em></label>`).join("")}</div></div>`;
  }
  document.addEventListener("change", async e => {
    const c = e.target.closest("[data-mpick]"); if (!c) return;
    const id = c.dataset.mpick, picked = [...document.querySelectorAll(`[data-mpick="${CSS.escape(id)}"]:checked`)].map(x => x.value);
    if (!picked.length) { c.checked = true; toast("至少留一条"); return; }
    const base = A(id).clone_of || id;
    if (await api("/api/settings", { meter_pick: { [base]: picked } })) toast("悬浮窗已更新");
  });
  // 接入余额：打开就用这位队员自己的登录凭证去它官方接口查剩余，链路每一步的结果都摆出来
  function balancePanel(a) {
    const b = a.balance;
    if (!b || !a.installed || a.sealed) return "";
    if (!b.supported) return `<div class="bal-box off"><b>接入余额</b><span>${esc(b.why || a.name + " 还没有查剩余额度的接口，名下显示的是本机记录的用量。")}</span></div>`;
    const r = b.result, busy = ui.balBusy === a.id;
    const steps = r && r.steps && r.steps.length ? `<ol class="bal-steps">${r.steps.map(x => `<li class="${x.ok ? "ok" : "bad"}"><b>${x.ok ? "✓" : "✗"} ${esc(x.name)}</b>${x.detail ? `<span>${esc(x.detail)}</span>` : ""}</li>`).join("")}</ol>` : "";
    const wins = r && r.ok ? `<div class="bal-wins">${((a.meter && a.meter.all) || r.windows.slice(0, 4)).map(w => `<span><i><b style="width:${Math.round(w.remain * 100)}%"></b></i>${esc(w.label)} 剩 ${Math.round(w.remain * 100)}%</span>`).join("")}</div>` : "";
    return `<div class="bal-box ${b.on ? "on" : ""}"><div class="bal-top"><b>接入余额</b>
      <button class="sw ${b.on ? "on" : ""}" data-balsw="${esc(a.id)}" role="switch" aria-checked="${b.on}" ${busy ? "disabled" : ""}></button></div>
      <p class="bal-desc">打开后用 ${esc(a.name)} 自己的登录凭证查剩余额度（凭证只在本机用，只发给它官方的接口）。</p>
      ${busy ? `<div class="small muted">正在跑链路…</div>` : b.on ? steps + wins + `<button class="btn sm ghost bal-re" data-balre="${esc(a.id)}">重新检查</button>` : ""}</div>`;
  }
  document.addEventListener("click", async e => {
    const sw = e.target.closest("[data-balsw],[data-balre]"); if (!sw) return;
    e.preventDefault();
    const a = A(sw.dataset.balsw || sw.dataset.balre), on = sw.dataset.balre ? true : !(a.balance && a.balance.on); // 重新检查 = 开着再跑一遍
    ui.balBusy = a.id; painted.hero = null; pHero && route.page === "hero" && pHero();
    const r = await api("/api/balance/link", { agent: a.id, on });
    ui.balBusy = null;
    if (r) { a.balance = { ...a.balance, on: r.on, result: r.result }; toast(!on ? "已关闭接入余额" : r.result && r.result.ok ? "余额接上了" : "链路没跑通，看看是哪一步"); }
    painted.hero = null; if (route.page === "hero") pHero();
  });
  // Claude 封印：默认开。解封要先看风险说明再确认；重新封印随时一键
  function sealPanel(a) {
    return a.sealed
      ? `<div class="seal-box"><b>🔒 已封印</b><span>${esc(a.name)} 只当主位：不接派活、不进聊天室、不查额度，任何能把它当队员唤醒的入口都关着。</span><button class="btn sm" data-unseal>解封…</button></div>`
      : `<div class="seal-box open"><b>🔓 已解封</b><span>${esc(a.name)} 可以被派活、进聊天室、显示用量。</span><button class="btn sm" data-reseal>重新封印</button></div>`;
  }
  document.addEventListener("click", async e => {
    if (e.target.closest("[data-reseal]")) {
      e.preventDefault();
      if (await api("/api/settings", { claude_seal: true })) { S.settings.claude_seal = true; toast("已重新封印"); }
      return;
    }
    if (e.target.closest("[data-unseal]")) {
      e.preventDefault();
      $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><div class="modal seal-modal" role="dialog" aria-label="解封 Claude"><h2>解封 Claude？</h2>
        <p>解封后，Claude 会像其他队员一样：能被主位或聊天室派活、能进群聊、能按职能被自动挑中，悬浮窗也会显示它的用量。</p>
        <div class="warn-box"><b>⚠️ 会增加 Claude 的不稳定风险</b><ul>
          <li>后台用命令行（claude -p）跑 Claude，可能因为没登录、额度用完或限流而失败。</li>
          <li>它同时在当主位时，派给它的活会和你正在用的对话抢同一份订阅额度。</li>
          <li>派活起的 Claude 会话多了，你自己的对话可能更容易撞到用量上限。</li></ul></div>
        <p class="small muted">随时可以在这里重新封印。</p>
        <div class="modal-act"><button class="btn ghost" data-seal-cancel>先不了</button><button class="btn primary danger" data-seal-ok>我了解风险，解封</button></div></div></div>`;
      return;
    }
    if (e.target.closest("[data-seal-cancel]") || e.target.id === "sheet-bg" && $(".seal-modal")) { $("#layer").innerHTML = ""; return; }
    if (e.target.closest("[data-seal-ok]")) {
      $("#layer").innerHTML = "";
      if (await api("/api/settings", { claude_seal: false })) { S.settings.claude_seal = false; toast("已解封"); }
    }
  });
  async function pHero() {
    const a = A(route.id);
    shell(`<a class="back" href="#team">${icon("chev", "flip")}全部队员</a><div class="hero" id="hero"></div>`);
    const list = (await quests()).filter(q => q.agent === a.id);
    const done = list.filter(q => q.done);
    const avg = done.length ? done.reduce((s, q) => s + (q.seconds || 0), 0) / done.length : null;
    const cny = done.reduce((s, q) => s + ((q.cost || {}).cny || 0), 0);
    const jobs = a.jobs || {};
    const cats = CAT ? CAT.categories : [];
    const shown = cats.filter(c => jobs["cat:" + c.id] || c.tags.some(t => jobs["tag:" + t]) || ui.openCat === c.id);
    const others = cats.filter(c => !shown.includes(c));
    const act = !a.installed ? (a.install ? `<button class="btn primary" data-recruit="${esc(a.id)}">${icon("plus")}一键招募</button>` : `<span class="small muted">${esc(a.desc)}</span>`)
      : a.billing === "manual" && a.install ? `<button class="btn" data-recruit="${esc(a.id)}">${icon("plus")}装命令行版</button>${a.place === "office" ? "" : `<button class="btn primary" data-move="office" data-id="${esc(a.id)}">${icon("star")}上阵</button>`}` : a.place === "office"
      ? `<button class="btn" data-move="lounge" data-id="${esc(a.id)}">${icon("sofa")}去休息室</button>` : `<button class="btn primary" data-move="office" data-id="${esc(a.id)}">${icon("star")}上阵</button>`;
    part("hero", $("#hero"), `
      <div class="hero-art" style="--c:${esc(a.color)}"><div class="hero-fig ${a.lihui ? "with-lh" : ""}">${a.lihui ? lihui(a, true) : ""}${figureHTML({ ...a, state: a.state === "off" ? "break" : a.state }, "desk")}</div>
        <div class="hero-plate"><h1>${esc(a.name)}</h1><p>${esc(a.vendor || "")} · ${esc(a.desc || "")}</p>
          ${a.billing === "claude" ? sealPanel(a) : ""}${loginPanel(a)}${balancePanel(a)}${meterPickPanel(a)}
          <div class="row-pills">${statePill(a)}<span class="pill">${a.place === "office" ? "上阵中" : "休息室"}</span>${a.clone_of ? `<span class="pill accent">👥 分身</span>` : ""}${quotaPill(a)}</div>
          <div class="hero-act">${act}${a.installed ? (S.lead === a.id ? `<span class="pill accent">👑 主位</span>` : `<button class="btn" data-lead="${esc(a.id)}">👑 设为主位</button>`) : ""}<a class="btn ghost" href="#quests" data-qa-go="${esc(a.id)}">${icon("quests")}任务记录</a>${a.installed && a.billing !== "manual" ? `<button class="btn ghost" data-msg="${esc(a.id)}">💬 留言</button>` : ""}</div>
          ${a.installed && (a.open.app || a.open.cli) ? `<div class="hero-act">${openBtns(a, "sm")}</div>` : ""}
          ${a.installed && a.billing !== "manual" ? `<div class="hero-act">${brainBtn(a)}${cloneBar(a)}${a.api ? `<button class="clone-chip x" data-api-x="${esc(a.id)}">解雇</button>` : ""}</div>` : ""}</div></div>
      <div class="hero-side">
        <div class="stats"><div class="stat"><div class="k">完成任务</div><div class="v">${done.length}</div></div>
          <div class="stat"><div class="k">平均用时</div><div class="v">${avg == null ? "—" : mins(avg)}</div></div>
          <div class="stat"><div class="k">${a.billing === "deepseek" ? "花掉金币" : "消耗方式"}</div><div class="v">${a.billing === "deepseek" ? money(cny) : BILLING[a.billing] || "—"}</div></div></div>
        <div class="slots-h"><h2>${icon("skills")}职能</h2><span>派活写职能名时，会按 主力 → 替补 自动挑人</span></div>
        <div class="slots">${shown.map(c => catCard(a, c, ui.openCat === c.id)).join("") || `<div class="slot-empty">还没装配职能，从下面选一个大类开始</div>`}</div>
        ${others.length ? `<div class="slots-h"><h2>${icon("plus")}装配新职能</h2><span>选一个大类，再点词条</span></div>
        <div class="cat-grid">${others.map(c => `<button class="cat-tile" data-cat="${esc(c.id)}"><span class="cat-ic">${CAT_IC[c.id] || "✨"}</span><b>${esc(c.name)}</b><small>${c.tags.length} 个词条</small></button>`).join("")}</div>` : ""}
        <div class="slots-h"><h2>${icon("clock")}最近在做</h2></div>
        <ul class="tl">${(a.acts || []).slice(0, 6).map(x => `<li><time>${esc(x.t.slice(0, 5))}</time><span>${esc(x.plain)}</span></li>`).join("") || `<li><time></time><span class="muted">${a.billing === "manual" ? "网页版，看不到过程" : "暂时没有动态"}</span></li>`}</ul>
      </div>`);
  }
  // ---------- 大脑（模型 + 思考强度）和分身 ----------
  const short = m => String(m || "").split("/").pop().replace(/^deepseek-/, "").replace(/^claude-/, "");
  async function brainOf(id, fresh) {
    ui.brains = ui.brains || {};
    if (fresh || !ui.brains[id]) ui.brains[id] = await api("/api/brain?agent=" + encodeURIComponent(id)) || { models: [], current: {}, chosen: {} };
    return ui.brains[id];
  }
  function effOf(o) { // 当前模型、它的强度档位、现在落在哪一档
    const ch = o.chosen || {}, cur = o.current || {};
    const model = ch.model || cur.model;
    const m = (o.models || []).find(x => x.id === model);
    const effs = m ? (m.efforts || []) : (o.custom_efforts || []);
    const inherited = ch.model && m && !effs.includes(cur.effort) ? null : cur.effort;
    const eff = ch.effort || inherited || (m && m.default);
    return { ch, cur, model, m, effs, eff };
  }
  // 角色卡上的大脑按钮：像 GPT 那样一颗小胶囊「GPT-6.1-Sol 中 ⌄」
  function brainBtn(a) {
    const b = a.brain; if (!b) return "";
    return `<button class="brain-btn ${b.set ? "set" : ""}" data-bmenu="${esc(a.id)}" title="${esc(a.name)} · ${esc(b.model || "默认模型")} · 模型与思考程度" aria-label="${esc(a.name)}：模型与思考程度" ${b.switch ? "" : "disabled"}><b>${esc(b.name || "默认")}</b>${b.effort_name ? `<em>${esc(b.effort_name)}</em>` : ""}${b.switch ? icon("down") : ""}</button>`;
  }
  function cloneBar(a) {
    if (a.clone_of) return `<a class="clone-chip" href="#hero/${encodeURIComponent(a.clone_of)}" title="回到本体">${avatar(A(a.clone_of), 20)}本体</a><button class="clone-chip x" data-clone-x="${esc(a.id)}">解散</button>`;
    const kids = S.agents.filter(x => x.clone_of === a.id);
    return kids.map(k => `<a class="clone-chip" href="#hero/${encodeURIComponent(k.id)}" title="${esc(k.name)} · ${esc((k.brain && k.brain.label) || "")}">${avatar(k, 20)}${esc(k.abbr)}</a>`).join("")
      + `<button class="clone-chip cl-add" data-clone-new="${esc(a.id)}" title="开个分身：同一个外壳换个大脑，并行干活">${icon("plus")}分身</button>`;
  }
  // 弹层：上面是强度 + 模型（点开换模型），下面是胶囊拖动条
  async function brainMenu(anchor, a, view) {
    closePop();
    const o = await brainOf(a.id, true);
    if (o.custom_model) return brainEditor(anchor, a, o);
    const pop = document.createElement("div"); pop.className = "pop bp"; pop.id = "pop"; pop.dataset.agent = "brain:" + a.id;
    document.body.appendChild(pop);
    const nm = e => (o.names || {})[e] || e || "";
    const draw = (v, q = "") => {
      const { ch, cur, m, model, effs, eff } = effOf(o);
      if (v === "models") {
        const ms = (o.models || []).filter(x => !q || (x.id + " " + (x.name || "")).toLowerCase().includes(q));
        const groups = {}; ms.forEach(x => (groups[x.group || (x.id.includes("/") ? x.id.split("/")[0] : "")] ||= []).push(x));
        pop.innerHTML = `<div class="bp-nav"><button data-bpback>${icon("chev", "flip")}</button><b>模型</b><button data-bfresh title="重新识别本机模型">${o.detecting ? "识别中…" : "刷新"}</button></div>
          ${(o.models || []).length > 8 ? `<label class="search bp-q">${icon("search")}<input placeholder="搜模型" value="${esc(q)}"></label>` : ""}
          <div class="dd-list">${q ? "" : `<button class="dd-it ${ch.model ? "" : "on"}" data-bm=""><b>默认</b><small>${esc(cur.model ? short(cur.model) : "跟随它自己")}</small></button>`}
          ${Object.entries(groups).map(([g, xs]) => (g ? `<div class="dd-g">${esc(g)}</div>` : "") + xs.map(x => `<button class="dd-it ${ch.model === x.id ? "on" : ""}" data-bm="${esc(x.id)}"><b>${esc(x.group ? x.name : g ? String(x.name || x.id).replace(g + "/", "") : x.name || x.id)}</b></button>`).join("")).join("") || `<p class="small muted" style="padding:8px">没找到</p>`}</div>`;
        const inp = pop.querySelector(".bp-q input"); if (inp) { inp.oninput = () => { const v2 = inp.value; draw("models", v2.trim().toLowerCase()); const n = pop.querySelector(".bp-q input"); n.focus(); n.setSelectionRange(v2.length, v2.length); }; if (!q) inp.focus(); }
      } else {
        const i = Math.max(0, effs.indexOf(eff));
        pop.innerHTML = `<div class="bp-top"><span class="bp-ic">${icon("bolt")}</span><button class="bp-head" data-bpmodels><b>${esc(nm(eff) || "—")}</b><span>${esc((m && m.name) || short(model) || "默认")} ${icon("chev")}</span></button>
            ${ch.effort ? `<button class="bp-reset" data-be="">默认</button>` : ""}</div>
          ${effs.length > 1 ? `<div class="slide" data-cap="${esc(a.id)}" data-effs="${esc(effs.join(","))}" style="--n:${effs.length};--i:${i}"><div class="sl-fill"></div>${effs.map((e, k) => `<i style="--k:${k}"></i>`).join("")}<div class="sl-thumb"></div></div>` : `<p class="small muted bp-none">这个模型没有思考档位</p>`}`;
      }
      const r = anchor.getBoundingClientRect();
      pop.style.left = Math.max(12, Math.min(r.left, innerWidth - pop.offsetWidth - 12)) + "px";
      pop.style.top = (r.bottom + 6 + pop.offsetHeight > innerHeight - 12 ? Math.max(12, r.top - 6 - pop.offsetHeight) : r.bottom + 6) + "px";
    };
    pop.redraw = draw;
    draw(view || "main");
    pop.addEventListener("click", async e => {
      if (e.target.closest("[data-bpmodels]")) return draw("models");
      if (e.target.closest("[data-bpback]")) return draw("main");
      const it = e.target.closest("[data-bm]"), be = e.target.closest("[data-be]"), fr = e.target.closest("[data-bfresh]");
      if (it) { await setBrain(a, { model: it.dataset.bm }); draw("main"); }
      if (be) { await setBrain(a, { effort: "" }); draw("main"); }
      if (fr) { fr.textContent = "识别中…"; await api("/api/brain?fresh=1&agent=" + encodeURIComponent(a.id)); setTimeout(async () => { Object.assign(o, await brainOf(a.id, true)); if ($("#pop") === pop) draw("models"); }, 1500); }
    });
  }
  function brainEditor(anchor, a, o) {
    const pop = document.createElement("div");
    pop.id = "pop"; pop.className = "pop bp brain-editor"; pop.dataset.agent = "brain:" + a.id;
    pop.setAttribute("role", "dialog"); pop.setAttribute("aria-label", a.name + " 模型设置");
    const current = effOf(o), names = o.names || {};
    pop.innerHTML = `<div class="bp-nav"><b>${esc(a.name)} · 模型设置</b><button type="button" data-bclose aria-label="关闭模型设置">${icon("close")}</button></div>
      <p class="brain-current">当前：<strong>${esc(current.model || "CLI 默认")}</strong>${current.eff ? ` · ${esc(current.eff)}` : ""}</p>
      <form class="brain-form">
        <label for="brain-model">模型 ID</label>
        <input id="brain-model" name="model" list="brain-model-list" value="${esc(current.ch.model || "")}" placeholder="${esc(current.cur.model || "输入完整模型 ID，或留空跟随 CLI")}" autocomplete="off" spellcheck="false" maxlength="512">
        <datalist id="brain-model-list"></datalist>
        <div class="brain-model-help"><span>可选择已识别模型，也可直接输入完整 ID。</span><button type="button" data-brefresh>刷新列表</button></div>
        <label for="brain-effort">思考程度</label><select id="brain-effort" name="effort"></select>
        <p class="small muted" data-bnote></p>
        <p class="small muted">保存后从下一次派单生效。留空模型 ID 跟随本机 CLI；恢复默认会清除两项覆盖。</p>
        <div class="brain-actions"><button class="btn ghost sm" type="button" data-breset>恢复默认</button><button class="btn primary sm" type="submit">保存配置</button></div>
      </form>`;
    document.body.appendChild(pop);
    const form = $("form", pop), modelInput = form.elements.model, effortInput = form.elements.effort;
    const position = () => {
      const r = anchor.getBoundingClientRect();
      pop.style.left = Math.max(12, Math.min(r.left, innerWidth - pop.offsetWidth - 12)) + "px";
      pop.style.top = Math.max(12, Math.min(r.bottom + 6, innerHeight - pop.offsetHeight - 12)) + "px";
    };
    const fillModels = () => {
      const ids = [...new Set([...(o.models || []).map(m => m.id), current.ch.model, (o.current || {}).model].filter(Boolean))];
      $("datalist", pop).innerHTML = ids.map(id => `<option value="${esc(id)}"></option>`).join("");
    };
    const fillEfforts = desired => {
      const draft = { ...o, chosen: { model: modelInput.value.trim() } }, e = effOf(draft);
      effortInput.innerHTML = `<option value="">自动${e.eff ? `（${esc(e.eff)} · ${esc(names[e.eff] || e.eff)}）` : "（CLI 默认）"}</option>` +
        e.effs.map(value => `<option value="${esc(value)}">${esc(value)} · ${esc(names[value] || value)}</option>`).join("");
      effortInput.value = e.effs.includes(desired) ? desired : "";
      $("[data-bnote]", pop).textContent = !e.m && e.model
        ? "此 ID 未列入本机模型列表，是否可用及支持哪些档位取决于当前账号和模型服务。"
        : "自动优先沿用兼容的本机档位，否则采用模型推荐档位。";
      position();
    };
    fillModels(); fillEfforts(current.ch.effort || "");
    modelInput.oninput = () => fillEfforts(effortInput.value);
    $("[data-bclose]", pop).onclick = closePop;
    const save = async patch => {
      const buttons = $$('button', pop); buttons.forEach(b => b.disabled = true);
      try {
        if (await setBrain(a, patch)) { closePop(); toast("已保存，下次派单生效"); }
      } finally { buttons.forEach(b => b.disabled = false); }
    };
    form.onsubmit = e => { e.preventDefault(); save({ model: modelInput.value.trim(), effort: effortInput.value }); };
    $("[data-breset]", pop).onclick = () => save({ model: "", effort: "" });
    $("[data-brefresh]", pop).onclick = async e => {
      const button = e.currentTarget; button.disabled = true; button.textContent = "识别中…";
      try {
        const result = await api("/api/brain?fresh=1&agent=" + encodeURIComponent(a.id));
        if (!result) return;
        Object.assign(o, result);
        if (result.detecting) {
          await new Promise(resolve => setTimeout(resolve, 1500));
          Object.assign(o, await brainOf(a.id, true));
        }
        if ($("#pop") === pop) { fillModels(); fillEfforts(effortInput.value); }
      } catch { toast("模型列表刷新失败", "close"); }
      finally { button.disabled = false; button.textContent = "刷新列表"; }
    };
    modelInput.focus();
  }
  async function setBrain(a, patch) {
    const o = await brainOf(a.id);
    const b = { ...(o.chosen || {}), ...patch };
    if (patch.model !== undefined && patch.effort === undefined) { // 旧菜单换模型时清除不兼容的显式档位
      const m = (o.models || []).find(x => x.id === (b.model || (o.current || {}).model));
      if (b.effort && m && !m.efforts.includes(b.effort)) delete b.effort;
    }
    Object.keys(b).forEach(k => { if (!b[k]) delete b[k]; });
    let saved;
    try { saved = await api("/api/brain", { agent: a.id, ...b }); }
    catch { toast("保存失败，请检查本地服务", "close"); return false; }
    if (!saved) return false;
    o.chosen = saved.chosen || b;
    const e = effOf(o);
    a.brain = { ...(a.brain || {}), set: !!(b.model || b.effort), model: e.model, effort: e.eff,
      label: [(e.m && e.m.name) || e.model || "默认", (o.names || {})[e.eff] || e.eff].filter(Boolean).join(" "),
      name: (e.m && e.m.name) || e.model || "默认", effort_name: (o.names || {})[e.eff] || e.eff || "" };
    painted.hero = null; if (route.page === "hero") pHero();
    if (route.page === "chat") paintChat();
    return true;
  }
  // 拖动条：按住拖，松手吸附到最近一档
  let capDrag = null;
  const capIndex = (el, x) => { const r = el.getBoundingClientRect(), n = +getComputedStyle(el).getPropertyValue("--n"); return Math.max(0, Math.min(n - 1, Math.round((x - r.left - 17) / Math.max(1, r.width - 34) * (n - 1)))); };
  document.addEventListener("pointerdown", e => {
    const c = e.target.closest(".slide"); if (!c) return;
    e.preventDefault(); c.setPointerCapture(e.pointerId);
    capDrag = { el: c, i: capIndex(c, e.clientX) }; c.classList.add("drag"); capShow(c, capDrag.i);
  });
  document.addEventListener("pointermove", e => { if (!capDrag) return; const i = capIndex(capDrag.el, e.clientX); if (i !== capDrag.i) { capDrag.i = i; capShow(capDrag.el, i); } });
  document.addEventListener("pointerup", async () => {
    if (!capDrag) return;
    const { el, i } = capDrag; capDrag = null; el.classList.remove("drag");
    const a = A(el.dataset.cap), pop = $("#pop");
    await setBrain(a, { effort: el.dataset.effs.split(",")[i] });
    if (pop && pop.redraw) pop.redraw("main");
  });
  function capShow(el, i) { // 拖的时候顶上的字跟着变
    el.style.setProperty("--i", i);
    const e = el.dataset.effs.split(",")[i], o = (ui.brains || {})[el.dataset.cap] || {}, h = $("#pop .bp-head b");
    if (h) h.textContent = (o.names || {})[e] || e;
  }
  document.addEventListener("click", async e => {
    const bm = e.target.closest("[data-bmenu]"), cn = e.target.closest("[data-clone-new]"), cx = e.target.closest("[data-clone-x]"), ax = e.target.closest("[data-api-x]");
    if (!bm && !cn && !cx && !ax) return;
    if (ax) {
      e.preventDefault(); e.stopPropagation();
      const a = A(ax.dataset.apiX);
      if (await askBox(`解雇 ${a.name}？`, "它和它的分身会离开队伍；供应商账户不动", "解雇", true) && await api("/api/api-agent/remove", { agent: a.id })) { toast(`${a.name} 离开了`, "team"); location.hash = "#team"; }
      return;
    }
    e.preventDefault(); e.stopPropagation();
    if (bm) {
      const a = A(bm.dataset.bmenu), k = "brain:" + a.id;
      if (ui.qcClosed && ui.qcClosed.id === k && Date.now() - ui.qcClosed.t < 500) return (ui.qcClosed = null);
      return brainMenu(bm, a);
    }
    if (cn) {
      const a = A(cn.dataset.cloneNew), r = await api("/api/clone", { base: a.id });
      if (r && r.id) { toast(`${a.name} 分身成功！`, "star"); if (route.page === "hero") setTimeout(() => (location.hash = "#hero/" + encodeURIComponent(r.id)), 300); }
    } else if (cx) {
      const a = A(cx.dataset.cloneX);
      if (!(await askBox(`解散 ${a.name}？`, "它的工位、职能和大脑设置会一起清掉", "解散", true))) return;
      if (await api("/api/clone/remove", { agent: a.id })) { toast(`${a.name} 解散了`, "team"); if (route.page === "hero") location.hash = "#hero/" + encodeURIComponent(a.clone_of); }
    }
  }, true);

  // 改职能：本地立刻生效（不等服务器），后台再同步；同步期间服务推来的旧数据不覆盖
  function setJobs(a, changes) {
    for (const [k, v] of Object.entries(changes)) { if (v) a.jobs[k] = v; else delete a.jobs[k]; }
    ui.lock = { id: a.id, jobs: { ...a.jobs }, until: Date.now() + 2500 };
    painted.hero = null; pHero();
    Promise.all(Object.entries(changes).map(([key, level]) => api("/api/assign", { agent: a.id, key, level: level || "clear" })));
  }
  function catCard(a, c, open) {
    const jobs = a.jobs || {}, whole = jobs["cat:" + c.id];
    const tags = c.tags.map(t => {
      const own = jobs["tag:" + t], eff = own || whole;
      const st = own ? own : whole ? "inherit" : "none";
      if (!open && !own && !whole) return "";
      const holders = S.agents.filter(x => x.id !== a.id && (x.jobs["tag:" + t] === "prefer" || (!x.jobs["tag:" + t] && x.jobs["cat:" + c.id] === "prefer")));
      return `<button class="tchip ${st} ${eff || ""}" data-tag="${esc(t)}" data-cat="${esc(c.id)}" title="${holders.length ? "主力还有：" + esc(holders.map(h => h.name).join("、")) : ""}">
        ${eff === "prefer" ? icon("star") : eff === "ok" ? icon("sub") : eff === "avoid" ? icon("ban") : icon("plus")}${esc(t)}${st === "inherit" ? "<em>整类</em>" : ""}</button>`;
    }).join("");
    return `<div class="slot ${open ? "open" : ""}" data-cat="${esc(c.id)}"><div class="slot-h"><span class="cat-ic">${CAT_IC[c.id] || "✨"}</span><b>${esc(c.name)}</b>
      <div class="seg whole">${[["", "单选词条"], ["prefer", "整类装配"]].map(([v, t]) => `<button class="${(whole ? "prefer" : "") === v ? "on" : ""}" data-whole="${v}" data-cat="${esc(c.id)}">${t}</button>`).join("")}</div>
      <button class="btn sm ghost" data-clear="${esc(c.id)}" title="卸下这一类的所有词条">清空</button>
      <button class="ib" data-toggle="${esc(c.id)}" title="${open ? "收起" : "展开全部词条"}">${icon(open ? "collapse" : "down")}</button></div>
      <div class="tchips">${tags || `<span class="small muted">点右边 ▾ 展开，选具体词条</span>`}</div>
      ${open ? `<p class="small muted hint">点词条切换：未装备 → 主力 → 替补${whole ? " → 禁用（整类里排除这一项）" : ""} → 未装备</p>` : ""}</div>`;
  }
  document.addEventListener("click", async e => {
    if (route.page !== "hero") return;
    const a = A(route.id);
    const tile = e.target.closest(".cat-tile"), tg = e.target.closest("[data-toggle]"), wh = e.target.closest("[data-whole]"), tc = e.target.closest(".tchip"), qa = e.target.closest("[data-qa-go]");
    if (qa) { ui.qAgent = qa.dataset.qaGo; return; }
    if (tile) { ui.openCat = tile.dataset.cat; painted.hero = null; await pHero(); $(`.slot[data-cat="${CSS.escape(tile.dataset.cat)}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" }); return; }
    if (tg) { ui.openCat = ui.openCat === tg.dataset.toggle ? null : tg.dataset.toggle; painted.hero = null; pHero(); return; }
    const cl = e.target.closest("[data-clear]");
    if (wh) { const v = wh.dataset.whole; ui.openCat = wh.dataset.cat; setJobs(a, { ["cat:" + wh.dataset.cat]: v || null }); return; }
    if (cl) {
      const c = CAT.categories.find(x => x.id === cl.dataset.clear);
      const ch = { ["cat:" + c.id]: null }; c.tags.forEach(t => { if (a.jobs["tag:" + t]) ch["tag:" + t] = null; });
      ui.openCat = c.id; setJobs(a, ch); toast(`已清空「${c.name}」`, "skills"); return;
    }
    if (tc) {
      const t = tc.dataset.tag, whole = a.jobs["cat:" + tc.dataset.cat], own = a.jobs["tag:" + t];
      const order = whole ? [undefined, "prefer", "ok", "avoid"] : [undefined, "prefer", "ok"];
      const next = order[(order.indexOf(own) + 1) % order.length];
      ui.openCat = tc.dataset.cat;
      setJobs(a, { ["tag:" + t]: next || null });
    }
  });

  // ---------- 任务 ----------
  async function pQuests() {
    shell(`<div class="head"><div><h1>任务</h1><p>每次派活都是一个任务：谁派的、谁接的、用了多久、花了多少。点开看完整交代和回复。</p></div>
      <div class="act"><label class="search">${icon("search")}<input id="q-q" placeholder="搜任务内容" value="${esc(ui.q)}"></label></div></div>
      <div class="filters" id="q-f" style="margin-bottom:14px"></div><div id="q-run"></div><div class="card ql" id="q-list"></div>`);
    $("#q-q").oninput = e => { ui.q = e.target.value.trim(); painted.qlist = null; pQuests(); };
    const list = await quests();
    const agents = [...new Set(list.map(q => q.agent))];
    part("qf", $("#q-f"), `<button class="fchip all ${ui.qAgent ? "" : "on"}" data-qa="">全部</button>` + agents.map(id => `<button class="fchip ${ui.qAgent === id ? "on" : ""}" data-qa="${esc(id)}">${avatar(A(id), 22)}${esc(A(id).name)}</button>`).join(""));
    part("qrun", $("#q-run"), S.running.length ? `<div class="card" style="margin-bottom:14px"><div class="card-h"><h2>进行中</h2></div><div class="card-b">${S.running.map(r => { const a = A(r.agent); return `<div class="q">${ringAv({ ...a, state: "working" }, 36)}<div class="q-main"><div class="q-top"><b>${esc(a.name)}</b><span class="t" data-since="${r.start}"></span></div><div class="q-task" data-notr>${esc(r.task)}</div>${a.acts[0] ? `<div class="q-act">${esc(a.acts[0].plain)}</div>` : ""}</div></div>`; }).join("")}</div></div>` : "");
    const shown = list.filter(q => (!ui.qAgent || q.agent === ui.qAgent) && (!ui.q || ((q.task || "") + (q.reply || "")).includes(ui.q)));
    part("qlist", $("#q-list"), shown.length ? shown.map(q => {
      const a = A(q.agent), open = ui.openRun === q.run;
      return `<div class="qr" data-run="${esc(q.run)}">${avatar(a, 40)}<div style="min-width:0"><div class="tt" data-notr>${esc((q.task || "").split("\n")[0])}</div>
        <div class="meta"><span>${esc(A(q.frm).name)} → ${esc(a.name)}</span>${q.tag ? `<span class="pill">${esc(q.tag)}</span>` : ""}${q.batch ? `<span class="pill">${esc(q.batch)}</span>` : ""}</div></div>
        <div class="right"><span>${esc((q.t || "").slice(5, 16))}</span>${q.done ? `<span>${mins(q.seconds)}${costText(q.cost) ? " · " + costText(q.cost) : ""}</span>` : `<span class="pill hp">进行中</span>`}</div></div>
        ${open ? `<div class="qd"><div class="blk"><b>交代</b>${U(q.task)}</div>${q.reply ? `<div class="blk"><b>回复</b>${U(q.reply)}</div>` : ""}</div>` : ""}`;
    }).join("") : `<div class="empty"><div class="big">📜</div>${S.project_key ? "这个项目还没有任务记录" : "通用工作区还没有任务记录"}</div>`);
  }
  document.addEventListener("click", e => {
    if (route.page !== "quests") return;
    const f = e.target.closest("[data-qa]"); if (f) { ui.qAgent = f.dataset.qa; painted.qf = painted.qlist = null; pQuests(); return; }
    const r = e.target.closest(".qr"); if (r) { ui.openRun = ui.openRun === r.dataset.run ? null : r.dataset.run; painted.qlist = null; pQuests(); }
  });

  // ---------- 办公室 ----------
  // 每个在用 agentbus 的 agent 一间办公室（Claude 办公室、GPT 办公室…）。里面一个对话窗口一组（组名 = 那个窗口的名字，带最后使用时间）；
  // 每间有自己的主题（全景背景 + 长条共用桌）和战备预设组（这位主位派活时从哪些队员里挑、各自主力干什么）。
  const themeOf = o => (S.art.themes || []).find(t => t.name === o.theme) || (S.art.themes || [])[0] || null;
  const ago = t => {
    if (!t) return "—";
    const m = Math.round((Date.now() / 1000 - t) / 60), d = new Date(t * 1000);
    return m < 1 ? "刚刚" : m < 60 ? `${m} 分钟前` : m < 720 ? `${Math.round(m / 60)} 小时前` : `${d.getMonth() + 1}-${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  };
  const groupName = g => g.direct ? "直接在用" : g.title;
  const groupIds = (o, g) => { const ids = g.active ? [...g.members] : [o.agent, ...g.workers]; return [...new Set(ids)].sort((x, y) => (x === o.agent ? -1 : y === o.agent ? 1 : 0)).slice(0, 6); };
  // 一组人坐在主题长桌后面：只露出人（立绘格 y 150～768），前面一张拉长的共用桌
  function deskRow(o, g, w) {
    const t = themeOf(o), ids = groupIds(o, g);
    const fig = id => {
      const a = A(id), st = g.active ? a.state : "off", cell = st === "working" || st === "queued" ? 0 : st === "off" ? 2 : 1;
      return a.char ? `<i class="dr-f ${g.active && st === "working" ? "w" : ""}" style="background-image:url(${esc(a.char)});background-position:${cell * 50}% 36.9%" title="${esc(a.name)} · ${esc((STATE[st] || {}).t || "")}"></i>`
        : `<span class="dr-a">${avatar(a, Math.round(w * .7))}</span>`;
    };
    const v = t && t.desk_view, dim = (t && t.desk_dim) || [1536, 1024];
    const desk = t && v ? `<div class="dr-desk" style="background-image:url(${esc(t.desk)});background-size:${dim[0] / v[2] * 100}% ${dim[1] / v[3] * 100}%;background-position:${v[0] / (dim[0] - v[2]) * 100}% ${v[1] / (dim[1] - v[3]) * 100}%"></div>` : "";
    return `<div class="drow" style="--w:${w}px">${ids.map(fig).join("")}${desk}</div>`;
  }
  const sceneBg = o => { const t = themeOf(o); return t ? `style="background-image:url(${esc(t.bg)})"` : ""; };
  function pOffice() {
    const list = S.offices || [];
    if (route.id) return pOfficeOne(list.find(o => o.id === route.id));
    shell(`<div class="head"><div><h1>办公室</h1><p>每个在用 agentbus 的 agent 一间办公室。同一个 agent 开了几个对话窗口，里面就分几组；每间有自己的战备预设组，点进去调。</p></div></div><div id="offs"></div>`);
    part("offs", $("#offs"), list.length ? `<div class="offgrid">${list.map(o => {
      const act = o.groups.filter(g => g.active), show = act[0] || o.groups[0];
      return `<a class="card offc ${o.active ? "on" : ""}" href="#office/${encodeURIComponent(o.id)}">
        <div class="off-scene" ${sceneBg(o)}>${show ? deskRow(o, show, 66) : ""}<span class="off-th">${esc(o.theme || "")}</span></div>
        <div class="offc-h">${avatar(A(o.agent), 36)}<div><b>${esc(o.name)}</b><span>${act.length ? `${act.length} 个窗口在干活` : "现在没人"} · 近 7 天 ${o.groups.length} 个窗口</span></div>${icon("chev")}</div>
        <div class="offc-f">今天派活 ${o.today} 单 · 战备 ${o.lineup.office.length} 人${o.lineup.custom ? "（已调整）" : ""}</div></a>`;
    }).join("")}</div>` : `<div class="card"><div class="empty"><div class="big">🏢</div>现在没有 agent 在用 agentbus<br><span class="small">在 Claude、GPT 这些 agent 里接入 agentbus 后，每个都会有自己的办公室</span></div></div>`);
    // 新手入口：把 agentbus 接进各个 agent（注册 MCP），装一次，以后每个新对话都会在悬浮窗问要不要接入
    const can = S.agents.filter(a => a.installed && !a.api && !a.clone_of && a.billing !== "manual" && ["claude", "codex", "opencode", "zcode", "gemini", "codebuddy", "qwen", "kimi", "cursor"].includes(a.id));
    const mode = (S.settings || {}).auto_join || "ask";
    if (!$("#join-card")) $("#offs").insertAdjacentHTML("afterend", `<div class="card" id="join-card" style="margin-top:16px"></div>`);
    part("joincard", $("#join-card"), `<div class="card-h"><h2>让 agent 接入 agentbus</h2></div><div class="card-b">
      <p class="small muted" style="margin:0 0 10px">每个 agent 点一次就行。接好以后，你在它里面开新对话，悬浮窗会问这个窗口要不要接入 bus，不用每次先说“接入 agentbus”。</p>
      <div class="jc-row">${can.map(a => `<button class="btn sm" data-join-agent="${esc(a.id)}">${avatar(a, 20)}接入 ${esc(a.name)}</button>`).join("")}</div>
      <label class="lu-sel" style="margin-top:12px">新对话<select data-autojoin>${[["ask", "每次问我"], ["always", "总是自动接入"], ["never", "从不（要用时自己说）"]].map(([k, t]) => `<option value="${k}" ${mode === k ? "selected" : ""}>${t}</option>`).join("")}</select></label></div>`);
  }
  function lineupHTML(o) {
    const L = o.lineup, cats = CAT ? CAT.categories : [];
    // 主力可以是大类（cat:视觉设计）也可以是具体岗位（tag:录屏）：具体岗位也列进下拉框
    const prefer = id => { const j = (L.jobs || {})[id] || {}; const k = Object.keys(j).find(x => j[x] === "prefer" && x.startsWith("cat:")) || Object.keys(j).find(x => j[x] === "prefer"); return k || ""; };
    const pool = S.agents.filter(a => a.installed && a.billing !== "manual" && a.id !== "claude" && a.clone_of !== "claude" && !L.office.includes(a.id));
    const rows = L.office.map(id => { const a = A(id); return `<div class="lu-r">${avatar(a, 34)}<div><b>${esc(a.name)}</b><span>${esc(a.vendor || "")}${a.brain && a.brain.label ? " · " + esc(a.brain.label) : ""}</span></div>
      <label class="lu-sel">主力<select data-lu-prefer="${esc(id)}"><option value="">—</option>${prefer(id).startsWith("tag:") ? `<option value="${esc(prefer(id))}" selected>${esc(prefer(id).slice(4))}</option>` : ""}${cats.map(c => `<option value="cat:${esc(c.id)}" ${prefer(id) === "cat:" + c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></label>
      <button class="ib" data-lu-x="${esc(id)}" title="从这间办公室的战备里拿掉">${icon("close")}</button></div>`; }).join("");
    return `<div class="card-h"><h2>战备预设组</h2><span class="pill ${L.custom ? "accent" : ""}">${L.custom ? "已调整" : "跟项目阵容一样"}</span>
        ${L.custom ? `<button class="btn sm ghost" data-lu-reset style="margin-left:auto">恢复默认</button>` : ""}</div>
      <div class="card-b"><p class="small muted" style="margin:0 0 10px">${esc(A(o.agent).name)} 派活、按岗位自动挑人时只从这里挑。最多 6 位。</p>${rows || `<div class="empty">还没有队员</div>`}
        ${L.office.length < 6 && pool.length ? `<label class="lu-add">${icon("plus")}<select data-lu-add><option value="">添加队员…</option>${pool.map(a => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("")}</select></label>` : ""}</div>`;
  }
  // 经验包：装到这间办公室（这位主位派活都带上），或者装到某个窗口的项目文件夹（在那个文件夹里派活都带上）
  const packName = id => { const p = (S.packs || []).find(x => x.id === id); return p ? `${p.name}${p.version ? " v" + p.version : ""}` : id.split("/").pop(); };
  const mountsOf = q => (S.pack_mounts || []).filter(m => Object.entries(q).every(([k, v]) => m[k] === v)).map(m => m.pack);
  const packIcon = id => { const p = (S.packs || []).find(x => x.id === id); return p && p.kind === "skill" ? "🧩" : "📦"; };
  function packsHTML(o) {
    const on = mountsOf({ office: o.id }), dirs = [...new Set(o.groups.map(g => g.cwd).filter(Boolean))];
    const rows = on.map(id => `<div class="pk"><span>${packIcon(id)}</span><b>${esc(packName(id))}</b><em>整间办公室</em><button class="ib" data-pk-off="${esc(id)}" data-pk-office="${esc(o.id)}" title="卸下">${icon("close")}</button></div>`).join("")
      + dirs.map(d => mountsOf({ dir: d }).map(id => `<div class="pk"><span>${packIcon(id)}</span><b>${esc(packName(id))}</b><em title="${esc(d)}">📁 ${esc(d.split("/").pop())}</em><button class="ib" data-pk-off="${esc(id)}" data-pk-dir="${esc(d)}" title="卸下">${icon("close")}</button></div>`).join("")).join("");
    return `<div class="card-h"><h2>经验包 / Skill</h2><a class="more" href="#playbook">经验库 ${icon("chev")}</a></div><div class="card-b">
      <button class="btn primary pk-big" data-pk-open="${esc(o.id)}">📦 装经验包 / Skill</button>
      <p class="small muted" style="margin:10px 0 4px">装到整间办公室：${esc(A(o.agent).name)} 派活都会带上；也可以只装到某个窗口的项目文件夹。</p>
      ${rows || `<div class="empty small">还没装</div>`}</div>`;
  }
  // 装经验包 / Skill 的弹窗：先选装到哪，再从经验库里挑（能搜）
  function packPicker(o) {
    const dirs = [...new Set(o.groups.map(g => g.cwd).filter(Boolean))];
    const st = { target: JSON.stringify({ office: o.id }), q: "" };
    $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><div class="modal pkm"><div class="pkm-h"><h2>给 ${esc(o.name)} 装经验包 / Skill</h2><button class="ib" data-pkm-x>${icon("close")}</button></div>
      <div class="pkm-bar"><label class="lu-sel">装到<select data-pkm-target><option value='${esc(JSON.stringify({ office: o.id }))}'>整间办公室（${esc(A(o.agent).name)} 派活都带上）</option>
        ${dirs.map(d => `<option value='${esc(JSON.stringify({ dir: d }))}'>📁 ${esc(d.split("/").pop())}（只在这个文件夹里）</option>`).join("")}</select></label>
        <label class="search">${icon("search")}<input data-pkm-q placeholder="搜名字或说明"></label></div><div class="pkm-list"></div></div></div>`;
    const draw = () => {
      const t = JSON.parse(st.target), q = st.q.toLowerCase(), have = new Set(mountsOf(t));
      const items = (S.packs || []).filter(p => !q || (p.name + " " + (p.desc || "")).toLowerCase().includes(q));
      const groups = [["经验包", items.filter(p => p.kind !== "skill")], ...["Claude", "GPT", "通用"].map(w => [`${w} 的 Skill`, items.filter(p => p.kind === "skill" && p.owner === w)])];
      $(".pkm-list").innerHTML = groups.filter(([, l]) => l.length).map(([h, l]) => `<div class="pkm-g">${esc(h)}<span>${l.length}</span></div>` + l.map(p => `<div class="pkm-r">
          <span class="pkm-i">${p.kind === "skill" ? "🧩" : "📦"}</span><div><b>${esc(p.name)}${p.version ? " v" + esc(p.version) : ""}</b><span>${esc(p.desc || "")}</span></div>
          ${have.has(p.id) ? `<button class="btn sm ghost" data-pkm-off="${esc(p.id)}">已装 · 卸下</button>` : `<button class="btn sm primary" data-pkm-on="${esc(p.id)}">装上</button>`}</div>`).join("")).join("")
        || `<div class="empty">没找到</div>`;
    };
    draw();
    const close = () => ($("#layer").innerHTML = "");
    $("#sheet-bg").onclick = async e => {
      if (e.target.id === "sheet-bg" || e.target.closest("[data-pkm-x]")) return close();
      const on = e.target.closest("[data-pkm-on]"), off = e.target.closest("[data-pkm-off]");
      if (on || off) {
        await api("/api/pack/mount", { ...JSON.parse(st.target), pack: (on || off).dataset[on ? "pkmOn" : "pkmOff"], on: !!on });
        if (on) toast("装好了，派活时会带上", "star");
        setTimeout(draw, 600);
      }
    };
    $("[data-pkm-target]").onchange = e => { st.target = e.target.value; draw(); };
    $("[data-pkm-q]").oninput = e => { st.q = e.target.value; draw(); };
  }
  function pOfficeOne(o) {
    ui.officeView ??= (() => { try { return localStorage.getItem("officeView") || "cards"; } catch { return "cards"; } })();
    shell(`<div class="head"><div><a class="back-lnk" href="#office">${icon("chev", "flip")}全部办公室</a><h1 id="of-t"></h1><p id="of-p"></p></div><div class="act" id="of-act"></div></div>
      <div class="of-grid"><div id="of-g"></div><div class="of-side"><div class="card" id="of-lu"></div><div class="card" id="of-pk"></div></div></div>`);
    if (!o) { part("of-t", $("#of-t"), "办公室"); part("of-g", $("#of-g"), `<div class="card"><div class="empty"><div class="big">🏢</div>这间办公室近 7 天没人用</div></div>`); return; }
    part("of-t", $("#of-t"), esc(o.name));
    part("of-p", $("#of-p"), `${o.groups.filter(g => g.active).length} 个窗口在干活 · 近 7 天 ${o.groups.length} 个窗口 · 今天派活 ${o.today} 单`);
    part("of-act", $("#of-act"), `<label class="lu-sel">主题<select data-theme-sel="${esc(o.id)}">${(S.art.themes || []).map(t => `<option ${t.name === o.theme ? "selected" : ""}>${esc(t.name)}</option>`).join("")}</select></label>
      <div class="seg">${[["cards", "卡片"], ["list", "列表"]].map(([k, t]) => `<button class="${ui.officeView === k ? "on" : ""}" data-oview="${k}">${t}</button>`).join("")}</div>`);
    part("of-lu", $("#of-lu"), lineupHTML(o));
    part("of-pk", $("#of-pk"), packsHTML(o));
    const status = g => g.active ? `<span class="pill hp">在干活</span>` : `<span class="pill">${ago(g.last)}</span>`;
    part("of-g", $("#of-g"), ui.officeView === "list"
      ? `<div class="card ogl"><div class="ogl-h"><span></span><span>窗口</span><span>最后使用</span><span>谁在里面</span><span>派活</span><span></span></div>${o.groups.map(g => `<div class="ogl-r ${g.active ? "on" : ""}">
          <i class="dot"></i><b title="${esc(groupName(g))}">${esc(groupName(g))}</b><span>${g.active ? "现在" : ago(g.last)}</span>
          <span class="avs">${groupIds(o, g).map(id => avatar(A(id), 22)).join("")}</span><span>${g.jobs} 单</span>${status(g)}</div>`).join("")}</div>`
      : `<div class="ogcards">${o.groups.map(g => {
          const rows = g.active ? groupIds(o, g).map(id => { const a = A(id), r = S.running.find(x => x.agent === id && x.room === g.id);
            const act = r ? (r.queued ? "排队：" + r.queued : r.task) : a.state === "working" ? ((a.acts[0] || {}).plain || "在干活") : id === o.agent && g.members.length > 1 ? "等队员交活" : (STATE[a.state] || {}).t;
            return `<div class="ogr">${avatar(a, 26)}<div><b>${id === o.agent ? "👑 " : ""}${esc(a.name)}</b><span>${esc(act || "")}</span></div>${r ? `<span class="pill hp"><span data-since="${r.start}"></span></span>` : ""}</div>`; }).join("") : "";
          return `<div class="card ogcard ${g.active ? "on" : ""}"><div class="og-scene" ${sceneBg(o)}>${deskRow(o, g, 70)}</div>
            <div class="card-h"><h2 title="${esc(groupName(g))}">${esc(groupName(g))}</h2>${status(g)}</div>
            <div class="card-b"><div class="small muted">最后使用 ${g.active ? "现在" : ago(g.last)} · 派活 ${g.jobs} 单${g.cwd ? ` · 📁 ${esc(g.cwd.split("/").pop())}${mountsOf({ dir: g.cwd }).length ? "（装了经验包）" : ""}` : ""}</div>${rows}</div></div>`;
        }).join("")}</div>`);
  }
  document.addEventListener("click", async e => {
    if (route.page !== "office") return;
    const v = e.target.closest("[data-oview]");
    if (v) { ui.officeView = v.dataset.oview; try { localStorage.setItem("officeView", ui.officeView); } catch {} painted["of-act"] = painted["of-g"] = null; return pOffice(); }
    const po = e.target.closest("[data-pk-open]");
    if (po) return packPicker((S.offices || []).find(y => y.id === po.dataset.pkOpen));
    const x = e.target.closest("[data-lu-x]"), o = (S.offices || []).find(y => y.id === route.id);
    if (x && o) await api("/api/office/lineup", { office: o.id, members: o.lineup.office.filter(id => id !== x.dataset.luX) });
    if (e.target.closest("[data-lu-reset]") && o && await api("/api/office/lineup", { office: o.id, reset: true })) toast("恢复成默认阵容了", "team");
    const pk = e.target.closest("[data-pk-off]");
    if (pk && await api("/api/pack/mount", { pack: pk.dataset.pkOff, office: pk.dataset.pkOffice || undefined, dir: pk.dataset.pkDir || undefined, on: false })) toast("经验包卸下了", "close");
  });
  document.addEventListener("click", async e => {
    const b = e.target.closest("[data-join-agent]"); if (!b) return;
    const r = await api("/api/connect-lead", { agent: b.dataset.joinAgent });
    if (!r) return;
    if (r.manual) {
      $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><div class="modal"><h2>接入 ${esc(r.name)}</h2><p class="muted small">${esc(r.name)} 不支持一键接入。${esc(r.where)}：</p><pre class="cmd">${esc(r.snippet)}</pre><div style="text-align:right"><button class="btn primary" data-jc-x>好</button></div></div></div>`;
      $("#sheet-bg").onclick = ev => { if (ev.target.id === "sheet-bg" || ev.target.closest("[data-jc-x]")) $("#layer").innerHTML = ""; };
    } else toast(r.already ? `${r.name} 早就接好了` : `${r.name} 接好了，下次开新对话就会问你`, "check");
  });
  document.addEventListener("click", async e => { // 更新
    const go = e.target.closest("[data-updgo]"); if (go && go.dataset.updgo) return api("/api/open-url", { url: go.dataset.updgo });
    if (e.target.closest("[data-updhide]")) { ui.updHide = true; painted.updb = null; return paint(); }
    if (!e.target.closest("[data-updcheck]")) return;
    const r = await api("/api/update?fresh=1"), box = $("#upd-res"); if (!r || !box) return;
    box.hidden = false;
    box.innerHTML = r.error ? `<span class="small muted">${esc(r.error)}</span>` : r.available
      ? `<div class="lbl"><b>🎉 有新版本 ${esc(r.latest)}（现在 ${esc(r.current)}）</b><span style="white-space:pre-wrap">${esc(r.notes || "")}</span></div>
         <div class="row-end"><span class="small muted">源码装的：<span class="code-inline">cd ~/agentbus && git pull</span></span><button class="btn primary sm" data-updgo="${esc(r.url || "")}">去下载</button></div>`
      : `<span class="small muted">已经是最新版（${esc(r.current)}）</span>`;
  });
  document.addEventListener("change", async e => {
    if (e.target.matches("[data-updrepo]")) { if (await api("/api/settings", { update_repo: e.target.value })) toast("仓库记住了，点“现在检查”试试", "check"); return; }
    if (e.target.matches("[data-chatset]")) { const k = e.target.dataset.chatset, v = k === "duty" ? e.target.value : Number(e.target.value); if (await api("/api/settings", { chat: { [k]: v } })) toast("聊天室规则改好了", "check"); return; }
    if (e.target.matches("[data-autojoin]")) { await api("/api/settings", { auto_join: e.target.value }); toast("记住了", "check"); return; }
    if (route.page !== "office") return;
    const o = (S.offices || []).find(y => y.id === route.id); if (!o) return;
    const t = e.target;
    if (t.matches("[data-lu-add]") && t.value) { await api("/api/office/lineup", { office: o.id, members: [...o.lineup.office, t.value] }); toast(`${A(t.value).name} 进了 ${o.name} 的战备`, "star"); }
    if (t.matches("[data-lu-prefer]")) await api("/api/office/lineup", { office: o.id, prefer: { [t.dataset.luPrefer]: t.value } });
    if (t.matches("[data-theme-sel]")) await api("/api/office/theme", { office: o.id, theme: t.value });
    if (t.matches("[data-pk-add]") && t.value && await api("/api/pack/mount", { ...JSON.parse(t.value), on: true })) toast("经验包装好了，派活时会带上", "star");
  });

  // ---------- 聊天室 ----------
  // 像工作群一样。派活模式：@ 谁谁干活，回复自动发回群里，回复里再 @ 别人就接力（最多 4 次）；没 @ 人就交给值班的。
  // 群聊模式（头脑风暴）：主人一句话，成员按顺序挨个读群里的内容接着聊，有轮数、条数、字数、重复、跳过的限制，随时能停。
  // 上下文 = 压缩要点 + 最近的消息（8000 字以内），太长了后台自动压缩。附件：选文件 / 粘贴图片 / 拖进来，给队员的是本地完整路径。
  // 设计借鉴 agentchattr（MIT）；叫醒 agent 走 agentbus 自己的派活，不需要主位。基地右侧的小聊天框用的是同一套。
  ui.chat = { channel: (() => { try { return localStorage.getItem("chatChannel") || "大厅"; } catch { return "大厅"; } })(), data: null };
  const drafts = new Map();
  function chatDraft(channel = ui.chat.channel) {
    if (!drafts.has(channel)) drafts.set(channel, { text: "", entries: [], sending: false });
    return drafts.get(channel);
  }
  const mentionable = () => S.agents.filter(a => a.installed && a.billing !== "manual" && !a.sealed); // 封印的 Claude 不进聊天室
  const chatName = id => id === "user" ? "我" : id === "agentbus" ? "agentbus" : A(id).name;
  const chatText = t => esc(t).replace(/@(所有人|[\w一-龥·.-]+)/g, '<b class="at">@$1</b>').replace(/\n/g, "<br>");
  const fileHTML = f => f.image && (f.uploaded || f.path.replace(/\\/g, "/").includes("/.agentbus/chat/files/"))
    ? `<a class="cf img" href="/api/chat/file?p=${encodeURIComponent(f.path)}" target="_blank" title="${esc(f.path)}"><img src="/api/chat/file?p=${encodeURIComponent(f.path)}" alt=""></a>`
    : `<span class="cf" title="${esc(f.path)}">📎 ${esc(f.name || f.path.split(/[\\/]/).pop())}<em>${esc(f.path)}</em></span>`;
  const jobText = (m, mini) => { const j = m.task_state; if (!j) return m.text;
    if (!mini && ["starting", "queued", "running"].includes(j.status)) return m.text;
    const mark = { ok: "✅", failed: "❌", error: "❌", stopped: "⏹", stalled: "⏹", interrupted: "⚠️", queued: "⏳", running: "▶" }[j.status] || "⏳";
    return `${mark} ${chatName(m.agent)}${m.round ? " · 第 " + m.round + " 轮" : ""} · ${j.label}${j.reason ? " · " + j.reason : j.progress ? " · " + j.progress.detail : ""}`;
  };
  const msgHTML = (m, mini) => m.system ? `<div class="cm sys">${chatText(jobText(m, mini))}</div>`
    : `<div class="cm ${m.frm === "user" ? "me" : ""} ${m.talk ? "talk" : ""}">${m.frm === "user" ? "" : avatar(A(m.frm), mini ? 24 : 32)}<div class="cb"><div class="cn">${esc(chatName(m.frm))}${m.talk ? `<em>群聊</em>` : ""}<span>${new Date(m.t * 1000).toTimeString().slice(0, 5)}</span></div>
        <div class="ct" data-notr>${chatText(mini ? m.text.slice(0, 300) + (m.text.length > 300 ? "…" : "") : m.text)}${(m.files || []).length ? `<div class="cfs">${m.files.map(fileHTML).join("")}</div>` : ""}</div></div></div>`;
  let pendingChat = null;
  async function loadChat(force = true) {
    const channel = ui.chat.channel;
    if (!force && pendingChat?.channel === channel) return;
    const request = pendingChat = { channel };
    const r = await fetch(`/api/chat?channel=${encodeURIComponent(channel)}`).then(x => x.ok ? x.json() : null).catch(() => null);
    if (request !== pendingChat) return;
    pendingChat = null;
    if (!r) { ui.chatRev = null; return; } // 下一次状态推送时重试
    if (r.channel !== ui.chat.channel) return;
    ui.chat.data = r;
    if (route.page === "chat") paintChat();
    if (route.page === "base") paintMiniChat();
  }
  function pChat() {
    const mounting = !painted.__shell;
    shell(`<div class="head"><div><h1>聊天室</h1><p>@ 队员就是派活，做完的回复发回群里；切到“群聊”，说一句话队员会轮流接着聊。不需要主位。</p></div></div>
      <div class="chat"><aside class="ch-l" id="ch-l"></aside><section class="ch-m"><div class="ch-bar" id="ch-bar"></div><div class="ch-msgs" id="ch-msgs"></div>
        <div class="ch-run" id="ch-run"></div>
        <div class="ch-in" id="ch-in"><div class="ch-quick" id="ch-quick"></div>
          <div class="ch-box"><div class="ch-field"><div class="ch-att" id="ch-att"></div><textarea id="ch-text" rows="2"></textarea>
              <div class="ch-tools"><label class="ib ch-clip" title="加附件（也可以粘贴图片、把文件拖进来）">📎<input type="file" multiple id="ch-file" hidden></label><span class="small muted">Enter 发送 · Shift+Enter 换行 · 能粘贴图片、拖文件</span></div></div>
            <button class="btn primary" id="ch-send">${icon("chev")}发送</button></div>
          <div class="ch-pop" id="ch-pop" hidden></div></div></section></div>`);
    if (mounting) { $("#ch-text").value = chatDraft().text; paintAtt(); }
    if (!ui.chat.data || ui.chat.data.channel !== ui.chat.channel) loadChat(false); else paintChat();
  }
  function paintChat() {
    const d = ui.chat.data; if (!d || d.channel !== ui.chat.channel || !$("#ch-l")) return;
    const run = Array.isArray(S.running) ? S.running.filter(r => r.channel === d.channel ||
      (!r.channel && r.job && d.running_tasks?.some(t => t.job === r.job))) : (d.running_tasks || []);
    part("chl", $("#ch-l"), `<div class="ch-lh">频道<button class="ib" data-ch-new title="新建频道">${icon("plus")}</button></div>` +
      d.channels.map(c => `<button class="ch-c ${c.name === ui.chat.channel ? "on" : ""}" data-ch="${esc(c.name)}"># ${esc(c.name)}</button>`).join(""));
    const mt = d.meta || {}, u = d.usage || {}, talk = mt.mode === "talk";
    const pct = Math.min(100, Math.round((u.chars || 0) / (u.compress_at || 16000) * 100));
    const dutyTxt = !d.duty_now ? "没人回（只记录）" : `${A(d.duty_now).name}${!mt.duty || mt.duty === "auto" ? "（自动）" : ""}`;
    const rule = talk ? `你说一句，${esc((d.talk_members || []).map(x => A(x).name).join("、") || "成员")} 按顺序轮流接着聊，最多 ${mt.rounds || 2} 轮；只聊不动手，要人干活就 @ 他`
      : mt.handoff_mode === "single" ? "Claude 管需求与产品验收 → Codex 出方案与技术审核 → WorkBuddy 执行和修复；两方审核后汇总修改意见，再交执行"
      : `@ 谁谁干活，干完把结果发回群里；没 @ 人时交给 ${esc(dutyTxt)}`;
    part("chbar", $("#ch-bar"), `<div class="chb-top"><b># ${esc(d.channel)}</b>
        <button class="chb-mode ${talk ? "talk" : ""}" data-chset title="频道设置">${talk ? "💬 群聊" : "🛠 派活"}<span>切换 ${icon("down")}</span></button>
        <span class="ch-ctx" title="给队员的上下文：压缩要点 + 最近消息（${u.budget} 字以内）；没压缩的部分到 ${u.compress_at} 字会自动压缩">上下文<i><b style="width:${pct}%"></b></i>${Math.round((u.chars || 0) / 100) / 10}k${u.compressions ? ` · 压过 ${u.compressions} 次` : ""}</span>
        <button class="btn sm ghost" data-chcompress title="把旧消息压成要点">压缩</button>
        ${run.length || d.talking ? `<button class="btn sm danger" data-chstop>⏹ 停止</button>` : ""}
        <button class="ib" data-chset title="频道设置">${icon("settings")}</button></div>
      <div class="chb-rule">${rule}${d.cwd ? `<br>共同工作目录：${esc(d.cwd)}` : ""}</div>
      <div class="ch-brains">${S.agents.filter(a => a.installed && a.brain?.custom_model && (mt.members || []).includes(a.id)).map(a => `<div><span>${esc(a.name)}</span>${brainBtn(a)}</div>`).join("")}</div>`);
    const ta = $("#ch-text"); if (ta) ta.placeholder = talk ? "说一句话，大家轮流接着聊…（@ 某人 = 单独给他派活）" : "@ 队员派活，比如：@GPT 帮我找三张参考图…（不 @ 就交给值班的）";
    if (painted.chatMessages !== d.messages) {
      const box = $("#ch-msgs"), near = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
      if (part("chm", box, d.messages.length ? d.messages.map(m => msgHTML(m)).join("")
        : `<div class="empty"><div class="big">💬</div>还没有消息<br><span class="small">试试：@GPT 帮我找三张参考图；或者切到“群聊”问一句大家怎么看</span></div>`) && near) box.scrollTop = 1e9;
      painted.chatMessages = d.messages;
    }
    part("chr", $("#ch-run"), (d.talking ? `<span class="pill accent">💬 群聊进行中…</span>` : "") + run.map(r => {
      const detail = r.queued ? "排队中 · " + r.queued : r.progress?.detail || (r.worker ? "执行中" : "已接单，正在启动");
      return `<span class="pill ${r.queued ? "gold" : "hp"}">${avatar(A(r.agent), 16)}<b>${esc(A(r.agent).name)}</b><span class="ch-detail" role="status">${esc(detail)}</span>${Number.isFinite(r.start) ? `<time title="本棒已用时间" data-since="${r.start}"></time>` : ""}</span>`;
    }).join(""));
    part("chq", $("#ch-quick"), mentionable().map(a => `<button class="chip" data-at="${esc(a.name)}">${avatar(a, 20)}@${esc(a.name)}</button>`).join("") + `<button class="chip" data-at="所有人">@所有人</button>`);
    paintAtt();
  }
  const paintAtt = () => {
    const d = chatDraft(), el = $("#ch-att") || $("#mc-att"), send = $("#ch-send") || $("#mc-send");
    if (el) el.innerHTML = d.entries.map((a, i) => `<span class="cf">${a.result?.image ? "🖼" : "📎"} ${esc(a.name)}${a.state === "loading" ? ` <small role="status">上传中…</small>` : a.state === "error" ? ` <button data-attretry="${i}" title="${esc(a.error)}">上传失败，重试</button>` : ""}<button data-attx="${i}" title="移除附件"${d.sending ? " disabled" : ""}>×</button></span>`).join("");
    if (send) send.disabled = d.sending || d.entries.some(a => a.state !== "ready");
  };
  async function uploadEntry(a) {
    a.state = "loading"; paintAtt();
    try {
      const resp = await fetch("/api/chat/upload", { method: "POST", headers: { "X-Name": encodeURIComponent(a.name) }, body: a.file });
      const r = await resp.json();
      if (!resp.ok || !r.path) throw new Error(r.error || "附件没传上");
      a.result = r; a.state = "ready"; delete a.file;
    } catch (e) { a.error = e.message || "附件没传上"; a.state = "error"; }
    paintAtt();
  }
  function upload(file) {
    const d = chatDraft();
    if (d.sending) return;
    if (d.entries.length >= 10) return toast("每条消息最多可附 10 个文件", "close");
    const a = { file, name: file.name || "粘贴的图片.png" };
    d.entries.push(a); return uploadEntry(a);
  }
  async function sendChat(fromMini) {
    const channel = ui.chat.channel, d = chatDraft(), ta = $(fromMini ? "#mc-text" : "#ch-text");
    if (!ta || d.sending || d.entries.some(a => a.state !== "ready")) return;
    const original = d.text = ta.value, text = original.trim(), files = d.entries.map(a => a.result);
    if (!text && !files.length) return;
    d.sending = true; paintAtt();
    try {
      const r = await api("/api/chat/send", { channel, text: text || "（附件）", files });
      if (!r?.ok) return;
      d.entries = [];
      if (d.text === original) d.text = "";
      if (ui.chat.channel !== channel) return;
      const field = $("#ch-text") || $("#mc-text"); if (field) field.value = d.text;
      const pop = $("#ch-pop"); if (pop) pop.hidden = true;
      if (!r.jobs.length && !/@/.test(text) && ui.chat.data?.meta?.mode !== "talk" && !ui.chat.data?.meta?.duty) toast("发出去了（没 @ 人，也没值班的，所以没人回）", "check");
      loadChat();
    } catch { toast("未确认送达，草稿已保留，请先查看频道消息", "close"); }
    finally { d.sending = false; paintAtt(); }
  }
  function switchChannel(name) {
    ui.chat.channel = name;
    try { localStorage.setItem("chatChannel", name); } catch {}
    const field = $("#ch-text"); if (field) field.value = chatDraft().text;
    paintAtt(); painted.chl = painted.chm = painted.chbar = null; return loadChat();
  }
  function insertAt(name, fromMini) {
    const ta = $(fromMini ? "#mc-text" : "#ch-text"), v = ta.value, i = ta.selectionStart, before = v.slice(0, i).replace(/@[^\s@]*$/, "");
    chatDraft().text = ta.value = before + "@" + name + " " + v.slice(i); ta.focus(); const pop = $("#ch-pop"); if (pop) pop.hidden = true;
  }
  const chatSet = async kw => { if (!await api("/api/chat/settings", { channel: ui.chat.channel, ...kw })) return false; painted.chbar = null; loadChat(); return true; };
  // 频道设置：这个频道用来派活还是群聊；派活时没 @ 人谁来回；群聊时谁参加、聊几轮
  function chatSettings() {
    const d = ui.chat.data, mt = d.meta || {}, pool = mentionable();
    const st = { mode: mt.mode || "work", duty: mt.duty || "auto", members: [...(mt.members || [])], rounds: Number(mt.rounds || 2), project: mt.project || "" };
    const draw = () => {
      $("#cs-body").innerHTML = `
        <div class="cs-row"><div><b>共同工作项目</b><span>本频道的群聊和派单都使用这个项目文件夹</span></div>
          <select data-csproject><option value="">跟随当前项目</option>${(S.projects || []).map(p => `<option value="${esc(p)}" ${st.project === p ? "selected" : ""}>${esc(p)}</option>`).join("")}</select></div>
        <div class="cs-modes">${[["work", "🛠 派活", "@ 队员就是给他派活：他去干，干完把结果发回群里；他回复里 @ 别人就接力。适合布置任务。"],
          ["talk", "💬 群聊（头脑风暴）", "你说一句，几位队员按顺序读前面的内容轮流发言：补充、反驳、提建议，只聊不动手。适合讨论方案、出点子。"]]
          .map(([k, t, dsc]) => `<button class="cs-mode ${st.mode === k ? "on" : ""}" data-csmode="${k}"><b>${t}</b><span>${dsc}</span></button>`).join("")}</div>
        ${st.mode === "work" ? `<div class="cs-row"><div><b>没 @ 人时谁来回</b><span>自动 = 优先用便宜的 API 队员，没有就用主位办公室战备里的第一位（换电脑也能用）</span></div>
            <select data-csduty><option value="auto" ${st.duty === "auto" ? "selected" : ""}>自动（推荐）</option>${pool.map(a => `<option value="${esc(a.id)}" ${st.duty === a.id ? "selected" : ""}>${esc(a.name)}${a.api ? "（API）" : ""}</option>`).join("")}<option value="none" ${st.duty === "none" ? "selected" : ""}>没人回（只记录）</option></select></div>`
        : `<div class="cs-row col"><div><b>谁参加</b><span>不选 = 自动用主位办公室战备里的前 4 位。人越多、轮数越多，花的额度越多</span></div>
            <div class="cs-mem">${pool.map(a => `<button class="chip ${st.members.includes(a.id) ? "on" : ""}" data-csmem="${esc(a.id)}">${avatar(a, 20)}${esc(a.name)}</button>`).join("")}</div></div>
          <div class="cs-row"><div><b>聊几轮</b><span>每轮每人最多说一次；没人有新东西说时会提前结束</span></div>
            <div class="seg sm">${[1, 2, 3].map(n => `<button class="${st.rounds === n ? "on" : ""}" data-csround="${n}">${n} 轮</button>`).join("")}</div></div>`}
        <p class="small muted">字数、上下文、并发这些上限在 设置 → 聊天室 里调。</p>`;
    };
    $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><div class="modal csm"><h2>频道设置 · # ${esc(d.channel)}</h2><div id="cs-body"></div>
      <div class="cs-act"><button class="btn ghost" data-csx>取消</button><button class="btn primary" data-csok>保存</button></div></div></div>`;
    draw();
    $("#sheet-bg").onclick = async e => {
      const t = e.target;
      if (t.id === "sheet-bg" || t.closest("[data-csx]")) return ($("#layer").innerHTML = "");
      const m = t.closest("[data-csmode]"); if (m) { st.mode = m.dataset.csmode; return draw(); }
      const mm = t.closest("[data-csmem]"); if (mm) { const id = mm.dataset.csmem; st.members = st.members.includes(id) ? st.members.filter(x => x !== id) : [...st.members, id]; return draw(); }
      const r = t.closest("[data-csround]"); if (r) { st.rounds = Number(r.dataset.csround); return draw(); }
      if (t.closest("[data-csok]") && await chatSet(st)) { $("#layer").innerHTML = ""; toast("频道设置保存了", "check"); }
    };
    $("#sheet-bg").onchange = e => { if (e.target.matches("[data-csduty]")) st.duty = e.target.value; if (e.target.matches("[data-csproject]")) st.project = e.target.value; };
  }
  document.addEventListener("click", async e => {
    if (route.page !== "chat" && route.page !== "base") return;
    const d = chatDraft(), ax = e.target.closest("[data-attx]"), retry = e.target.closest("[data-attretry]");
    if (ax && !d.sending) { d.entries.splice(Number(ax.dataset.attx), 1); return paintAtt(); }
    if (retry) { const a = d.entries[Number(retry.dataset.attretry)]; if (a?.state === "error") return uploadEntry(a); }
    if (e.target.closest("#mc-send")) return sendChat(true);
    const mat = e.target.closest("[data-mcat]"); if (mat) return insertAt(mat.dataset.mcat, true);
    if (route.page !== "chat") return;
    const c = e.target.closest("[data-ch]"); if (c) return switchChannel(c.dataset.ch);
    if (e.target.closest("#ch-send")) return sendChat();
    const at = e.target.closest("[data-at]"); if (at) return insertAt(at.dataset.at);
    if (e.target.closest("[data-chset]")) return chatSettings();
    if (e.target.closest("[data-chstop]")) { await api("/api/chat/stop", { channel: ui.chat.channel }); toast("停了", "close"); return loadChat(); }
    if (e.target.closest("[data-chcompress]")) { await api("/api/chat/compress", { channel: ui.chat.channel }); toast("在压缩了，要一会儿", "check"); return; }
    if (e.target.closest("[data-ch-new]") && !$("#ch-newname")) { // 应用窗口里 prompt() 弹不出来：就地出一个输入框
      $("#ch-l").insertAdjacentHTML("beforeend", `<input class="ch-new" id="ch-newname" placeholder="新频道名，回车建好">`);
      $("#ch-newname").focus();
    }
  });
  document.addEventListener("change", e => {
    if (route.page !== "chat" && route.page !== "base") return;
    const t = e.target;
    if (["ch-file", "mc-file"].includes(t.id)) { [...t.files].forEach(upload); t.value = ""; }
  });
  document.addEventListener("keydown", async e => {
    if (route.page === "chat" && e.target.id === "ch-newname" && e.key === "Enter" && !e.isComposing) {
      const n = e.target.value.trim(); if (!n) return;
      if (await api("/api/chat/channel", { name: n })) return switchChannel(n);
      return;
    }
    if (!["ch-text", "mc-text"].includes(e.target.id)) return;
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(e.target.id === "mc-text"); }
    if (e.key === "Escape") { const pop = $("#ch-pop"); if (pop) pop.hidden = true; }
  });
  document.addEventListener("input", e => { // 打 @ 时弹出队员列表
    if (["ch-text", "mc-text"].includes(e.target.id)) chatDraft().text = e.target.value;
    if (e.target.id !== "ch-text") return;
    const m = e.target.value.slice(0, e.target.selectionStart).match(/@([^\s@]*)$/), pop = $("#ch-pop");
    if (!m) { pop.hidden = true; return; }
    const q = m[1].toLowerCase(), list = mentionable().filter(a => !q || a.name.toLowerCase().includes(q) || a.id.includes(q)).slice(0, 8);
    pop.innerHTML = list.map(a => `<button data-at="${esc(a.name)}">${avatar(a, 22)}<b>${esc(a.name)}</b><span>${esc(a.vendor || "")}</span></button>`).join("") || `<span class="small muted">没有这个队员</span>`;
    pop.hidden = false;
  });
  document.addEventListener("paste", e => { // 粘贴图片 / 文件
    if (!["ch-text", "mc-text"].includes(e.target.id)) return;
    const fs = [...(e.clipboardData || {}).files || []];
    if (fs.length) { e.preventDefault(); fs.forEach(upload); }
  });
  document.addEventListener("dragover", e => { if (e.target.closest && e.target.closest("#ch-in, #mc")) e.preventDefault(); });
  document.addEventListener("drop", e => { // 浏览器里拖文件进来
    if (!e.target.closest || !e.target.closest("#ch-in, #mc")) return;
    e.preventDefault(); [...e.dataTransfer.files].forEach(upload);
  });
  // Older native shells intercept drops before the page receives the file bytes.
  try {
    window.__TAURI__ && window.__TAURI__.event.listen("tauri://drag-drop", ev => {
      if (route.page !== "chat" && route.page !== "base") return;
      if (ev.payload?.paths?.length) toast("此窗口无法直接上传拖入的文件，请用回形针选择", "close");
    });
  } catch {}
  // 基地右侧的小聊天框：和聊天室同一套（当前频道最近几条 + 输入框）
  function paintMiniChat() {
    const el = $("#mc"); if (!el) return;
    const d = ui.chat.data;
    if (!d || d.channel !== ui.chat.channel) { loadChat(false); return; }
    const ats = mentionable().filter(a => a.place === "office" || a.api).slice(0, 8);
    part("mc", el, `<div class="card-h"><h2>${icon("chat")}聊天室 <span class="muted small"># ${esc(d.channel)}</span></h2><a class="more" href="#chat">完整 ${icon("chev")}</a></div>
      <div class="mc-list" id="mc-list">${d.messages.slice(-6).map(m => msgHTML(m, true)).join("") || `<div class="empty small">@ 队员就能派活</div>`}</div>
      <div class="mc-ats">${ats.map(a => `<button data-mcat="${esc(a.name)}" title="@${esc(a.name)}">${avatar(a, 22)}</button>`).join("")}</div>
      <div class="mc-in"><div class="ch-field"><div class="ch-att" id="mc-att"></div><textarea id="mc-text" rows="2" placeholder="@GPT 帮我…"></textarea><label class="ib ch-clip" title="添加附件">📎<input type="file" multiple id="mc-file" hidden></label></div><button class="btn primary sm" id="mc-send">${icon("chev")}</button></div>`);
    const ta = $("#mc-text"); if (ta && ta.value !== chatDraft().text) ta.value = chatDraft().text;
    const l = $("#mc-list"); if (l) l.scrollTop = 1e9;
    paintAtt();
  }

  // ---------- 战报 ----------
  async function pReport() {
    shell(`<div class="head"><div><h1>战报</h1><p>一批活做完来这里结算：真实花掉的金币（DeepSeek 余额差）和体力（GPT 额度百分比）。几单同时跑会合并计算，不重复。</p></div>
      <div class="act" id="newbatch"></div></div><div class="picker" id="picker" style="margin-bottom:16px"></div><div id="rep"></div>`);
    if (ui.batch === null) ui.batch = S.batch || "";
    const opts = [...new Set([S.batch, ...S.batches].filter(Boolean))];
    part("picker", $("#picker"), `<button class="fchip all ${ui.batch === "" ? "on" : ""}" data-b="">全部</button>` + opts.map(b => `<button class="fchip all ${ui.batch === b ? "on" : ""}" data-b="${esc(b)}">${esc(b)}${b === S.batch ? " · 当前" : ""}</button>`).join(""));
    if (part("newbatch", $("#newbatch"), `<label class="search" style="width:220px">${icon("plus")}<input id="nb" placeholder="开新一批，比如 周报-1111"></label><button class="btn primary" id="nb-go">开始</button>`))
      $("#nb-go").onclick = async () => { const v = $("#nb").value.trim(); if (!v) return; if (await api("/api/batch", { batch: v })) { ui.batch = v; painted = { __shell: true }; toast(`开始新一批：${v}`, "report"); } };
    const b = await api(`/api/bill?batch=${encodeURIComponent(ui.batch)}`) || { per_agent: [] };
    const per = b.per_agent || [];
    const runs = per.reduce((s, r) => s + r.runs, 0), secs = per.reduce((s, r) => s + r.seconds, 0);
    const wk = per.reduce((s, r) => s + (r.weekly || 0), 0), fh = per.reduce((s, r) => s + (r.five_hour || 0), 0);
    const hasGpt = per.some(r => A(r.agent).billing === "codex");
    const credits = per.reduce((s, r) => s + (r.credits || 0), 0);
    const mvp = per.slice().sort((x, y) => y.runs - x.runs)[0];
    const maxRuns = Math.max(1, ...per.map(r => r.runs));
    part("rep", $("#rep"), !per.length ? `<div class="card"><div class="empty"><div class="big">🏆</div>这一批还没有记账的任务<br><span class="small">用 agentbus 派活会自动记账</span></div></div>` : `
      <div class="tiles">
        <div class="card tile"><div class="k"><span class="ki gold">${icon("coin")}</span>金币消耗</div><div class="v">${money(b.total_cny)}</div><div class="s">DeepSeek 余额差</div></div>
        <div class="card tile"><div class="k"><span class="ki hp">${icon("bolt")}</span>GPT 体力</div><div class="v">${wk ? "-" + wk.toFixed(0) + "%" : hasGpt ? "<1%" : "—"}</div><div class="s">${hasGpt ? `本周额度 · 5 小时 -${fh.toFixed(0)}%` : "这一批没用 GPT"}${credits > 0.01 ? ` · <b style="color:var(--hp-low)">付费 credits -${credits.toFixed(1)}</b>` : ""}</div></div>
        <div class="card tile"><div class="k"><span class="ki acc">${icon("quests")}</span>完成任务</div><div class="v">${runs}</div><div class="s">${per.length} 位队员参与</div></div>
        <div class="card tile"><div class="k"><span class="ki blue">${icon("clock")}</span>总用时</div><div class="v">${mins(secs)}</div><div class="s">各任务用时相加</div></div></div>
      ${mvp ? `<div class="mvp"><span class="crown">👑</span>${avatar(A(mvp.agent), 56)}<div><h3>本批 MVP · ${esc(A(mvp.agent).name)}</h3><p>完成 ${mvp.runs} 个任务，用时 ${mins(mvp.seconds)}</p></div></div>` : ""}
      <div class="report"><div class="card"><div class="card-h"><h2>队员消耗</h2></div><div class="card-b">${per.map(r => { const a = A(r.agent); return `<div class="brk">${avatar(a, 34)}<div><div class="nm">${esc(a.name)}</div><div class="bar"><i style="width:${r.runs / maxRuns * 100}%"></i></div></div>
          <div class="val">${r.cny ? money(r.cny) : r.weekly || r.five_hour ? `体力 -${r.weekly || r.five_hour}%` : BILLING[a.billing] || "—"}<small>${r.runs} 次 · ${mins(r.seconds)}${r.unknown ? ` · ${r.unknown} 次无数据` : ""}</small></div></div>`; }).join("")}</div></div>
        <div class="card"><div class="card-h"><h2>任务明细</h2></div><div class="card-b">${(b.records || []).slice().reverse().slice(0, 30).map(r => { const a = A(r.agent); return `<div class="brk">${avatar(a, 30)}<div style="min-width:0"><div class="nm" style="font-weight:550;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${U(r.task)}</div><div class="small muted">${esc(a.name)}${r.tag ? " · " + esc(r.tag) : ""} · ${mins(r.seconds)}</div></div><div class="val">${costText(r.cost) || "—"}${r.cost && r.cost.merged ? `<small>${r.cost.merged} 单合并计算</small>` : ""}</div></div>`; }).join("")}</div></div></div>
      ${S.agents.some(a => a.billing === "claude" && a.installed) ? `<p class="small muted" style="margin-top:14px">Claude 额度看不到：订阅用量只在加密的登录信息后面，agentbus 不去读。</p>` : ""}`);
  }
  document.addEventListener("click", e => { if (route.page !== "report") return; const b = e.target.closest("[data-b]"); if (b) { ui.batch = b.dataset.b; painted.picker = painted.rep = null; pReport(); } });

  // ---------- 项目 ----------
  $("#proj").addEventListener("click", e => { e.stopPropagation(); const bo = S && baseOffice(); location.hash = bo ? "#office/" + encodeURIComponent(bo.id) : "#office"; });
  function openProjPop(anchor) {
    closePop();
    const pop = document.createElement("div"); pop.className = "pop proj-pop"; pop.id = "pop";
    const item = (key, name, sub) => `<button class="pa ${S.project_key === key ? "on" : ""}" data-use="${esc(key)}"><span class="pick">${S.project_key === key ? icon("check") : ""}</span><span class="nm">${esc(name)}${sub ? `<small>${esc(sub)}</small>` : ""}</span></button>`;
    pop.innerHTML = `<h4>切换项目</h4><p class="hint">每个项目有自己的阵容、职能和项目说明；悬浮窗和右侧浏览器一起切换</p>
      ${item("", "无项目", "通用阵容")}${S.projects.map(p => item(p, p, "")).join("")}
      <div class="pop-sep"></div><button class="pa" data-newproj><span class="pick">${icon("plus")}</span><span class="nm">新建项目</span></button>
      <a class="pa" href="#playbook" data-close><span class="pick">${icon("skills")}</span><span class="nm">经验包</span></a>
      <a class="pa" href="#settings" data-close><span class="pick">${icon("settings")}</span><span class="nm">项目说明和文件夹</span></a>`;
    document.body.appendChild(pop);
    const r = anchor.getBoundingClientRect();
    const left = innerWidth < 760 ? 12 : Math.min(r.right + 10, innerWidth - 312);
    pop.style.left = (left + 300 > innerWidth ? Math.max(12, r.right - 300) : left) + "px";
    pop.style.top = Math.max(12, Math.min(r.top, innerHeight - pop.offsetHeight - 12)) + "px";
    pop.onclick = async e => {
      const u = e.target.closest("[data-use]");
      if (u) { closePop(); if (await api("/api/project/use", { name: u.dataset.use })) { toast(u.dataset.use ? `切到项目「${u.dataset.use}」` : "切到无项目", "folder"); if (u.dataset.use) askPlaybook(); } return; }
      if (e.target.closest("[data-newproj]")) { closePop(); newProject(); return; }
      if (e.target.closest("[data-close]")) closePop();
    };
  }
  function newProject() {
    $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><form class="modal" id="np" autocomplete="off"><h2>新建项目</h2>
      <p class="muted small">一个项目 = 一套阵容 + 职能配置 + 给所有 agent 看的项目说明。派活记账也按项目分开。</p>
      <label class="fld"><span>项目名</span><input name="name" required placeholder="比如 小红书运营" maxlength="40"></label>
      <label class="fld"><span>项目文件夹</span><input name="root" placeholder="~/Documents/小红书运营（agent 在这里干活；不填就是主目录）"></label>
      <label class="fld"><span>项目说明</span><textarea name="brief" rows="5" placeholder="目标、文件夹放什么、红线……所有 agent 接活时都会先读这一段"></textarea></label>
      <div class="fld"><span>阵容</span><div class="seg" id="np-copy"><button type="button" class="on" data-v="1">复制当前阵容</button><button type="button" data-v="0">从空开始</button></div></div>
      <div class="modal-act"><button type="button" class="btn ghost" id="np-x">取消</button><button class="btn primary">创建并切换</button></div></form></div>`;
    const f = $("#np"); let copy = true;
    $("#np-copy").onclick = e => { const b = e.target.closest("[data-v]"); if (!b) return; copy = b.dataset.v === "1"; $$("#np-copy button").forEach(x => x.classList.toggle("on", x === b)); };
    $("#np-x").onclick = () => ($("#layer").innerHTML = "");
    $("#sheet-bg").onclick = e => { if (e.target.id === "sheet-bg") $("#layer").innerHTML = ""; };
    f.onsubmit = async e => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(f));
      if (await api("/api/project/new", { ...d, copy })) { $("#layer").innerHTML = ""; toast(`项目「${d.name}」建好了`, "folder"); askPlaybook(true); }
    };
    f.elements.name.focus();
  }
  function closePop() { const p = $("#pop"); p && p.remove(); }
  // 应用窗口里系统的 confirm() 弹不出来（直接当“取消”），用自己的确认框
  function askBox(title, sub, ok = "确定", danger = false) {
    return new Promise(res => {
      $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><div class="modal ask"><h2>${esc(title)}</h2>${sub ? `<p class="muted small">${esc(sub)}</p>` : ""}
        <div class="modal-act"><button class="btn ghost" id="ak-x">取消</button><button class="btn ${danger ? "danger-fill" : "primary"}" id="ak-ok">${esc(ok)}</button></div></div></div>`;
      const done = v => { $("#layer").innerHTML = ""; res(v); };
      $("#ak-x").onclick = () => done(false); $("#ak-ok").onclick = () => done(true);
      $("#sheet-bg").onclick = e => { if (e.target.id === "sheet-bg") done(false); };
    });
  }

  // ---------- 经验包 ----------
  // 选项目时问一句：按项目文档干活（默认），还是用经验包（多半是导入了别人的）
  async function askPlaybook(isNew) {
    const pb = await api("/api/playbook"); if (!pb) return;
    const docs = pb.docs || [];
    const body = pb.has
      ? `<p>这个项目有经验包 <b>「${esc(pb.manifest.name)}」v${esc(pb.manifest.version)}</b>：${pb.steps.length} 步工作流、${pb.lessons.length} 条经验。用上之后，派活时每个 agent 会自动带上它那一步的做法和相关经验。</p>`
      : `<p>这个项目还没有经验包。${docs.length ? `在项目里找到 ${docs.length} 份现成的经验文档，可以让主位整理成经验包：` : "以后干活攒下的经验可以慢慢沉淀进来。"}</p>${docs.length ? `<ul class="docs">${docs.slice(0, 6).map(d => `<li>${esc(d.split("/").slice(-2).join("/"))}</li>`).join("")}</ul>` : ""}`;
    $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><div class="modal"><h2>${icon("skills")} 怎么开始「${esc(pb.project)}」？</h2>${body}
      <div class="choice">
        <button class="opt on" data-pb="scratch"><b>按项目里的文档干活（推荐）</b><span>主位和队员照项目文件夹里的说明、复盘经验来做，新规矩直接写进这些文档${pb.has ? "；经验包留着，分享时再导出" : ""}</span></button>
        ${pb.has ? `<button class="opt" data-pb="merge"><b>把经验包融合进本地文档</b><span>适合导入了别人的经验包：主位对比你的工作流和复盘文档，把值得加的写进本地文件，你确认后才改</span></button>`
                 : docs.length ? `<button class="opt" data-pb="draft"><b>整理成经验包（分享用）</b><span>主位读这些文档，整理出一份可以导出分享的经验包，整理好你再过目</span></button>` : ""}</div>
      <div class="modal-act"><button class="btn ghost" id="pb-later">以后再说</button></div></div></div>`;
    $("#pb-later").onclick = () => { $("#layer").innerHTML = ""; if (isNew) location.hash = "#team"; };
    $$("[data-pb]").forEach(b => b.onclick = async () => {
      const v = b.dataset.pb;
      if (v === "use") await api("/api/playbook/equip", { enabled: true });
      if (v === "scratch" && pb.has) await api("/api/playbook/equip", { enabled: false });
      if (v === "draft") { const r = await api("/api/playbook/draft", {}); if (r) toast(`已交给主位 ${A(r.lead).name} 整理，好了会出现在「经验」页`, "skills"); }
      if (v === "fit") { const r = await api("/api/playbook/fit", {}); if (r) return fitStarted(r); }
      if (v === "merge") { const r = await api("/api/playbook/merge", {}); if (r && r.mode === "chat") return fitStarted({ ...r, merge: true }); }
      $("#layer").innerHTML = "";
      location.hash = v === "use" || v === "draft" || v === "fit" ? "#playbook" : isNew ? "#team" : location.hash;
      painted.pb = null;
    });
  }
  // 主位是桌面端（比如 Claude）：分析在它自己的对话里做，这里告诉主人去说一句
  function fitStarted(r) {
    const a = A(r.lead);
    if (r.mode !== "chat") { toast(`交给主位 ${a.name} 分析你的习惯，几分钟后看建议`, "star"); return; }
    $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><div class="modal"><div style="display:flex;gap:12px;align-items:center">${avatar(a, 48)}<div><h2>交给主位 ${esc(a.name)}</h2><p class="muted small" style="margin:2px 0 0">主位在你和它的对话里分析，最了解你的习惯</p></div></div>
      <p>在主位的对话里说一句：</p><pre class="cmd">${esc(r.say)}</pre>
      <p class="small muted">${r.merge ? "它会把经验包和你本地的工作流、复盘文档逐条对比，列出值得加的、冲突的，你同意后它才改本地文件。" : r.refresh ? "它会读项目里的文档，整理出新版经验包；好了「经验」页会出现「换成新版」，旧版留档。" : "它会读你的习惯和派活记录，写出装配建议；建议会出现在「经验」页，你逐项确认后才生效。"}</p>
      <div class="modal-act"><button class="btn ghost" id="fit-x">知道了</button>${a.open && a.open.app ? `<button class="btn primary" data-open="${esc(a.id)}" data-how="app">${icon("open")}打开 ${esc(a.name)}</button>` : ""}</div></div></div>`;
    $("#fit-x").onclick = () => ($("#layer").innerHTML = "");
    $("#sheet-bg").onclick = e => { if (e.target.id === "sheet-bg") $("#layer").innerHTML = ""; };
    location.hash = "#playbook";
  }
  const ui_pb = { filter: "all", q: "", open: null, tab: null };
  async function pPlaybook() {
    shell(`<div class="head"><div><h1>经验</h1><p id="pb-sub">经验包是分享用的：把你的经验导出给别人，或者导入别人的、融合进你的本地文档。干活以本地文档为准。</p></div><div class="act" id="pb-act"></div></div><div id="pb"></div>`);
    const pb = await api("/api/playbook"); if (!pb) return;
    if (pb.project === "通用") { part("pb", $("#pb"), `<div class="card"><div class="empty"><div class="big">📦</div>经验包跟着项目走<br><span class="small">先在左下角选一个项目，或者新建一个</span></div></div>`); part("pbact", $("#pb-act"), ""); return; }
    if (!pb.has) {
      part("pbact", $("#pb-act"), "");
      part("pb", $("#pb"), `<div class="card"><div class="empty"><div class="big">📦</div>「${esc(pb.project)}」还没有经验包
        ${pb.drafting ? `<br><span class="small">主位正在整理，好了会自动出现在这里</span>` : pb.docs.length ? `<br><span class="small">项目里找到 ${pb.docs.length} 份现成文档，可以让主位整理成经验包</span><div style="margin-top:14px"><button class="btn primary" id="pb-draft">${icon("skills")}让主位整理</button></div>` : `<br><span class="small">干活攒下的经验以后会慢慢沉淀进来</span>`}
        <div style="margin-top:10px"><button class="btn ghost sm" data-pbimport>导入别人的经验包</button></div></div></div>`);
      const d = $("#pb-draft"); if (d) d.onclick = async () => { const r = await api("/api/playbook/draft", {}); if (r) { toast(`已交给 ${A(r.lead).name} 整理`, "skills"); painted.pb = null; pPlaybook(); } };
      return;
    }
    const m = pb.manifest;
    part("pbact", $("#pb-act"), `<button class="btn ghost sm" data-pbexport title="打包分享给别人：自动去掉路径、名字、密钥">导出</button><button class="btn ghost sm" data-pbimport title="导入别人的经验包：原来的会先备份">导入</button>
      <button class="btn primary" data-pbmerge title="对比本地的工作流和复盘文档，把值得加的写进本地文件，你确认后才改">融合进本地文档</button>
      <div class="seg" title="一般不用：干活以本地文档为准"><button class="${pb.enabled ? "on" : ""}" data-pbon="1">派活时带上</button><button class="${pb.enabled ? "" : "on"}" data-pbon="0">不带</button></div>`);
    if (pb.fitting) setTimeout(() => { if (route.page === "playbook") { painted.pb = painted.pbact = null; pPlaybook(); } }, 8000); // 分析中：隔几秒看一眼
    const lessons = pb.lessons.filter(x => (ui_pb.filter === "all" || (ui_pb.filter === "off" ? !x.on : x.scope === ui_pb.filter)) && (!ui_pb.q || x.text.includes(ui_pb.q) || (x.tags || []).some(t => t.includes(ui_pb.q))));
    const ps = pb.personal || {}, nMine = Object.keys(ps.steps || {}).length + (ps.lessons || []).length;
    const TABS = [["map", "总览"], ["flow", "工作流", `${pb.steps.filter(x => x.on).length}/${pb.steps.length}`], ["lessons", "经验", (pb.proposals || []).length ? `新 ${pb.proposals.length}` : `${pb.lessons.filter(x => x.on).length}/${pb.lessons.length}`],
      ["mine", "我的习惯", nMine || ""], ["fit", "装配建议", pb.proposal || pb.fitting ? "●" : ""]];
    if (!TABS.some(t => t[0] === ui_pb.tab)) ui_pb.tab = "map";
    const body = {
      map: () => mapHTML(pb) + `<div class="pb-stats">
          <button class="pbs" data-pbtab="flow"><b>${pb.steps.filter(x => x.on).length}<small>/${pb.steps.length}</small></b><em>步骤</em></button>
          <button class="pbs" data-pbtab="lessons"><b>${pb.lessons.filter(x => x.on).length}<small>/${pb.lessons.length}</small></b><em>经验</em></button>
          <button class="pbs" data-pbtab="mine"><b>${nMine}</b><em>我的习惯</em></button></div>
        ${pb.proposal ? `<button class="fit-banner go" data-pbtab="fit">${avatar(A(S.lead || "claude"), 28)}<span>主位给了装配建议，等你确认</span>${icon("chev")}</button>` : ""}`,
      flow: () => `<div class="flow">${pb.steps.map(st => { const w = st.who[0]; return `<div class="fstep ${st.on ? "" : "off"} ${ui_pb.open === st.id ? "open" : ""}" data-step="${esc(st.id)}">
        <div class="fs-top"><span class="fs-n">${st.step}</span><b data-notr>${esc(st.name)}</b><button class="sw ${st.on ? "on" : ""}" data-stepsw="${esc(st.id)}" role="switch" aria-checked="${st.on}" title="${st.on ? "卸下这一步" : "装上这一步"}"></button></div>
        <p>${U(st.description)}</p>
        <div class="fs-caps">${st.caps_names.map(c => `<span class="pill">${esc(c)}</span>`).join("")}${st.scope === "通用" ? `<span class="pill accent">通用</span>` : ""}${(ps.steps || {})[st.id] ? `<span class="pill gold">按你的习惯改</span>` : ""}</div>
        <div class="fs-who">${w ? `${avatar(A(w.id), 24)}<span>${esc(w.name)}${w.level === "prefer" ? " · 主力" : ""}</span>${w.lack.length ? `<em>缺：${esc(w.lack.join("、"))}</em>` : ""}` : `<span class="muted">还没人负责</span>`}<span class="uses">用过 ${st.uses} 次</span></div>
        ${ui_pb.open === st.id ? `<pre class="fs-body">${esc((pb.stepBodies || {})[st.id] || "")}</pre>` : ""}</div>`; }).join("")}</div>`,
      lessons: () => ((pb.proposals || []).length ? `<div class="card props"><div class="props-h"><b>待采纳 ${pb.proposals.length} 条</b><span class="small muted">队员干活时提的，采纳了才会在派活时带上</span></div>
          ${pb.proposals.map(x => `<div class="prop">${avatar(A(x.from) || { color: "#999" }, 26)}<div class="ls-m"><div data-notr>${esc(x.text)}</div><div class="ls-t">${(x.tags || []).map(t => `<span class="chip-s">${esc(t)}</span>`).join("")}<span class="muted small">${esc((A(x.from) || {}).name || x.from)} · ${esc(x.date)}</span></div></div>
            <button class="btn sm ghost" data-prop="${esc(x.pid)}" data-v="0">不要</button><button class="btn sm primary" data-prop="${esc(x.pid)}" data-v="1">采纳</button></div>`).join("")}</div>` : "") + `<div class="toolbar"><div class="seg">${[["all", "全部"], ["通用", "通用"], ["本项目", "本项目"], ["off", "已卸下"]].map(([k, t]) => `<button class="${ui_pb.filter === k ? "on" : ""}" data-lf="${k}">${t}</button>`).join("")}</div>
        <label class="search">${icon("search")}<input id="pb-q" placeholder="搜经验或岗位" value="${esc(ui_pb.q)}"></label></div>
        <div class="card lessons">${lessons.map(x => `<div class="ls ${x.on ? "" : "off"}"><button class="sw ${x.on ? "on" : ""}" data-lsw="${esc(x.id)}" role="switch" aria-checked="${x.on}"></button>
        <div class="ls-m"><div data-notr>${esc(x.text)}</div><div class="ls-t">${(x.tags || []).map(t => `<span class="chip-s">${esc(t)}</span>`).join("")}<span class="pill ${x.scope === "通用" ? "accent" : ""}">${esc(x.scope)}</span><span class="muted small">用过 ${x.uses} 次</span></div></div><button class="ib ls-ar" data-lsar="${esc(x.id)}" title="归档：以后不再带上（不删）">${icon("close")}</button></div>`).join("") || `<div class="empty">没有符合的经验</div>`}</div>`,
      mine: () => personalHTML(pb) || `<div class="card"><div class="empty"><div class="big">⭐</div>还没有你的习惯<br><span class="small">点「融入我的习惯」，主位会从你的文档里提炼</span></div></div>`,
      fit: () => pb.fitting ? `<div class="fit-banner">${avatar(A(S.lead || "claude"), 28)}<span>主位 ${esc(A(S.lead || "claude").name)} 正在读你的习惯，好了会出现在这里…</span></div>`
        : pb.proposal ? wizardHTML(pb) : `<div class="card"><div class="empty"><div class="big">🧩</div>现在没有待确认的建议<br><span class="small">主位会对照你的习惯，告诉你哪些步骤直接用、哪些改、哪些跳过</span><div style="margin-top:14px"><button class="btn primary" data-fit>${icon("star")}融入我的习惯</button></div></div></div>`,
    };
    part("pb", $("#pb"), `
      <div class="pb-hero ${pb.enabled ? "" : "dim"}"><div class="pb-ic">📦</div><div><h2><span data-notr>${esc(m.name)}</span> <span class="pill">v${esc(m.version)}</span>${pb.enabled ? `<span class="pill hp">派活时带上</span>` : ""}${m.imported ? `<span class="pill accent">导入于 ${esc(m.imported)}</span>` : ""}</h2>
        <p>${U(m.description || "")}</p></div></div>
      ${pb.draft ? `<div class="fit-banner"><span>按项目文档整理的新版已经写好了，换上后再导出分享（旧版整个留档）。</span><button class="btn sm primary" data-pbadopt>换成新版</button></div>`
        : pb.stale ? `<div class="fit-banner"><span>经验包是 ${esc(pb.stale.pb_date)} 整理的，之后项目文档又改过（${esc(pb.stale.newer.join("、"))}）。分享前先按文档更新一下。</span><button class="btn sm" data-pbrefresh>按文档更新</button></div>` : ""}
      <div class="withnav"><nav class="sidenav">${TABS.map(([k, t, n]) => `<button class="${ui_pb.tab === k ? "on" : ""}" data-pbtab="${k}">${t}${n ? `<em class="${n === "●" ? "dot" : ""}">${n}</em>` : ""}</button>`).join("")}</nav>
      <div class="pb-body">${body[ui_pb.tab]()}</div></div>`);
    const q = $("#pb-q"); if (q && !q.oninput) q.oninput = e => { ui_pb.q = e.target.value.trim(); painted.pb = null; pPlaybook(); };
  }
  let rsT; addEventListener("resize", () => { clearTimeout(rsT); rsT = setTimeout(() => { if (route.page === "playbook" && ui_pb.tab === "map") { painted.pb = null; pPlaybook(); } }, 200); });
  // 经验包一图：按前置关系分层，从左到右（窄屏从上到下），并行的步骤上下排开，简单连线
  function mapHTML(pb) {
    const ps = pb.personal || {}, steps = pb.steps, by = Object.fromEntries(steps.map(s => [s.id, s]));
    const depth = {}; const dep = s => depth[s.id] ??= (s.after || []).reduce((d, x) => by[x] ? Math.max(d, dep(by[x]) + 1) : d, 0);
    steps.forEach(dep);
    const layers = []; steps.forEach(s => (layers[depth[s.id]] ||= []).push(s));
    const avail = Math.max(320, ($("#page") || document.body).clientWidth - 112 - (innerWidth >= 760 ? 176 : 0)), L = layers.length, wide = Math.max(...layers.map(l => l.length));
    const vertical = avail / L < 120;
    const colW = vertical ? Math.min(180, avail / wide) : Math.min(190, avail / L), rowH = vertical ? 142 : 138;
    const W = vertical ? wide * colW : L * colW, H = (vertical ? L : wide) * rowH + 8;
    const pos = {};
    layers.forEach((l, i) => l.forEach((s, j) => {
      const off = (j - (l.length - 1) / 2);
      pos[s.id] = vertical ? [W / 2 + off * colW, i * rowH + 40] : [i * colW + colW / 2, H / 2 - 22 + off * rowH];
    }));
    const R = 30;
    const edges = steps.flatMap(s => (s.after || []).filter(x => pos[x]).map(x => {
      const [x1, y1] = pos[x], [x2, y2] = pos[s.id], off = !by[x].on || !s.on;
      const d = vertical ? `M${x1} ${y1 + R + 48} C${x1} ${(y1 + y2) / 2} ${x2} ${(y1 + y2) / 2} ${x2} ${y2 - R - 6}`
                         : `M${x1 + R + 2} ${y1} C${(x1 + x2) / 2} ${y1} ${(x1 + x2) / 2} ${y2} ${x2 - R - 6} ${y2}`;
      return `<path class="${off ? "off" : ""}" d="${d}" marker-end="url(#fl-arrow)"/>`;
    })).join("");
    const lessonsOf = st => pb.lessons.filter(x => x.on && (x.tags || []).some(t => st.tags.includes(t))).length;
    const nodes = steps.map(st => {
      const [x, y] = pos[st.id], w = st.who[0], n = lessonsOf(st), mine = (ps.steps || {})[st.id];
      return `<button class="fnode ${st.on ? "" : "off"}" style="left:${x}px;top:${y - R}px;width:${colW - 14}px" data-mapstep="${esc(st.id)}" title="${esc(st.description)}">
        <span class="fn-av">${w ? avatar(A(w.id), 2 * R) : `<span class="fn-q">?</span>`}<i class="fn-n">${st.step}</i>${mine ? `<i class="fn-star" title="按你的习惯改过">★</i>` : ""}</span>
        <b data-notr>${esc(st.name)}</b><span class="fn-who ${w ? "" : "none"}">${w ? esc(w.name) : "待分配"}</span>${n ? `<em class="fn-ls">${n} 条经验</em>` : ""}</button>`;
    }).join("");
    return `<div class="flowmap ${pb.enabled ? "" : "dim"}"><div class="fm-in" style="width:${W}px;height:${H}px">
      <svg width="${W}" height="${H}" aria-hidden="true"><defs><marker id="fl-arrow" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M1 1 9 5 1 9" fill="none"/></marker></defs>${edges}</svg>${nodes}</div></div>`;
  }
  // 装配向导：主位的建议已预选，主人逐项确认
  const wz = { steps: {}, lessons: {}, personal: {}, assign: {}, showAll: false };
  function wizardHTML(pb) {
    const p = pb.proposal;
    if (p.error) return `<div class="card wizard"><div class="card-b"><b>${esc(p.error)}</b> <button class="btn sm" data-fit>重新分析</button></div></div>`;
    const stepName = id => (pb.steps.find(x => x.id === id) || {}).name || id;
    const lesson = id => pb.lessons.find(x => x.id === id) || { text: id };
    for (const x of p.steps || []) wz.steps[x.id] ??= { decision: x.decision, note: x.note || "" };
    for (const x of p.lessons || []) wz.lessons[x.id] ??= x.decision !== "skip";
    (p.personal || []).forEach((x, i) => (wz.personal[i] ??= true));
    (p.assign || []).forEach((x, i) => (wz.assign[i] ??= true));
    const DEC = [["use", "直接用"], ["adapt", "按习惯改"], ["skip", "跳过"]];
    const skips = (p.lessons || []).filter(x => x.decision === "skip"), keep = (p.lessons || []).filter(x => x.decision !== "skip");
    return `<div class="card wizard"><div class="wz-h">${avatar(A(S.lead || "claude"), 36)}<div><b>主位的装配建议</b><p>${esc(p.summary || "")}</p></div></div>
      <div class="wz-sec"><h3>1. 每一步怎么用</h3>${(p.steps || []).map(x => { const c = wz.steps[x.id]; return `<div class="wz-row">
        <div class="wz-l"><b>${esc(stepName(x.id))}</b><span>${esc(x.reason || "")}</span>${c.decision === "adapt" ? `<textarea class="inp" data-wznote="${esc(x.id)}" rows="2" placeholder="按你的习惯，这一步怎么改">${esc(c.note)}</textarea>` : ""}</div>
        <div class="seg">${DEC.map(([v, t]) => `<button class="${c.decision === v ? "on" : ""}" data-wzstep="${esc(x.id)}" data-v="${v}">${t}</button>`).join("")}</div></div>`; }).join("")}</div>
      ${(p.conflicts || []).length ? `<div class="wz-sec"><h3>2. 和你习惯冲突的地方</h3>${p.conflicts.map(x => `<div class="wz-conf"><div><span class="pill">经验包</span>${esc(x.pack)}</div><div><span class="pill gold">你的习惯</span>${esc(x.habit)}</div><div class="sug">建议：${esc(x.suggest)}</div></div>`).join("")}</div>` : ""}
      <div class="wz-sec"><h3>3. 经验用哪些</h3>${skips.map(x => `<label class="wz-chk"><input type="checkbox" data-wzlesson="${esc(x.id)}" ${wz.lessons[x.id] ? "checked" : ""}><span>${esc(lesson(x.id).text)}<em>建议不用：${esc(x.reason || "")}</em></span></label>`).join("") || `<p class="small muted">主位建议全部照用</p>`}
        ${keep.length ? `<button class="btn sm ghost" data-wzall>${wz.showAll ? "收起" : `其余 ${keep.length} 条建议照用，展开看看`}</button>` : ""}
        ${wz.showAll ? keep.map(x => `<label class="wz-chk"><input type="checkbox" data-wzlesson="${esc(x.id)}" ${wz.lessons[x.id] ? "checked" : ""}><span>${esc(lesson(x.id).text)}</span></label>`).join("") : ""}</div>
      ${(p.personal || []).length ? `<div class="wz-sec"><h3>4. 从你的习惯里提炼的新规矩</h3>${p.personal.map((x, i) => `<label class="wz-chk"><input type="checkbox" data-wzp="${i}" ${wz.personal[i] ? "checked" : ""}><span>${esc(x.text)}<em>${(x.tags || []).map(esc).join("、")} · 出处：${esc(x.reason || "")}</em></span></label>`).join("")}</div>` : ""}
      ${(p.assign || []).length ? `<div class="wz-sec"><h3>5. 每一步交给谁</h3>${p.assign.map((x, i) => `<label class="wz-chk"><input type="checkbox" data-wza="${i}" ${wz.assign[i] ? "checked" : ""}><span>${esc(stepName(x.step))} → ${avatar(A(x.agent), 20)} <b>${esc(A(x.agent).name)}</b><em>${esc(x.reason || "")}</em></span></label>`).join("")}</div>` : ""}
      <div class="wz-act"><button class="btn ghost" data-wzdrop>先不装</button><button class="btn primary" data-wzok>${icon("check")}确认装配</button></div></div>`;
  }
  function personalHTML(pb) {
    const ps = pb.personal || {}, steps = Object.entries(ps.steps || {}), ls = ps.lessons || [];
    if (!steps.length && !ls.length) return "";
    return `<div class="section-t"><h2>我的习惯</h2><span>个人层，只在你这台电脑上；派活时优先于经验包原文${ps.applied ? " · " + esc(ps.applied) + " 装配" : ""}</span></div>
      <div class="card lessons">${steps.map(([id, v]) => `<div class="ls"><span class="pill gold">改步骤</span><div class="ls-m"><b>${esc((pb.steps.find(x => x.id === id) || {}).name || id)}</b>：${esc(v.note)}</div></div>`).join("")}
        ${ls.map(x => `<div class="ls"><span class="pill gold">我的</span><div class="ls-m"><div data-notr>${esc(x.text)}</div><div class="ls-t">${(x.tags || []).map(t => `<span class="chip-s">${esc(t)}</span>`).join("")}</div></div></div>`).join("")}</div>`;
  }
  document.addEventListener("input", e => { const n = e.target.closest("[data-wznote]"); if (n) wz.steps[n.dataset.wznote].note = n.value; });
  document.addEventListener("change", e => {
    const l = e.target.closest("[data-wzlesson]"), pp = e.target.closest("[data-wzp]"), a = e.target.closest("[data-wza]");
    if (l) wz.lessons[l.dataset.wzlesson] = l.checked; if (pp) wz.personal[pp.dataset.wzp] = pp.checked; if (a) wz.assign[a.dataset.wza] = a.checked;
  });
  document.addEventListener("click", async e => {
    if (route.page !== "playbook") return;
    const redo = () => { painted.pb = painted.pbact = null; pPlaybook(); };
    if (e.target.closest("[data-fit]")) { const r = await api("/api/playbook/fit", {}); if (r) fitStarted(r); return redo(); }
    const ws = e.target.closest("[data-wzstep]"); if (ws) { wz.steps[ws.dataset.wzstep].decision = ws.dataset.v; return redo(); }
    if (e.target.closest("[data-wzall]")) { wz.showAll = !wz.showAll; return redo(); }
    if (e.target.closest("[data-wzdrop]")) { Object.assign(wz, { steps: {}, lessons: {}, personal: {}, assign: {} }); await api("/api/playbook/apply", { drop: true }); return redo(); }
    if (e.target.closest("[data-wzok]")) {
      const pb = await api("/api/playbook"); const p = pb.proposal;
      const body = { steps: wz.steps, lessons: wz.lessons, personal: (p.personal || []).filter((x, i) => wz.personal[i]), assign: (p.assign || []).filter((x, i) => wz.assign[i]) };
      const r = await api("/api/playbook/apply", body);
      if (r) { toast(`装配好了：改了 ${r.adapt} 步，加了 ${r.personal} 条你的习惯`, "check"); Object.assign(wz, { steps: {}, lessons: {}, personal: {}, assign: {} }); }
      return redo();
    }
  });
  document.addEventListener("click", async e => {
    if (route.page !== "playbook") return;
    const on = e.target.closest("[data-pbon]"), ss = e.target.closest("[data-stepsw]"), ls = e.target.closest("[data-lsw]"), lf = e.target.closest("[data-lf]"), st = e.target.closest("[data-step]");
    const redo = () => { painted.pb = null; pPlaybook(); };
    if (on) { await api("/api/playbook/equip", { enabled: on.dataset.pbon === "1" }); toast(on.dataset.pbon === "1" ? "这个项目用经验包" : "这个项目不用经验包，按项目文件夹里的文档干活；要分享时再导出", "skills"); painted.pbact = null; return redo(); }
    if (ss) { e.stopPropagation(); await api("/api/playbook/equip", { step: ss.dataset.stepsw, on: !ss.classList.contains("on") }); return redo(); }
    if (ls) { await api("/api/playbook/equip", { lesson: ls.dataset.lsw, on: !ls.classList.contains("on") }); return redo(); }
    if (lf) { ui_pb.filter = lf.dataset.lf; return redo(); }
    const tb = e.target.closest("[data-pbtab]"), nd = e.target.closest("[data-mapstep]");
    if (tb) { ui_pb.tab = tb.dataset.pbtab; return redo(); }
    if (nd) { ui_pb.tab = "flow"; ui_pb.open = nd.dataset.mapstep; await redo(); $(`.fstep[data-step="${CSS.escape(nd.dataset.mapstep)}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" }); return; }
    if (st) { ui_pb.open = ui_pb.open === st.dataset.step ? null : st.dataset.step; return redo(); }
  });
  document.addEventListener("click", async e => {
    if (route.page !== "playbook") return;
    const ex = e.target.closest("[data-pbexport]"), im = e.target.closest("[data-pbimport]"), pr = e.target.closest("[data-prop]"), ar = e.target.closest("[data-lsar]");
    const rf = e.target.closest("[data-pbrefresh]"), ad = e.target.closest("[data-pbadopt]"), mg = e.target.closest("[data-pbmerge]");
    if (mg) { e.stopPropagation(); const r = await api("/api/playbook/merge", {}); if (r) { if (r.mode === "chat") fitStarted({ ...r, merge: true }); else toast(`交给主位 ${A(r.lead).name} 对比本地文档，建议好了会回复你`, "skills"); } return; }
    if (rf) { e.stopPropagation(); const r = await api("/api/playbook/draft", {}); if (r) { if (r.mode === "chat") fitStarted({ ...r, refresh: true }); else toast(`交给主位 ${A(r.lead).name} 按文档整理，好了这里会提示`, "skills"); } return; }
    if (ad) { e.stopPropagation(); if (await askBox("换成新版？", "旧版会整个改名留档，不会删", "换成新版") && await api("/api/playbook/adopt-draft", {})) { toast("换好了，可以导出分享了", "check"); painted.pb = painted.pbact = null; pPlaybook(); } return; }
    if (!ex && !im && !pr && !ar) return;
    e.stopPropagation();
    const redo = () => { painted.pb = painted.pbact = null; pPlaybook(); };
    if (ex) { const r = await api("/api/playbook/export", { hide_names: true }); if (r) { const rep = Object.entries(r.report || {}).map(([k, v]) => `${k} ${v} 处`).join("、"); toast(`导出好了，已在${((S.platform || {}).files) || "访达"}里选中${rep ? "；去掉了 " + rep : ""}`, "check"); } return; }
    if (im) {
      const inp = document.createElement("input"); inp.type = "file"; inp.accept = ".zip,.agentpack";
      inp.onchange = async () => {
        const f = inp.files[0]; if (!f) return;
        if (!(await askBox(`导入「${f.name}」？`, "这个项目原来的经验包会先整个备份（改名留着），不会删", "导入"))) return;
        const buf = new Uint8Array(await f.arrayBuffer()); let bin = ""; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
        const r = await api("/api/playbook/import", { data: btoa(bin) });
        if (r) { ui_pb.tab = "map"; redo(); const m = await api("/api/playbook/merge", {}); if (m && m.mode === "chat") fitStarted({ ...m, merge: true }); else if (m) toast(`导入了「${r.name}」，已交给主位对比本地文档`, "skills"); }
      };
      inp.click(); return;
    }
    if (pr) { const r = await api("/api/playbook/lesson", { pid: pr.dataset.prop, adopt: pr.dataset.v === "1" }); if (r) toast(pr.dataset.v === "1" ? `采纳了，编号 ${r.id}` : "不要了", "skills"); return redo(); }
    if (ar) { if (await askBox("归档这条经验？", "以后派活不再带上它；文件里挪到归档，不删", "归档") && await api("/api/playbook/archive", { id: ar.dataset.lsar })) toast("归档了", "skills"); return redo(); }
  }, true);
  document.addEventListener("mousedown", e => {
    const p = $("#pop"); if (!p || p.contains(e.target) || e.target.closest("#proj, #sw-proj")) return;
    if (p.dataset.agent) ui.qcClosed = { id: p.dataset.agent, t: Date.now() };
    closePop();
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") { $("#layer").innerHTML = ""; closePop(); } });

  // ---------- 首次装配：识别本机 agent → 评估 → 一键装配 ----------
  async function setupWizard() {
    $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><div class="modal setupw"><div class="sw-load"><div class="big">🔍</div><b>正在看这台电脑上装了哪些 agent…</b><span class="small muted">顺便看看它们能用哪些模型</span></div></div></div>`;
    const r = await api("/api/setup"); if (!r) { $("#layer").innerHTML = ""; return; }
    const st = { step: 1 };
    const office = r.agents.filter(a => a.office), rest = r.agents.filter(a => !a.office);
    const chips = xs => xs.map(x => `<span class="chip-s">${esc(x)}</span>`).join("");
    const card = a => `<div class="sw-a ${a.lead ? "lead" : ""}">${avatar(A(a.id), 40)}<div class="sw-m"><b>${a.lead ? "👑 " : ""}${esc(a.name)}${a.lead ? `<em>主位 · 负责统筹</em>` : ""}</b>
        <div class="sw-j">${a.main.length ? chips(a.main) : `<span class="small muted">${a.web ? "网页版，需要你传话" : "待命，需要时再派"}</span>`}</div>
        ${a.models.length ? `<div class="sw-md">${a.models.slice(0, 3).map(m => `<span>${esc(m.name)}${m.tags.length ? `<i>${esc(m.tags.join("·"))}</i>` : ""}</span>`).join("")}</div>` : ""}</div></div>`;
    const draw = () => {
      const m = $(".modal.setupw"); if (!m) return;
      if (st.step === 1) m.innerHTML = `<h2>找到了 ${r.found} 位 agent</h2><p class="muted small">这台电脑上已经装好、可以直接用的：</p>
        <div class="sw-found">${r.agents.map(a => `<div class="sw-f">${avatar(A(a.id), 44)}<b>${esc(a.name)}</b><span>${esc(a.caps.slice(0, 3).join(" · "))}</span></div>`).join("")}</div>
        ${r.missing.length ? `<p class="small muted">还能招募：${esc(r.missing.join("、"))}</p>` : ""}
        ${r.keys ? `<p class="small muted">另外发现 ${r.keys} 个 API key，之后可以在「供应商」里一键接入成 API 队员。</p>` : ""}
        <div class="modal-act"><button class="btn ghost" data-sw-skip>我自己来</button><button class="btn primary" data-sw-next>看看怎么安排 →</button></div>`;
      else m.innerHTML = `<h2>建议这样安排</h2><p class="muted small">按每位的能力和擅长分好了职能，主位负责统筹、把活分下去。以后随时能在「队员」里改。</p>
        <div class="sw-sec">上阵 · ${office.length} 个工位</div><div class="sw-list">${office.map(card).join("")}</div>
        ${rest.length ? `<div class="sw-sec">休息室待命</div><div class="sw-list two">${rest.map(card).join("")}</div>` : ""}
        <div class="modal-act"><button class="btn ghost" data-sw-back>← 上一步</button><button class="btn ghost" data-sw-skip>我自己来</button><button class="btn primary" data-sw-go>就这样，开始用</button></div>`;
    };
    $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><div class="modal setupw"></div></div>`;
    draw();
    $(".modal.setupw").addEventListener("click", async e => {
      if (e.target.closest("[data-sw-next]")) { st.step = 2; return draw(); }
      if (e.target.closest("[data-sw-back]")) { st.step = 1; return draw(); }
      const go = e.target.closest("[data-sw-go]"), skip = e.target.closest("[data-sw-skip]");
      if (!go && !skip) return;
      if (go) { go.disabled = true; go.textContent = "装配中…"; }
      if (await api(go ? "/api/setup/apply" : "/api/setup/skip", go ? { lead: r.lead, office: r.office, jobs: r.jobs } : {})) {
        S.settings.setup_done = true; $("#layer").innerHTML = "";
        if (go) toast(`装配好了：${A(r.lead).name} 当主位，${office.length} 位上阵`, "star");
        if (!S.settings.tour_done) setTimeout(() => tourStart(), 600);
      }
    });
  }
  document.addEventListener("click", e => { if (e.target.closest("[data-setup]")) { e.preventDefault(); setupWizard(); } });

  // 接入主位：支持一键的直接接；不支持的弹出一段配置，主人复制过去
  async function connectLead(btn) {
    const lead = (A(S.lead) || {}).name || "主位";
    const t = btn.textContent; btn.disabled = true; btn.textContent = "接入中…";
    const r = await api("/api/connect-lead", {});
    btn.disabled = false; btn.textContent = t;
    if (!r) return;
    if (!r.manual) { btn.textContent = `已接入 ${r.name} ✓`; toast(r.already ? `${r.name} 之前就接好了` : `接好了，重开一下 ${r.name} 就能用`, "check"); return; }
    $("#tour") && ($("#tour").style.display = "none");
    $("#layer").innerHTML = `<div class="sheet-bg" id="sheet-bg"><div class="modal"><h2>把 agentbus 接进 ${esc(r.name)}</h2>
      <p class="muted small">${esc(r.name)} 不支持一键接入。${esc(r.where)}：</p><pre class="cmd" id="cl-snip">${esc(r.snippet)}</pre>
      <p class="muted small">它不支持 MCP 的话，也可以让它在终端里直接派活：<span class="code-inline">${esc(r.cli)}</span></p>
      <div class="modal-act"><button class="btn ghost" id="cl-x">关闭</button><button class="btn primary" id="cl-cp">复制配置</button></div></div></div>`;
    const close = () => { $("#layer").innerHTML = ""; $("#tour") && ($("#tour").style.display = ""); };
    $("#cl-x").onclick = close; $("#sheet-bg").onclick = e => { if (e.target.id === "sheet-bg") close(); };
    $("#cl-cp").onclick = () => navigator.clipboard && navigator.clipboard.writeText(r.snippet).then(() => toast("复制好了", "check"));
  }

  document.addEventListener("click", e => { const b = e.target.closest("[data-connect-lead]"); if (b && !e.target.closest("#tour")) { e.preventDefault(); connectLead(b); } });
  // ---------- 新手引导：聚光灯一步步讲 ----------
  // 9 步：先讲清楚是什么，再把主位接上、派出第一单（这一段也能从「怎么派活」单独打开），最后认识聊天室、办公室、账单和悬浮窗
  const TOUR = [
    { t: "欢迎来到 agentbus", d: "你电脑上的 AI 们（Claude、GPT、OpenCode、Gemini……）在这里组成一个团队：你只跟一位「主位」说话，它把活分给其他队员同时去做，做完自动叫醒它汇报。每一单谁做的、花了多少额度和钱，都记得清清楚楚。" },
    { lead: true, sel: "#lead", t: "① 把 agentbus 接进主位", connect: true,
      d: () => `主位就是你平时聊天用的那个 agent（现在是 ${tl().lead}）。点下面的按钮接上，以后每开一个新对话，悬浮窗会问你这个窗口要不要接入。接上后先让它看一眼办公室：`,
      ex: () => ["看看 agentbus 办公室，现在谁在岗、额度还剩多少"] },
    { lead: true, sel: "#lead", t: "② 用平常的话派活", d: "点名派给某位队员，或者只说要干什么、让 agentbus 按职能挑人；一句话交代几件事，几位队员会同时开工。派出去之后主位不用干等，做完会被自动叫醒。",
      ex: () => [`让 ${tl().t[0]} 写一份产品介绍，存到项目文件夹`, `同时安排：${tl().t[0]} 写文案，${tl().t[1]} 做配图，${tl().t[2]} 整理资料，做完一起告诉我`],
      cmd: () => `agentbus ask ${tl().ids[0]} "写一份产品介绍" --tag 文案策划` },
    { lead: true, sel: "#run", t: "③ 看进度和结果", d: "派出去的活实时出现在这里：谁在干、干了多久、花了多少。卡住太久的会被自动停掉重试；做完主位会告诉你结果和文件放在哪。",
      ex: () => ["刚才派的活做完了吗？", "这一批一共花了多少额度和钱？"] },
    { sel: '.nav[data-page="chat"]', t: "聊天室", d: "不经过主位也能直接派活：在频道里 @ 队员，他就开工，回复发回群里；回复里再 @ 别人就接力下去。频道切成「群聊」模式，几位队员会轮流讨论同一个话题。" },
    { sel: "#scene", t: "办公室", d: "每位主位有自己的一间办公室和一套上阵阵容。把休息室的队员拖上工位就是上阵；在「队员」页给他们装职能、选模型、开分身。" },
    { sel: "#lounge", t: "招募更多队员", d: "休息室最后的「招募」能一键安装新的命令行 agent，或者接入官方 / 中转的 API key，API 队员也能读写项目文件。" },
    { sel: '.nav[data-page="report"]', t: "战报", d: "按项目和批次算账：订阅额度（比如 GPT 的 5 小时 / 每周）用了多少、按量付费花了多少钱。额度用完会拦住，不会悄悄去扣付费 credits。" },
    { t: "悬浮窗", d: "屏幕边上的小胶囊就是悬浮窗：开工和做完时自己弹出来，点开看队员和进度。鼠标移上去旁边会出现「专注模式」，开了就只安静地收着。好了，去主位那里派第一单吧！" },
  ];
  // 示例里用这台电脑上真实的阵容：主位 + 上阵的队员（没有就用“队员 A/B/C”）
  const tl = () => {
    const lead = (A(S.lead) || {}).name || "主位";
    const pool = S.agents.filter(a => a.installed && a.id !== S.lead && a.billing !== "manual").sort((x, y) => (!!x.clone_of - !!y.clone_of) || ((y.place === "office") - (x.place === "office"))); // 本体优先、上阵的优先
    const used = new Set();
    const pickBy = (cap, cat) => { // 按能力 / 职能挑：写文案找会中文写作的，配图找会生图的，整理资料找能读写文件的
      const a = pool.find(x => !used.has(x.id) && ((x.jobs || {})["cat:" + cat] === "prefer" || (x.caps || []).includes(cap))) || pool.find(x => !used.has(x.id));
      if (a) used.add(a.id); return a;
    };
    const pick = [pickBy("cn", "writing"), pickBy("image", "visual"), pickBy("files", "office")];
    const cl = pick.find(a => a && a.cloneable) || pool.find(a => a.cloneable);
    return { lead, t: [0, 1, 2].map(i => (pick[i] || {}).name || "队员 " + "ABC"[i]), ids: [0, 1, 2].map(i => (pick[i] || {}).id || "队员"), c: (cl || {}).name || "某位队员" };
  };
  const tv = v => typeof v === "function" ? v() : v;
  let tourI = -1, tourOnly = false;
  const tourFrom = TOUR.findIndex(x => x.lead), tourTo = TOUR.length - 1 - [...TOUR].reverse().findIndex(x => x.lead);
  function tourStart(only) { // only = "lead"：只看“在主位里怎么派活”这一章
    closePop(); $("#layer").innerHTML = "";
    if (route.page !== "base") location.hash = "#base";
    tourOnly = only === "lead"; tourI = tourOnly ? tourFrom : 0; setTimeout(tourDraw, route.page === "base" ? 0 : 500);
  }
  function tourEnd() {
    const only = tourOnly; tourI = -1; tourOnly = false; $("#tour") && $("#tour").remove();
    if (!only && !S.settings.tour_done) { S.settings.tour_done = true; api("/api/settings", { tour_done: true }); }
  }
  function tourDraw() {
    if (tourI < 0) return;
    const st = TOUR[tourI], el = st.sel && $(st.sel);
    let box = $("#tour"); if (!box) { box = document.createElement("div"); box.id = "tour"; document.body.appendChild(box); }
    if (el) el.scrollIntoView({ block: "nearest" });
    const r = el ? el.getBoundingClientRect() : null, pad = 6;
    const last = tourI === (tourOnly ? tourTo : TOUR.length - 1);
    box.innerHTML = `<div class="tour-hit"></div>${r ? `<div class="tour-spot" style="left:${r.left - pad}px;top:${r.top - pad}px;width:${r.width + pad * 2}px;height:${r.height + pad * 2}px"></div>`
      : `<div class="tour-spot none" style="left:calc(50% - 1px);top:calc(50% - 1px);width:2px;height:2px"></div>`}
      <div class="tour-card ${st.ex ? "wide" : ""}" role="dialog" aria-label="${esc(st.t)}"><span class="tour-n">${tourOnly ? `在主位里派活 · ${tourI - tourFrom + 1} / ${tourTo - tourFrom + 1}` : `${tourI + 1} / ${TOUR.length}`}</span><h3>${esc(st.t)}</h3><p>${esc(tv(st.d))}</p>
        ${st.connect ? `<div class="tour-connect"><button class="btn sm primary" data-connect-lead>把 agentbus 接进 ${esc(tl().lead)}</button><span id="tour-cl"></span></div>` : ""}
        ${st.ex ? `<div class="tour-ex">${tv(st.ex).map(x => `<button class="ex" data-copy="${esc(x)}" title="点一下复制"><span>你对主位说</span>${esc(x)}<em>复制</em></button>`).join("")}</div>` : ""}
        ${st.cmd ? `<p class="tour-cmd">在命令行里等于：<code>${esc(tv(st.cmd))}</code></p>` : ""}
        <div class="tour-act"><button class="lnk" data-tskip>${last ? "" : "跳过"}</button><span></span>${tourI > (tourOnly ? tourFrom : 0) ? `<button class="btn sm ghost" data-tprev>上一步</button>` : ""}<button class="btn sm primary" data-tnext>${last ? (tourOnly ? "知道了" : "开始用") : "下一步"}</button></div></div>`;
    const card = box.querySelector(".tour-card"), cw = card.offsetWidth, ch = card.offsetHeight;
    let x, y;
    if (!r) { x = (innerWidth - cw) / 2; y = (innerHeight - ch) / 2; }
    else if (r.right + 16 + cw < innerWidth) { x = r.right + 16; y = r.top; }          // 右边
    else if (r.bottom + 16 + ch < innerHeight) { x = r.left; y = r.bottom + 16; }      // 下面
    else if (r.top - 16 - ch > 0) { x = r.left; y = r.top - 16 - ch; }                 // 上面
    else { x = r.left - 16 - cw; y = r.top; }                                          // 左边
    card.style.left = Math.max(12, Math.min(x, innerWidth - cw - 12)) + "px";
    card.style.top = Math.max(12, Math.min(y, innerHeight - ch - 12)) + "px";
  }
  document.addEventListener("click", e => {
    const tb = e.target.closest("[data-tour]");
    if (tb) { e.preventDefault(); e.stopPropagation(); return tourStart(tb.dataset.tour); }
    if (tourI < 0 || !e.target.closest("#tour")) return;
    e.preventDefault(); e.stopPropagation();
    if (e.target.closest("[data-connect-lead]")) { connectLead(e.target.closest("[data-connect-lead]")); return; }
    const cp = e.target.closest("[data-copy]");
    if (cp) { navigator.clipboard && navigator.clipboard.writeText(cp.dataset.copy).then(() => { cp.classList.add("ok"); cp.querySelector("em").textContent = "已复制"; }).catch(() => {}); return; }
    const end = tourOnly ? tourTo : TOUR.length - 1, start = tourOnly ? tourFrom : 0;
    if (e.target.closest("[data-tnext]")) { if (tourI >= end) return tourEnd(); tourI++; tourDraw(); }
    else if (e.target.closest("[data-tprev]")) { tourI = Math.max(start, tourI - 1); tourDraw(); }
    else if (e.target.closest("[data-tskip]")) tourEnd();
  }, true);
  document.addEventListener("keydown", e => {
    if (tourI < 0) return;
    if (e.key === "Escape") tourEnd();
    else if (e.key === "ArrowRight" || e.key === "Enter") { if (tourI >= (tourOnly ? tourTo : TOUR.length - 1)) tourEnd(); else { tourI++; tourDraw(); } }
    else if (e.key === "ArrowLeft") { tourI = Math.max(tourOnly ? tourFrom : 0, tourI - 1); tourDraw(); }
  });
  addEventListener("resize", () => tourI >= 0 && tourDraw());

  // ---------- 设置 ----------
  function applyTheme(t) { if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; }
  let projInfo = null;
  // 接入：每家 agent 怎么让 agentbus 发现新对话、接入后怎么把握手送进那个窗口。三层：一键装钩子 → 手动配置 → 复制接入口令
  async function loadConnect() {
    const box = $("#connect-g .card"); if (!box) return;
    const list = await fetch("/api/connectors").then(r => r.json()).catch(() => null);
    if (!list) { box.innerHTML = `<div class="empty small">读不到</div>`; return; }
    const tag = c => !c.present ? `<span class="pill">没装</span>` : c.hooked ? `<span class="pill hp">✓ 钩子已装</span>` : c.can_hook ? `<span class="pill gold">可一键装</span>` : `<span class="pill">用接入口令</span>`;
    box.innerHTML = `<div class="row col"><div class="lbl"><span>新对话会被自动发现、在悬浮窗里问你要不要接入。装了钩子的，接入后在那个窗口发下一句话就会收到 agentbus 的规矩；没装的，可以手动配置，或者复制接入口令贴进那个对话框发送。改别家配置前都会先备份。</span></div></div>`
      + list.map(c => `<div class="row cn-row ${c.present ? "" : "dim"}" data-cn="${esc(c.id)}"><div class="lbl"><b>${esc(c.name)} ${tag(c)}</b>
          <span>${c.hooked ? "新对话自动报到，接入后下一句话收到握手" : c.can_hook ? `钩子写进 <span class="code-inline">${esc(c.manual.path)}</span>` : esc(c.why || "这家还没有可靠的钩子，用接入口令")}</span></div>
          <div class="cn-act">${c.can_hook ? (c.hooked ? `<button class="btn sm ghost" data-cn-remove>卸载钩子</button>` : `<button class="btn sm primary" data-cn-install ${c.present ? "" : "disabled"}>一键装</button>`) : ""}
          ${c.manual ? `<button class="btn sm ghost" data-cn-manual>手动配置</button>` : ""}<button class="btn sm ghost" data-cn-copy>复制接入口令</button></div>
          ${c.manual ? `<div class="cn-manual" hidden><p class="small">${esc(c.manual.how)}</p><pre data-notr>${esc(c.manual.snippet)}</pre><button class="btn sm" data-cn-copysnip>复制配置</button></div>` : ""}</div>`).join("");
    box._list = list;
  }
  document.addEventListener("click", async e => {
    const row = e.target.closest("[data-cn]"); if (!row) return;
    const c = ($("#connect-g .card")._list || []).find(x => x.id === row.dataset.cn); if (!c) return;
    const copy = async (t, btn, done) => { try { await navigator.clipboard.writeText(t); btn.textContent = done; } catch (err) { toast("复制失败，请手动选中复制"); } };
    if (e.target.closest("[data-cn-copy]")) return copy(c.phrase, e.target.closest("button"), "已复制，贴进对话框发送");
    if (e.target.closest("[data-cn-copysnip]")) return copy(c.manual.snippet, e.target.closest("button"), "已复制");
    if (e.target.closest("[data-cn-manual]")) { const m = row.querySelector(".cn-manual"); m.hidden = !m.hidden; return; }
    if (e.target.closest("[data-cn-install],[data-cn-remove]")) {
      const inst = !!e.target.closest("[data-cn-install]");
      const r = await api("/api/connectors/" + (inst ? "install" : "remove"), { id: c.id });
      if (r && !r.error) toast(inst ? (r.note || "装好了") : "已卸载（原配置有备份）");
      loadConnect();
    }
  });
  const SET_TABS = [["wallet", "供应商"], ["connect", "接入"], ["chat", "聊天室"], ["sched", "调度"], ["hud", "悬浮窗"], ["general", "通用"], ["about", "关于"]]; // 项目不用手动管了（跟着窗口所在文件夹走）
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-settab]"); if (!b) return;
    ui.setTab = b.dataset.settab; try { localStorage.setItem("setTab", ui.setTab); } catch {}
    painted.setnav = null; pSettings(); window.scrollTo(0, 0);
  });
  // ---------- 供应商（API 账户）----------
  async function walletData(fresh) { if (fresh || !ui.wallet) ui.wallet = await api("/api/wallet"); return ui.wallet || { accounts: [], found: [], presets: {} }; }
  function walletHTML(w) {
    const bal = ui.walletBal || {};
    const rows = w.accounts.map(a => `<div class="row wa"><i class="wa-dot" style="background:${esc(a.color)}"></i><div class="lbl"><b>${esc(a.name)}</b>
        <span>${esc(a.provider_name)} · ${a.from === ((S.platform || {}).keystore || "钥匙串") ? esc(a.from) : "关联 " + esc(a.from)} · ${a.models} 个模型${a.ok ? "" : " · <em class=warn>读不到 key</em>"}</span></div>
      ${a.can_balance ? `<span class="pill gold wa-bal">${bal[a.id] ? esc(bal[a.id]) : "…"}</span>` : ""}
      <div class="stepper" title="同时最多几个活用这个账户"><button data-wlim="${esc(a.id)}" data-d="-1">−</button><b>${a.limit}</b><button data-wlim="${esc(a.id)}" data-d="1">+</button></div>
      <button class="ib" data-wrm="${esc(a.id)}" title="移出供应商">${icon("close")}</button></div>`).join("");
    const found = w.found.filter(f => !f.added);
    return `<h2>供应商 <span class="small muted" style="font-weight:400">· 接入的 API 账户</span></h2><div class="card">
      ${rows || `<div class="row"><div class="lbl"><b>还没有账户</b><span>关联本机已有的 key，或者填一个新的，就能招募 API 队员</span></div></div>`}
      ${found.length ? `<div class="row col"><div class="lbl"><b>本机发现 ${found.length} 个 key</b><span>关联 = 用的时候去原处读，不复制、不显示</span></div>
        <div class="found">${found.map(f => `<button class="fk" data-wlink="${esc(f.fp)}"><b>${esc(f.name)}</b><span>${esc(f.from)}${f.also ? " · " + esc(f.also.join("、")) : ""}${f.models ? " · " + f.models + " 个模型" : ""}</span><em>${icon("plus")}关联</em></button>`).join("")}</div></div>` : ""}
      <div class="row-end"><button class="btn sm primary" data-apiwiz>🔌 接入 API</button></div></div>`;
  }
  async function paintWallet(fresh) {
    const g = $("#wallet-g"); if (!g) return;
    const w = await walletData(fresh);
    part("wallet", g, walletHTML(w));
    ui.walletBal ||= {};
    for (const a of w.accounts.filter(a => a.can_balance && !ui.walletBal[a.id])) {
      api("/api/wallet/balance?id=" + encodeURIComponent(a.id)).then(b => { if (b && b.ok) { ui.walletBal[a.id] = b.text; painted.wallet = null; paintWallet(); } });
    }
  }
  document.addEventListener("click", async e => {
    if (route.page !== "settings") return;
    const lk = e.target.closest("[data-wlink]"), rm = e.target.closest("[data-wrm]"), lim = e.target.closest("[data-wlim]");
    if (!lk && !rm && !lim) return;
    if (lk && await api("/api/wallet/link", { fp: lk.dataset.wlink })) toast("关联好了", "coin");
    if (rm) { const a = ui.wallet.accounts.find(x => x.id === rm.dataset.wrm); if (!(await askBox(`把「${a.name}」移出供应商？`, a.from === ((S.platform || {}).keystore || "钥匙串") ? "存在本机的 key 会一起删掉" : "只是取消关联，原来那里的 key 不动", "移出", true))) return; await api("/api/wallet/remove", { id: a.id }); }
    if (lim) { const a = ui.wallet.accounts.find(x => x.id === lim.dataset.wlim); await api("/api/wallet/update", { id: a.id, limit: Math.max(1, Math.min(20, a.limit + +lim.dataset.d)) }); }
    await walletData(true); painted.wallet = null; paintWallet();
  });

  async function pSettings() {
    shell(`<div class="head"><div><h1>设置</h1><p>改完立即生效，悬浮窗、应用窗口和右侧浏览器同步。</p></div></div><div class="withnav"><nav class="sidenav" id="set-nav"></nav><div class="set" id="set"></div></div>`);
    // 分栏：一次只看一组，记住上次看的是哪组
    if (route.id && SET_TABS.some(x => x[0] === route.id)) ui.setTab = route.id; // 悬浮窗点「手动配置」直接跳到 #settings/connect
    ui.setTab ??= (() => { try { const t = localStorage.getItem("setTab"); return SET_TABS.some(x => x[0] === t) ? t : "wallet"; } catch { return "wallet"; } })();
    part("setnav", $("#set-nav"), SET_TABS.map(([k, t]) => `<button class="${ui.setTab === k ? "on" : ""}" data-settab="${k}">${t}</button>`).join(""));
    $("#set").dataset.tab = ui.setTab;
    const st = S.settings, h = st.hud;
    const sw = (k, on) => `<button class="sw ${on ? "on" : ""}" data-sw="${k}" role="switch" aria-checked="${on}"></button>`;
    const segm = (k, val, opts) => `<div class="seg">${opts.map(([v, t]) => `<button class="${String(val) === String(v) ? "on" : ""}" data-seg="${k}" data-v="${v}">${t}</button>`).join("")}</div>`;
    if (S.project_key && (!projInfo || projInfo.name !== S.project_key)) projInfo = await api("/api/project?name=" + encodeURIComponent(S.project_key));
    const proj = S.project_key ? projInfo : null;
    const editing = document.activeElement && ["pj-root", "pj-brief"].includes(document.activeElement.id);
    if (editing) return; // 正在打字时不重画
    setTimeout(() => paintWallet(), 0);
    setTimeout(() => loadConnect(), 0);
    if (part("set", $("#set"), `
      <div class="group" data-g="proj"><h2>当前项目</h2><div class="card">
        <div class="row"><div class="lbl"><b>${esc(S.project_key ? S.project : "无项目")}</b><span>${S.project_key ? "这个项目有自己的阵容和职能配置" : "通用阵容；派活记在“通用”下"}</span></div><button class="btn sm" id="sw-proj">${icon("folder")}切换 / 新建</button></div>
        ${proj ? `<div class="row col"><div class="lbl"><b>项目文件夹</b><span>agent 在这里干活</span></div><input class="inp" id="pj-root" value="${esc(proj.root)}"></div>
        <div class="row col"><div class="lbl"><b>项目说明</b><span>所有 agent 接活时都会先读这一段</span></div><textarea class="inp" id="pj-brief" rows="8">${esc(proj.brief)}</textarea>
          <div class="row-end"><button class="btn ghost sm" id="pj-rm">移除这个项目</button><button class="btn primary sm" id="pj-save">保存</button></div></div>` : ""}
      </div></div>
      <div class="group" id="connect-g" data-g="connect"><h2>接入</h2><div class="card"><div class="empty small">读取中…</div></div></div>
      <div class="group" id="wallet-g" data-g="wallet"><h2>供应商</h2><div class="card"><div class="empty small">读取中…（第一次要扫一下本机的 key，稍等）</div></div></div>
      <div class="group" data-g="chat"><h2>聊天室 <span class="small muted" style="font-weight:400">· 每个频道的默认规则和上限</span></h2><div class="card">
        <div class="row"><div class="lbl"><b>没 @ 人时谁来回（默认）</b><span>自动 = 优先用便宜的 API 队员，没有就用主位办公室战备里的第一位。每个频道还能在频道设置里单独改</span></div>
          <select class="inp sm" data-chatset="duty"><option value="auto" ${st.chat.duty === "auto" ? "selected" : ""}>自动（推荐）</option><option value="none" ${st.chat.duty === "none" ? "selected" : ""}>没人回（只记录）</option></select></div>
        ${[["talk_rounds", "群聊默认几轮", "每轮每人最多说一次", 1, 3], ["talk_max", "一次群聊最多几条", "到了就收尾，防止聊个没完", 2, 30], ["talk_chars", "群聊每条最多几个字", "超出的截掉", 100, 3000],
           ["context", "给队员的上下文最多几个字", "压缩要点 + 最近的消息，放满为止", 2000, 40000], ["compress_at", "没压缩的部分到多少字自动压缩", "用 API 队员把旧消息压成要点；没有 API 队员就截断", 4000, 100000],
           ["max_running", "一个频道同时最多几单在跑", "多了的不派，提示等前面做完", 1, 10], ["hops", "队员之间互相 @ 最多接力几次", "防止两个队员互相踢皮球（主人 @ 的不算）", 1, 10],
           ["max_text", "一条消息最多几个字", "超出的截掉", 500, 50000]]
          .map(([k, t, d, lo, hi]) => `<div class="row"><div class="lbl"><b>${t}</b><span>${d}</span></div><input class="inp sm num" type="number" min="${lo}" max="${hi}" value="${esc(st.chat[k])}" data-chatset="${k}"></div>`).join("")}
      </div></div>
      <div class="group" data-g="sched"><h2>调度</h2><div class="card">
        <div class="row"><div class="lbl"><b>屏幕锁</b><span>录屏、操控电脑的活一次只放一个，其余排队${(S.scheduler || {}).screen && S.scheduler.screen.length ? " · 现在 " + esc(S.scheduler.screen.join("、")) + " 在用" : ""}</span></div>${sw("sched.screen_lock", (S.scheduler || {}).screen_lock !== false)}</div>
        ${[["codex", "GPT 额度"], ["deepseek", "DeepSeek 账户"], ["claude", "Claude 额度"]].map(([b, t]) => { const sc = S.scheduler || { limits: {}, used: {} }; return `<div class="row"><div class="lbl"><b>${t}同时最多</b><span>超过就排队${sc.used && sc.used[b] ? ` · 现在 ${sc.used[b]} 个在跑` : ""}</span></div>
          <div class="stepper"><button data-slim="${b}" data-d="-1">−</button><b>${sc.limits[b] || "—"}</b><button data-slim="${b}" data-d="1">+</button></div></div>`; }).join("")}
        <div class="row"><div class="lbl"><b>API 账户</b><span>每个账户的上限在上面供应商里调</span></div></div>
      </div></div>
      <div class="group" data-g="hud"><h2>悬浮窗</h2><div class="card">
        <div class="row"><div class="lbl"><b>显示悬浮窗</b><span>挂在屏幕边上，随时看到谁在干活、额度还剩多少</span></div>${sw("hud.enabled", h.enabled)}</div>
        <div class="row"><div class="lbl"><b>位置</b><span>也可以直接拖着胶囊放到屏幕任意位置，靠近边缘会自动吸附</span></div>
          <div class="dock-pick">${[["top", "顶部"], ["bottom", "底部"], ["right", "右侧"], ["left", "左侧"], ["float", "悬浮"]].map(([v, t]) => `<button class="dock-opt ${h.dock === v ? "on" : ""}" data-seg="hud.dock" data-v="${v}"><span class="mini-screen"><i class="${v}"></i></span>${t}</button>`).join("")}</div></div>
        <div class="row"><div class="lbl"><b>显示方式</b><span>头像：黑色简洁；场景：全身 Q 版 + 办公室背景</span></div>${segm("hud.view", h.view, [["avatar", "头像"], ["scene", "场景"]])}</div>
        <div class="row"><div class="lbl"><b>专注模式</b><span>悬浮窗只保持折叠，不自动展开、点了也不展开；鼠标移上去（或点一下）右边弹出一张小卡片看进度</span></div>${sw("hud.focus", h.focus)}</div>
        <div class="row"><div class="lbl"><b>派活时自动弹出</b><span>有任务开始或完成时，悬浮窗自己展开到最上层（专注模式下不弹）</span></div>${sw("hud.autopop", h.autopop)}</div>
        <div class="row"><div class="lbl"><b>弹出停留</b><span>自动展开后多久收起</span></div>${segm("hud.stay", h.stay, [[4, "4 秒"], [6, "6 秒"], [10, "10 秒"], [0, "不收起"]])}</div>
        <div class="row"><div class="lbl"><b>空闲时缩成小球</b><span>没人干活多久后，只留一个小圆头像</span></div>${segm("hud.ball_min", h.ball_min ?? 3, [[3, "3 分钟"], [10, "10 分钟"], [0, "不缩"]])}</div>
        <div class="row"><div class="lbl"><b>空闲时自动隐藏</b><span>有活开工会自己出来；也可以从菜单栏图标里打开</span></div>${segm("hud.hide_min", h.hide_min ?? 20, [[20, "20 分钟"], [60, "1 小时"], [0, "不隐藏"]])}</div>
        <div class="row"><div class="lbl"><b>完成提醒</b><span>任务完成时弹出完成卡片</span></div>${sw("hud.notify_done", h.notify_done)}</div>
      </div></div>
      <div class="group" data-g="general"><h2>通用</h2><div class="card">
        <div class="row"><div class="lbl"><b>自动装配</b><span>重新看一遍这台电脑上的 agent，按能力分好职能、排好阵容</span></div><button class="btn sm" data-setup>重新评估</button></div>
        <div class="row"><div class="lbl"><b>开机自动打开</b><span>开机登录后自动启动 agentbus</span></div>${sw("login", st.login)}</div>
        <div class="row"><div class="lbl"><b>外观</b></div>${segm("theme", st.theme, [["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]])}</div>
        <div class="row"><div class="lbl"><b>语言</b><span>默认跟随系统：中文系统显示中文，其他显示英文</span></div><div>${segm("lang", st.lang || "system", [["system", "跟随系统"], ["en", "English"], ["zh", "<span data-notr>中文</span>"]])}</div></div>
      </div></div>
      <div class="group" data-g="general"><h2>接入的 agent</h2><div class="card">
        ${S.agents.map(a => `<a class="row link" href="#hero/${encodeURIComponent(a.id)}">${avatar(a, 34)}<div class="lbl"><b>${esc(a.name)}</b><span>${esc(a.desc || "")}</span></div><span class="pill ${a.installed ? "hp" : ""}">${a.installed ? "已接入" : "未安装"}</span></a>`).join("")}
      </div></div>
      <div class="group" data-g="about"><h2>关于</h2><div class="card">
        <div class="row"><div class="lbl"><b>agentbus ${esc(st.version || "")}</b><span>本地服务 <span class="code-inline">http://127.0.0.1:8790</span> · 数据在 <span class="code-inline">~/.agentbus</span></span></div></div>
        <div class="row"><div class="lbl"><b>检查更新</b><span>从 GitHub 的发布页（Releases）看有没有新版本，每 6 小时自动查一次；有新版本会在基地顶上提示</span></div><button class="btn sm" data-updcheck>现在检查</button></div>
        <div class="row"><div class="lbl"><b>GitHub 仓库</b><span>owner/name，比如 someone/agentbus</span></div><input class="inp sm" style="width:220px" data-updrepo value="${esc(st.update_repo || "")}" placeholder="owner/name"></div>
        <div class="row col" id="upd-res" hidden></div>
        <div class="row"><div class="lbl"><b>新手引导</b><span>一步步看每个地方是干什么的</span></div><button class="btn sm" data-tour>重新看一遍</button></div>
        <div class="row"><div class="lbl"><b>在主位里怎么派活</b><span>单派一个、同时派几个、看结果，带示例说法</span></div><button class="btn sm" data-tour="lead">看看</button></div>
        <div class="row"><div class="lbl"><b>接入主位</b><span>把 agentbus 的工具（派活、看额度、握手）装进主位 ${esc((A(S.lead) || {}).name || "")}，装一次就行</span></div><button class="btn sm" data-connect-lead>接入</button></div>
        <div class="row"><div class="lbl"><b>在主位里用</b><span>派活 <span class="code-inline">agentbus ask &lt;队员或职能&gt; "任务" --tag 职能</span>；MCP 工具 <span class="code-inline">agentbus_*</span></span></div></div>
      </div></div>`)) {
      painted.wallet = null;
      const save = $("#pj-save");
      if (save) save.onclick = async () => { if (await api("/api/project/brief", { name: S.project_key, brief: $("#pj-brief").value, root: $("#pj-root").value.trim() })) { projInfo = null; toast("项目说明已保存", "folder"); } };
      $("#sw-proj").onclick = e => { e.stopPropagation(); openProjPop($("#sw-proj")); };

      const rm = $("#pj-rm");
      if (rm) rm.onclick = async () => {
        if (rm.dataset.sure !== "1") { rm.dataset.sure = "1"; rm.textContent = "再点一次确认移除（文件不会删）"; return; }
        const n = S.project_key;
        if (await api("/api/project/remove", { name: n })) { projInfo = null; toast(`已移除项目「${n}」，文件都还在`, "folder"); }
      };
    }
  }
  document.addEventListener("click", async e => {
    if (route.page !== "settings") return;
    const s = e.target.closest("[data-sw]"), g = e.target.closest("[data-seg]");
    const set = async (k, v) => {
      const body = k.startsWith("hud.") ? { hud: { [k.slice(4)]: v } } : { [k]: v };
      if (k.startsWith("hud.")) S.settings.hud[k.slice(4)] = v; else S.settings[k] = v;
      if (k === "theme") applyTheme(v);
      painted.set = null; pSettings();
      if (await api("/api/settings", body)) { toast("已保存", "settings"); if (k === "lang") I18N.sync(v); }
    };
    const sl = e.target.closest("[data-slim]");
    if (sl) { const sc = S.scheduler; const n = Math.max(1, Math.min(20, (sc.limits[sl.dataset.slim] || 3) + +sl.dataset.d)); sc.limits[sl.dataset.slim] = n; painted.set = null; pSettings(); if (await api("/api/settings", { scheduler: { limits: { [sl.dataset.slim]: n } } })) toast("已保存", "settings"); return; }
    if (s && s.dataset.sw === "sched.screen_lock") { const v = !s.classList.contains("on"); S.scheduler.screen_lock = v; painted.set = null; pSettings(); if (await api("/api/settings", { scheduler: { screen_lock: v } })) toast(v ? "屏幕锁开了" : "屏幕锁关了", "settings"); return; }
    if (s) return set(s.dataset.sw, !s.classList.contains("on"));
    if (g) { const v = g.dataset.v; return set(g.dataset.seg, /^\d+$/.test(v) ? Number(v) : v); }
  });

  fetch("/api/catalog").then(r => r.json()).then(c => { CAT = c; c.categories.forEach(x => (catName[x.id] = x.name)); painted = { __shell: painted.__shell }; S && paint(); });
  connect();
})();
