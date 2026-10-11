"""coder-1 ürün backend'i — web sitesi bununla konuşur.

  POST /api/task            {text} -> {task_id}
  GET  /api/task/{id}       durum + olaylar (polling, ?after=n)
  GET  /api/task/{id}/stream  aynı olay akışı, SSE (frontend ikisinden birini seçer)
  POST /api/task/{id}/stop  çalışan görevi iptal et ({"t":"stop"} + done)
  GET  /api/tasks           tüm görevler (id, status, created, n_events)
  GET  /api/task/{id}/files çalışma alanı dosya listesi
  GET  /api/task/{id}/file?path=  dosya içeriği (editör)
  GET  /api/task/{id}/diff  workspace vs orijinal unified diff
  GET  /api/task/{id}/screen  görev bilgisayarının ekranı (JPEG; ekran yoksa 404)
  GET  /api/model           model bilgisi

v1: görevler Docker sandbox'ında koşar (varsa), host'a düşülür; boşta >1 saat
kalan çalışma alanları archive/ altına taşınır (CODER1_IDLE_S ile ayarlanır).
Sözleşme: aynı endpoint isimleri + aynı olay tipleri, sadece ekleme.
"""
import json
import os
import re
import urllib.parse
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import Request, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from agent.computer import docker_status, start_sandbox, cleanup_stale_containers
from agent.loop import agent_task
from agent import brain as _brain
from agent.loop2 import agent_task2
from agent.workspace import Workspace

MODEL_URL = os.environ.get("CODER1_MODEL_URL", "http://localhost:9100")
WORK_ROOT = os.path.abspath(os.environ.get("CODER1_WORK",
                                          os.path.expanduser("~/coder1_work")))
ARCHIVE_DIR = os.path.join(WORK_ROOT, "archive")
IDLE_S = float(os.environ.get("CODER1_IDLE_S", "3600"))
SWEEP_S = float(os.environ.get("CODER1_SWEEP_S", "600"))
USE_DOCKER = os.environ.get("CODER1_NO_DOCKER", "") != "1"

TASKS = {}      # id -> {status, events, result, workdir, cancel, ...}


class TaskReq(BaseModel):
    text: str
    name: str = ""
    answers: dict | None = None


PREFS_DIR = os.path.join(WORK_ROOT, "prefs")
USE_BRAIN = os.environ.get("C1_BRAIN", "ollama") == "ollama"


def _prefs_path(name):
    safe = "".join(ch for ch in (name or "anon") if ch.isalnum() or ch in "-_")[:40] or "anon"
    return os.path.join(PREFS_DIR, safe + ".json")


def _load_prefs(name):
    try:
        with open(_prefs_path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_prefs(name, prefs):
    try:
        os.makedirs(PREFS_DIR, exist_ok=True)
        with open(_prefs_path(name), "w") as f:
            json.dump(prefs, f, ensure_ascii=False)
    except OSError:
        pass


def _run(tid, text, workdir, answers=None):
    T = TASKS[tid]
    sb, kind, why = start_sandbox(workdir, tid, prefer_docker=USE_DOCKER)
    T["sandbox"] = sb
    T["sandbox_kind"] = kind
    if why:
        T["events"].append({"t": "sandbox", "kind": kind, "note": why})

    def on_event(ev):
        T["events"].append(ev)
        T["last_activity"] = time.time()

    try:
        if USE_BRAIN and _brain.available():
            res = agent_task2(text, workdir, on_event=on_event, cancel=T["cancel"],
                              sandbox=sb, history=T.get("history"),
                              prefs=_load_prefs(T.get("user")), answers=answers,
                              user=T.get("user", ""))
            T["history"] = res.get("history") or T.get("history") or []
            if res.get("prefs"):
                _save_prefs(T.get("user"), res["prefs"])
        else:
            res = agent_task(MODEL_URL, text, workdir, on_event=on_event,
                             cancel=T["cancel"], sandbox=sb)
        T["result"] = res
        if res.get("stopped"):
            T["status"] = "stopped"
        elif res.get("waiting"):
            T["status"] = "waiting"
        else:
            T["status"] = "done" if res.get("ok") else "failed"
    except Exception as e:
        T["status"] = "error"
        T["events"].append({"t": "error", "text": str(e)[:500]})
    finally:
        T["last_activity"] = time.time()
        try:
            sb.close()
        except Exception:
            pass
        T["sandbox"] = None       # görev bitti; ekran artık yok (404 dürüst)


def _archive_idle_workdirs():
    """Boşta kalan çalışma alanlarını WORK_ROOT/archive/<tid>'ye taşır."""
    while True:
        time.sleep(SWEEP_S)
        now = time.time()
        for tid, T in list(TASKS.items()):
            if T["status"] == "running":
                continue
            if now - T.get("last_activity", T["created"]) <= IDLE_S:
                continue
            src = T["workdir"]
            dst = os.path.join(ARCHIVE_DIR, tid)
            try:
                os.makedirs(ARCHIVE_DIR, exist_ok=True)
                os.replace(src, dst)          # dizin adı archive'e taşınır
                T["workdir"] = dst
                T["archived"] = True
            except OSError:
                pass
        # TASKS kaydı olmayan sahipsiz dizinler de aynı kurala girer
        try:
            for name in os.listdir(WORK_ROOT):
                p = os.path.join(WORK_ROOT, name)
                if (not os.path.isdir(p) or name == "archive"
                        or name in TASKS):
                    continue
                if now - os.path.getmtime(p) > IDLE_S:
                    os.makedirs(ARCHIVE_DIR, exist_ok=True)
                    os.replace(p, os.path.join(ARCHIVE_DIR, name))
        except OSError:
            pass


@asynccontextmanager
async def _lifespan(app):
    os.makedirs(WORK_ROOT, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    cleanup_stale_containers()
    t = threading.Thread(target=_archive_idle_workdirs, daemon=True)
    t.start()
    yield


app = FastAPI(title="coder-1 backend", lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.post("/api/task")
def new_task(req: TaskReq):
    tid = uuid.uuid4().hex[:10]
    workdir = os.path.join(WORK_ROOT, tid)
    now = time.time()
    TASKS[tid] = {"status": "running", "events": [], "result": None,
                  "workdir": workdir, "cancel": threading.Event(),
                  "created": now, "last_activity": now,
                  "text": req.text[:200], "archived": False,
                  "sandbox": None, "sandbox_kind": None,
                  "user": req.name[:40], "history": []}
    threading.Thread(target=_run, args=(tid, req.text, workdir, req.answers),
                     daemon=True).start()
    return {"task_id": tid}


@app.post("/api/task/{tid}/reply")
def reply_task(tid: str, req: TaskReq):
    """Soru sorulan ya da biten göreve aynı çalışma alanında devam et."""
    T = TASKS.get(tid)
    if not T:
        return JSONResponse({"error": "yok"}, status_code=404)
    if T["status"] == "running":
        return JSONResponse({"error": "görev hâlâ çalışıyor"}, status_code=409)
    T["status"] = "running"
    T["cancel"] = threading.Event()
    T["last_activity"] = time.time()
    if req.name:
        T["user"] = req.name[:40]
    threading.Thread(target=_run, args=(tid, req.text, T["workdir"], req.answers),
                     daemon=True).start()
    return {"task_id": tid, "n_events": len(T["events"])}


@app.get("/api/task/{tid}")
def get_task(tid: str, after: int = 0):
    T = TASKS.get(tid)
    if not T:
        return {"error": "yok"}
    return {"status": T["status"], "events": T["events"][after:],
            "n_events": len(T["events"]), "result": T.get("result"),
            "sandbox": T.get("sandbox_kind"), "archived": T.get("archived", False)}


@app.get("/api/task/{tid}/stream")
def stream_task(tid: str):
    """SSE: polling kontratıyla aynı olay nesnelerini akar."""
    T = TASKS.get(tid)
    if not T:
        return JSONResponse({"error": "yok"}, status_code=404)

    def gen():
        sent = 0
        yield f"data: {json.dumps({'t':'meta','task_id':tid,'status':T['status']}, ensure_ascii=False)}\n\n"
        while True:
            evs = T["events"][sent:]
            for ev in evs:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            sent += len(evs)
            if T["status"] != "running" and not T["events"][sent:]:
                yield f"data: {json.dumps({'t':'eos','status':T['status']}, ensure_ascii=False)}\n\n"
                return
            time.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/task/{tid}/stop")
def stop_task(tid: str):
    T = TASKS.get(tid)
    if not T:
        return {"error": "yok"}
    if T["status"] != "running":
        return {"ok": False, "status": T["status"]}
    T["cancel"].set()
    T["events"].append({"t": "stop_requested"})
    return {"ok": True, "status": "stopping"}


@app.get("/api/tasks")
def list_tasks():
    out = []
    for tid, T in sorted(TASKS.items(), key=lambda kv: kv[1]["created"],
                         reverse=True):
        out.append({"task_id": tid, "status": T["status"],
                    "created": T["created"],
                    "last_activity": T["last_activity"],
                    "n_events": len(T["events"]),
                    "text": T["text"], "archived": T.get("archived", False),
                    "sandbox": T.get("sandbox_kind")})
    return {"tasks": out}


@app.get("/api/task/{tid}/files")
def files(tid: str):
    wd = _workdir_of(tid)
    if not wd:
        return {"files": []}
    return {"files": Workspace(wd).list_files()}


from fastapi.responses import FileResponse, HTMLResponse


_LOCAL_REF = re.compile(r"""(\b(?:src|href)\s*=\s*)(["'])(?!https?:|//|#|mailto:|tel:|data:|javascript:)([^"'?#]+)(#[^"']*)?\2""", re.I)


def _workdir_of(tid):
    """Görev bellekte yoksa (sunucu yeniden başlamış) diskteki çalışma alanını bul."""
    T = TASKS.get(tid)
    if T:
        return T["workdir"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", tid or ""):
        return None
    for d in (os.path.join(WORK_ROOT, tid), os.path.join(ARCHIVE_DIR, tid)):
        if os.path.isdir(d):
            return d
    return None


@app.get("/api/task/{tid}/raw")
def raw_file(tid: str, path: str, request: Request):
    """önizleme: üretilen dosyayı ham içerikle servisler; HTML'deki yerel css/js/sayfa bağlantıları
    aynı uç noktaya (token'lı) yönlendirilir ki iframe'de stil ve sayfalar arası geçiş çalışsın."""
    wd = _workdir_of(tid)
    if not wd:
        return {"error": "yok"}
    try:
        ws = Workspace(wd)
        fp = ws._abs(path)
        if not path.lower().endswith((".html", ".htm")):
            return FileResponse(fp)
        html = open(fp, encoding="utf-8", errors="replace").read()
        tok = request.query_params.get("token", "")
        base = path.rsplit("/", 1)[0] + "/" if "/" in path else ""

        def _sub(m):
            target = m.group(3).strip()
            rel = os.path.normpath(base + target).replace("\\", "/").lstrip("./")
            q = f"raw?path={urllib.parse.quote(rel)}" + (f"&token={urllib.parse.quote(tok)}" if tok else "")
            return f'{m.group(1)}{m.group(2)}{q}{m.group(4) or ""}{m.group(2)}'

        return HTMLResponse(_LOCAL_REF.sub(_sub, html))
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/task/{tid}/file")
def file(tid: str, path: str):
    wd = _workdir_of(tid)
    if not wd:
        return {"error": "yok"}
    try:
        return {"path": path, "content": Workspace(wd).read(path)}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/task/{tid}/diff")
def task_diff(tid: str):
    T = TASKS.get(tid)
    if not T:
        return {"error": "yok"}
    try:
        return {"task_id": tid, "diff": Workspace(T["workdir"]).diff()}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/task/{tid}/screen")
def task_screen(tid: str):
    """Görev bilgisayarının gerçek ekranı — X görüntüsü yoksa 404."""
    T = TASKS.get(tid)
    if not T:
        return JSONResponse({"error": "yok"}, status_code=404)
    sb = T.get("sandbox")
    if sb is None:
        return JSONResponse({"error": "ekran yok — görev çalışmıyor"},
                            status_code=404)
    jpg = sb.screenshot()
    if not jpg:
        return JSONResponse({"error": "ekran yok — sandbox'ta display yok"},
                            status_code=404)
    return Response(content=jpg, media_type="image/jpeg")


@app.get("/api/model")
def model_info():
    import urllib.request
    try:
        r = urllib.request.urlopen(MODEL_URL.rstrip("/") + "/healthz", timeout=5)
        out = json.loads(r.read())
        out["docker"] = docker_status()
        if USE_BRAIN and _brain.available():
            out["brain"] = _brain.current_label()
            out["model"] = "coder-1"
        return out
    except Exception as e:
        return {"ok": False, "err": str(e), "model_url": MODEL_URL}


def main():
    import uvicorn
    port = int(os.environ.get("PORT", "9200"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
