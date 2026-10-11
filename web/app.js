/* coder-1 web — sohbet + canlı olay akışı + Monaco editör + terminal.
 * Backend: agent/main.py'nin /api/* uçları. Sadece HTTP polling (WS yok).
 * API tabanı: aynı origin; farklıysa ?api=http://host:port veya
 * localStorage "coder1.api" ile ayarlanır.
 */

let API = (() => {
  const q = new URLSearchParams(location.search).get("api");
  if (q) localStorage.setItem("coder1.api", q);
  return (localStorage.getItem("coder1.api") || location.origin)
    .replace(/\/+$/, "");
})();

/* Canlı backend adresi: tünel değişince Vercel yeniden deploy'u beklemeden
 * Edge Config'ten (salt okunur anahtar) güncel adresi alır. ?api= verilmişse
 * ya da localhost'taysak dokunmaz. */
const EDGE = { id: "ecfg_tquihxfzp6zgddt2sx4vtfcpkwid", token: "db70e0dd-5fbd-4e73-8235-a036551c77a7" };
const API_PINNED = !!localStorage.getItem("coder1.api") || /^https?:\/\/(localhost|127\.)/.test(location.origin);
let _resolvedAt = 0;
async function resolveBackend(force) {
  if (API_PINNED || !EDGE.id) return false;
  if (!force && Date.now() - _resolvedAt < 15000) return false;
  _resolvedAt = Date.now();
  try {
    const r = await fetch(`https://edge-config.vercel.com/${EDGE.id}/item/backend`,
      { headers: { Authorization: "Bearer " + EDGE.token }, signal: AbortSignal.timeout(4000) });
    const u = await r.json();
    if (typeof u !== "string" || !/^https?:\/\//.test(u)) return false;
    const base = u.replace(/\/+$/, "");
    if (base === API) return false;
    const h = await fetch(base + "/healthz", { signal: AbortSignal.timeout(6000) }).catch(() => null);
    if (h && h.ok) { API = base; return true; }
  } catch { /* edge yok → aynı origin'den devam */ }
  return false;
}
const backendReady = resolveBackend(true);
async function fetchApi(path, init) {
  await backendReady;
  const go = () => fetch(API + path, init);
  let r = null, err = null;
  try { r = await go(); } catch (e) { err = e; }
  if (r && r.status < 502) return r;
  if (await resolveBackend(true)) {        // adres değişmiş → yeni adrese tekrar dene
    try { return await go(); } catch (e) { err = e; }
  }
  if (r) return r;
  throw err;
}

const POLL_MS = 1500;
const $ = (id) => document.getElementById(id);
const chatLog = $("chat-log"), termLog = $("term-log"), tree = $("file-tree");
const input = $("input"), btnSend = $("btn-send"), runBar = $("run-bar");
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

const S = {
  taskId: null,
  seen: 0,
  running: false,
  timer: null,
  pollTimer: null,
  startAt: 0,
  steps: 0,
  sawStep: false,
  todos: new Map(),      // i -> {state, text?}
  todoCard: null,
  sawContent: false,     // görünür cevap/araç geldi mi
  currentFile: null,
  fileCache: new Map(),  // path -> content
  autoOpened: false,
  awaiting: null,       // açık soru kartı {answers, questions, card}
  status: "idle",
};
const APP = document.querySelector(".app");
const NAME = () => localStorage.getItem("c1.name") || "";
const PHASES = { anla: "isteği anlıyor", planla: "planlıyor", yaz: "kod yazıyor",
  dogrula: "test ediyor", onar: "hatayı düzeltiyor", duzenle: "projeyi düzenliyor", rapor: "raporluyor" };

/* ---------- yardımcılar ---------- */

const esc = (s) => String(s ?? "");

/* ---------- giriş (parola) ---------- */

let TOK = localStorage.getItem("c1.tok") || "";
const withTok = (p) =>
  p + (p.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOK);

function showLogin() {
  $("login").classList.remove("hidden");
  setTimeout(() => $("login-pass").focus(), 60);
}
function hideLogin() {
  const l = $("login");
  if (l.classList.contains("hidden")) return;
  l.classList.add("leaving");
  setTimeout(() => { l.classList.remove("leaving"); l.classList.add("hidden"); }, 350);
}

function authFail(r) {
  if (r.status === 401) {
    TOK = ""; localStorage.removeItem("c1.tok");
    showLogin();
    return true;
  }
  return false;
}

async function apiGet(path) {
  const r = await fetchApi(withTok(path));
  if (authFail(r)) throw new Error("yetkisiz");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
async function apiPost(path, body) {
  const r = await fetchApi(withTok(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (authFail(r)) throw new Error("yetkisiz");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

let authMode = "login";
$("login-mode").addEventListener("click", () => {
  authMode = authMode === "login" ? "register" : "login";
  $("login-btn").textContent = authMode === "login" ? "Giriş yap" : "Kayıt ol";
  $("login-q").textContent = authMode === "login" ? "Hesabın yok mu?" : "Hesabın var mı?";
  $("login-mode").textContent = authMode === "login" ? "Kayıt ol" : "Giriş yap";
  $("login-err").classList.add("hidden");
});
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    name: $("login-name").value.trim(),
    password: $("login-pass").value,
  };
  try {
    const r = await fetchApi("/api/" + authMode, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (r.ok && j.token) {
      TOK = j.token; localStorage.setItem("c1.tok", TOK);
      if (j.name) localStorage.setItem("c1.name", j.name);
      hideLogin(); $("login-err").classList.add("hidden");
      paintUser(); loadModel();
    } else {
      $("login-err").textContent = j.detail || (r.status >= 500 ? "sunucu uyanıyor, birkaç saniye sonra tekrar dene" : "giriş başarısız");
      $("login-err").classList.remove("hidden");
    }
  } catch {
    $("login-err").textContent = "sunucuya ulaşılamadı";
    $("login-err").classList.remove("hidden");
  }
});
if (!TOK) showLogin();

function nearBottom(node, px = 80) {
  return node.scrollHeight - node.scrollTop - node.clientHeight < px;
}
function scrollIfClose(node) {
  if (nearBottom(node)) node.scrollTop = node.scrollHeight;
}
function forceScroll(node) { node.scrollTop = node.scrollHeight; }

function fmtArgs(call) {
  const a = call.args || {};
  if (call.name === "run_command") return a.cmd || "";
  if (a.path != null) return a.path;
  if (a.cmd != null) return a.cmd;
  try { return JSON.stringify(a); } catch { return ""; }
}

/* model metninden araç JSON kuyruğunu ayıkla */
function cleanModelText(t) {
  return esc(t)
    .replace(/<\|tool\|>[\s\S]*?(<\|im_end\|>)?\s*$/u, "")
    .replace(/<\|im_(start|end)\|>/g, "")
    .trim();
}

/* ---------- sohbet ---------- */

function welcomeGone() {
  const w = chatLog.querySelector(".welcome");
  if (w) w.remove();
}

function addVote(bubble, text) {
  const row = el("div", "vote-row");
  ["👍", "👎"].forEach((g, i) => {
    const b = el("button", "vote-btn", g);
    b.onclick = () => {
      apiPost("/api/feedback", { rating: i === 0 ? 1 : -1,
        task_id: S.taskId, text: text.slice(0, 2000),
        name: localStorage.getItem("c1.name") || "" }).catch(() => {});
      row.textContent = i === 0 ? "teşekkürler — iyi cevap eğitime girecek" : "not alındı";
      row.classList.add("voted");
    };
    row.appendChild(b);
  });
  bubble.appendChild(row);
}

function mdLite(t) {
  return String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]))
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\n/g, "<br>");
}

function addMsg(cls, text) {
  welcomeGone();
  const keep = nearBottom(chatLog);
  const b = el("div", `msg ${cls}`);
  b.appendChild(el("div", "msg-body", text));
  chatLog.appendChild(b);
  if (keep) forceScroll(chatLog);
  return b;
}

function addToolCard(call) {
  welcomeGone();
  const keep = nearBottom(chatLog);
  const card = el("div", "tool-card");
  card.appendChild(el("span", "tool-ico", "⚙"));
  const main = el("div", "tool-main");
  const line = el("div", "tool-line");
  line.appendChild(el("b", "tool-name", esc(call.name)));
  const argStr = fmtArgs(call);
  if (argStr) line.appendChild(el("span", "tool-args", argStr));
  main.appendChild(line);
  card.appendChild(main);
  chatLog.appendChild(card);
  if (keep) forceScroll(chatLog);
}

function addToolResult(name, out) {
  const keep = nearBottom(chatLog);
  const d = el("details", "tool-out");
  const sum = el("summary", null, `${esc(name)} çıktısı`);
  d.appendChild(sum);
  d.appendChild(el("pre", null, esc(out)));
  chatLog.appendChild(d);
  if (keep) forceScroll(chatLog);
}

function showThinking() {
  if (S.think) return;
  const b = el("div", "msg msg-model");
  const bd = el("div", "msg-body thinking");
  bd.appendChild(el("i")); bd.appendChild(el("i")); bd.appendChild(el("i"));
  b.appendChild(bd);
  chatLog.appendChild(b);
  forceScroll(chatLog);
  S.think = b;
}
function hideThinking() { if (S.think) { S.think.remove(); S.think = null; } }

function addBanner(kind, text) {
  const keep = nearBottom(chatLog);
  const b = el("div", `banner banner-${kind}`, text);
  chatLog.appendChild(b);
  if (keep) forceScroll(chatLog);
}

function renderTodos() {
  if (!S.todos.size) return;
  if (!S.todoCard || !S.todoCard.isConnected) {
    S.todoCard = el("div", "todo-card");
    S.todoCard.appendChild(el("div", "todo-title", "Plan"));
    chatLog.appendChild(S.todoCard);
  }
  let list = S.todoCard.querySelector(".todo-list");
  if (!list) {
    list = el("div", "todo-list");
    S.todoCard.appendChild(list);
  }
  list.innerHTML = "";
  for (const [i, t] of [...S.todos.entries()].sort((a, b) => a[0] - b[0])) {
    const row = el("div", `todo-item todo-${t.state}`);
    row.appendChild(el("span", "todo-mark",
      t.state === "done" ? "✓" : t.state === "bad" ? "✗" : "●"));
    row.appendChild(el("span", null, t.text || `adım ${i + 1}`));
    list.appendChild(row);
  }
  forceScroll(chatLog);
}

/* ---------- terminal (bilgisayar) ---------- */

let termEmpty = true;
function termLine(cls, text) {
  if (termEmpty) { termLog.innerHTML = ""; termEmpty = false; }
  const keep = nearBottom(termLog);
  termLog.appendChild(el("div", `tline ${cls}`, text));
  if (keep) forceScroll(termLog);
}
function termCmd(cmd)    { termLine("t-cmd", "$ " + esc(cmd)); }
function termOut(out)    { termLine("t-out", esc(out)); }
function termNote(text)  { termLine("t-note", "# " + esc(text)); }

/* ---------- olay işleme ---------- */

function onEvent(ev) {
  if (ev.t !== "start") hideThinking();
  if (ev.t === "model" || ev.t === "tool" || ev.t === "answer")
    S.sawContent = true;
  switch (ev.t) {
    case "start":
      addBanner("info", "görev alındı");
      $("term-status").textContent = "çalışıyor";
      break;
    case "model": {
      const txt = cleanModelText(ev.text);
      if (txt) addMsg("msg-model", txt);
      if (typeof ev.step === "number") {
        S.sawStep = true;
        S.steps = Math.max(S.steps, ev.step + 1);
        updSteps();
      }
      break;
    }
    case "tool":
      if (!S.sawStep) { S.steps += 1; updSteps(); }
      addToolCard(ev.call || {});
      if (ev.call && ev.call.name === "run_command")
        termCmd((ev.call.args || {}).cmd || "");
      else if (ev.call)
        termNote(`${ev.call.name} ${fmtArgs(ev.call)}`);
      break;
    case "tool_result":
      addToolResult(ev.name || "araç", ev.out);
      if (ev.name === "run_command") termOut(ev.out);
      break;
    case "answer": {
      const _txt = cleanModelText(ev.text) || ev.text || "";
      const _m = addMsg("msg-answer", "");
      _m.querySelector(".msg-body").innerHTML = mdLite(_txt);
      addVote(_m, _txt);
      break;
    }
    case "suggest": {
      const items = (ev.items || []).filter(Boolean);
      if (!items.length) break;
      const m = addMsg("msg-answer msg-suggest", "");
      const body = m.querySelector(".msg-body");
      body.appendChild(el("div", "ask-title", "Sonraki adım? Birini seç ya da kendin yaz:"));
      const row = el("div", "ask-opts");
      for (const it of items) {
        const c = el("button", "chip", it);
        c.type = "button";
        c.addEventListener("click", () => {
          row.querySelectorAll("button").forEach(n => n.disabled = true);
          c.classList.add("sel");
          send(it);
        });
        row.appendChild(c);
      }
      body.appendChild(row);
      break;
    }
    case "todo":
      S.todos.set(ev.i, { state: ev.state, text: ev.text || ev.label });
      renderTodos();
      break;
    case "phase":
      $("run-status").textContent = PHASES[ev.name] || ev.text || "çalışıyor";
      addBanner("phase", ev.text || ev.name);
      break;
    case "file":
      openCanvas(); refreshFiles(true);
      break;
    case "ask":
      addAskCard(ev);
      break;
    case "error":
      addBanner("err", "hata: " + esc(ev.text));
      termNote("hata: " + esc(ev.text));
      break;
    case "done":
      break; // durum güncellemesi poll'da
  }
}

/* ---------- soru kartı (anlayana kadar sor) ---------- */

function addAskCard(ev) {
  welcomeGone();
  const card = el("div", "msg msg-answer ask-card");
  const body = el("div", "msg-body");
  body.appendChild(el("p", "ask-intro", "Tam istediğin gibi olsun diye birkaç kısa soru:"));
  const answers = {};
  (ev.questions || []).forEach((q, qi) => {
    const blk = el("div", "ask-q");
    blk.appendChild(el("div", "ask-title", esc(q.q)));
    const opts = el("div", "ask-opts");
    (q.options || []).forEach((o) => {
      const b = el("button", "chip", esc(o));
      b.type = "button";
      b.onclick = () => {
        answers[q.key || `s${qi + 1}`] = o;
        opts.querySelectorAll(".chip").forEach(c => c.classList.toggle("sel", c === b));
        other.value = "";
      };
      opts.appendChild(b);
    });
    const other = el("input", "ask-other");
    other.placeholder = "başka bir şey yaz…";
    other.oninput = () => {
      if (other.value.trim()) {
        answers[q.key || `s${qi + 1}`] = other.value.trim();
        opts.querySelectorAll(".chip").forEach(c => c.classList.remove("sel"));
      }
    };
    blk.appendChild(opts); blk.appendChild(other);
    body.appendChild(blk);
  });
  const go = el("button", "btn-primary ask-go", "Böyle yap →");
  go.type = "button";
  go.onclick = () => {
    const keys = Object.keys(answers);
    if (!keys.length) { go.textContent = "önce bir seçim yap"; return; }
    card.querySelectorAll("button,input").forEach(n => n.disabled = true);
    const summary = keys.map(k => answers[k]).join(", ");
    S.awaiting = null;
    send(summary, answers);
  };
  body.appendChild(go);
  card.appendChild(body);
  chatLog.appendChild(card);
  forceScroll(chatLog);
  S.awaiting = { answers, card };
}

/* ---------- çalışma alanı (canvas) ---------- */

function openCanvas() {
  if (APP.dataset.canvas === "open") return;
  APP.dataset.canvas = "open";
  if (monacoOk && editor) setTimeout(() => editor.layout(), 220);
}
function closeCanvas() { APP.dataset.canvas = "closed"; }
function setWtab(name) {
  APP.dataset.wtab = name;
  document.querySelectorAll(".wtab").forEach(b =>
    b.classList.toggle("active", b.dataset.wtab === name));
  if (name === "files" && monacoOk && editor) setTimeout(() => editor.layout(), 60);
}
function findHtml(files) {
  return files.find(p => /(^|\/)index\.html$/.test(p)) || files.find(p => p.endsWith(".html")) || null;
}
let lastHtml = null;
function showPreview() {
  if (!S.taskId || !lastHtml) return;
  openCanvas(); setWtab("preview");
  $("preview-empty").classList.add("hidden");
  $("preview").src = API + withTok(`/api/task/${S.taskId}/raw?path=${encodeURIComponent(lastHtml)}`);
  if (innerWidth < 900) setTab("canvas");
}
function setTab(name) {
  APP.dataset.tab = name;
  document.querySelectorAll(".tabbar .tab").forEach(t =>
    t.classList.toggle("active", t.dataset.tab === name));
  if (monacoOk && editor) requestAnimationFrame(() => editor.layout());
}

/* ---------- sohbet geçmişi (sol panel) ---------- */

const HIST_KEY = "c1.history";
const loadHist = () => { try { return JSON.parse(localStorage.getItem(HIST_KEY) || "[]"); } catch { return []; } };
function saveHist(id, title) {
  const h = loadHist().filter(x => x.id !== id);
  h.unshift({ id, title: title.slice(0, 60), t: Date.now() });
  localStorage.setItem(HIST_KEY, JSON.stringify(h.slice(0, 40)));
  renderHist();
}
function renderHist() {
  const h = loadHist(), nav = $("history");
  nav.innerHTML = "";
  if (!h.length) { nav.appendChild(el("div", "empty-hint", "henüz sohbet yok")); return; }
  for (const it of h) {
    const b = el("button", "hist-item" + (it.id === S.taskId ? " active" : ""), it.title);
    b.title = new Date(it.t).toLocaleString("tr-TR");
    b.onclick = () => openTask(it.id, it.title);
    nav.appendChild(b);
  }
}
async function openTask(id, title) {
  if (S.running) return;
  resetChat();
  S.taskId = id;
  $("chat-title").textContent = title;
  APP.dataset.side = "closed";
  try {
    const r = await apiGet(`/api/task/${id}?after=0`);
    addMsg("msg-user", r.text || title);
    for (const ev of (r.events || [])) if (ev.t !== "start") onEvent(ev);
    S.seen = r.n_events ?? (r.events || []).length;
    S.status = r.status || "done";
    hideThinking();
    if (r.status === "running") { setRunning(true); poll(); }
    else await refreshFiles(true);
  } catch { addBanner("warn", "bu sohbet sunucuda artık yok (arşivlendi)"); }
  renderHist();
}
function resetChat() {
  clearTimeout(S.pollTimer); hideThinking();
  chatLog.innerHTML = "";
  S.taskId = null; S.seen = 0; S.todos.clear(); S.todoCard = null; S.sawContent = false;
  S.fileCache.clear(); S.autoOpened = false; S.currentFile = null; S.awaiting = null; S.status = "idle";
  lastHtml = null; termEmpty = true;
  termLog.innerHTML = ""; termLog.appendChild(el("div", "empty-hint", "ajan komut çalıştırınca burada görünür"));
  tree.innerHTML = ""; tree.appendChild(el("div", "empty-hint", "görev başlayınca dosyalar burada"));
  $("editor-empty").classList.remove("hidden");
  $("preview").removeAttribute("src"); $("preview-empty").classList.remove("hidden");
  if (monacoOk && editor) editor.setValue("");
  $("editor-fallback").textContent = "";
  $("chat-title").textContent = "coder-1";
  $("term-status").textContent = "boşta";
  closeCanvas(); setWtab("files");
}
function newChat() {
  if (S.running) return;
  resetChat();
  chatLog.appendChild(WELCOME.cloneNode(true));
  bindSugg();
  renderHist();
  input.focus();
}
function paintUser() {
  const n = NAME();
  $("user-name").textContent = n || "—";
  $("user-avatar").textContent = (n[0] || "?").toUpperCase();
}

/* ---------- dosya ağacı + editör ---------- */

const LANG = {
  py: "python", js: "javascript", mjs: "javascript", ts: "typescript",
  json: "json", md: "markdown", html: "html", css: "css",
  yml: "yaml", yaml: "yaml", sh: "shell", txt: "plaintext",
  rs: "rust", go: "go", java: "java", c: "c", cpp: "cpp", h: "c",
  toml: "ini", ini: "ini", xml: "xml", sql: "sql",
};
const langOf = (p) => {
  const ext = p.split(".").pop().toLowerCase();
  return LANG[ext] || "plaintext";
};

let editor = null, monacoOk = false;
function initEditor() {
  try {
    if (!window.require) throw new Error("loader yok");
    require(["vs/editor/editor.main"], () => {
      monacoOk = true;
      editor = monaco.editor.create($("editor"), {
        value: "", language: "plaintext", theme: "vs-dark",
        readOnly: true, minimap: { enabled: false },
        fontSize: 13, automaticLayout: true,
        scrollBeyondLastLine: false, renderLineHighlight: "none",
        padding: { top: 10 },
      });
      if (S.currentFile) showFile(S.currentFile, true);
    }, () => { /* CDN yoksa fallback aktif */ });
  } catch { /* fallback */ }
}

function showFile(path, force) {
  const cached = S.fileCache.get(path);
  const paint = (content) => {
    $("editor-empty").classList.add("hidden");
    if (monacoOk && editor) {
      $("editor-fallback").classList.add("hidden");
      $("editor").classList.remove("hidden");
      const m = monaco.editor.createModel(esc(content), langOf(path));
      const old = editor.getModel();
      editor.setModel(m);
      if (old) old.dispose();
      editor.layout();
    } else {
      $("editor").classList.add("hidden");
      const fb = $("editor-fallback");
      fb.classList.remove("hidden");
      fb.textContent = esc(content);
    }
    tree.querySelectorAll(".fitem").forEach(n =>
      n.classList.toggle("sel", n.dataset.path === path));
  };
  if (cached != null && !force) return paint(cached);
  if (!S.taskId) return;
  apiGet(`/api/task/${S.taskId}/file?path=${encodeURIComponent(path)}`)
    .then((r) => {
      const c = r.content ?? `(okunamadı: ${r.error || "?"})`;
      S.fileCache.set(path, c);
      paint(c);
    })
    .catch(() => paint("(dosya yüklenemedi)"));
}

function renderTree(paths) {
  if (!paths.length) {
    tree.innerHTML = "";
    tree.appendChild(el("div", "empty-hint", "çalışma alanı boş"));
    return;
  }
  const root = { dirs: {}, files: [] };
  for (const p of paths) {
    const parts = p.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++)
      node = node.dirs[parts[i]] = node.dirs[parts[i]] || { dirs: {}, files: [] };
    node.files.push(p);
  }
  const ICO_DIR = '<svg class="fi" viewBox="0 0 16 16"><path d="M1.5 2.5h4.6l1.4 1.7h7A1.5 1.5 0 0 1 16 5.7v7.1a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 0 12.8V4a1.5 1.5 0 0 1 1.5-1.5z"/></svg>';
  const ICO_FILE = '<svg class="fi" viewBox="0 0 16 16"><path d="M3 1h6.5L13 4.5V15H3V1zm6 .5V5h3.5L9 1.5z"/></svg>';
  tree.innerHTML = "";
  const walk = (node, depth, container) => {
    for (const [name, child] of Object.entries(node.dirs).sort()) {
      const d = el("details", "fdir");
      if (depth < 2) d.open = true;
      const s = el("summary");
      s.innerHTML = ICO_DIR;
      s.appendChild(document.createTextNode(name));
      d.appendChild(s);
      const inner = el("div", "fdir-inner");
      walk(child, depth + 1, inner);
      d.appendChild(inner);
      container.appendChild(d);
    }
    for (const p of node.files.sort()) {
      const name = p.split("/").pop();
      const it = el("div", "fitem");
      it.innerHTML = ICO_FILE;
      it.appendChild(document.createTextNode(name));
      it.dataset.path = p;
      it.title = p;
      if (p === S.currentFile) it.classList.add("sel");
      it.onclick = () => { S.currentFile = p; showFile(p); };
      container.appendChild(it);
    }
  };
  walk(root, 0, tree);
}

async function refreshFiles(autoOpen) {
  if (!S.taskId) return;
  try {
    const r = await apiGet(`/api/task/${S.taskId}/files`);
    const files = r.files || [];
    renderTree(files);
    lastHtml = findHtml(files);
    $("btn-preview").classList.toggle("dim", !lastHtml);
    if (autoOpen && files.length && !S.autoOpened) {
      S.autoOpened = true;
      S.currentFile = files[0];
      showFile(files[0], true);
    }
  } catch { /* sessiz */ }
}

/* ---------- çalıştırma ---------- */

function fmtElapsed() {
  return ((performance.now() - S.startAt) / 1000).toFixed(1) + "s";
}
function updSteps() { $("step-count").textContent = `${S.steps} adım`; }

function setRunning(on) {
  S.running = on;
  runBar.classList.toggle("hidden", !on);
  $("run-pill").classList.toggle("hidden", !on);
  input.disabled = on;
  btnSend.disabled = on;
  $("btn-new").disabled = on;
  if (on) {
    $("run-status").textContent = "başlıyor";
    S.startAt = performance.now();
    S.steps = 0;
    S.sawStep = false;
    updSteps();
    S.timer = setInterval(() => { $("elapsed").textContent = fmtElapsed(); }, 200);
  } else {
    clearInterval(S.timer);
    input.focus();
  }
}

async function poll() {
  if (!S.running || !S.taskId) return;
  try {
    const r = await apiGet(`/api/task/${S.taskId}?after=${S.seen}`);
    if (r.error) { finish("error", "görev bulunamadı"); return; }
    const evs = r.events || [];
    S.seen = r.n_events != null ? r.n_events : S.seen + evs.length;
    for (const ev of evs) onEvent(ev);
    if (r.status === "running") showThinking();
    if (r.status && r.status !== "running") {
      S.status = r.status;
      if (r.status === "waiting") { finish("waiting", ""); return; }
      const ok = r.status === "done";
      finish(ok ? "done" : r.status, ok ? "tamamlandı" : `bitti (${r.status})`);
      return;
    }
  } catch (e) {
    /* ağ hatası — denemeye devam; UI'da sessiz kal */
  }
  if (S.seen % 3 === 0) refreshFiles(false); // canlılık: ara sıra ağacı tazele
  S.pollTimer = setTimeout(poll, POLL_MS);
}

function finish(kind, text) {
  clearTimeout(S.pollTimer);
  hideThinking();
  setRunning(false);
  $("term-status").textContent =
    ({ done: "bitti", stopped: "durduruldu", error: "hata", waiting: "soru bekliyor" })[kind] || "hata";
  if (kind === "stopped") addBanner("warn", "durduruldu");
  else if (kind === "waiting") { input.placeholder = "seçeneklerden seç ya da buraya yaz…"; }
  else if (kind === "done") {
    if (!S.sawContent) addBanner("warn", "model boş cevap üretti");
  } else addBanner("err", text);
  if (kind !== "waiting") input.placeholder = "coder-1'e yaz…";
  refreshFiles(true).then(() => { if (kind === "done" && lastHtml) showPreview(); });
}

async function send(text, answers) {
  welcomeGone();
  addMsg("msg-user", text);
  if (S.awaiting) {           // kart yerine yazıyla cevapladı
    S.awaiting.card.querySelectorAll("button,input").forEach(n => n.disabled = true);
    S.awaiting = null;
  }
  const cont = !!S.taskId;    // aynı sohbete devam (soru cevabı / ek istek)
  if (!cont) { S.seen = 0; S.fileCache.clear(); S.autoOpened = false; S.currentFile = null; }
  S.todos.clear(); S.todoCard = null; S.think = null; S.sawContent = false;
  setRunning(true);
  showThinking();
  const body = { text, name: NAME(), answers: answers || null };
  const path = cont ? `/api/task/${S.taskId}/reply` : "/api/task";
  try {
    let r;
    try {
      r = await apiPost(path, body);
    } catch (e) {
      if (!/HTTP 50[234]|Failed to fetch/.test(e.message)) throw e;
      addBanner("warn", "sunucu uyanıyor, tekrar deneniyor…");
      await new Promise((ok) => setTimeout(ok, 6000));
      r = await apiPost(path, body);
    }
    S.taskId = r.task_id;
    if (cont && r.n_events != null) S.seen = r.n_events;
    if (!cont) { $("chat-title").textContent = text.slice(0, 60); saveHist(S.taskId, text); }
    poll();
  } catch (e) {
    finish("error", "backend'e ulaşılamadı: " + e.message);
  }
}

async function stopTask() {
  if (!S.taskId) return;
  try { await apiPost(`/api/task/${S.taskId}/stop`); }
  catch { /* 404/henüz yok — sessiz */ }
  finish("stopped", "");
}

/* ---------- UI bağlama ---------- */

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = input.value.trim();
  if (!t || S.running) return;
  input.value = "";
  input.style.height = "auto";
  send(t);
});
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 140) + "px";
});
$("btn-stop").addEventListener("click", stopTask);
$("btn-refresh").addEventListener("click", () => refreshFiles(false));
$("btn-tree-toggle").addEventListener("click", () => {
  tree.classList.toggle("collapsed");
  if (monacoOk && editor) setTimeout(() => editor.layout(), 60);
});

/* sekmeler, çalışma alanı, kenar çubuğu */
document.querySelectorAll(".tabbar .tab").forEach((b) =>
  b.addEventListener("click", () => setTab(b.dataset.tab)));
document.querySelectorAll(".wtab").forEach((b) =>
  b.addEventListener("click", () => { setWtab(b.dataset.wtab); if (b.dataset.wtab === "preview") showPreview(); }));
$("btn-canvas").addEventListener("click", () => {
  if (innerWidth < 900) { setTab("canvas"); return; }
  APP.dataset.canvas === "open" ? closeCanvas() : openCanvas();
});
$("btn-canvas-close").addEventListener("click", () => { closeCanvas(); setTab("chat"); });
$("btn-side").addEventListener("click", () => { APP.dataset.side = "open"; });
$("btn-side-close").addEventListener("click", () => { APP.dataset.side = "closed"; });
$("side-scrim").addEventListener("click", () => { APP.dataset.side = "closed"; });
$("btn-new").addEventListener("click", newChat);
$("btn-logout").addEventListener("click", () => {
  TOK = ""; localStorage.removeItem("c1.tok"); localStorage.removeItem("c1.name");
  newChat(); showLogin();
});
const WELCOME = chatLog.querySelector(".welcome").cloneNode(true);
function bindSugg() {
  chatLog.querySelectorAll(".sugg").forEach(b =>
    b.onclick = () => { if (!S.running) send(b.dataset.q); });
}
bindSugg(); renderHist(); paintUser();

/* model bilgisi */
function loadModel() {
  const pill = $("model-pill");
  pill.textContent = "model: …"; pill.className = "pill pill-dim";
  apiGet("/api/model")
    .then((r) => {
      if (r.ok) {
        const m = r.params ? `${(r.params / 1e6).toFixed(0)}M` : "";
        pill.textContent = r.brain
          ? `coder-1 · beyin: ${r.brain.replace("qwen2.5-coder:", "qwen2.5-coder ").toUpperCase().replace("QWEN2.5-CODER", "Qwen2.5-Coder")}`
          : `model: ${r.model || "coder-1"}${m ? " · " + m : ""}`;
        pill.className = "pill pill-ok";
      } else {
        pill.textContent = "model: çevrimdışı"; pill.className = "pill pill-bad";
      }
    })
    .catch(() => { pill.textContent = "model: çevrimdışı"; pill.className = "pill pill-bad"; });
}
if (TOK) loadModel();
