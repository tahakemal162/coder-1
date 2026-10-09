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


def _gen_ids(messages, temperature, top_p, max_tokens):
    msgs = [{"role": "system", "content": SYSTEM_CODER}] + [
        m if isinstance(m, dict) else m.dict() for m in messages]
    prompt = render_chat(msgs)
    ids = torch.tensor([TOK.encode(prompt)], device=DEVICE)
    sampler = make_sampler(temperature=temperature, top_p=top_p,
                           seen=set(ids[0].tolist()))
    acc = []

    def cb(tid):
        acc.append(tid)

    MODEL.generate_ids(ids, max_tokens, sampler,
                       eos_id=TOK.special.get("<|im_end|>"), on_token=cb)
    return acc


@app.post("/v1/chat/completions")
def chat(req: ChatReq):
    if req.stream:
        def stream():
            msgs = [{"role": "system", "content": SYSTEM_CODER}] + [
                m if isinstance(m, dict) else m.dict() for m in req.messages]
            prompt = render_chat(msgs)
            ids = torch.tensor([TOK.encode(prompt)], device=DEVICE)
            sampler = make_sampler(temperature=req.temperature, top_p=req.top_p,
                                   seen=set(ids[0].tolist()))
            pieces = []

            def cb(tid):
                pieces.append(TOK.decode([tid]))

            MODEL.generate_ids(ids, req.max_tokens, sampler,
                               eos_id=TOK.special.get("<|im_end|>"), on_token=cb)
            for p in pieces:
                yield f"data: {json.dumps({'choices':[{'delta':{'content':p}}]}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    acc = _gen_ids(req.messages, req.temperature, req.top_p, req.max_tokens)
    text = TOK.decode(acc)
    return JSONResponse({
        "id": "coder1",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
    })


class CompReq(BaseModel):
    prompt: str
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 256


@app.post("/v1/completions")
def completions(req: CompReq):
    ids = torch.tensor([TOK.encode(req.prompt)], device=DEVICE)
    sampler = make_sampler(temperature=req.temperature, top_p=req.top_p,
                           seen=set(ids[0].tolist()))
    acc = []
    MODEL.generate_ids(ids, req.max_tokens, sampler,
                       eos_id=TOK.special.get("<|im_end|>"),
                       on_token=lambda t: acc.append(t))
    return {"choices": [{"text": TOK.decode(acc)}]}


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
