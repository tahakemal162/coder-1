"""Veri kaynakları — hepsi gerçek kod, hepsi bizim boru hattımızdan geçer.

Kaynaklar (hesapsız indirilebilir olanlar):
  - HF datasets streaming: smollm-corpus (python-edu), code_search_net
  - GitHub tarball: codeload.github.com üzerinden seçili açık kaynak repolar
    (auth gerekmez; kendi küratörlüğümüz repo listesi)

Tüm kaynaklar clean.py süzgecinden geçer: sahte/sentetik değil, gerçek kod.
"""
import io
import os
import tarfile
import urllib.request

from .clean import looks_like_code, Dedup


def iter_hf_dataset(name, subset=None, split="train", field="text", limit=None):
    """HF datasets üzerinden streaming okuma. Kimlik gerektirmeyen açık veri."""
    from datasets import load_dataset  # geç import: kurulu değilse net hata
    kw = {"split": split, "streaming": True}
    ds = load_dataset(name, subset, **kw) if subset else load_dataset(name, **kw)
    n = 0
    for row in ds:
        t = row.get(field) or ""
        if t:
            yield t
            n += 1
            if limit and n >= limit:
                return


def iter_github_tarball(owner_repo, ref="main", max_files=400):
    """Bir public reponun tarball'ını çekip metin dosyalarını akıtır.
    Örn. 'psf/requests'. Auth'suz codeload kullanır."""
    owner, repo = owner_repo.split("/", 1)
    for branch in (ref, "main", "master"):
        url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{branch}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "coder1-data"})
            buf = io.BytesIO(urllib.request.urlopen(req, timeout=60).read())
            break
        except Exception:
            buf = None
    if buf is None:
        return
    tf = tarfile.open(fileobj=buf, mode="r:gz")
    n = 0
    exts = (".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".cpp", ".h",
            ".rb", ".php", ".cs", ".html", ".css", ".sql", ".sh")
    for m in tf:
        if n >= max_files:
            break
        if not m.isfile() or not m.name.lower().endswith(exts) or m.size > 200_000:
            continue
        try:
            text = tf.extractfile(m).read().decode("utf-8", errors="strict")
        except Exception:
            continue
        yield f"# file: {m.name}\n{text}"
        n += 1


def iter_repo_list(path, per_repo_files=400):
    """repo listesi dosyasından (satır başına owner/repo) tüm kodu akıtır."""
    with open(path) as f:
        repos = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    dd = Dedup()
    for r in repos:
        for text in iter_github_tarball(r, max_files=per_repo_files):
            if looks_like_code(text) and dd.is_new(text):
                yield text


def iter_local_dir(root, exts=(".py", ".js", ".ts", ".md", ".txt")):
    """Yerel klasördeki dosyaları akıtır (test/duman verisi)."""
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if fn.lower().endswith(exts):
                p = os.path.join(dp, fn)
                try:
                    yield open(p, encoding="utf-8").read()
                except Exception:
                    pass
