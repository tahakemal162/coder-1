"""Pretokenize the whole corpus into uint16 token shards via
model.data.pretokenize.pretokenize_stream (8M tokens/shard)."""
import sys
import time

sys.path.insert(0, "/home/ubuntu/coder-1")
from model.data.pretokenize import pretokenize_stream
from model.tokenizer.bpe import BPE

RAW = "/home/ubuntu/coder-1/data_out/raw_code.txt"
SEP = "\n\n<<<CODER1_DOC_BREAK>>>\n\n"
OUT = "/home/ubuntu/coder-1/data_out/shards"


def docs():
    with open(RAW, encoding="utf-8") as f:
        raw = f.read()
    for d in raw.split(SEP):
        if d.strip():
            yield d


tok = BPE.load("/home/ubuntu/coder-1/data_out/tok16k")
t0 = time.time()
idx = pretokenize_stream(docs(), tok, OUT, shard_tokens=8_000_000)
total = sum(e["tokens"] for e in idx)
print(f"{total} tokens in {len(idx)} shards, {(time.time()-t0)/60:.1f}min")
for e in idx:
    print(e)
