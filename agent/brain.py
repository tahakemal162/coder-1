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


import time

# ---- Bulut beyin zinciri (diğer şirketler gibi: hazır güçlü model + bizim ürün katmanı) ----
# Her sağlayıcı: {"id","label","kind": "openai"|"gemini", "url","key","model"}; sırayla denenir,
# hata/kota (429, 5xx, zaman aşımı) alan sağlayıcı COOLDOWN sn dinlenir; hiçbiri yoksa yerel Ollama.
COOLDOWN = int(os.environ.get("C1_BRAIN_COOLDOWN", "180"))


def _providers():
    raw = os.environ.get("C1_PROVIDERS", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            pass
    out = []
    ork = os.environ.get("OPENROUTER_API_KEY", "").strip()
    gmk = os.environ.get("GEMINI_API_KEY", "").strip()
    OR = "https://openrouter.ai/api/v1/chat/completions"
    if ork:
        out.append({"id": "or-super", "label": "Nemotron 3 Super 120B", "kind": "openai", "url": OR, "key": ork,
                    "model": "nvidia/nemotron-3-super-120b-a12b:free"})
        out.append({"id": "or-ultra", "label": "Nemotron 3 Ultra 550B", "kind": "openai", "url": OR, "key": ork,
                    "model": "nvidia/nemotron-3-ultra-550b-a55b:free"})
    GM = "https://generativelanguage.googleapis.com/v1beta/models"
    if gmk:
        out.append({"id": "gm-lite", "label": "Gemini 3.1 Flash Lite", "kind": "gemini", "url": GM, "key": gmk,
                    "model": "gemini-3.1-flash-lite"})
        out.append({"id": "gm-flash", "label": "Gemini Flash", "kind": "gemini", "url": GM, "key": gmk,
                    "model": "gemini-flash-latest"})
    if ork:
        out.append({"id": "or-gemma31", "label": "Gemma 4 31B", "kind": "openai", "url": OR, "key": ork,
                    "model": "google/gemma-4-31b-it:free"})
        out.append({"id": "or-gemma26", "label": "Gemma 4 26B", "kind": "openai", "url": OR, "key": ork,
                    "model": "google/gemma-4-26b-a4b-it:free"})
        out.append({"id": "or-cohere", "label": "Cohere North Mini Code", "kind": "openai", "url": OR, "key": ork,
                    "model": "cohere/north-mini-code:free"})
    return out


PROVIDERS = _providers()
_COOL = {}          # id -> yeniden deneme zamanı
_LAST = ["yerel"]


def current_label():
    if not PROVIDERS:
        return BRAIN_MODEL
    live = [p["label"] for p in PROVIDERS if _COOL.get(p["id"], 0) <= time.time()]
    return (live[0] + " (bulut)") if live else BRAIN_MODEL + " (yerel yedek)"


def _oai_call(p, messages, max_tokens, temperature, schema, timeout):
    body = {"model": p["model"], "messages": [{"role": "system", "content": SYSTEM}] + messages,
            "max_tokens": max(max_tokens, 400), "temperature": temperature}
    if "openrouter" in p["url"]:
        body["reasoning"] = {"enabled": False}
    if schema is not None:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}
    req = urllib.request.Request(p["url"], data=json.dumps(body).encode(), headers={
        "Authorization": "Bearer " + p["key"], "Content-Type": "application/json",
        "HTTP-Referer": "https://coder-1-rosy.vercel.app", "X-Title": "coder-1"})
    r = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    ch = (r.get("choices") or [{}])[0]
    txt = (ch.get("message") or {}).get("content") or ""
    if not txt.strip():
        raise RuntimeError("boş cevap")
    return txt


def _gem_schema(sc, in_props=False):
    if isinstance(sc, dict):
        if in_props:  # property adları (ör. "title") olduğu gibi kalır
            return {k: _gem_schema(v) for k, v in sc.items()}
        return {k: _gem_schema(v, in_props=(k == "properties")) for k, v in sc.items()
                if k not in ("additionalProperties", "default", "title", "$schema")}
    if isinstance(sc, list):
        return [_gem_schema(x) for x in sc]
    return sc


def _gem_call(p, messages, max_tokens, temperature, schema, timeout):
    contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                for m in messages]
    body = {"systemInstruction": {"parts": [{"text": SYSTEM}]}, "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature}}
    if schema is not None:
        body["generationConfig"]["responseMimeType"] = "application/json"
        body["generationConfig"]["responseSchema"] = _gem_schema(schema)
    url = f"{p['url']}/{p['model']}:generateContent?key={p['key']}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    parts = ((r.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    txt = "".join(x.get("text", "") for x in parts)
    if not txt.strip():
        raise RuntimeError("boş cevap")
    return txt


def _cloud(messages, max_tokens, temperature, schema, on_delta, cancel, timeout):
    """Zincirdeki ilk çalışan bulut sağlayıcıyla cevap; hepsi düşerse None."""
    import sys
    for p in PROVIDERS:
        if cancel is not None and cancel.is_set():
            raise RuntimeError("iptal edildi")
        if _COOL.get(p["id"], 0) > time.time():
            continue
        try:
            fn = _gem_call if p["kind"] == "gemini" else _oai_call
            txt = fn(p, messages, max_tokens, temperature, schema, min(timeout, 240))
            _LAST[0] = p["label"]
            if on_delta:
                on_delta(txt)
            return txt
        except Exception as e:
            _COOL[p["id"]] = time.time() + COOLDOWN
            print(f"[brain] {p['id']} düştü: {str(e)[:120]}", file=sys.stderr, flush=True)
    return None


def _parse(text, schema):
    if schema is None:
        return text
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:]
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
    """Önce bulut zinciri (hızlı, güçlü), düşerse yerel Ollama. schema verilirse dict döner."""
    if PROVIDERS:
        txt = _cloud(messages, max_tokens, temperature, schema, on_delta, cancel, timeout)
        if txt is not None:
            return _parse(txt, schema)
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
