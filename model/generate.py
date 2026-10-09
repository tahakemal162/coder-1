"""Örnekleme — greedy / top-k / top-p / sıcaklık / tekrar cezası."""
import torch


def make_sampler(temperature=0.8, top_p=0.95, top_k=50, rep_penalty=1.1, seen=None):
    """next_logits (vocab,) -> token id döndüren fonksiyon üretir."""
    seen = seen if seen is not None else set()

    def sample(logits):
        logits = logits.float().clone()
        if rep_penalty != 1.0 and seen:
            idx = torch.tensor(list(seen), device=logits.device)
            v = logits[idx]
            logits[idx] = torch.where(v > 0, v / rep_penalty, v * rep_penalty)
        if temperature <= 0:
            nxt = int(logits.argmax())
            seen.add(nxt)
            return nxt
        logits = logits / temperature
        if top_k and top_k > 0:
            kth = logits.topk(min(top_k, logits.numel())).values[-1]
            logits = torch.where(logits < kth, torch.full_like(logits, -1e30), logits)
        probs = torch.softmax(logits, -1)
        if top_p and top_p < 1.0:
            sp, si = torch.sort(probs, descending=True)
            cum = torch.cumsum(sp, -1)
            mask = cum - sp > top_p
            sp[mask] = 0
            probs = torch.zeros_like(probs).scatter(-1, si, sp)
            probs = probs / probs.sum()
        nxt = int(torch.multinomial(probs, 1))
        seen.add(nxt)
        return nxt

    return sample


def generate_text(model, tok, prompt, max_new=200, on_text=None, eos="<|im_end|>", **kw):
    """Pratik sarmalayıcı: metin → metin. on_text callback'i akış verir."""
    device = next(model.parameters()).device
    ids = torch.tensor([tok.encode(prompt)], device=device)
    eos_id = tok.special.get(eos)
    sampler = make_sampler(seen=set(ids[0].tolist()), **kw)
    out_ids = []

    def _cb(tid):
        out_ids.append(tid)
        if on_text:
            on_text(tok.decode(out_ids))

    model.generate_ids(ids, max_new, sampler, eos_id=eos_id, on_token=_cb)
    return tok.decode(out_ids)
