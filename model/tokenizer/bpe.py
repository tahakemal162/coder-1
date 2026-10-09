"""coder-1 tokenizer — sıfırdan byte-level BPE.

GPT-2 yaklaşımı: baytlar yazdırılabilir unicode'a eşlenir, regex ile ön-bölme,
en sık bayt-çifti birleştirmeleri öğrenilir. Hiçbir hazır tokenizer/model yok;
vocab ve merges bizim eğittiğimiz metinden çıkar.
"""
import json
import re
from collections import Counter, defaultdict

# GPT-2 bayt→unicode eşlemesi: kontrol/boşluk baytları görünür karakterlere kayar
def _byte_map():
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, [chr(c) for c in cs]))

B2U = _byte_map()
U2B = {v: k for k, v in B2U.items()}

# Kod-dostu ön-bölme: kelimeler, sayılar, girinti, operatörler, boşluk satırları
PRE_RE = re.compile(
    r"\n[ \t]+|\r?\n| ?\d+| ?[A-Za-z_çğıöşüÇĞİÖŞÜ]+| ?[^\sA-Za-z0-9_çğıöşüÇĞİÖŞÜ]+| +|[^\s]",
    re.DOTALL)

SPECIALS = ["<|pad|>", "<|im_start|>", "<|im_end|>", "<|tool|>", "<|tool_result|>",
            "<|file|>", "<|fim_prefix|>", "<|fim_suffix|>", "<|fim_middle|>",
            "<|repo|>", "<|done|>"]
SPECIAL_RE = re.compile("(" + "|".join(re.escape(s) for s in SPECIALS) + ")")


def _pairs(word):
    return zip(word, word[1:])


class BPE:
    """Eğitilmiş BPE tokenizer. encode(text)->list[int], decode(list[int])->str."""

    def __init__(self):
        self.merges = {}          # (a,b) -> merged
        self.rank = {}            # (a,b) -> merge sırası
        self.vocab = {}           # string parça -> id
        self.inv = {}             # id -> parça
        self.special = {}         # special adı -> id

    # ---------- eğitim ----------
    def train(self, texts, vocab_size=32000, min_freq=2, progress=None):
        """texts: str akışı. vocab_size'e kadar merge öğren."""
        words = Counter()
        for t in texts:
            t = "".join(B2U[b] for b in t.encode("utf-8"))
            for w in PRE_RE.findall(t):
                words[tuple(w)] += 1
        # başlangıç vocab = tek karakterler
        chars = sorted({c for w in words for c in w})
        for i, c in enumerate(chars):
            self.vocab[c] = i
        next_id = len(self.vocab)
        n_merge = vocab_size - len(SPECIALS) - len(self.vocab)
        for step in range(max(0, n_merge)):
            pc = Counter()
            for w, f in words.items():
                for p in _pairs(w):
                    pc[p] += f
            if not pc:
                break
            best, freq = pc.most_common(1)[0]
            if freq < min_freq:
                break
            merged = best[0] + best[1]
            self.merges[best] = merged
            self.rank[best] = step
            self.vocab[merged] = next_id
            next_id += 1
            new_words = Counter()
            for w, f in words.items():
                nw, i = [], 0
                while i < len(w):
                    if i < len(w) - 1 and (w[i], w[i + 1]) == best:
                        nw.append(merged)
                        i += 2
                    else:
                        nw.append(w[i])
                        i += 1
                new_words[tuple(nw)] += f
            words = new_words
            if progress and step % 500 == 0:
                progress(step, n_merge)
        for s in SPECIALS:
            self.vocab[s] = next_id
            self.special[s] = next_id
            next_id += 1
        self.inv = {v: k for k, v in self.vocab.items()}
        return self

    # ---------- kodlama ----------
    def _encode_piece(self, piece):
        if len(piece) == 1:
            return [self.vocab.get(piece, self.vocab.get("?"))]
        w = tuple(piece)
        while len(w) > 1:
            best, brank = None, 1 << 62
            for p in _pairs(w):
                r = self.rank.get(p)
                if r is not None and r < brank:
                    best, brank = p, r
            if best is None:
                break
            nw, i = [], 0
            while i < len(w):
                if i < len(w) - 1 and (w[i], w[i + 1]) == best:
                    nw.append(best[0] + best[1])
                    i += 2
                else:
                    nw.append(w[i])
                    i += 1
            w = tuple(nw)
        out = []
        for t in w:
            if t in self.vocab:
                out.append(self.vocab[t])
            else:  # eğitimde görülmeyen nadir karakter → bayt parçalarına düş
                for ch in t:
                    out.append(self.vocab.get(ch, self.vocab.get("?")))
        return [i for i in out if i is not None]

    def encode(self, text, specials_ok=True):
        ids = []
        for chunk in SPECIAL_RE.split(text):
            if not chunk:
                continue
            if specials_ok and chunk in self.special:
                ids.append(self.special[chunk])
                continue
            mapped = "".join(B2U[b] for b in chunk.encode("utf-8"))
            for piece in PRE_RE.findall(mapped):
                ids.extend(self._encode_piece(piece))
        return ids

    def encode_special(self, name):
        return [self.special[name]]

    def decode(self, ids):
        s = "".join(self.inv.get(i, "") for i in ids if i in self.inv)
        return bytes(U2B[c] for c in s if c in U2B).decode("utf-8", errors="replace")

    @property
    def vocab_size(self):
        return len(self.vocab)

    # ---------- kayıt ----------
    def save(self, dir_path):
        import os
        os.makedirs(dir_path, exist_ok=True)
        merges_list = sorted(self.merges.items(), key=lambda kv: self.rank[kv[0]])
        with open(os.path.join(dir_path, "vocab.json"), "w") as f:
            json.dump(self.vocab, f, ensure_ascii=False)
        with open(os.path.join(dir_path, "merges.txt"), "w") as f:
            f.write("#version: coder1\n")
            for (a, b), _merged in merges_list:
                f.write(a + " " + b + "\n")

    @staticmethod
    def load(dir_path):
        import os
        tk = BPE()
        with open(os.path.join(dir_path, "vocab.json")) as f:
            tk.vocab = {k: int(v) for k, v in json.load(f).items()}
        tk.inv = {v: k for k, v in tk.vocab.items()}
        with open(os.path.join(dir_path, "merges.txt")) as f:
            for i, line in enumerate(f):
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                a, b = line.split(" ", 1)
                tk.merges[(a, b)] = a + b
                tk.rank[(a, b)] = i
        tk.special = {s: tk.vocab[s] for s in SPECIALS if s in tk.vocab}
        return tk
