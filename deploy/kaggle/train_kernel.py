# Kaggle kernel — coder-1 eğitimi (T4x2 veya P100, ~12s/oturum, ~30s/hafta).
#
# Kaggle'da "New Notebook" → kod hücrelerine sırayla:
#   !git clone <veya dataset yükle> coder-1
#   %cd coder-1
#   !pip install -r requirements.txt
# Bu dosya tek hücre olarak da çalışır: veri → tokenizer → pretrain → kaydet.
#
# Oturum sınırı: --ckpt_every 500 + --resume ile parça parça eğitim.
# Çıktılar /kaggle/working/ altına → oturum bitince "Save Version" ile saklanır.
#
# Aşağıdakiler Kaggle ortam değişkenleriyle doldurulur (bizim makinede değil):
#   C1_DATA_GB   = kaç MB ham metin toplansın (varsayılan 400)
#   C1_PRESET    = nano|mini|small  (varsayılan small — T4x2 hedefi)
#   C1_STEPS     = oturum başına adım (varsayılan 9000 — ~11s @ T4x2)
#   C1_RESUME    = "1" ise /kaggle/working/runs/<preset> içinden devam

import os
import subprocess
import sys

ROOT = "/kaggle/working/coder-1"
DATA_GB = int(os.environ.get("C1_DATA_GB", "400"))
PRESET = os.environ.get("C1_PRESET", "small")
STEPS = int(os.environ.get("C1_STEPS", "9000"))
RESUME = os.environ.get("C1_RESUME", "1") == "1"

sys.path.insert(0, ROOT)


def sh(cmd):
    print("+", cmd, flush=True)
    subprocess.run(cmd, shell=True, check=True, cwd=ROOT)


def main():
    os.chdir(ROOT)

    # 1) Veri: GitHub tarball küratörlü liste + code_search_net → raw.txt
    raw = "/kaggle/working/raw_code.txt"
    if not os.path.exists(raw):
        from model.data.sources import iter_repo_list
        from model.data.clean import looks_like_code
        total = 0
        with open(raw, "w") as f:
            for text in iter_repo_list("model/data/repos.txt", per_repo_files=500):
                if looks_like_code(text):
                    f.write(text + "\n\n")
                    total += len(text)
                    if total % (50 << 20) < len(text):
                        print(f"  {total>>20} MB")
                    if total >= DATA_GB << 20:
                        break
    print("veri hazır")

    # 2) Tokenizer: gerçek corpus üzerinde 16k vocab (kendi BPE'miz)
    tok_dir = "/kaggle/working/tok16k"
    if not os.path.exists(tok_dir + "/vocab.json"):
        from model.tokenizer.bpe import BPE

        def texts():
            with open(raw) as f:
                chunk = f.read(4 << 20)
                while chunk:
                    yield chunk
                    chunk = f.read(4 << 20)
        BPE().train(texts(), vocab_size=16000, min_freq=3,
                    progress=lambda s, n: print(f"merge {s}/{n}", flush=True)
                    ).save(tok_dir)

    # 3) Ön-tokenleme
    shards = "/kaggle/working/shards"
    if not os.path.exists(shards + "/index.json"):
        from model.tokenizer.bpe import BPE as _B
        from model.data.pretokenize import pretokenize_stream
        tok = _B.load(tok_dir)

        def stream():
            with open(raw) as f:
                chunk = f.read(8 << 20)
                while chunk:
                    yield chunk
                    chunk = f.read(8 << 20)
        pretokenize_stream(stream(), tok, shards, shard_tokens=8_000_000)

    # 4) Eğitim — resume'lu
    resume = "--resume" if RESUME else ""
    sh(f"python -m model.pretrain --preset {PRESET} --data {shards} "
       f"--tokenizer {tok_dir} --out /kaggle/working/runs/{PRESET} "
       f"--steps {STEPS} --batch 8 --accum 8 --seq 1024 --lr 3e-4 "
       f"--ckpt_every 500 --sample_every 2000 --device cuda {resume}")

    # 5) Değerlendirme
    sh(f"python -m model.eval --ckpt /kaggle/working/runs/{PRESET}/last "
       f"--tokenizer {tok_dir} --out /kaggle/working/eval_{PRESET}.json --device cuda")


if __name__ == "__main__":
    main()
