"""coder-1 beyin katmanı — yerel Ollama (OpenAI uyumlu değil, native /api/chat).

Ürün beyni açık ağırlıklı bir kod modelidir (varsayılan qwen2.5-coder:3b);
coder-1 kimliği, soru sorma akışı, doğrulama ve onarım döngüsü bizimdir.
"""
import json
import threading
import os
import urllib.request

BRAIN_URL = os.environ.get("C1_BRAIN_URL", "http://127.0.0.1:11434").rstrip("/")
BRAIN_MODEL = os.environ.get("C1_BRAIN_MODEL", "qwen2.5-coder:3b")
FAST_MODEL = os.environ.get("C1_FAST_MODEL", BRAIN_MODEL)
NUM_CTX = int(os.environ.get("C1_BRAIN_CTX", "4096"))

SYSTEM = (
    "Sen coder-1'sin: Türkçe konuşan, kısa ve net bir yazılım geliştirme asistanısın. "
    "Kullanıcılar genelde prompt yazmayı bilmez; onların hayalindeki şeyi tam anlamak için "
    "gerekirse seçenekli sorular sorarsın, sonra isteğe birebir özel, çalışan kod üretirsin. "
    "Asla yer tutucu, sahte veri veya 'buraya kod gelecek' bırakmazsın."
)


def available():
    try:
        urllib.request.urlopen(BRAIN_URL + "/api/version", timeout=3)
        return True
    except Exception:
        return False


_GATE = threading.Lock()          # CPU'da tek model: çağrılar sırayla
_WAITING = [0]
TLS = threading.local()           # TLS.on_wait = callable(n_onde) → görev "sırada" bilgisi verir


def _acquire(cancel):
    if _GATE.acquire(blocking=False):
        return True
    hook = getattr(TLS, "on_wait", None)
    _WAITING[0] += 1
    try:
        if hook:
            try:
                hook(_WAITING[0])
            except Exception:
                pass
        while not _GATE.acquire(timeout=1.0):
            if cancel is not None and cancel.is_set():
                return False
        return True
    finally:
        _WAITING[0] -= 1


def chat(messages, max_tokens=1200, temperature=0.3, schema=None,
         on_delta=None, model=None, cancel=None, timeout=900):
    """Ollama /api/chat (stream). schema verilirse JSON zorlanır ve dict döner."""
    if not _acquire(cancel):
        raise RuntimeError("iptal edildi")
    try:
        return _chat(messages, max_tokens, temperature, schema, on_delta, model, cancel, timeout)
    finally:
        _GATE.release()


def _chat(messages, max_tokens, temperature, schema, on_delta, model, cancel, timeout):
    body = {
        "model": model or BRAIN_MODEL,
        "messages": [{"role": "system", "content": SYSTEM}] + messages,
        "stream": True,
        "keep_alive": "30m",
        "options": {"temperature": temperature, "num_predict": max_tokens,
                    "num_ctx": NUM_CTX, "repeat_penalty": 1.05},
    }
    if schema is not None:
        body["format"] = schema
        body["options"]["temperature"] = min(temperature, 0.2)
    req = urllib.request.Request(BRAIN_URL + "/api/chat",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    out = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r:
            if cancel is not None and cancel.is_set():
                break
            try:
                j = json.loads(line)
            except ValueError:
                continue
            d = (j.get("message") or {}).get("content", "")
            if d:
                out.append(d)
                if on_delta:
                    on_delta(d)
            if j.get("done"):
                break
    text = "".join(out)
    if schema is None:
        return text
    try:
        return json.loads(text)
    except ValueError:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except ValueError:
                pass
        return {}
