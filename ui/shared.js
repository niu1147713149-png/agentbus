// 系统标记：Windows 换字体和滚动条（看 UA 就行，不用等服务器）
if (/Windows/i.test(navigator.userAgent)) document.documentElement.classList.add("os-windows");
// ---------- 语言：跟随系统（中文系统显示中文，其他英文），设置里可以固定 ----------
// 界面文案写的是中文；英文模式下按 i18n/en.js 的词典把页面上的文字节点和 title / placeholder 换成英文。
// 带变量的文案在词典里是「{0} 位待命」这样的模板。用户自己写的内容（聊天消息、任务）放在 [data-notr] 里，不翻。
(function (g) {
  const pick = v => v === "en" || v === "zh" ? v : (navigator.language || "en").toLowerCase().startsWith("zh") ? "zh" : "en";
  let pref = "system";
  try { pref = localStorage.getItem("ab-lang") || "system"; } catch (e) { /* 无痕窗口等：按系统 */ }
  const lang = pick(pref);
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  const D = g.AB_EN || {}, CJK = /[㐀-鿿！-～　-〿]/;
  const pats = [];
  if (lang === "en") {
    for (const k in D) {
      if (!/\{\d+\}/.test(k)) continue;
      const lit = k.replace(/\{\d+\}/g, "");
      if (!CJK.test(lit)) continue; // 只有变量没有中文的不当模板
      const re = new RegExp("^" + k.split(/\{\d+\}/).map(x => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("([\\s\\S]*?)") + "$");
      pats.push({ re, t: D[k], w: lit.length });
    }
    pats.sort((a, b) => b.w - a.w); // 字面部分长的更具体，先试
  }
  const memo = new Map(), missing = new Set();
  function tr(s) {
    if (lang !== "en" || !s || !CJK.test(s)) return s;
    if (memo.has(s)) return memo.get(s);
    const lead = s.match(/^\s*/)[0], tail = s.match(/\s*$/)[0], core = s.trim().replace(/\s+/g, " ");
    let out = D[core];
    if (out == null) {
      for (const p of pats) {
        const m = core.match(p.re);
        if (m) { out = p.t.replace(/\{(\d+)\}/g, (_, i) => tr(m[+i + 1] || "")); break; }
      }
    }
    if (out == null && /( · |，|；|：|、)/.test(core)) { // 整句没有：按分隔符拆开分别译（比如「{名字} · 工作中」）
      const parts = core.split(/( · |，|；|：|、)/);
      const sep = { "，": ", ", "；": "; ", "：": ": ", "、": ", " };
      const got = parts.map((p, i) => i % 2 ? (sep[p] || p) : tr(p));
      if (got.some((p, i) => i % 2 === 0 && p !== parts[i])) out = got.join("");
    }
    if (out == null) { missing.add(core); out = core; }
    const r = lead + out + tail;
    if (memo.size > 5000) memo.clear();
    memo.set(s, r);
    return r;
  }
  const ATTRS = ["title", "placeholder", "aria-label", "alt"];
  const skip = n => { const e = n.nodeType === 1 ? n : n.parentElement; return !e || !!e.closest("[data-notr],script,style,pre,code"); };
  function fixText(n) { if (CJK.test(n.nodeValue) && !skip(n) && !(n.parentElement && n.parentElement.closest("textarea"))) { const t = tr(n.nodeValue); if (t !== n.nodeValue) n.nodeValue = t; } }
  function fixAttrs(e) { for (const a of ATTRS) { const v = e.getAttribute && e.getAttribute(a); if (v && CJK.test(v) && !skip(e)) { const t = tr(v); if (t !== v) e.setAttribute(a, t); } } }
  function walk(root) {
    if (root.nodeType === 3) return fixText(root);
    if (root.nodeType !== 1) return;
    fixAttrs(root);
    const it = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let n = it.nextNode(); n; n = it.nextNode()) n.nodeType === 3 ? fixText(n) : fixAttrs(n);
  }
  if (lang === "en") {
    new MutationObserver(ms => { // 微任务里换，浏览器画出来之前就已经是英文
      for (const m of ms) {
        if (m.type === "childList") m.addedNodes.forEach(walk);
        else if (m.type === "characterData") fixText(m.target);
        else fixAttrs(m.target);
      }
    }).observe(document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS });
    document.addEventListener("DOMContentLoaded", () => { walk(document.body); document.title = tr(document.title); });
    for (const f of ["alert", "confirm", "prompt"]) { const o = g[f]; g[f] = (msg, ...r) => o.call(g, tr(String(msg ?? "")), ...r); }
  }
  // 服务端存的语言和本地缓存不一样（在另一个窗口改过）：记下来，重新加载一次
  function sync(v) {
    v = v || "system";
    if (v === pref) return;
    try { localStorage.setItem("ab-lang", v); } catch (e) { return; }
    if (pick(v) !== lang) location.reload(); else pref = v;
  }
  g.I18N = { lang, pref: () => pref, tr, sync, missing, walk };
})(window);

// 主界面和悬浮窗共用的小工具：图标、头像、状态文案、数字格式。
(function (g) {
  const P = {
    pencil: '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/>',
    pill: '<rect x="3" y="8" width="18" height="8" rx="4"/><circle cx="7.5" cy="12" r="1.6" fill="currentColor"/>',
    chat: '<path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 3.5V17h0A2.5 2.5 0 0 1 4 14.5z"/><path d="M8.5 9.5h7M8.5 12.5h4.5"/>',
    office: '<path d="M4 20V8.5L12 4l8 4.5V20"/><path d="M10 20v-4h4v4"/><path d="M8.5 10.5h1.5M14 10.5h1.5M8.5 13.5h1.5M14 13.5h1.5"/><path d="M2.5 20h19"/>',
    base: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20h14V9.5"/><path d="M10 20v-5h4v5"/>',
    team: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19c.6-3.2 2.8-5 5.5-5s4.9 1.8 5.5 5"/><circle cx="17" cy="9" r="2.4"/><path d="M15.5 14.2c2.3.2 4 1.8 4.5 4.8"/>',
    skills: '<path d="M12 3.5l2.3 4.9 5.2.6-3.9 3.6 1.1 5.2L12 15.2l-4.7 2.6 1.1-5.2L4.5 9l5.2-.6z"/>',
    quests: '<path d="M6 4h10a2 2 0 0 1 2 2v13l-3-2-3 2-3-2-3 2V6"/><path d="M9 8.5h6M9 12h4"/>',
    report: '<path d="M7 4h10v3a5 5 0 0 1-10 0z"/><path d="M7 5.5H4.5a2.5 2.5 0 0 0 3 3.6M17 5.5h2.5a2.5 2.5 0 0 1-3 3.6"/><path d="M12 12v4M8.5 20h7M9.5 16h5v4h-5z"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2.8v2.4M12 18.8v2.4M4.2 7.5l2 1.2M17.8 15.3l2 1.2M4.2 16.5l2-1.2M17.8 8.7l2-1.2"/><circle cx="12" cy="12" r="7.2"/>',
    coin: '<ellipse cx="12" cy="12" rx="8" ry="8"/><path d="M12 7.5v9M9.5 9.5c0-1 1-1.7 2.5-1.7s2.5.7 2.5 1.6c0 2.4-5 1.3-5 3.8 0 1 1.1 1.7 2.5 1.7s2.5-.7 2.5-1.7"/>',
    bolt: '<path d="M13 3 5.5 13.5H12L11 21l7.5-10.5H12z"/>',
    clock: '<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 2"/>',
    check: '<path d="M5 12.5l4.2 4.2L19 7"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    chev: '<path d="M9 6l6 6-6 6"/>',
    down: '<path d="M6 9l6 6 6-6"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="3"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/>',
    open: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    sofa: '<path d="M4 11V9a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v2"/><path d="M3 12a1.5 1.5 0 0 1 3 0v2h12v-2a1.5 1.5 0 0 1 3 0v5H3z"/><path d="M5 17v2M19 17v2"/>',
    star: '<path d="M12 4l2.2 4.6 5 .6-3.7 3.4 1 5L12 15.1 7.5 17.6l1-5L4.8 9.2l5-.6z" fill="currentColor"/>',
    ban: '<circle cx="12" cy="12" r="7.5"/><path d="M6.8 17.2 17.2 6.8"/>',
    sub: '<circle cx="12" cy="12" r="6.5"/>',
    collapse: '<path d="M7 14l5-5 5 5"/>',
    pin: '<path d="M9 4h6l-1 5 3 3v1H7v-1l3-3z"/><path d="M12 13v7"/>',
    folder: '<path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
    gift: '<rect x="4" y="9" width="16" height="11" rx="2"/><path d="M12 9v11M4 13h16M12 9c-2-4-6-3.5-5-1s5 1 5 1 3.5 1.5 5-1-3-3-5 1"/>',
  };
  const icon = (n, cls = "") => `<svg class="i ${cls}" viewBox="0 0 24 24" aria-hidden="true">${P[n] || ""}</svg>`;

  // 没有定制形象的 agent：默认皮肤小机器人
  const robot = (color, cls = "") => `<svg class="robot ${cls}" viewBox="0 0 120 120" aria-hidden="true">
      <line x1="60" y1="14" x2="60" y2="28" stroke="${color}" stroke-width="5" stroke-linecap="round"/>
      <circle cx="60" cy="12" r="7" fill="${color}"/>
      <rect x="18" y="28" width="84" height="70" rx="30" fill="${color}"/>
      <rect x="28" y="40" width="64" height="44" rx="20" fill="#fff" opacity=".94"/>
      <g class="eyes"><ellipse cx="46" cy="61" rx="6" ry="7.5" fill="#2a2724"/><ellipse cx="74" cy="61" rx="6" ry="7.5" fill="#2a2724"/>
      <circle cx="48" cy="58" r="2" fill="#fff"/><circle cx="76" cy="58" r="2" fill="#fff"/></g>
      <ellipse cx="38" cy="73" rx="5" ry="3" fill="#ff9e9e" opacity=".55"/><ellipse cx="82" cy="73" rx="5" ry="3" fill="#ff9e9e" opacity=".55"/>
      <path d="M54 74q6 5 12 0" stroke="#2a2724" stroke-width="3" fill="none" stroke-linecap="round"/>
      <rect x="10" y="52" width="10" height="20" rx="5" fill="${color}"/><rect x="100" y="52" width="10" height="20" rx="5" fill="${color}"/>
    </svg>`;

  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // 头像：有立绘的用立绘头像，没有的用小机器人
  // API 队员：通用形象 + 厂商标识。大头像把图标放在耳机的空白圆牌上，小头像放右下角
  const brandMark = (a, cls, base = "") => a.brand ? `<img class="${cls}" src="${base}${esc(a.brand)}" alt="" draggable="false">`
    : `<i class="${cls} lt" style="--c:${esc(a.color)}">${esc((a.provider_name || a.name || "A").slice(0, 1))}</i>`;
  const avatar = (a, size = 40, base = "") => a.avatar
    ? `<span class="av ${a.api ? "api-av" : ""}" style="--s:${size}px;--c:${esc(a.color)}"><img src="${base}${esc(a.avatar)}" alt="" draggable="false">${a.api ? brandMark(a, size >= 52 ? "bd-disc" : "bd-corner", base) : ""}</span>`
    : a.api ? `<span class="av letter" style="--s:${size}px;--c:${esc(a.color)}"><b>${esc((a.provider_name || a.name || "A").slice(0, 1))}</b></span>`
    : `<span class="av bot" style="--s:${size}px;--c:${esc(a.color)}">${robot(a.color)}</span>`;

  const STATE = {
    working: { t: "工作中", c: "var(--st-working)" },
    break: { t: "摸鱼中", c: "var(--st-break)" },
    off: { t: "睡觉中", c: "var(--st-off)" },
    manual: { t: "等传话", c: "var(--st-manual)" },
    queued: { t: "排队中", c: "#e8a23a" },
    absent: { t: "未招募", c: "var(--st-absent)" },
  };
  const LEVEL = { prefer: "主力", ok: "替补", avoid: "禁用" };
  const BILLING = { codex: "体力", claude: "体力", deepseek: "金币", free: "免费", manual: "网页", none: "未接入" };

  const money = v => v == null ? "—" : "¥" + Number(v).toFixed(Math.abs(v) < 10 ? 2 : 1);
  const mins = s => s == null ? "—" : s < 60 ? `${Math.round(s)} 秒` : s < 3600 ? `${Math.round(s / 60)} 分钟` : `${(s / 3600).toFixed(1)} 小时`;
  const clock = s => { s = Math.max(0, Math.floor(s)); const m = Math.floor(s / 60), h = Math.floor(m / 60); return h ? `${h}:${String(m % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}` : `${m}:${String(s % 60).padStart(2, "0")}`; };
  const costText = c => {
    if (!c || !c.ok) return "";
    if (c.cny != null) return "金币 " + money(c.cny);
    const w = c.weekly || 0, f = c.five_hour || 0;
    if (w || f) return `体力 ${f ? "-" + f + "%" : ""}${w ? (f ? " · 周 -" : "周 -") + w + "%" : ""}`;
    return "体力 <1%";
  };
  const resetText = ts => { if (!ts) return ""; const d = new Date(ts * 1000), n = new Date(); const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; return d.toDateString() === n.toDateString() ? `今天 ${hm} 恢复` : `${d.getMonth() + 1}/${d.getDate()} ${hm} 恢复`; };

  const U = s => `<span data-notr>${esc(s)}</span>`; // 用户自己写的内容（任务、回复、消息）：不翻译
  g.AB = { U, tr: g.I18N.tr, lang: g.I18N.lang, brandMark, icon, robot, esc, avatar, STATE, LEVEL, BILLING, money, mins, clock, costText, resetText };
})(window);
