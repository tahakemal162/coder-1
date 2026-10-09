"""Metin akışını token-id shard'larına çevirir (uint16 .bin + index.json)."""
import json
import os
import numpy as np


def pretokenize_stream(text_iter, tok, out_dir, shard_tokens=8_000_000, eos="<|im_end|>"):
    """text_iter → out_dir/shard_000.bin ... uint16 token akışı.
    Her metin sonuna im_end basılır: sınırlar doküman bazlı."""
    os.makedirs(out_dir, exist_ok=True)
    eos_id = tok.special.get(eos)
    buf, idx = [], []
    shard_no = 0

    def flush():
        nonlocal buf, shard_no
        if not buf:
            return
        path = os.path.join(out_dir, f"shard_{shard_no:03d}.bin")
        np.array(buf, dtype=np.uint16).tofile(path)
        idx.append({"file": os.path.basename(path), "tokens": len(buf)})
        shard_no += 1
        buf = []

    for text in text_iter:
        ids = tok.encode(text)
        if eos_id is not None:
            ids.append(eos_id)
        buf.extend(ids)
        while len(buf) >= shard_tokens:
            flush()
    flush()
    with open(os.path.join(out_dir, "index.json"), "w") as f:
        json.dump(idx, f)
    return idx
