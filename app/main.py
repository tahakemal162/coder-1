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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve.server as srv
import agent.main as agent_mod
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT = os.environ.get("C1_CKPT", os.path.join(_here, "deploy", "artifacts"))
TOKD = os.environ.get("C1_TOK", os.path.join(CKPT, "tok"))

srv.TOK = srv.BPE.load(TOKD)
srv.MODEL = srv.CoderModel.load(CKPT, device=srv.DEVICE)
srv.MODEL.eval()

app = FastAPI(title="coder-1", lifespan=agent_mod._lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# --- kullanıcı kayıt/giriş: ad+parola -> token ---
import secrets
import hashlib
import json as _json
import threading
from fastapi import Request
from fastapi.responses import JSONResponse

USERS_FILE = os.environ.get("C1_USERS", "/home/ubuntu/c1run/users.json")
TOKENS_FILE = USERS_FILE.replace("users.json", "tokens.json")
try:
    TOKENS = _json.load(open(TOKENS_FILE))   # yeniden başlatmada oturumlar düşmesin
except Exception:
    TOKENS = {}
# ajan döngüsünün aynı süreçteki /v1 çağrıları için dahili token (dışarı çıkmaz)
_INTERNAL = secrets.token_hex(24)
TOKENS[_INTERNAL] = "__internal__"
os.environ["CODER1_INTERNAL_TOKEN"] = _INTERNAL
_users_lock = threading.Lock()


def _users():
    try:
        return _json.load(open(USERS_FILE))
    except Exception:
        return {}


def _save_users(u):
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
    tmp = USERS_FILE + ".tmp"
    _json.dump(u, open(tmp, "w"))
    os.replace(tmp, USERS_FILE)


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 60000).hex()


def _check_pw(body, require_name=False):
    name = (body.get("name") or "").strip().lower()
    pw = body.get("password") or ""
    if not name or not pw:
        return None, "ad ve parola gerekli"
    u = _users().get(name)
    if not u or u.get("google"):
        return None, "kullanıcı yok — önce kayıt ol"
    return (name if _hash(pw, u["salt"]) == u["hash"] else None), "yanlış parola"


def _issue(name):
    tok = secrets.token_hex(16)
    TOKENS[tok] = name
    try:
        persist = {k: v for k, v in TOKENS.items() if v != "__internal__"}
        _json.dump(persist, open(TOKENS_FILE + ".tmp", "w"))
        os.replace(TOKENS_FILE + ".tmp", TOKENS_FILE)
    except OSError:
        pass
    return {"token": tok, "name": name}


@app.post("/api/register")
def register(body: dict):
    name = (body.get("name") or "").strip().lower()
    pw = body.get("password") or ""
    if not (2 <= len(name) <= 24) or not name.replace("_", "").isalnum():
        return JSONResponse({"detail": "ad 2-24 karakter, harf/rakam/_"}, 400)
    if len(pw) < 4:
        return JSONResponse({"detail": "parola en az 4 karakter"}, 400)
    with _users_lock:
        u = _users()
        if name in u:
            return JSONResponse({"detail": "bu ad alınmış"}, 409)
        salt = secrets.token_hex(8)
        u[name] = {"salt": salt, "hash": _hash(pw, salt),
                   "created": __import__("time").time()}
        _save_users(u)
    return _issue(name)


FB_FILE = os.environ.get("C1_FB", "/home/ubuntu/c1run/feedback.jsonl")


@app.post("/api/feedback")
def feedback(body: dict):
    import time as _t
    row = {"ts": _t.time(), "task_id": body.get("task_id"),
           "rating": int(body.get("rating", 0)),
           "text": (body.get("text") or "")[:2000],
           "user": (body.get("name") or "")[:32]}
    try:
        open(FB_FILE, "a").write(_json.dumps(row, ensure_ascii=False) + "\n")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"detail": str(e)}, 500)


GOOGLE_CLIENT_ID = os.environ.get("C1_GOOGLE_CLIENT_ID", "").strip()


@app.get("/api/auth/config")
def auth_config():
    return {"google_client_id": GOOGLE_CLIENT_ID}


@app.post("/api/auth/google")
def login_google(body: dict):
    """Google ile giriş: tarayıcıdan gelen ID token'ı Google'a doğrulatır, e-postayı hesap yapar."""
    import urllib.request as _ur
    import urllib.parse as _up
    if not GOOGLE_CLIENT_ID:
        return JSONResponse({"detail": "Google ile giriş bu sunucuda kapalı"}, 503)
    cred = (body.get("credential") or "").strip()
    if not cred:
        return JSONResponse({"detail": "kimlik bilgisi yok"}, 400)
    try:
        info = _json.loads(_ur.urlopen("https://oauth2.googleapis.com/tokeninfo?id_token="
                                       + _up.quote(cred), timeout=10).read())
    except Exception:
        return JSONResponse({"detail": "Google doğrulaması başarısız"}, 401)
    if info.get("aud") != GOOGLE_CLIENT_ID or info.get("email_verified") not in ("true", True):
        return JSONResponse({"detail": "Google kimliği geçersiz"}, 401)
    email = (info.get("email") or "").lower()
    name = "g:" + email
    with _users_lock:
        u = _users()
        if name not in u:
            u[name] = {"google": True, "email": email, "display": info.get("name") or email.split("@")[0],
                       "created": __import__("time").time()}
            _save_users(u)
    out = _issue(name)
    out["display"] = u[name].get("display") or email
    return out


@app.post("/api/login")
def login(body: dict):
    name, err = _check_pw(body)
    if not name:
        return JSONResponse({"detail": err}, status_code=401)
    return _issue(name)


@app.middleware("http")
async def auth_gate(req: Request, call_next):
    if req.method == "OPTIONS":  # CORS ön uçuşu token taşımaz
        return await call_next(req)
    if (req.url.path.startswith("/api") or req.url.path.startswith("/v1")) and \
            req.url.path not in ("/api/login", "/api/register", "/api/auth/google", "/api/auth/config"):
        tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not tok:
            tok = req.query_params.get("token", "")
        if tok not in TOKENS:
            return JSONResponse({"detail": "yetkisiz"}, status_code=401,
                                headers={"Access-Control-Allow-Origin": "*"})
    return await call_next(req)

# ajan backend route'ları (/api/*)
for r in agent_mod.app.routes:
    app.router.routes.append(r)
# geri kalan (/v1/*, /healthz) model app'ine düşer
app.mount("/", srv.app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                log_level="warning")
