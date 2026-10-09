"""coder-1 birleşik servis — model (/v1/*) + ajan backend (/api/*) tek app'te.

Deploy/tek-process modu. Yerelde iki ayrı sunucu (serve:9100 + agent:9200)
da hâlâ çalışır; bu dosya tek port isteyen hostlar içindir.

Env:
  C1_CKPT   model checkpoint dizini (varsayılan deploy/artifacts)
  C1_TOK    tokenizer dizini      (varsayılan deploy/artifacts/tok)
  PORT      dinlenecek port       (varsayılan 8080)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serve.server as srv
from agent.main import app  # /api/* + lifespan

_here = os.path.dirname(os.path.abspath(__file__))
CKPT = os.environ.get("C1_CKPT", os.path.join(_here, "deploy", "artifacts"))
TOKD = os.environ.get("C1_TOK", os.path.join(CKPT, "tok"))

srv.TOK = srv.BPE.load(TOKD)
srv.MODEL = srv.CoderModel.load(CKPT, device=srv.DEVICE)
srv.MODEL.eval()

# /api/* önce eşleşir; geri kalan (/v1/*, /healthz) model app'ine düşer.
app.mount("/", srv.app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                log_level="warning")
