"""coder-1 data team: corpus crawl driver.

Phase 1: GitHub codeload tarballs for repos.txt (300 files/repo max),
         filtered by looks_like_code + Dedup, ~150MB cap.
Phase 2: CodeSearchNet function records (HF `code_search_net` is dead on
         datasets>=3; Nan-Do/code-search-net-{lang} mirrors carry the same
         real records: code/docstring/repo/sha). ~100MB target.
Output: data_out/raw_code.txt, docs separated by <<<CODER1_DOC_BREAK>>>.
Log:    data_out/crawl_log.jsonl — one JSON record per repo/lang.
"""
import json
import sys
import time

sys.path.insert(0, "/home/ubuntu/coder-1")
from model.data.sources import iter_github_tarball
from model.data.clean import looks_like_code, Dedup

OUT = "/home/ubuntu/coder-1/data_out/raw_code.txt"
LOG = "/home/ubuntu/coder-1/data_out/crawl_log.jsonl"
SEP = "\n\n<<<CODER1_DOC_BREAK>>>\n\n"
GH_CAP = 150_000_000
TOTAL_CAP = 250_000_000
CSN_LANGS = ("python", "javascript", "go", "java")


def main():
    repos = [l.strip() for l in open("/home/ubuntu/coder-1/model/data/repos.txt")
             if l.strip() and not l.startswith("#")]
    dd = Dedup()
    written = 0

    with open(OUT, "w", encoding="utf-8") as out, open(LOG, "w") as log:
        # ---- phase 1: github tarballs ----
        for r in repos:
            if written >= GH_CAP:
                break
            t0 = time.time()
            nb = nf = 0
            err = None
            try:
                for text in iter_github_tarball(r, max_files=300):
                    if looks_like_code(text) and dd.is_new(text):
                        b = text.encode("utf-8")
                        out.write(text)
                        out.write(SEP)
                        written += len(b) + len(SEP.encode())
                        nb += len(b)
                        nf += 1
                        if written >= GH_CAP:
                            break
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:180]}"
            rec = {"src": "github", "repo": r, "files": nf, "bytes": nb,
                   "sec": round(time.time() - t0, 1)}
            if err:
                rec["err"] = err
            if nf == 0 and not err:
                rec["err"] = "no_files_yielded (download failed or empty)"
            log.write(json.dumps(rec) + "\n")
            log.flush()
            print(rec, flush=True)

        gh_bytes = written
        print(f"GH phase done: {gh_bytes/1e6:.1f}MB", flush=True)

        # ---- phase 2: code_search_net mirrors ----
        from datasets import load_dataset
        for lang in CSN_LANGS:
            if written >= TOTAL_CAP:
                break
            t0 = time.time()
            nb = nf = 0
            err = None
            try:
                ds = load_dataset(f"Nan-Do/code-search-net-{lang}",
                                  split="train", streaming=True)
                for row in ds:
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
                    if written >= TOTAL_CAP or nf % 2000 == 0:
                        print(f"  {lang}: {nf} funcs, {nb/1e6:.1f}MB", flush=True)
                    if written >= TOTAL_CAP:
                        break
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:180]}"
            rec = {"src": "code_search_net", "lang": lang, "funcs": nf,
                   "bytes": nb, "sec": round(time.time() - t0, 1)}
            if err:
                rec["err"] = err
            log.write(json.dumps(rec) + "\n")
            log.flush()
            print(rec, flush=True)

    print(f"TOTAL: {written/1e6:.1f}MB -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
