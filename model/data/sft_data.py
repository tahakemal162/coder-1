"""SFT veri seti üretici — sahte veri YOK:

1) code_search_net fonksiyonları: docstring (gerçek talimat) → fonksiyon (gerçek kod)
   => [{'user': "Şunu yaz: <docstring>", 'assistant': "```py\n<func>\n```"}]
2) Ajan izleri: bizim araç formatında gerçek görevler -> agent loop çalıştırılıp
   başarılı trace'ler SFT'ye eklenir (bootstrapping; ajan kendi verisini üretir).

Çıktı: messages formatında JSONL.
"""
import argparse
import json
import random


def build_csn(out_path, per_lang=4000):
    """code_search_net: function + docstring çiftleri → SFT satırları."""
    from datasets import load_dataset
    n = 0
    with open(out_path, "w") as f:
        for lang in ("python", "javascript", "go", "java"):
            try:
                ds = load_dataset("code_search_net", lang, split="train",
                                  streaming=True)
            except Exception as e:
                print(f"{lang} atlandı: {e}")
                continue
            cnt = 0
            for row in ds:
                doc = (row.get("func_documentation_string") or "").strip()
                code = (row.get("func_code_string") or "").strip()
                if not doc or not code or len(doc) < 30 or len(code) < 40:
                    continue
                user = random.choice([
                    "Şu işi yapan bir fonksiyon yaz:\n\n",
                    "Aşağıdaki işlevi kodla:\n\n",
                    "Write a function that:\n\n",
                    "Implement:\n\n",
                ]) + doc
                asst = f"```{'python' if lang == 'python' else lang}\n{code}\n```"
                f.write(json.dumps({"messages": [
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": asst},
                ]}, ensure_ascii=False) + "\n")
                cnt += 1
                n += 1
                if cnt >= per_lang:
                    break
            print(f"{lang}: {cnt} satır")
    print(f"toplam {n} -> {out_path}")


def trace_to_sft(trace, out_f):
    """agent.loop trace'ini SFT satırına çevirir (başarılı görevler)."""
    msgs = []
    task = None
    for ev in trace:
        if ev["t"] == "start":
            task = ev["task"]
            msgs.append({"role": "user", "content": task})
        elif ev["t"] == "model":
            msgs.append({"role": "assistant", "content": ev["text"]})
        elif ev["t"] == "tool_result":
            msgs.append({"role": "tool",
                         "content": f"<|tool_result|>{ev['out']}<|im_end|>"})
    if len(msgs) > 1:
        out_f.write(json.dumps({"messages": msgs}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-lang", type=int, default=4000)
    args = ap.parse_args()
    build_csn(args.out, args.per_lang)
