"""coder-1 mimarisi — sıfırdan yazılmış decoder-only transformer.

LLaMA-ailesi tasarım: RMSNorm, RoPE, SwiGLU, Grouped-Query Attention.
Harici model ağırlığı kullanılmaz; tüm ağırlıklar bizim eğitimimizden gelir.
"""
import math
import torch
import torch.nn as nn
import torch.nn.functional as F

from .config import ModelConfig


class RMSNorm(nn.Module):
    def __init__(self, dim, eps):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        return x * torch.rsqrt(x.float().pow(2).mean(-1, keepdim=True) + self.eps).to(x.dtype) * self.weight


class RoPE(nn.Module):
    def __init__(self, head_dim, theta, max_seq_len):
        super().__init__()
        self.head_dim, self.theta = head_dim, theta
        self._build(max_seq_len)

    def _build(self, T):
        inv = 1.0 / (self.theta ** (torch.arange(0, self.head_dim, 2).float() / self.head_dim))
        t = torch.arange(T).float()
        freqs = torch.outer(t, inv)                      # (T, d/2)
        self.register_buffer("cos", freqs.cos(), persistent=False)
        self.register_buffer("sin", freqs.sin(), persistent=False)

    def forward(self, x, pos):  # x: (B, H, T, hd)
        need = pos + x.size(2)
        if need > self.cos.size(0):
            self._build(need + 512)                      # eğitim penceresini aşan üretim: uzat
        cos = self.cos[pos:pos + x.size(2)].to(x.dtype)
        sin = self.sin[pos:pos + x.size(2)].to(x.dtype)
        x1, x2 = x[..., ::2], x[..., 1::2]
        o = torch.stack([-x2, x1], dim=-1).flatten(-2)   # rotate_half
        return x * torch.repeat_interleave(cos, 2, -1)[None, None] + o * torch.repeat_interleave(sin, 2, -1)[None, None]


class Attention(nn.Module):
    def __init__(self, c: ModelConfig):
        super().__init__()
        self.nh, self.nkv, self.hd = c.n_heads, c.n_kv_heads, c.d_model // c.n_heads
        self.q = nn.Linear(c.d_model, self.nh * self.hd, bias=False)
        self.k = nn.Linear(c.d_model, self.nkv * self.hd, bias=False)
        self.v = nn.Linear(c.d_model, self.nkv * self.hd, bias=False)
        self.o = nn.Linear(self.nh * self.hd, c.d_model, bias=False)
        self.rope = RoPE(self.hd, c.rope_theta, c.max_seq_len)

    def forward(self, x, pos=0, past=None):
        B, T, _ = x.shape
        q = self.q(x).view(B, T, self.nh, self.hd).transpose(1, 2)
        k = self.k(x).view(B, T, self.nkv, self.hd).transpose(1, 2)
        v = self.v(x).view(B, T, self.nkv, self.hd).transpose(1, 2)
        q = self.rope(q, pos)
        k = self.rope(k, pos)
        if past is not None:
            pk, pv = past
            k = torch.cat([pk, k], 2)
            v = torch.cat([pv, v], 2)
        kk = k.repeat_interleave(self.nh // self.nkv, 1)
        vv = v.repeat_interleave(self.nh // self.nkv, 1)
        y = F.scaled_dot_product_attention(q, kk, vv, is_causal=past is None)
        y = y.transpose(1, 2).reshape(B, T, -1)
        return self.o(y), (k, v)


class MLP(nn.Module):
    def __init__(self, c: ModelConfig):
        super().__init__()
        self.gate = nn.Linear(c.d_model, c.d_ff, bias=False)
        self.up = nn.Linear(c.d_model, c.d_ff, bias=False)
        self.down = nn.Linear(c.d_ff, c.d_model, bias=False)

    def forward(self, x):
        return self.down(F.silu(self.gate(x)) * self.up(x))


class Block(nn.Module):
    def __init__(self, c: ModelConfig):
        super().__init__()
        self.n1 = RMSNorm(c.d_model, c.norm_eps)
        self.attn = Attention(c)
        self.n2 = RMSNorm(c.d_model, c.norm_eps)
        self.mlp = MLP(c)

    def forward(self, x, pos=0, past=None):
        a, past = self.attn(self.n1(x), pos, past)
        x = x + a
        x = x + self.mlp(self.n2(x))
        return x, past


class CoderModel(nn.Module):
    def __init__(self, c: ModelConfig):
        super().__init__()
        self.cfg = c
        self.embed = nn.Embedding(c.vocab_size, c.d_model)
        self.blocks = nn.ModuleList([Block(c) for _ in range(c.n_layers)])
        self.norm = RMSNorm(c.d_model, c.norm_eps)
        self.head = nn.Linear(c.d_model, c.vocab_size, bias=False)
        if c.tie_embeddings:
            self.head.weight = self.embed.weight
        self.drop = nn.Dropout(c.dropout)
        self.apply(self._init)
        # ölçekli artık init (GPT-2 tarzı): çıkış projeksiyonları / sqrt(2L)
        for b in self.blocks:
            nn.init.normal_(b.attn.o.weight, std=0.02 / math.sqrt(2 * c.n_layers))
            nn.init.normal_(b.mlp.down.weight, std=0.02 / math.sqrt(2 * c.n_layers))

    @staticmethod
    def _init(m):
        if isinstance(m, (nn.Linear, nn.Embedding)):
            nn.init.normal_(m.weight, std=0.02)

    def forward(self, ids, pos=0, pasts=None):
        x = self.drop(self.embed(ids))
        new_pasts = []
        for i, blk in enumerate(self.blocks):
            x, p = blk(x, pos, None if pasts is None else pasts[i])
            new_pasts.append(p)
        x = self.norm(x)
        return self.head(x), new_pasts

    @torch.no_grad()
    def generate_ids(self, ids, max_new, sampler, eos_id=None, on_token=None):
        """ids: (1,T) prefix. sampler(next_logits)->next_id. KV-cache'li döngü."""
        pasts, cur = None, ids
        for _ in range(max_new):
            logits, pasts = self(cur, pos=ids.size(1) - cur.size(1), pasts=pasts)
            nxt = int(sampler(logits[0, -1]))
            if on_token:
                on_token(nxt)
            if eos_id is not None and nxt == eos_id:
                break
            ids = torch.cat([ids, torch.tensor([[nxt]], device=ids.device)], 1)
            cur = ids[:, -1:]
        return ids

    def save(self, dir_path):
        import os
        os.makedirs(dir_path, exist_ok=True)
        self.cfg.save(os.path.join(dir_path, "config.json"))
        torch.save(self.state_dict(), os.path.join(dir_path, "model.pt"))

    @staticmethod
    def load(dir_path, device="cpu"):
        import os
        cfg = ModelConfig.load(os.path.join(dir_path, "config.json"))
        m = CoderModel(cfg)
        m.load_state_dict(torch.load(os.path.join(dir_path, "model.pt"), map_location=device))
        return m.to(device)
