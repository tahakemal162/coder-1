"""SFT — talimat/ajan formatında ince ayar.

Özellik: sadece ASİSTAN token'larına loss uygulanır (prompt maskeli, -100).
Girdi: JSONL [{"messages":[{role,content},...]}, ...] — bizim chat formatında.
"""
import argparse
import json
import os
import torch
from torch.utils.data import Dataset, DataLoader

from .architecture import CoderModel
from .tokenizer.bpe import BPE
from .pretrain import cosine_lr


class SFTData(Dataset):
    def __init__(self, jsonl_path, tok, seq_len):
        self.rows = []
        self.tok, self.seq_len = tok, seq_len
        self.im_end = tok.special["<|im_end|>"]
        with open(jsonl_path) as f:
            for line in f:
                if line.strip():
                    self.rows.append(json.loads(line))

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        # Segman segman encode → etiket hizalaması yapısal olarak doğru.
        # Sadece asistan gövdesi (içerik + <|im_end|>) öğrenilir; header maskeli.
        msgs = self.rows[i]["messages"]
        ids, labels = [], []
        for m in msgs:
            hdr_ids = self.tok.encode(f"<|im_start|>{m['role']}\n")
            body_ids = self.tok.encode(f"{m['content']}<|im_end|>\n")
            ids.extend(hdr_ids + body_ids)
            if m["role"] == "assistant":
                labels.extend([-100] * len(hdr_ids) + body_ids)
            else:
                labels.extend([-100] * (len(hdr_ids) + len(body_ids)))
        ids = ids[:self.seq_len]
        labels = labels[:self.seq_len]
        pad = self.tok.special.get("<|pad|>", 0)
        while len(ids) < self.seq_len:
            ids.append(pad)
            labels.append(-100)
        return torch.tensor(ids), torch.tensor(labels)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True, help="pretrain çıktısı last/ dizini")
    ap.add_argument("--data", required=True, help="SFT jsonl")
    ap.add_argument("--tokenizer", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--steps", type=int, default=800)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--accum", type=int, default=4)
    ap.add_argument("--seq", type=int, default=1024)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--warmup", type=int, default=50)
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args()

    tok = BPE.load(args.tokenizer)
    model = CoderModel.load(args.ckpt, device=args.device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, betas=(0.9, 0.95),
                            weight_decay=0.01)
    ds = SFTData(args.data, tok, args.seq)
    dl = DataLoader(ds, batch_size=args.batch, shuffle=True, drop_last=True)
    it = iter(dl)
    os.makedirs(args.out, exist_ok=True)

    model.train()
    for step in range(args.steps):
        for g in opt.param_groups:
            g["lr"] = cosine_lr(step, args.steps, args.lr, args.warmup)
        opt.zero_grad(set_to_none=True)
        tot = 0.0
        for _ in range(args.accum):
            try:
                x, y = next(it)
            except StopIteration:
                it = iter(dl)
                x, y = next(it)
            x, y = x.to(args.device), y.to(args.device)
            logits, _ = model(x)
            loss = torch.nn.functional.cross_entropy(
                logits[:, :-1].reshape(-1, logits.size(-1)),
                y[:, 1:].reshape(-1), ignore_index=-100) / args.accum
            loss.backward()
            tot += loss.item()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        if (step + 1) % 10 == 0:
            print({"step": step + 1, "loss": round(tot, 4)})
        if (step + 1) % 200 == 0 or step + 1 == args.steps:
            model.save(os.path.join(args.out, "last"))
    print("sft bitti.")


if __name__ == "__main__":
    main()
