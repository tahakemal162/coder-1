"""Kalıcı depo: Vercel Blob (özel). Render gibi diski silinen sunucularda kullanıcı,
oturum, tercih ve proje dosyalarını saklar. C1_BLOB_TOKEN yoksa her şey yerel diskte kalır."""
import io
import json
import os
import tarfile
import threading
import time
import urllib.parse
import urllib.request

TOKEN = os.environ.get("C1_BLOB_TOKEN", "").strip()
PREFIX = os.environ.get("C1_BLOB_PREFIX", "c1/")
BASE = "https://blob.vercel-storage.com"
_lock = threading.Lock()
_urls = {}   # pathname -> url (liste/put sonucu)


def enabled():
    return bool(TOKEN)


def _req(url, method="GET", data=None, headers=None, timeout=30):
    h = {"Authorization": "Bearer " + TOKEN, "x-api-version": "7"}
    h.update(headers or {})
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.read()


def put(path, data: bytes, ctype="application/octet-stream"):
    if not TOKEN:
        return None
    name = PREFIX + path
    out = _req(BASE + "/" + urllib.parse.quote(name), "PUT", data,
               {"x-content-type": ctype, "x-allow-overwrite": "1", "x-add-random-suffix": "0",
                "x-vercel-blob-access": "private"})
    j = json.loads(out)
    with _lock:
        _urls[name] = j.get("url") or j.get("downloadUrl")
    return j


def _url_of(path):
    name = PREFIX + path
    with _lock:
        u = _urls.get(name)
    if u:
        return u
    j = json.loads(_req(BASE + "/?prefix=" + urllib.parse.quote(name) + "&limit=5"))
    for b in j.get("blobs", []):
        if b.get("pathname") == name:
            with _lock:
                _urls[name] = b["url"]
            return b["url"]
    return None


def get(path):
    """bytes ya da None (yoksa)."""
    if not TOKEN:
        return None
    try:
        u = _url_of(path)
        if not u:
            return None
        return _req(u + ("&" if "?" in u else "?") + "v=" + str(time.time_ns()),
                    headers={"Cache-Control": "no-cache"})
    except Exception:
        return None


def list_paths(prefix=""):
    if not TOKEN:
        return []
    out, cursor = [], None
    while True:
        q = "?prefix=" + urllib.parse.quote(PREFIX + prefix) + "&limit=1000"
        if cursor:
            q += "&cursor=" + urllib.parse.quote(cursor)
        j = json.loads(_req(BASE + "/" + q))
        for b in j.get("blobs", []):
            with _lock:
                _urls[b["pathname"]] = b["url"]
            out.append(b["pathname"][len(PREFIX):])
        cursor = j.get("cursor") if j.get("hasMore") else None
        if not cursor:
            break
    return out


# ---- JSON dosyaları (users/tokens/prefs) ----
def load_json(path, default):
    b = get(path)
    if b is None:
        return default
    try:
        return json.loads(b.decode("utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    try:
        put(path, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json")
    except Exception:
        pass


def save_json_async(path, obj):
    threading.Thread(target=save_json, args=(path, obj), daemon=True).start()


def append_line(path, line):
    """küçük günlükler için: oku-ekle-yaz (nadiren çağrılır)."""
    def _go():
        try:
            old = get(path) or b""
            put(path, old + line.encode("utf-8"), "text/plain")
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


# ---- çalışma alanı arşivi (tar.gz) ----
_SKIP = ("node_modules", ".git", "__pycache__")


def push_workdir(tid, workdir):
    if not TOKEN or not os.path.isdir(workdir):
        return False
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for root, dirs, files in os.walk(workdir):
            dirs[:] = [d for d in dirs if d not in _SKIP]
            for f in files:
                p = os.path.join(root, f)
                if os.path.getsize(p) > 5_000_000:
                    continue
                tf.add(p, arcname=os.path.relpath(p, workdir))
    try:
        put(f"ws/{tid}.tgz", buf.getvalue(), "application/gzip")
        return True
    except Exception:
        return False


def push_workdir_async(tid, workdir):
    threading.Thread(target=push_workdir, args=(tid, workdir), daemon=True).start()


def pull_workdir(tid, dest):
    b = get(f"ws/{tid}.tgz")
    if not b:
        return False
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(b), mode="r:gz") as tf:
        for m in tf.getmembers():
            if m.name.startswith("/") or ".." in m.name.split("/"):
                continue
            tf.extract(m, dest)
    return True
