"""coder-1 2. kat — anla → sor → planla → yaz → doğrula → onar → raporla.

Rakip ürünlerden farkı: belirsiz istekte önce seçenekli sorular, kullanıcı
cevaplarına birebir özel üretim, gerçek çalıştırma/testle doğrulama, hatada
kendi kendine onarım ve 'neyi test ettim' raporu. Kullanıcı tercihleri
(prefs) hatırlanır, aynı şey iki kez sorulmaz.
"""
import html.parser
import json
import os
import re
import shutil
import tempfile
import time

from . import brain
from .workspace import Workspace

MAX_REPAIR = 2
FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.S)

UNDERSTAND_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["chat", "web", "python", "other"]},
        "clear": {"type": "boolean"},
        "summary": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "q": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["key", "q", "options"],
            },
        },
    },
    "required": ["kind", "clear", "summary", "questions"],
}
SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    "required": ["items"],
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "files": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "purpose": {"type": "string"}},
            "required": ["path", "purpose"]}},
        "steps": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "files", "steps", "tests"],
}


def _code_of(text):
    """Model çıktısından dosya içeriğini al: en büyük çit bloğu, yoksa tümü."""
    blocks = FENCE_RE.findall(text)
    if blocks:
        return max(blocks, key=len).strip("\n") + "\n"
    return text.strip() + "\n"


class _HTMLCheck(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.scripts, self.in_script, self.problems = [], [], False, []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "script" and not dict(attrs).get("src"):
            self.in_script = True
            self.scripts.append("")

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script and self.scripts:
            self.scripts[-1] += data


def _check_html(content, must_have):
    p = _HTMLCheck()
    try:
        p.feed(content)
    except Exception as e:
        return [f"HTML ayrıştırılamadı: {e}"], []
    probs = []
    if "html" not in p.tags or "body" not in p.tags:
        probs.append("eksik <html>/<body> yapısı")
    low = content.lower()
    for kw in must_have:
        if kw.lower() not in low:
            probs.append(f"kullanıcının istediği '{kw}' kodda geçmiyor")
    return probs, p.scripts


def _node_ok():
    node = shutil.which("node")
    if not node:
        return False
    try:
        import subprocess
        v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5).stdout
        return int(v.lstrip("v").split(".")[0]) >= 16
    except Exception:
        return False


def _check_js(scripts, ws: Workspace):
    if not _node_ok():
        return []
    probs = []
    for i, src in enumerate(scripts):
        if not src.strip():
            continue
        # sadece sözdizimi kontrolü: kod çalıştırılmaz, bu yüzden host node'u yeterli
        # (sandbox imajında node olmayabilir)
        import subprocess
        fd, path = tempfile.mkstemp(suffix=".js", dir=ws.root)
        with os.fdopen(fd, "w") as f:
            f.write(src)
        try:
            r = subprocess.run([shutil.which("node"), "--check", path],
                               capture_output=True, text=True, timeout=20)
            out = (r.stdout + r.stderr).replace(path, "script%d.js" % (i + 1))
            code = r.returncode
        except Exception as e:  # node çöktü/zaman aşımı → kontrolü atla
            out, code = str(e), 0
        finally:
            os.unlink(path)
        if code != 0:
            probs.append("JavaScript sözdizimi hatası:\n" + out[:800])
    return probs


BUILD_WORDS = ("yap", "oluştur", "yaz", "kodla", "geliştir", "site", "sayfa", "uygulama",
               "fonksiyon", "program", "oyun", "hesap", "liste", "form", "html", "css", "python",
               "javascript", "script", "api", "bot", "sayaç", "portfol", "tasarla", "kur", "ekle",
               "düzelt", "değiştir", "build", "create", "make", "write", "app", "website", "page")
PY_WORDS = ("python", "fonksiyon", "def ", "doctest", "script", ".py", "pytest", "class ", "algoritma")


def _guess_kind(text, model_kind):
    low = text.lower()
    build = any(w in low for w in BUILD_WORDS)
    if model_kind == "chat" and not build:
        return "chat"
    if any(w in low for w in PY_WORDS) and not any(w in low for w in ("site", "sayfa", "html", "web")):
        return "python"
    if model_kind in ("web", "python", "other") and model_kind != "chat":
        return model_kind if model_kind != "other" or not build else "web"
    return "web"


DETAIL_WORDS = ("renk", "mavi", "kırmızı", "yeşil", "mor", "siyah", "beyaz", "turuncu", "sarı", "pembe",
                "koyu", "açık", "modern", "minimal", "sade", "animasyon", "responsive", "mobil", "tema",
                "font", "başlık", "buton", "özellik", "ile", "olsun", "içinde", "tarz", "stil", "ingilizce",
                "türkçe", "dark", "light", "color", "style", "with")


def _detail_score(text):
    low = text.lower()
    score = sum(1 for w in DETAIL_WORDS if w in low)
    score += low.count(",") + (1 if any(ch.isdigit() for ch in low) else 0)
    score += 1 if len(low) > 90 else 0
    return score


def _user_keywords(text, answers):
    kws = []
    for a in (answers or {}).values():
        a = str(a).strip()
        if 2 < len(a) <= 24 and " " not in a and not a.isdigit():
            kws.append(a)
    return kws[:4]


ASK_MARK = "Tam istediğin gibi olsun diye birkaç soru: "


def _filter_questions(qs, task_text, prefs):
    """Cevabı zaten istekte ya da tercihlerde olan soruları ele."""
    low = task_text.lower()
    known = " ".join(str(v).lower() for v in (prefs or {}).values())
    out = []
    for q in qs or []:
        if not q.get("q"):
            continue
        opts = [str(o).lower() for o in (q.get("options") or []) if str(o).strip()]
        if any(len(o) >= 3 and o in low for o in opts):
            continue
        if str(q.get("key", "")).lower() in (prefs or {}) and any(o in known for o in opts):
            continue
        out.append(q)
    return out[:3]


def _project_files(workdir):
    """Çalışma alanındaki kaynak dosyalar (gizli/klasör dışı), en fazla 3."""
    out = []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith(".") or not f.endswith((".html", ".py", ".js", ".css")):
                continue
            out.append(os.path.relpath(os.path.join(root, f), workdir))
    return sorted(out)[:3]


DEFAULT_STEPS = {
    "web": ["karanlık mod ekle", "mobil görünümü iyileştir", "verileri tarayıcıda sakla"],
    "python": ["hatalı girişleri yakala", "komut satırı arayüzü ekle", "daha fazla test ekle"],
}


VERBISH = ("ekle", "yap", "koy", "göster", "kaydet", "iyileştir", "düzelt", "değiştir", "sırala",
           "filtrele", "ayır", "destek", "mod", "animasyon", "büyüt", "küçült", "renk", "ara", "sil",
           "kullan", "çevir", "tut", "say", "hesapla", "paylaş", "yazdır", "indir", "oyna", "sesli")


def _next_steps(history, kind, cancel=None):
    """Bitmiş proje için 3 somut 'sonraki adım' önerisi (emir kipinde, kısa)."""
    try:
        sg = brain.chat(history + [{"role": "user", "content": (
            "Az önce bitirdiğin bu projeyi kullanıcı bir adım ileri taşımak istese ne isteyebilir? "
            "3 kısa, somut, EMİR kipinde Türkçe geliştirme önerisi yaz (2-6 kelime; her biri bir özellik "
            "eklemeli ya da bir şeyi değiştirmeli). Örnek biçim: 'karanlık mod ekle', 'görevleri tarihe göre sırala'. "
            "SADECE JSON: {\"items\": [\"...\", \"...\", \"...\"]}")}],
            schema=SUGGEST_SCHEMA, max_tokens=150, temperature=0.4, cancel=cancel, model=brain.FAST_MODEL) or {}
        items = [x.strip().rstrip(".").lower() for x in (sg.get("items") or []) if isinstance(x, str) and x.strip()]
        items = [x for x in items if 6 <= len(x) <= 60 and " " in x and any(v in x for v in VERBISH)][:3]
    except Exception:
        items = []
    defaults = DEFAULT_STEPS.get(kind, DEFAULT_STEPS["web"])
    return (items + [d for d in defaults if d not in items])[:3]


def agent_task2(task_text, workdir, on_event=None, cancel=None, sandbox=None,
                history=None, prefs=None, answers=None, user=""):
    """Bir konuşma turunu çalıştır. Soru sorulursa {"waiting": True} döner."""
    ws = Workspace(workdir, runner=sandbox)
    history = list(history or [])
    prefs = dict(prefs or {})
    trace = []

    def emit(t, **kw):
        ev = {"t": t, **kw}
        trace.append(ev)
        if on_event:
            on_event(ev)

    def stopped():
        return cancel is not None and cancel.is_set()

    def model_text(text):
        emit("model", text=text)

    emit("start", task=task_text)
    history.append({"role": "user", "content": task_text})
    if answers:
        history.append({"role": "user", "content":
                        "Cevaplarım: " + json.dumps(answers, ensure_ascii=False)})
        prefs.update({k: v for k, v in answers.items()})

    # ---------- 1. anla ----------
    emit("phase", name="anla", text="isteği anlıyorum")
    pref_txt = ("Kullanıcının bilinen tercihleri (bunları tekrar sorma): "
                + json.dumps(prefs, ensure_ascii=False)) if prefs else ""
    asked_before = any(str(m.get("content", "")).startswith(("Cevaplarım:", ASK_MARK))
                       for m in history[:-1])
    try:
        u = brain.chat(history + [{"role": "user", "content": (
            "Yukarıdaki isteği değerlendir ve SADECE JSON ver.\n"
            "kind: 'chat' SADECE selamlaşma veya bilgi sorusuysa (bir şey yapılması/yazılması isteniyorsa asla chat değil), "
            "'web' (HTML/CSS/JS sayfa-uygulama), 'python' (python kodu/fonksiyon), 'other'.\n"
            "clear: istek üretime başlayacak kadar net mi? "
            "questions: net değilse en fazla 3 kısa Türkçe soru, her birine 2-4 somut seçenek "
            "(ör. renk, tema, özellik, dil). Netse boş liste. "
            + pref_txt + ("\nKullanıcı sorularını zaten cevapladı; artık clear=true ver." if asked_before else ""))}],
            schema=UNDERSTAND_SCHEMA, max_tokens=500, cancel=cancel, model=brain.FAST_MODEL)
    except Exception as e:
        emit("error", text=f"beyin servisi: {e}")
        emit("done", ok=False, reason="model_error")
        return {"ok": False, "trace": trace, "history": history, "prefs": prefs}
    if stopped():
        emit("done", ok=False, reason="stopped")
        return {"ok": False, "stopped": True, "trace": trace, "history": history, "prefs": prefs}
    kind = _guess_kind(task_text, u.get("kind", "other"))
    if asked_before:
        prev = next((m["content"] for m in history if m["role"] == "user"
                     and not m["content"].startswith("Cevaplarım:")), task_text)
        kind = _guess_kind(prev, u.get("kind", "other"))
        if kind == "chat":
            kind = "web"
    summary = u.get("summary") or task_text
    questions = _filter_questions(u.get("questions"), task_text, prefs)
    if _detail_score(task_text) >= 3:   # kullanıcı zaten yeterince detay verdi
        questions = []

    # ---------- düzenleme turu: proje bitmişken gelen ek istek ----------
    done_before = any(m.get("role") == "assistant" and
                      str(m.get("content", "")).startswith(("**Hazır.**", "**Bitti"))
                      for m in history[:-1])
    existing = _project_files(workdir)
    revision = bool(done_before and existing and not answers)
    if revision:
        kind = "web" if any(p.endswith(".html") for p in existing) else "python"
        summary = task_text
        questions = []

    if kind == "chat" and not revision:
        acc = []
        reply = brain.chat(history + [{"role": "user", "content":
                           "Kısa, samimi ve Türkçe cevap ver. Kod üretme; gerekirse ne yapabileceğini bir cümleyle söyle."}],
                           max_tokens=300, temperature=0.6, cancel=cancel,
                           on_delta=lambda d: acc.append(d))
        emit("answer", text=reply.strip())
        history.append({"role": "assistant", "content": reply.strip()})
        emit("done", ok=True, steps=1)
        return {"ok": True, "answer": reply, "trace": trace, "history": history, "prefs": prefs}

    if not revision and not asked_before and kind != "chat" and not questions and _detail_score(task_text) < 2:
        try:
            q2 = brain.chat(history + [{"role": "user", "content": (
                "İstek kısa ve detaysız. Sonucu kullanıcının hayaline birebir uydurmak için "
                "2-3 kısa Türkçe soru üret, her birine 2-4 somut seçenek ver (ör. renk/tema, "
                "ana özellik, dil/stil). SADECE JSON ver." + ("\n" + pref_txt if pref_txt else ""))}],
                schema=UNDERSTAND_SCHEMA, max_tokens=400, cancel=cancel, model=brain.FAST_MODEL)
            questions = _filter_questions(q2.get("questions"), task_text, prefs)
        except Exception:
            questions = []
    if questions and not asked_before:
        emit("ask", questions=questions, summary=summary)
        history.append({"role": "assistant", "content":
                        ASK_MARK + json.dumps(questions, ensure_ascii=False)})
        return {"ok": True, "waiting": True, "trace": trace, "history": history, "prefs": prefs}

    # ---------- anladığımı söyle ----------
    if answers:
        vals = [str(v).strip() for v in answers.values() if str(v).strip()]
        if vals:
            emit("answer", text="Anladım: " + ", ".join(f"**{v[:40]}**" for v in vals)
                 + " — buna göre yapıyorum.")
    elif prefs and not asked_before and not revision and _detail_score(task_text) < 3:
        vals = [str(v).strip()[:30] for v in list(prefs.values())[:4] if str(v).strip()]
        if vals:
            emit("answer", text="Önceki tercihlerini hatırlıyorum (" + ", ".join(vals)
                 + "); onlara göre yapıyorum, değiştirmek istersen söyle.")

    if revision:
        # ---------- 2b. düzenle ----------
        emit("phase", name="duzenle", text="mevcut projeyi düzenliyorum")
        plan = {"title": "Düzenleme: " + task_text[:70]}
        steps = [f"{p} dosyasında isteği uygula" for p in existing[:3]]
        for i, st in enumerate(steps):
            emit("todo", i=i, state="todo", text=st)
        written = {}
        for i, path in enumerate(existing[:3]):
            if stopped():
                break
            emit("todo", i=i, state="active", text=steps[i])
            cur = ws.read(path, max_chars=7000)
            emit("tool", call={"name": "edit_file", "args": {"path": path}})
            t0 = time.time()
            txt = brain.chat(history + [{"role": "user", "content": (
                f"Kullanıcının değişiklik isteği: {task_text}\n" + (pref_txt + "\n" if pref_txt else "")
                + f"Mevcut '{path}' dosyası:\n```\n{cur}\n```\n"
                "SADECE bu isteği uygula, geri kalan her şeyi aynen koru. Dosyanın TAM yeni halini "
                "tek kod bloğunda ver, açıklama yazma.")}],
                max_tokens=1800, temperature=0.2, cancel=cancel)
            new = _code_of(txt)
            if new.strip() and new.strip() != cur.strip():
                ws.write(path, new)
                written[path] = new
                emit("file", path=path, op="edit")
                emit("tool_result", name="edit_file", out=f"düzenlendi: {path} ({len(new)} karakter)",
                     dur_ms=int((time.time() - t0) * 1000))
            else:
                emit("tool_result", name="edit_file", out=f"değişiklik gerekmedi: {path}")
            emit("todo", i=i, state="done", text=steps[i])
        if written:
            try:
                d = ws.diff()
                if d and d.strip():
                    emit("tool", call={"name": "run_command", "args": {"cmd": "git diff"}})
                    emit("tool_result", name="run_command", out=d[:2000])
            except Exception:
                pass
    else:
        # ---------- 2. planla ----------
        emit("phase", name="planla", text="planlıyorum")
        plan = brain.chat(history + [{"role": "user", "content": (
            f"İstek özeti: {summary}\nTek bir küçük, çalışan proje planla ve SADECE JSON ver. "
            "files: " + ("tek bir index.html (CSS+JS içinde)" if kind == "web" else
                         "tek bir .py dosyası (içinde doctest)" if kind == "python" else "en fazla 3 dosya")
            + ". İstenmeyen ekstra dosya/arayüz ekleme. steps: 3-5 kısa adım. tests: nasıl doğrulanacağı (2-4 madde).")}],
            schema=PLAN_SCHEMA, max_tokens=600, cancel=cancel, model=brain.FAST_MODEL) or {}
        files = [f for f in (plan.get("files") or []) if f.get("path")][:3]
        if kind == "python":
            files = [f for f in files if f["path"].endswith(".py")][:1]
        elif kind == "web":
            files = [f for f in files if f["path"].endswith((".html", ".css", ".js"))]
            if any(f["path"].endswith(".html") for f in files):
                files = [f for f in files if f["path"].endswith(".html")][:1]
        if not files:
            files = [{"path": "index.html" if kind == "web" else "main.py", "purpose": summary}]
        steps = plan.get("steps") or [f"{f['path']} dosyasını yaz" for f in files]
        for i, s in enumerate(steps):
            emit("todo", i=i, state="todo", text=s)
        model_text(f"Plan: {plan.get('title') or summary}")

        # ---------- 3. yaz ----------
        emit("phase", name="yaz", text="kodu yazıyorum")
        written = {}
        for i, f in enumerate(files):
            if stopped():
                break
            path = f["path"].lstrip("./")
            emit("todo", i=min(i, len(steps) - 1), state="active", text=steps[min(i, len(steps) - 1)])
            emit("tool", call={"name": "write_file", "args": {"path": path}})
            ctx = "".join(f"\n--- {p} ---\n{c[:3000]}\n" for p, c in written.items())
            ask = (f"İstek: {summary}\n" + (pref_txt + "\n" if pref_txt else "")
                   + f"Şimdi '{path}' dosyasının TAM içeriğini yaz ({f.get('purpose','')}). "
                   "Tek bir kod bloğu içinde ver, açıklama yazma. Kullanıcının söylediği her detay "
                   "(renk, özellik, dil) kodda somut olarak bulunsun. Çalışır, eksiksiz kod; yer tutucu yok."
                   + (" Sayfa responsive olsun, tüm CSS ve JS aynı dosyada olsun." if path.endswith(".html") else "")
                   + (" Fonksiyonlara doctest ekle ve dosya sonuna `if __name__ == '__main__': import doctest; doctest.testmod()` koy." if path.endswith(".py") else "")
                   + (f"\nÖnceki dosyalar:{ctx}" if ctx else ""))
            acc = []
            t0 = time.time()
            txt = brain.chat(history + [{"role": "user", "content": ask}], max_tokens=1800,
                             temperature=0.3, cancel=cancel,
                             on_delta=lambda d: acc.append(d))
            content = _code_of(txt)
            ws.write(path, content)
            written[path] = content
            emit("file", path=path, op="write")
            emit("tool_result", name="write_file", out=f"yazıldı: {path} ({len(content)} karakter)",
                 dur_ms=int((time.time() - t0) * 1000))
            emit("todo", i=min(i, len(steps) - 1), state="done", text=steps[min(i, len(steps) - 1)])

    # ---------- 4. doğrula + onar ----------
    emit("phase", name="dogrula", text="test ediyorum")
    tested = []
    must = _user_keywords(task_text, answers)
    for path in list(written):
        for rnd in range(MAX_REPAIR + 1):
            if stopped():
                break
            content = written[path]
            problems = []
            if path.endswith(".html"):
                probs, scripts = _check_html(content, must)
                hard = [x for x in probs if not x.startswith("kullanıcının")]
                notes = [x for x in probs if x.startswith("kullanıcının")]
                problems += hard
                emit("tool", call={"name": "run_command", "args": {"cmd": f"html-check {path}"}})
                js = _check_js(scripts, ws)
                problems += js
                emit("tool_result", name="run_command",
                     out="HTML yapısı " + ("OK" if not hard else "; ".join(hard))
                     + (" · JS sözdizimi " + ("OK" if not js else "HATA\n" + "\n".join(js)) if scripts else "")
                     + ("\nnot: " + "; ".join(notes) if notes else ""))
            elif path.endswith(".py"):
                cmd = f"python3 -m py_compile {path} && python3 -m doctest {path} && echo DOCTEST_OK"
                emit("tool", call={"name": "run_command", "args": {"cmd": cmd}})
                out = ws.run(cmd, timeout=60, cancel=cancel)
                emit("tool_result", name="run_command", out=out[:2000])
                if "DOCTEST_OK" not in out:
                    problems.append(out[:1500])
            if not problems:
                tested.append(path)
                break
            if rnd == MAX_REPAIR:
                emit("error", text=f"{path}: onarım denemeleri bitti — {problems[0][:200]}")
                break
            emit("phase", name="onar", text=f"{path} hatasını düzeltiyorum ({rnd + 1}/{MAX_REPAIR})")
            emit("tool", call={"name": "edit_file", "args": {"path": path}})
            fix = brain.chat(history + [{"role": "user", "content": (
                f"'{path}' dosyasında şu sorunlar var:\n" + "\n".join(problems)
                + f"\n\nMevcut dosya:\n```\n{content[:6000]}\n```\n"
                "Sorunları gider ve dosyanın TAM düzeltilmiş halini tek kod bloğunda ver.")}],
                max_tokens=1800, temperature=0.2, cancel=cancel)
            written[path] = _code_of(fix)
            ws.write(path, written[path])
            emit("file", path=path, op="edit")
            emit("tool_result", name="edit_file", out=f"düzeltildi: {path}")

    # ---------- 5. raporla ----------
    emit("phase", name="rapor", text="raporluyorum")
    unresolved = [e["text"] for e in trace if e["t"] == "error"]
    rep = (("**Hazır.** " if not unresolved else "**Bitti, ama bir uyarı var.** ") + (plan.get("title") or summary) + "\n\n"
           + "Dosyalar: " + ", ".join(f"`{p}`" for p in written) + "\n"
           + "Test edilenler: " + (", ".join(f"`{p}` ✓" for p in tested) if tested else "—") + "\n"
           + ("Doğrulama: " + "; ".join(plan.get("tests") or []) + "\n" if plan.get("tests") else "")
           + ("Çözemediğim: " + "; ".join(u[:160] for u in unresolved) + "\n" if unresolved else "")
           + ("Önizleme sağda açılıyor." if any(p.endswith(".html") for p in written)
              else "Çalıştır: `python3 " + next(iter(written)) + "`" if written else ""))
    emit("answer", text=rep)
    history.append({"role": "assistant", "content": rep})
    if written and not stopped():
        emit("suggest", items=_next_steps(history, kind, cancel))
    ok = bool(written) and len(tested) == len(written)
    emit("done", ok=ok, steps=len(trace))
    return {"ok": ok, "answer": rep, "trace": trace, "history": history, "prefs": prefs,
            "files": list(written), "tested": tested}
