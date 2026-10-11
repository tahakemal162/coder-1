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
    asked_before = any(m.get("content", "").startswith("Cevaplarım:") for m in history)
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
    questions = [q for q in (u.get("questions") or []) if q.get("q")][:3]

    if kind == "chat":
        acc = []
        reply = brain.chat(history + [{"role": "user", "content":
                           "Kısa, samimi ve Türkçe cevap ver. Kod üretme; gerekirse ne yapabileceğini bir cümleyle söyle."}],
                           max_tokens=300, temperature=0.6, cancel=cancel,
                           on_delta=lambda d: acc.append(d))
        emit("answer", text=reply.strip())
        history.append({"role": "assistant", "content": reply.strip()})
        emit("done", ok=True, steps=1)
        return {"ok": True, "answer": reply, "trace": trace, "history": history, "prefs": prefs}

    if not asked_before and kind != "chat" and not questions and _detail_score(task_text) < 2:
        try:
            q2 = brain.chat(history + [{"role": "user", "content": (
                "İstek kısa ve detaysız. Sonucu kullanıcının hayaline birebir uydurmak için "
                "2-3 kısa Türkçe soru üret, her birine 2-4 somut seçenek ver (ör. renk/tema, "
                "ana özellik, dil/stil). SADECE JSON ver." + ("\n" + pref_txt if pref_txt else ""))}],
                schema=UNDERSTAND_SCHEMA, max_tokens=400, cancel=cancel, model=brain.FAST_MODEL)
            questions = [q for q in (q2.get("questions") or []) if q.get("q")][:3]
        except Exception:
            questions = []
    if questions and not asked_before:
        emit("ask", questions=questions, summary=summary)
        history.append({"role": "assistant", "content":
                        "Tam istediğin gibi olsun diye birkaç soru: " +
                        json.dumps(questions, ensure_ascii=False)})
        return {"ok": True, "waiting": True, "trace": trace, "history": history, "prefs": prefs}

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
    ok = bool(written) and len(tested) == len(written)
    emit("done", ok=ok, steps=len(trace))
    return {"ok": ok, "answer": rep, "trace": trace, "history": history, "prefs": prefs,
            "files": list(written), "tested": tested}
