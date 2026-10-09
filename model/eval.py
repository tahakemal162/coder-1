"""coder-1 değerlendirme — kendi HumanEval-mini setimiz.

Her görev: fonksiyon imzası + docstring → model gövdeyi yazar → gizli testler
sandbox'lı subprocess'te (timeout'lu) çalışır. pass@1 raporlanır.

Kullanım: python -m model.eval --ckpt runs/small/last --tokenizer runs/tok
"""
import argparse
import json
import multiprocessing as mp
import re

import torch

from .architecture import CoderModel
from .tokenizer.bpe import BPE
from .generate import make_sampler

# Kendi görev setimiz: imza + docstring + gizli testler (gerçek Python).
TASKS = [
    {"name": "add", "sig": "def add(a, b):\n    \"\"\"İki sayıyı topla.\"\"\"",
     "tests": ["assert add(2,3)==5", "assert add(-1,1)==0"]},
    {"name": "is_even", "sig": "def is_even(n):\n    \"\"\"n çiftse True döner.\"\"\"",
     "tests": ["assert is_even(4)", "assert not is_even(5)"]},
    {"name": "reverse_str", "sig": "def reverse_str(s):\n    \"\"\"Stringi ters çevir.\"\"\"",
     "tests": ["assert reverse_str('abc')=='cba'", "assert reverse_str('')==''"]},
    {"name": "fib", "sig": "def fib(n):\n    \"\"\"n. Fibonacci sayısını döner (fib(0)=0, fib(1)=1).\"\"\"",
     "tests": ["assert fib(0)==0", "assert fib(1)==1", "assert fib(10)==55"]},
    {"name": "max_list", "sig": "def max_list(xs):\n    \"\"\"Listedeki en büyük sayıyı döner; boşsa None.\"\"\"",
     "tests": ["assert max_list([1,9,3])==9", "assert max_list([]) is None"]},
    {"name": "count_vowels", "sig": "def count_vowels(s):\n    \"\"\"Stringdeki sesli harf sayısını döner (aeiou).\"\"\"",
     "tests": ["assert count_vowels('hello')==2", "assert count_vowels('xyz')==0"]},
    {"name": "is_palindrome", "sig": "def is_palindrome(s):\n    \"\"\"s tersine eşitse True.\"\"\"",
     "tests": ["assert is_palindrome('kayak')", "assert not is_palindrome('ev')"]},
    {"name": "sum_squares", "sig": "def sum_squares(xs):\n    \"\"\"Karelerin toplamını döner.\"\"\"",
     "tests": ["assert sum_squares([1,2,3])==14", "assert sum_squares([])==0"]},
    {"name": "fizzbuzz", "sig": "def fizzbuzz(n):\n    \"\"\"1..n için liste döner: 3ün katı 'fizz', 5in katı 'buzz', ikisi 'fizzbuzz', değilse sayı.\"\"\"",
     "tests": ["assert fizzbuzz(5)==[1,2,'fizz',4,'buzz']"]},
    {"name": "merge_dicts", "sig": "def merge_dicts(a, b):\n    \"\"\"İki dicti birleştirir; çakışmada b kazanır.\"\"\"",
     "tests": ["assert merge_dicts({'x':1},{'y':2})=={'x':1,'y':2}",
               "assert merge_dicts({'x':1},{'x':9})=={'x':9}"]},
    {"name": "clamp", "sig": "def clamp(x, lo, hi):\n    \"\"\"x'i [lo,hi] aralığına sıkıştır.\"\"\"",
     "tests": ["assert clamp(5,0,10)==5", "assert clamp(-3,0,10)==0", "assert clamp(99,0,10)==10"]},
    {"name": "unique", "sig": "def unique(xs):\n    \"\"\"Tekrarları atıp sırayı koruyan liste döner.\"\"\"",
     "tests": ["assert unique([1,1,2,1,3])==[1,2,3]", "assert unique([])==[]"]},
]


def _run_task(code, tests, q):
    try:
        g = {}
        exec(code, g)
        for t in tests:
            exec(t, g)
        q.put(True)
    except Exception as e:
        q.put(str(e))


def extract_code(text, fname):
    """Model çıktısından fonksiyon gövdesini derlenebilir kod olarak ayıkla."""
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.S)
    if m:
        return m.group(1)
    # fonksiyon başlangıcından itibaren al
    idx = text.find(f"def {fname}")
    if idx == -1:
        return text
    return text[idx:]


def eval_model(model, tok, max_new=160, verbose=True):
    device = next(model.parameters()).device
    model.eval()
    passed, results = 0, []
    for t in TASKS:
        fname = re.search(r"def (\w+)", t["sig"]).group(1)
        prompt = (f"<|im_start|>user\nAşağıdaki fonksiyonu tamamla. Sadece kod yaz.\n\n"
                  f"{t['sig']}\n<|im_end|>\n<|im_start|>assistant\n")
        ids = torch.tensor([tok.encode(prompt)], device=device)
        sampler = make_sampler(temperature=0.2, top_p=0.9)
        out = model.generate_ids(ids, max_new, sampler,
                                 eos_id=tok.special.get("<|im_end|>"))
        gen = tok.decode(out[0, ids.size(1):].tolist())
        # Model fonksiyonu komple yazdıysa onu al; sadece gövdeyse imzanın altına koy
        if f"def {fname}" in gen:
            code = extract_code(gen, fname)
        else:
            code = t["sig"] + "\n" + gen
        q = mp.Queue()
        p = mp.Process(target=_run_task, args=(code, t["tests"], q))
        p.start()
        p.join(10)
        if p.is_alive():
            p.terminate()
            ok, err = False, "timeout"
        else:
            r = q.get() if not q.empty() else "no result"
            ok, err = (r is True), (None if r is True else r)
        passed += ok
        results.append({"name": t["name"], "pass": bool(ok), "err": err})
        if verbose:
            print(f"{'PASS' if ok else 'FAIL'} {t['name']}" + ("" if ok else f"  ({err})"))
    score = passed / len(TASKS)
    print(f"\npass@1: {passed}/{len(TASKS)} = {score:.0%}")
    return {"score": score, "results": results}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--tokenizer", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args()
    tok = BPE.load(args.tokenizer)
    model = CoderModel.load(args.ckpt, device=args.device)
    res = eval_model(model, tok)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(res, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
