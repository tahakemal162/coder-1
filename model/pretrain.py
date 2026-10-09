"""Pre-training döngüsü — bizim döngümüz: AMP, cosine LR+warmup, grad-accum,
checkpoint kaydet/devam et. Kaggle 12s oturum sınırına göre tasarlandı:
her N adımda checkpoint, --resume ile kaldığı yerden devam.

Kullanım:
  python -m model.pretrain --preset small --data data/shards --out runs/small \
      --steps 6000 --batch 8 --accum 8 --seq 1024 --device cuda
"""
import argparse
import json
import math
import os
import time

import torch
from torch.utils.data import DataLoader

from .architecture import CoderModel
from .config import get_config
from .data.dataset import ShardedDataset
from .generate import generate_text
from .tokenizer.bpe import BPE


def cosine_lr(step, total, base, warmup, floor=0.1):
    if step < warmup:
        return base * (step + 1) / warmup
    p = (step - warmup) / max(1, total - warmup)
    return base * (floor + (1 - floor) * 0.5 * (1 + math.cos(math.pi * p)))


def save_ckpt(model, opt, step, out_dir, extra=None):
    os.makedirs(out_dir, exist_ok=True)
    model.save(os.path.join(out_dir, "last"))
    state = {"step": step, "opt": opt.state_dict()}
    if extra:
        state.update(extra)
    torch.save(state, os.path.join(out_dir, "train_state.pt"))


def try_resume(out_dir, device):
    """(model, opt_state, step) veya (None, None, 0) döner."""
    p = os.path.join(out_dir, "train_state.pt")
    if not os.path.exists(p):
        return None, None, 0
    st = torch.load(p, map_location="cpu")
    model = CoderModel.load(os.path.join(out_dir, "last"), device=device)
    print(f"[resume] step {st['step']}'den devam")
    return model, st["opt"], st["step"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="nano")
    ap.add_argument("--data", required=True)
    ap.add_argument("--tokenizer", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--steps", type=int, default=2000)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--accum", type=int, default=4)
    ap.add_argument("--seq", type=int, default=512)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--warmup", type=int, default=200)
    ap.add_argument("--ckpt_every", type=int, default=500)
    ap.add_argument("--sample_every", type=int, default=1000)
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    tok = BPE.load(args.tokenizer)
    cfg = get_config(args.preset, vocab_size=tok.vocab_size, max_seq_len=args.seq)
    model = CoderModel(cfg).to(args.device)
    print(f"model: {args.preset} ~{cfg.n_params()/1e6:.1f}M param, vocab {cfg.vocab_size}")

    start = 0
    r_opt = None
    if args.resume:
        try:
            r_model, r_opt, start = try_resume(args.out, args.device)
            if r_model is not None:
                model = r_model
        except Exception as e:
            print(f"[resume] atlandı: {e}")
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, betas=(0.9, 0.95),
                            weight_decay=0.1, fused=(args.device == "cuda"))
    if args.resume and r_opt is not None:
        try:
            opt.load_state_dict(r_opt)
        except Exception as e:
            print(f"[resume] optimizer state atlandı: {e}")

    ds = ShardedDataset(args.data, args.seq)
    dl = DataLoader(ds, batch_size=args.batch, shuffle=True, num_workers=2,
                    drop_last=True, persistent_workers=True)
    it = iter(dl)

    use_amp = args.device == "cuda"
    amp_dtype = torch.bfloat16 if use_amp and torch.cuda.is_bf16_supported() else torch.float16
    scaler = torch.amp.GradScaler(enabled=use_amp and amp_dtype == torch.float16)

    model.train()
    t0, tok_seen = time.time(), 0
    log_path = os.path.join(args.out, "train_log.jsonl")
    os.makedirs(args.out, exist_ok=True)

    for step in range(start, args.steps):
        lr = cosine_lr(step, args.steps, args.lr, args.warmup)
        for g in opt.param_groups:
            g["lr"] = lr
        opt.zero_grad(set_to_none=True)
        tot_loss = 0.0
        for _ in range(args.accum):
            try:
                x, y = next(it)
            except StopIteration:
                it = iter(dl)
                x, y = next(it)
            x, y = x.to(args.device), y.to(args.device)
            with torch.amp.autocast(device_type="cuda" if use_amp else "cpu",
                                    dtype=amp_dtype, enabled=use_amp):
                logits, _ = model(x)
                loss = torch.nn.functional.cross_entropy(
                    logits.view(-1, logits.size(-1)), y.view(-1),
                    ignore_index=tok.special.get("<|pad|>", -100)) / args.accum
            if use_amp and amp_dtype == torch.float16:
                scaler.scale(loss).backward()
            else:
                loss.backward()
            tot_loss += loss.item()
        if use_amp and amp_dtype == torch.float16:
            scaler.unscale_(opt)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            scaler.step(opt)
            scaler.update()
        else:
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()

        tok_seen += args.batch * args.accum * args.seq
        if (step + 1) % 10 == 0:
            el = time.time() - t0
            rec = {"step": step + 1, "loss": round(tot_loss, 4), "lr": lr,
                   "tok_s": int(tok_seen / el), "elapsed_s": int(el)}
            print(rec)
            with open(log_path, "a") as f:
                f.write(json.dumps(rec) + "\n")

        if (step + 1) % args.ckpt_every == 0 or step + 1 == args.steps:
            save_ckpt(model, opt, step + 1, args.out)
            print(f"[ckpt] step {step+1} kaydedildi")

        if (step + 1) % args.sample_every == 0:
            model.eval()
            sample = generate_text(model, tok, "def fibonacci(n):", max_new=60,
                                   temperature=0.7, top_p=0.9)
            print(f"[sample]\n{sample}\n---")
            model.train()

    save_ckpt(model, opt, args.steps, args.out)
    print("bitti.")


if __name__ == "__main__":
    main()
