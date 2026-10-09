"""coder-1 ajan döngüsü — model ↔ araçlar ↔ çalışma alanı.

Model <|tool|>{"name":..,"args":{..}} üretir → araç çalışır → sonuç
<|tool_result|> olarak modele döner → görev <|done|> ile biter.

v0: düz dosya sistemi çalışma alanı + subprocess. Docker yalıtımı ayrı modül.
"""
import json
import os
import re
import subprocess
import urllib.request

from .workspace import Workspace

TOOL_RE = re.compile(r"<\|tool\|>\s*(\{.*?\})\s*(?:<\|im_end\|>)?$", re.S)

MAX_STEPS = 20
TOOL_TIMEOUT = 60


def call_model(base_url, messages, max_tokens=1024, temperature=0.4):
    """serve/server.py'nin OpenAI-benzeri endpoint'ine istek."""
    body = json.dumps({
        "messages": messages, "temperature": temperature,
        "max_tokens": max_tokens, "stream": False,
    }).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/v1/chat/completions",
                                 data=body, headers={"Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=600)
    return json.loads(r.read())["choices"][0]["message"]["content"]


def run_tool(ws: Workspace, call: dict, events=None):
    """Tek araç çağrısını çalıştır → sonuç metni."""
    name, args = call.get("name"), call.get("args", {}) or {}
    try:
        if name == "list_files":
            out = ws.list_files(args.get("path", "."))
            return "\n".join(out) or "(boş)"
        if name == "read_file":
            return ws.read(args["path"])
        if name == "write_file":
            ws.write(args["path"], args["content"])
            return f"yazıldı: {args['path']}"
        if name == "edit_file":
            return ws.edit(args["path"], args["old"], args["new"])
        if name == "run_command":
            return ws.run(args["cmd"], timeout=args.get("timeout", TOOL_TIMEOUT))
        if name == "done" or call.get("name") == "<|done|>":
            return "__DONE__"
        return f"bilinmeyen araç: {name}"
    except Exception as e:
        return f"araç hatası: {e}"


def agent_task(base_url, task_text, workdir, on_event=None, max_steps=MAX_STEPS):
    """Bir görevi uçtan uca çalıştır. on_event(dict) UI'a canlı akış."""
    ws = Workspace(workdir)
    messages = [{"role": "user", "content": task_text}]
    trace = []

    def emit(t, **kw):
        ev = {"t": t, **kw}
        trace.append(ev)
        if on_event:
            on_event(ev)

    emit("start", task=task_text)
    for step in range(max_steps):
        reply = call_model(base_url, messages)
        emit("model", text=reply, step=step)
        m = TOOL_RE.search(reply.strip())
        if not m:
            # metin cevabı — görevin konuşma kısmı olabilir; son araç çağrısı yoksa bitir
            emit("answer", text=reply)
            if not trace or all(e["t"] != "tool" for e in trace):
                emit("done", ok=True, steps=step + 1)
                return {"ok": True, "answer": reply, "trace": trace}
            messages.append({"role": "assistant", "content": reply})
            continue
        try:
            call = json.loads(m.group(1))
        except json.JSONDecodeError:
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "tool", "content": "<|tool_result|>geçersiz JSON<|im_end|>"})
            continue
        emit("tool", call=call)
        result = run_tool(ws, call)
        emit("tool_result", name=call.get("name"), out=str(result)[:2000])
        if result == "__DONE__":
            emit("done", ok=True, steps=step + 1)
            return {"ok": True, "answer": reply, "trace": trace}
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "tool",
                         "content": f"<|tool_result|>{str(result)[:4000]}<|im_end|>"})
    emit("done", ok=False, reason="step_limit")
    return {"ok": False, "trace": trace}
