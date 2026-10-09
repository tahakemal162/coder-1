"""Token shard'larından eğitim pencereleri üreten torch Dataset."""
import json
import os
import numpy as np
import torch
from torch.utils.data import Dataset


class ShardedDataset(Dataset):
    """Her örnek: seq_len+1 token (girdi+hedef kaymalı). Ardışık pencereler."""

    def __init__(self, data_dir, seq_len):
        self.seq_len = seq_len
        self.shards = []
        with open(os.path.join(data_dir, "index.json")) as f:
            for e in json.load(f):
                arr = np.memmap(os.path.join(data_dir, e["file"]),
                                dtype=np.uint16, mode="r")
                self.shards.append(arr)
        self.total = sum(len(a) for a in self.shards)
        # start+seq_len+1 <= total garantisi: son pencere taşmaz
        self.n_samples = max(0, (self.total - 1) // seq_len)

    def __len__(self):
        return self.n_samples

    def _window(self, i):
        need = self.seq_len + 1
        out = np.empty(need, dtype=np.uint16)
        filled = 0
        off = i * self.seq_len
        for arr in self.shards:
            if off >= len(arr):
                off -= len(arr)
                continue
            take = min(need - filled, len(arr) - off)
            out[filled:filled + take] = arr[off:off + take]
            filled += take
            if filled >= need:
                break
            off = 0
        return out

    def __getitem__(self, i):
        w = np.asarray(self._window(i), dtype=np.int64)
        return torch.from_numpy(w[:-1]), torch.from_numpy(w[1:])
