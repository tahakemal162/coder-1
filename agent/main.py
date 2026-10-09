"""coder-1 ürün backend'i — web sitesi bununla konuşur.

  POST /api/task            {text} -> {task_id}
  GET  /api/task/{id}       durum + olaylar (polling, ?after=n)
  GET  /api/task/{id}/files çalışma alanı dosya listesi
  GET  /api/task/{id}/file?path=  dosya içeriği (editör)
  GET  /api/model           model bilgisi

v0: model servisi (serve/server.py) ayrı süreç; agent loop thread'de çalışır.
"""
import os
import sys
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent.loop import agent_task
from agent.workspace import Workspace

app = FastAPI(title="coder-1 backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

MODEL_URL = os.environ.get("CODER1_MODEL_URL", "http://localhost:9100")
WORK_ROOT = os.environ.get("CODER1_WORK", os.path.expanduser("~/coder1_work"))

TASKS = {}  # id -> {status, events, result, workdir}


class TaskReq(BaseModel):
    text: str


def _run(tid, text, workdir):
    T = TASKS[tid]
    def on_event(ev):
        T["events"].append(ev)
    try:
        res = agent_task(MODEL_URL, text, workdir, on_event=on_event)
        T["result"] = res
        T["status"] = "done" if res.get("ok") else "failed"
    except Exception as e:
        T["status"] = "error"
        T["events"].append({"t": "error", "text": str(e)[:500]})


@app.post("/api/task")
def new_task(req: TaskReq):
    tid = uuid.uuid4().hex[:10]
    workdir = os.path.join(WORK_ROOT, tid)
    TASKS[tid] = {"status": "running", "events": [], "result": None,
                  "workdir": workdir}
    threading.Thread(target=_run, args=(tid, req.text, workdir), daemon=True).start()
    return {"task_id": tid}


@app.get("/api/task/{tid}")
def get_task(tid: str, after: int = 0):
    T = TASKS.get(tid)
    if not T:
        return {"error": "yok"}
    return {"status": T["status"], "events": T["events"][after:],
            "n_events": len(T["events"]), "result": T.get("result")}


@app.get("/api/task/{tid}/files")
def files(tid: str):
    T = TASKS.get(tid)
    if not T:
        return {"files": []}
    return {"files": Workspace(T["workdir"]).list_files()}


@app.get("/api/task/{tid}/file")
def file(tid: str, path: str):
    T = TASKS.get(tid)
    if not T:
        return {"error": "yok"}
    try:
        return {"path": path, "content": Workspace(T["workdir"]).read(path)}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/model")
def model_info():
    import urllib.request, json
    try:
        r = urllib.request.urlopen(MODEL_URL.rstrip("/") + "/healthz", timeout=5)
        return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "err": str(e), "model_url": MODEL_URL}


def main():
    import uvicorn
    port = int(os.environ.get("PORT", "9200"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
