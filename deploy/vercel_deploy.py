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
    files = []
    for dp, _, fns in os.walk(WEB):
        for fn in fns:
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, WEB).replace(os.sep, "/")
            files.append({"file": rel, "data": open(p).read()})
    cfg = {
        "version": 2, "name": NAME, "public": True,
        "rewrites": [{"source": "/api/(.*)", "destination": backend_url.rstrip("/") + "/api/$1"}],
    }
    body = {"name": NAME, "files": [{"file": f["file"], "data": f["data"]}
                                    for f in files],
            "projectSettings": {"framework": None},
            **cfg}
    r = api("POST", "/v13/deployments?forceNew=1", body)
    print("deploy:", r.get("url") or r)
    return r


if __name__ == "__main__":
    deploy(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9200")
