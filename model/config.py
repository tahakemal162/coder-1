"""coder-1 model boyutları. Tümü bizim mimarimiz — PyTorch decoder-only transformer."""
from dataclasses import dataclass, asdict
import json


@dataclass
class ModelConfig:
    d_model: int          # gizli boyut
    n_layers: int         # transformer bloğu sayısı
    n_heads: int          # attention başı
    n_kv_heads: int       # GQA: kv başı (== n_heads ise MQA yok)
    d_ff: int             # SwiGLU ara boyutu
    vocab_size: int       # tokenizer vocab
    max_seq_len: int = 2048
    rope_theta: float = 10000.0
    norm_eps: float = 1e-5
    tie_embeddings: bool = True   # küçük modellerde parametre tasarrufu
    dropout: float = 0.0

    def n_params(self) -> int:
        emb = self.vocab_size * self.d_model
        head = 0 if self.tie_embeddings else self.vocab_size * self.d_model
        attn = self.d_model * self.d_model + 2 * self.n_kv_heads * (self.d_model // self.n_heads) * self.d_model + self.d_model * self.d_model
        mlp = 3 * self.d_model * self.d_ff
        blk = attn + mlp + 2 * self.d_model
        return emb + head + self.n_layers * blk + self.d_model

    def save(self, path):
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @staticmethod
    def load(path):
        with open(path) as f:
            return ModelConfig(**json.load(f))


# Boyutlar — aynı kod küçükten büyüğe ölçeklenir.
# nano : CPU duman testi (~25M)
# mini : CPU'da anlamlı deneme (~60M)
# small: Kaggle T4x2 hedefi (~124M, GPT-2-small sınıfı)
# base : ~350M — iyi GPU bulunursa
# pro  : ~1B — uzun vade
PRESETS = {
    "nano":  dict(d_model=384,  n_layers=6,  n_heads=6,  n_kv_heads=2,  d_ff=1024),
    "mini":  dict(d_model=512,  n_layers=8,  n_heads=8,  n_kv_heads=2,  d_ff=1408),
    "small": dict(d_model=768,  n_layers=12, n_heads=12, n_kv_heads=4,  d_ff=2048),
    "base":  dict(d_model=1024, n_layers=24, n_heads=16, n_kv_heads=4,  d_ff=2816),
    "pro":   dict(d_model=1536, n_layers=28, n_heads=16, n_kv_heads=4,  d_ff=4096),
}


def get_config(preset: str, vocab_size: int, **kw) -> ModelConfig:
    cfg = ModelConfig(**PRESETS[preset], vocab_size=vocab_size, **kw)
    return cfg
