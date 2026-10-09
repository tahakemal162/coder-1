"""Rebalance CSN tail: keep GitHub docs, append ~equal bytes per CSN lang
(python/javascript/go/java) so all four are represented, total <=250MB."""
import json
import sys
import time

sys.path.insert(0, "/home/ubuntu/coder-1")
from model.data.clean import looks_like_code, Dedup

RAW = "/home/ubuntu/coder-1/data_out/raw_code.txt"
LOG = "/home/ubuntu/coder-1/data_out/crawl_log.jsonl"
SEP = "\n\n<<<CODER1_DOC_BREAK>>>\n\n"
TOTAL_CAP = 250_000_000
LANGS = ("python", "javascript", "go", "java")

raw = open(RAW, encoding="utf-8").read()
docs = raw.split(SEP)
# GH docs were written first; CSN docs start with "# code_search_net/"
gh = []
for d in docs:
    if d.startswith("# code_search_net/"):
        break
    if d.strip():
        gh.append(d)

gh_bytes = sum(len(d.encode("utf-8")) for d in gh)
per_lang = (TOTAL_CAP - gh_bytes) // len(LANGS)
print(f"GH docs kept: {len(gh)} {gh_bytes/1e6:.1f}MB; per-lang cap {per_lang/1e6:.1f}MB",
      flush=True)

dd = Dedup()
for d in gh:
    dd.is_new(d)  # seed dedup with GH content

out = open(RAW, "w", encoding="utf-8")
log = open(LOG, "a")
written = gh_bytes
for d in gh:
    out.write(d)
    out.write(SEP)
written += len(gh) * len(SEP.encode())

from datasets import load_dataset
for lang in LANGS:
    t0 = time.time()
    nb = nf = 0
    err = None
    try:
        ds = load_dataset(f"Nan-Do/code-search-net-{lang}", split="train",
                          streaming=True)
        for row in ds:
            if nb >= per_lang:
                break
            code = (row.get("code") or "").strip()
            if not code or not looks_like_code(code) or not dd.is_new(code):
                continue
            hdr = f"# code_search_net/{lang} {row.get('repo','')}::{row.get('func_name','')}\n"
            text = hdr + code
            b = text.encode("utf-8")
            out.write(text)
            out.write(SEP)
            written += len(b) + len(SEP.encode())
            nb += len(b)
            nf += 1
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:180]}"
    rec = {"src": "code_search_net", "lang": lang, "funcs": nf,
           "bytes": nb, "sec": round(time.time() - t0, 1), "rebalanced": True}
    if err:
        rec["err"] = err
    log.write(json.dumps(rec) + "\n")
    log.flush()
    print(rec, flush=True)

out.close()
print(f"TOTAL {written/1e6:.1f}MB", flush=True)
