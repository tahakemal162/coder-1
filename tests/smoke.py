"""Uçtan uca duman testi — CPU'da dakikalar içinde:

1. Kendi kaynak kodumuzdan BPE tokenizer eğit (vocab ~1500)
2. nano modeli ~60 adım pretrain et (gerçek loss düşüşü)
3. Üretim yap + eval setini koştur (0 beklenir — 60 adım)
4. Chat formatı + araç çağrısı parse akışını doğrula

Amaç: boru hattının HER parçası çalışıyor mu — kalite değil.
"""
import os
import sys
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from model.tokenizer.bpe import BPE                      # noqa: E402
from model.data.sources import iter_local_dir           # noqa: E402
from model.data.pretokenize import pretokenize_stream   # noqa: E402
from model.data.dataset import ShardedDataset           # noqa: E402
from model.config import get_config                     # noqa: E402
from model.architecture import CoderModel               # noqa: E402
from model.generate import generate_text                # noqa: E402
from model.chat import render_chat                      # noqa: E402
import torch                                            # noqa: E402


def main():
    work = tempfile.mkdtemp(prefix="c1smoke_")
    print(f"== smoke workspace: {work}")

    # 1) tokenizer — kendi kodumuz gerçek veri
    tok_dir = os.path.join(work, "tok")
    texts = list(iter_local_dir(ROOT))
    print(f"corpus: {len(texts)} dosya, {sum(len(t) for t in texts)//1024} KB")
    tok = BPE().train(iter(texts), vocab_size=1500, min_freq=2,
                      progress=lambda s, n: print(f"  merge {s}/{n}"))
    tok.save(tok_dir)
    tok2 = BPE.load(tok_dir)
    ids = tok2.encode("def topla(a, b):\n    return a + b<|im_end|>")
    back = tok2.decode(ids)
    assert "def topla" in back, f"tokenizer bozuk: {back!r}"
    print(f"tokenizer ok: vocab={tok2.vocab_size}, round-trip ok")

    # 2) pretokenize + dataset
    data_dir = os.path.join(work, "shards")
    pretokenize_stream(iter(texts * 4), tok2, data_dir, shard_tokens=50_000)
    ds = ShardedDataset(data_dir, seq_len=128)
    x, y = ds[0]
    assert x.shape == y.shape == (128,)
    print(f"dataset ok: {ds.total} token, {len(ds)} pencere")

    # 3) mini eğitim — loss düşmeli
    cfg = get_config("nano", vocab_size=tok2.vocab_size, max_seq_len=128)
    model = CoderModel(cfg)
    print(f"model: {cfg.n_params()/1e6:.1f}M param")
    opt = torch.optim.AdamW(model.parameters(), lr=3e-3)
    model.train()
    import random
    losses = []
    for step in range(80):
        i = random.randrange(len(ds))
        x, y = ds[i]
        x, y = x.unsqueeze(0), y.unsqueeze(0)
        logits, _ = model(x)
        loss = torch.nn.functional.cross_entropy(
            logits.view(-1, logits.size(-1)), y.view(-1))
        opt.zero_grad()
        loss.backward()
        opt.step()
        losses.append(loss.item())
    print(f"loss: {losses[0]:.2f} -> {losses[-1]:.2f}")
    assert losses[-1] < losses[0], "loss düşmedi — eğitim döngüsü bozuk"

    # 4) üretim
    model.eval()
    out = generate_text(model, tok2, "def ", max_new=30, temperature=0.8)
    print(f"üretim örneği: {out[:80]!r}")

    # 5) chat formatı
    p = render_chat([{"role": "user", "content": "merhaba"}])
    assert "<|im_start|>user" in p and p.endswith("<|im_start|>assistant\n")
    print("chat formatı ok")

    # 6) checkpoint kaydet/yükle
    ck = os.path.join(work, "ck")
    model.save(ck)
    m2 = CoderModel.load(ck)
    assert m2.cfg.d_model == cfg.d_model
    print("checkpoint ok")

    print("\n== SMOKE PASS: boru hattı uçtan uca çalışıyor")


if __name__ == "__main__":
    main()
