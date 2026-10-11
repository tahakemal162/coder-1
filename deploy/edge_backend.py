"""Canlı backend (tünel) adresini Vercel Edge Config'e anında yazar (deploy gerekmez).
Kullanım: python3 deploy/edge_backend.py https://xxxx.lhr.life
"""
import json
import os
import sys
import urllib.request

CFG = "/home/ubuntu/c1run/edge_config.json"


def set_backend(url):
    cfg = json.load(open(CFG))
    body = {"items": [{"operation": "upsert", "key": "backend", "value": url.rstrip("/")}]}
    req = urllib.request.Request(
        f"https://api.vercel.com/v1/edge-config/{cfg['id']}/items",
        data=json.dumps(body).encode(), method="PATCH",
        headers={"Authorization": "Bearer " + os.environ["VERCEL_TOKEN"],
                 "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].startswith("https://"):
        sys.exit("kullanım: edge_backend.py https://<tünel-adresi>  (yalnızca URL yazar)")
    print("edge:", set_backend(sys.argv[1]))
