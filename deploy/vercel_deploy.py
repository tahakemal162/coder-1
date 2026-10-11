"""Vercel'e web/ klasörünü deploy eder (VERCEL_TOKEN env gerekli).

Kullanım: python3 deploy/vercel_deploy.py <BACKEND_URL>
Backend URL'i (bu VM'deki agent/main.py'nin tünel adresi) /api/* rewrite'ına
yazılır — tarayıcı Vercel'e gider, /api istekleri arka uca akar.
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
TOKEN = os.environ["VERCEL_TOKEN"]
NAME = "coder-1"


def api(method, path, body=None):
    req = urllib.request.Request(
        "https://api.vercel.com" + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"}, method=method)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=60).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: {e.code} {e.read()[:400]}")


def deploy(backend_url):
    import base64
    files = []
    for dp, _, fns in os.walk(WEB):
        for fn in fns:
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, WEB).replace(os.sep, "/")
            raw = open(p, "rb").read()
            try:
                files.append({"file": rel, "data": raw.decode()})
            except UnicodeDecodeError:
                files.append({"file": rel, "data": base64.b64encode(raw).decode(),
                              "encoding": "base64"})
    cfg = {
        "name": NAME,
        "rewrites": [{"source": "/api/(.*)", "destination": backend_url.rstrip("/") + "/api/$1"}],
    }
    body = {"name": NAME, "files": files,
            "projectSettings": {"framework": None}, "target": "production",
            **cfg}
    r = api("POST", "/v13/deployments?forceNew=1", body)
    print("deploy:", r.get("url") or r)
    import time
    for _ in range(40):  # alias için READY olmasını bekle (~2 dk)
        st = api("GET", f"/v13/deployments/{r['id']}").get("readyState") if r.get("id") else "ERROR"
        if st in ("READY", "ERROR", "CANCELED"):
            break
        time.sleep(3)
    for alias in os.environ.get("C1_ALIASES", "coder-1-rosy.vercel.app").split(","):
        alias = alias.strip()
        if alias and r.get("id"):
            try:
                api("POST", f"/v2/deployments/{r['id']}/aliases", {"alias": alias})
                print("alias:", alias)
            except RuntimeError as e:
                print("alias HATA:", e)
    return r


if __name__ == "__main__":
    deploy(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9200")
