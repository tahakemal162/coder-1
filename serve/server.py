"""coder-1 servis katmanı — kendi FastAPI sunucumuz.

Endpoint'ler (OpenAI-benzeri):
  GET  /healthz
  POST /v1/chat/completions   {messages:[{role,content}], stream?, temperature?, max_tokens?}
  POST /v1/completions        {prompt, ...}

Model ve tokenizer bizim: CoderModel + BPE. SSE ile token akışı.
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from model.architecture import CoderModel
from model.tokenizer.bpe import BPE
from model.chat import render_chat, SYSTEM_CODER
from model.generate import make_sampler

app = FastAPI(title="coder-1")
MODEL = None
TOK = None
DEVICE = "cpu"


class ChatReq(BaseModel):
    messages: list
    stream: bool = False
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 512


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": "coder-1", "params": MODEL.cfg.n_params() if MODEL else 0}


def _context_len():
    return MODEL.cfg.max_seq_len if MODEL else 2048


def _fit_ids(messages):
    """Prompt'u modelin bağlam penceresine sığdır.

    1) En eski (system dışı) mesajları düşürerek yeniden render et.
    2) Hâlâ sığmıyorsa token düzeyinde kırp: baş %25 (system kalır) +
       son %75 (üretim promptu `<|im_start|>assistant\\n` sonda kalır).
    Dönen: (ids, ctx_meta)
    """
    budget = _context_len()
    sys_msg = {"role": "system", "content": SYSTEM_CODER}
    raw = [m if isinstance(m, dict) else m.dict() for m in messages]
    dropped = 0

    msgs = [sys_msg] + raw
    ids = TOK.encode(render_chat(msgs))
    while len(ids) > budget and raw:
        raw.pop(0)
        dropped += 1
        msgs = [sys_msg] + raw
        ids = TOK.encode(render_chat(msgs))

    truncated = False
    if len(ids) > budget:
        # tek mesaj bile sığmıyor → baş+sonu tut (sonda <|im_start|>assistant var)
        head = max(1, budget // 4)
        tail = budget - head
        ids = ids[:head] + ids[-tail:]
        truncated = True

    return torch.tensor([ids], device=DEVICE), {
        "prompt_tokens": len(ids), "ctx": budget,
        "dropped_messages": dropped, "truncated": truncated}


def _gen_ids(messages, temperature, top_p, max_tokens):
    ids, meta = _fit_ids(messages)
    room = _context_len() - ids.size(1)
    if room < 1:
        return [], meta            # prompt pencereyi doldurdu
    sampler = make_sampler(temperature=temperature, top_p=top_p,
                           seen=set(ids[0].tolist()))
    acc = []

    def cb(tid):
        acc.append(tid)

    MODEL.generate_ids(ids, min(max_tokens, room), sampler,
                       eos_id=TOK.special.get("<|im_end|>"), on_token=cb)
    return acc, meta


@app.post("/v1/chat/completions")
def chat(req: ChatReq):
    if req.stream:
        def stream():
            ids, meta = _fit_ids(req.messages)
            room = _context_len() - ids.size(1)
            sampler = make_sampler(temperature=req.temperature, top_p=req.top_p,
                                   seen=set(ids[0].tolist()))
            pieces = []

            def cb(tid):
                pieces.append(TOK.decode([tid]))

            MODEL.generate_ids(ids, min(req.max_tokens, max(room, 0)), sampler,
                               eos_id=TOK.special.get("<|im_end|>"), on_token=cb)
            yield f"data: {json.dumps({'context':meta}, ensure_ascii=False)}\n\n"
            for p in pieces:
                yield f"data: {json.dumps({'choices':[{'delta':{'content':p}}]}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    acc, meta = _gen_ids(req.messages, req.temperature, req.top_p, req.max_tokens)
    text = TOK.decode(acc)
    return JSONResponse({
        "id": "coder1",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "context": meta,
    })


class CompReq(BaseModel):
    prompt: str
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 256


@app.post("/v1/completions")
def completions(req: CompReq):
    enc = TOK.encode(req.prompt)
    budget = _context_len()
    truncated = len(enc) > budget
    if truncated:
        enc = enc[-budget:]        # completions'ta sondan tut (son metin önemli)
    ids = torch.tensor([enc], device=DEVICE)
    room = max(0, budget - ids.size(1))
    sampler = make_sampler(temperature=req.temperature, top_p=req.top_p,
                           seen=set(ids[0].tolist()))
    acc = []
    MODEL.generate_ids(ids, min(req.max_tokens, room), sampler,
                       eos_id=TOK.special.get("<|im_end|>"),
                       on_token=lambda t: acc.append(t))
    return {"choices": [{"text": TOK.decode(acc)}],
            "context": {"prompt_tokens": int(ids.size(1)), "ctx": budget,
                        "truncated": truncated}}


def main():
    global MODEL, TOK, DEVICE
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--tokenizer", required=True)
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args()
    DEVICE = args.device
    TOK = BPE.load(args.tokenizer)
    MODEL = CoderModel.load(args.ckpt, device=DEVICE)
    MODEL.eval()
    print(f"coder-1 hazır: {MODEL.cfg.n_params()/1e6:.1f}M param @ :{args.port}")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
