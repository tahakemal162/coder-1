"""coder-1 ajan döngüsü — model ↔ araçlar ↔ çalışma alanı.

Model <|tool|>{"name":..,"args":{..}} üretir → araç çalışır → sonuç
<|tool_result|> olarak modele döner → görev <|done|> ile biter.

v1 (production hardening):
- Aynı araç çağrısının üst üste tekrarı tespit edilir: önce nudge
  ({"t":"nudge"}), hâlâ devam ederse görev "repeat_loop" ile kesilir.
- tool_result modele en fazla ~2k token karşılığı (~8k karakter, baş+son
  korunarak) enjekte edilir.
- Araç başına varsayılan zaman aşımı haritası + süre ölçümü (dur_ms).
- Model numaralı/maddeli adım listesi üretirse {"t":"plan","items":[..]}.
- write_file/edit_file başarısında {"t":"file","path","op":"write|edit"}.
- POST /api/task/{id}/stop → cancel Event → döngü {"t":"stop"} ile çıkar;
  uzun süren komutlar da (host ya da docker) öldürülür.
- Çalışma alanı agent.computer sandbox'ına devredilebilir (Docker).
"""
import json
import re
import threading
import time
import urllib.request

from .workspace import Workspace

TOOL_RE = re.compile(r"<\|tool\|>\s*(\{.*?\})\s*(?:<\|im_end\|>)?$", re.S)
PLAN_ITEM_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+(.+?)\s*$")

MAX_STEPS = 20
TOOL_TIMEOUT = 60                       # geriye uyumlu genel varsayılan
TOOL_TIMEOUTS = {                       # araç başına varsayılan süre (sn)
    "run_command": 60,
    "read_file": 15,
    "write_file": 15,
    "edit_file": 15,
    "list_files": 15,
    "create_project": 15,
    "open_editor": 15,
}
TOOL_TIMEOUT_MAX = 120                  # modelin isteyebileceği üst sınır
RESULT_INJECT_CHARS = 8000              # ~2k token karşılığı (≈4 chr/tok)
RESULT_HEAD = 6000                      # uzun sonuçlarda korunan baş kısmı
REPEAT_NUDGE_AT = 2                     # üst üste aynı çağrıda nudge
REPEAT_ABORT_AT = 3                     # hâlâ aynıysa görevi kes


def call_model(base_url, messages, max_tokens=1024, temperature=0.4):
    """serve/server.py'nin OpenAI-benzeri endpoint'ine istek."""
    body = json.dumps({
        "messages": messages, "temperature": temperature,
        "max_tokens": max_tokens, "stream": False,
    }).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/v1/chat/completions",
                                 data=body, headers={"Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=600)
    return json.loads(r.read())["choices"][0]["message"]["content"]


def _tool_signature(call: dict) -> str:
    """Aynı-çağrı tespiti için kararlı imza (ad + kanonik args)."""
    try:
        args = json.dumps(call.get("args") or {}, sort_keys=True,
                          ensure_ascii=False)
    except (TypeError, ValueError):
        args = str(call.get("args"))
    return f"{call.get('name')}:{args}"


def _plan_items(text: str):
    """Model çıktısındaki adım listesini çıkar (en az 2 maddelik blok)."""
    items = []
    for line in text.splitlines():
        m = PLAN_ITEM_RE.match(line)
        if m:
            item = m.group(1).strip()
            if item and not item.startswith("<|"):
                items.append(item[:200])
    return items if len(items) >= 2 else None


def _cap_result(text: str) -> str:
    """tool_result enjeksiyonunu ~2k token karşılığıyla sınırla (baş+son)."""
    if len(text) <= RESULT_INJECT_CHARS:
        return text
    tail = RESULT_INJECT_CHARS - RESULT_HEAD
    return (text[:RESULT_HEAD]
            + f"\n[...{len(text) - RESULT_INJECT_CHARS} karakter kesildi...]\n"
            + text[-tail:])


def _tool_timeout(name: str, args: dict) -> int:
    t = TOOL_TIMEOUTS.get(name, TOOL_TIMEOUT)
    if name == "run_command" and isinstance(args.get("timeout"), (int, float)):
        t = min(max(1, int(args["timeout"])), TOOL_TIMEOUT_MAX)
    return t


def run_tool(ws: Workspace, call: dict, events=None, emit=None,
             cancel: threading.Event = None):
    """Tek araç çağrısını çalıştır → (sonuç metni, durum bilgisi dict)."""
    name, args = call.get("name"), call.get("args", {}) or {}
    t0 = time.time()
    try:
        if name == "list_files":
            out = ws.list_files(args.get("path", "."))
            res = "\n".join(out) or "(boş)"
        elif name == "read_file":
            res = ws.read(args["path"])
        elif name == "write_file":
            ws.write(args["path"], args["content"])
            res = f"yazıldı: {args['path']}"
            if emit:
                emit("file", path=args["path"], op="write")
        elif name == "edit_file":
            res = ws.edit(args["path"], args["old"], args["new"])
            if not res.startswith("hata") and emit:
                emit("file", path=args["path"], op="edit")
        elif name == "run_command":
            res = ws.run(args["cmd"],
                         timeout=_tool_timeout(name, args), cancel=cancel)
        elif name == "done" or call.get("name") == "<|done|>":
            res = "__DONE__"
        else:
            res = f"bilinmeyen araç: {name}"
    except Exception as e:
        res = f"araç hatası: {e}"
    return res, {"dur_ms": int((time.time() - t0) * 1000)}


def agent_task(base_url, task_text, workdir, on_event=None,
               max_steps=MAX_STEPS, cancel: threading.Event = None,
               sandbox=None):
    """Bir görevi uçtan uca çalıştır. on_event(dict) UI'a canlı akış.

    cancel: threading.Event — set edildiğinde döngü {"t":"stop"} ile biter.
    sandbox: agent.computer Sandbox — ws.run buna delege eder.
    """
    ws = Workspace(workdir, runner=sandbox)
    messages = [{"role": "user", "content": task_text}]
    trace = []
    last_sig, repeat_n = None, 0

    def emit(t, **kw):
        ev = {"t": t, **kw}
        trace.append(ev)
        if on_event:
            on_event(ev)

    emit("start", task=task_text)
    for step in range(max_steps):
        if cancel is not None and cancel.is_set():
            emit("stop", reason="iptal edildi")
            emit("done", ok=False, reason="stopped")
            return {"ok": False, "stopped": True, "trace": trace}
        try:
            reply = call_model(base_url, messages)
        except Exception as e:
            emit("error", text=f"model servisi: {e}")
            emit("done", ok=False, reason="model_error")
            return {"ok": False, "trace": trace}
        emit("model", text=reply, step=step)

        plan = _plan_items(reply)
        if plan:
            emit("plan", items=plan)

        m = TOOL_RE.search(reply.strip())
        if not m:
            # metin cevabı — görevin konuşma kısmı olabilir; son araç çağrısı yoksa bitir
            emit("answer", text=reply)
            if not trace or all(e["t"] != "tool" for e in trace):
                emit("done", ok=True, steps=step + 1)
                return {"ok": True, "answer": reply, "trace": trace}
            messages.append({"role": "assistant", "content": reply})
            continue
        try:
            call = json.loads(m.group(1))
        except json.JSONDecodeError:
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "tool", "content": "<|tool_result|>geçersiz JSON<|im_end|>"})
            continue

        emit("tool", call=call)

        # --- aynı çağrı tekrarı koruması ---
        sig = _tool_signature(call)
        if sig == last_sig:
            repeat_n += 1
        else:
            repeat_n = 1
        last_sig = sig
        if repeat_n >= REPEAT_ABORT_AT:
            emit("nudge", text="aynı araç çağrısı tekrarlandı — döngü kesildi")
            emit("done", ok=False, reason="repeat_loop")
            return {"ok": False, "reason": "repeat_loop", "trace": trace}
        if repeat_n >= REPEAT_NUDGE_AT:
            emit("nudge", text="aynı çağrı tekrarlandı")
            inject = ("aynı araç çağrısını tekrar ettin; sonuç değişmeyecek. "
                      "Farklı bir araç ya da argüman dene, ya da <|done|> ile bitir.")
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "tool",
                             "content": f"<|tool_result|>{inject}<|im_end|>"})
            continue

        result, meta = run_tool(ws, call, emit=emit, cancel=cancel)
        emit("tool_result", name=call.get("name"),
             out=str(result)[:2000], dur_ms=meta["dur_ms"])
        if result == "__DONE__":
            emit("done", ok=True, steps=step + 1)
            return {"ok": True, "answer": reply, "trace": trace}
        if cancel is not None and cancel.is_set():
            emit("stop", reason="iptal edildi")
            emit("done", ok=False, reason="stopped")
            return {"ok": False, "stopped": True, "trace": trace}
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "tool",
                         "content": f"<|tool_result|>{_cap_result(str(result))}<|im_end|>"})
    emit("done", ok=False, reason="step_limit")
    return {"ok": False, "trace": trace}
