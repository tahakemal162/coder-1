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


def _csn_rows(lang):
    """code_search_net satırları akıt: (docstring, code) çiftleri.
    HF `code_search_net` datasets>=3 ile yüklenemez (script dataset); aynı
    kayıtları taşıyan Nan-Do/code-search-net-{lang} aynasına düş."""
    from datasets import load_dataset
    try:
        ds = load_dataset("code_search_net", lang, split="train",
                          streaming=True)
        for row in ds:
            yield (row.get("func_documentation_string") or "").strip(), \
                  (row.get("func_code_string") or "").strip()
        return
    except Exception:
        pass
    ds = load_dataset(f"Nan-Do/code-search-net-{lang}", split="train",
                      streaming=True)
    for row in ds:
        yield (row.get("docstring") or "").strip(), \
              (row.get("code") or "").strip()


def build_csn(out_path, per_lang=4000):
    """code_search_net: function + docstring çiftleri → SFT satırları."""
    n = 0
    with open(out_path, "w") as f:
        for lang in ("python", "javascript", "go", "java"):
            cnt = 0
            try:
                for doc, code in _csn_rows(lang):
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
            except Exception as e:
                print(f"{lang} yarıda kesildi: {e}")
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
