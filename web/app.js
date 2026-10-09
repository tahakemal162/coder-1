/* coder-1 web — sohbet + canlı olay akışı + Monaco editör + terminal.
 * Backend: agent/main.py'nin /api/* uçları. Sadece HTTP polling (WS yok).
 * API tabanı: aynı origin; farklıysa ?api=http://host:port veya
 * localStorage "coder1.api" ile ayarlanır.
 */

const API = (() => {
  const q = new URLSearchParams(location.search).get("api");
  if (q) localStorage.setItem("coder1.api", q);
  return (localStorage.getItem("coder1.api") || location.origin)
    .replace(/\/+$/, "");
})();

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
  currentFile: null,
  fileCache: new Map(),  // path -> content
  autoOpened: false,
};

/* ---------- yardımcılar ---------- */

const esc = (s) => String(s ?? "");

async function apiGet(path) {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
async function apiPost(path, body) {
  const r = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

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
    case "answer":
      addMsg("msg-answer", cleanModelText(ev.text) || esc(ev.text));
      break;
    case "todo":
      S.todos.set(ev.i, { state: ev.state, text: ev.text || ev.label });
      renderTodos();
      break;
    case "error":
      addBanner("err", "hata: " + esc(ev.text));
      termNote("hata: " + esc(ev.text));
      break;
    case "done":
      break; // durum güncellemesi poll'da
  }
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
  if (on) {
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
    if (r.status && r.status !== "running") {
      const ok = r.status === "done";
      finish(ok ? "done" : r.status,
        ok ? `tamamlandı · ${r.result?.steps ?? S.steps} adım`
           : `bitti (${r.status})`);
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
  setRunning(false);
  $("term-status").textContent =
    ({ done: "bitti", stopped: "durduruldu", error: "hata" })[kind] || "hata";
  if (kind === "stopped") addBanner("warn", "durduruldu");
  else if (kind === "done") addBanner("ok", text);
  else addBanner("err", text);
  refreshFiles(true);
}

async function send(text) {
  welcomeGone();
  addMsg("msg-user", text);
  S.seen = 0; S.todos.clear(); S.todoCard = null;
  S.fileCache.clear(); S.autoOpened = false; S.currentFile = null;
  $("editor-empty").classList.remove("hidden");
  if (monacoOk && editor) editor.setValue("");
  $("editor-fallback").textContent = "";
  setRunning(true);
  try {
    const r = await apiPost("/api/task", { text });
    S.taskId = r.task_id;
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

/* mobil sekmeler */
document.querySelectorAll(".tabbar .tab").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelector(".app").dataset.tab = b.dataset.tab;
    document.querySelectorAll(".tabbar .tab").forEach(t =>
      t.classList.toggle("active", t === b));
    if (monacoOk && editor) requestAnimationFrame(() => editor.layout());
  });
});

/* model bilgisi */
apiGet("/api/model")
  .then((r) => {
    const pill = $("model-pill");
    if (r.ok) {
      const m = r.params ? `${(r.params / 1e6).toFixed(0)}M` : "";
      pill.textContent = `model: ${r.model || "coder-1"}${m ? " · " + m : ""}`;
      pill.className = "pill pill-ok";
    } else {
      pill.textContent = "model: çevrimdışı";
      pill.className = "pill pill-bad";
    }
  })
  .catch(() => {
    $("model-pill").textContent = "model: çevrimdışı";
    $("model-pill").className = "pill pill-bad";
  });

initEditor();
