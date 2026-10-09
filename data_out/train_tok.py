"""Train the real coder-1 BPE tokenizer on the crawled corpus.

Sample: every Nth document up to ~16MB (stratified across GH+CSN sources —
a byte-level BPE trains fine on a sample; full corpus is used for shards).
Saves to data_out/tok16k/ (vocab.json + merges.txt, loadable by BPE.load).
"""
import sys
import time

sys.path.insert(0, "/home/ubuntu/coder-1")
from model.tokenizer.bpe import BPE

RAW = "/home/ubuntu/coder-1/data_out/raw_code.txt"
SEP = "\n\n<<<CODER1_DOC_BREAK>>>\n\n"
SAMPLE_MB = int(sys.argv[1]) if len(sys.argv) > 1 else 16
OUT = "/home/ubuntu/coder-1/data_out/tok16k"


def sample_docs():
    raw = open(RAW, encoding="utf-8").read()
    docs = [d for d in raw.split(SEP) if d.strip()]
    total = sum(len(d) for d in docs)
    step = max(1, total // (SAMPLE_MB * 1_000_000))
    picked = docs[::step]
    text = "".join(picked)
    print(f"corpus: {len(docs)} docs {total/1e6:.0f}MB; sample: "
          f"{len(picked)} docs {len(text)/1e6:.1f}MB (every {step}th)")
    # feed ~4MB chunks like the spec asks
    for i in range(0, len(text), 4_000_000):
        yield text[i:i + 4_000_000]


t0 = time.time()
tok = BPE()


def prog(step, total):
    el = time.time() - t0
    print(f"merge {step}/{total}  {el/60:.1f}min  eta {(el/max(step,1))*total/60:.0f}min",
          flush=True)


tok.train(sample_docs(), vocab_size=16000, min_freq=3, progress=prog)
print(f"trained vocab={tok.vocab_size} in {(time.time()-t0)/60:.1f}min")
tok.save(OUT)

# sanity round-trip
tok2 = BPE.load(OUT)
sample = "def quicksort(a):\n    return a if len(a)<2 else quicksort([x for x in a[1:] if x<a[0]])\n"
ids = tok2.encode(sample)
back = tok2.decode(ids)
print("encode:", ids[:20], "... n=", len(ids))
print("roundtrip ok:", back == sample)
top = sorted(tok2.merges.items(), key=lambda kv: tok2.rank[kv[0]])[:15]
print("first merges:", [(a + b) for (a, b), _ in top])
