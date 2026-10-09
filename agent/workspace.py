"""Görev başına yalıtılmış çalışma alanı — v1 (production hardening).

- Yol escape'i engellenir (realpath bazlı).
- Her write/edit `.coder1/ops.jsonl` manifest'ine işlenir: {op,path,sha1,ts}.
  İlk dokunuşta dosyanın orijinal hali `.coder1/orig/` altına kopyalanır →
  `diff()` workspace vs orijinal unified diff üretir (UI görüntüsü için).
- İkili/büyük dosya koruması: NUL içeren ya da sınırı aşan dosyalar reddedilir.
- `run()` iptal edilebilir (threading.Event) ve opsiyonel olarak bir
  `runner`'a (örn. Docker sandbox, agent/computer.py) devredilebilir.
"""
import difflib
import hashlib
import json
import os
import subprocess
import threading
import time

META_DIR = ".coder1"            # workspace içi meta klasörü (listelemede gizli)
OPS_FILE = "ops.jsonl"
MAX_FILE_BYTES = 2_000_000      # tek dosya yazma üst sınırı (2 MB)
MAX_READ_CHARS = 8000
SKIP_DIRS = {".git", "node_modules", "__pycache__", META_DIR}
BINARY_CHECK = 8192             # ikili tespiti için ilk N byte


def _looks_binary(raw: bytes) -> bool:
    if b"\x00" in raw[:BINARY_CHECK]:
        return True
    if not raw:
        return False
    # yüksek oranda yazdırılamaz byte → ikili say
    sample = raw[:BINARY_CHECK]
    bad = sum(1 for b in sample if b < 9 or (13 < b < 32))
    return bad / len(sample) > 0.05


class Workspace:
    """Bir görevin dosya/komut alanı. Yol escape'i engellenir."""

    def __init__(self, root, runner=None):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.runner = runner      # agent.computer.Sandbox veya None (host)
        self._ops_lock = threading.Lock()

    # ---------- yol güvenliği ----------

    def _abs(self, rel):
        p = os.path.realpath(os.path.join(self.root, rel.lstrip("/")))
        root_r = os.path.realpath(self.root)
        if p != root_r and not p.startswith(root_r + os.sep):
            raise ValueError("yol workspace dışına çıkıyor")
        return p

    def _rel(self, abspath):
        return os.path.relpath(abspath, self.root)

    # ---------- manifest / orijinal kopya ----------

    @property
    def _meta(self):
        d = os.path.join(self.root, META_DIR)
        os.makedirs(d, exist_ok=True)
        return d

    def _ops_path(self):
        return os.path.join(self._meta, OPS_FILE)

    def _orig_path(self, rel):
        return os.path.join(self._meta, "orig", rel)

    def _snapshot_orig(self, rel):
        """Dosyaya ilk dokunuşta orijinal içeriği sakla (diff için)."""
        ap = self._abs(rel)
        op = self._orig_path(rel)
        if os.path.exists(op) or not os.path.exists(ap):
            return
        try:
            with open(ap, "rb") as f:
                raw = f.read(MAX_FILE_BYTES + 1)
            os.makedirs(os.path.dirname(op), exist_ok=True)
            with open(op, "wb") as f:
                f.write(raw)
        except OSError:
            pass  # orijinal okunamazsa diff bu dosya için boş kalır

    def _log_op(self, op, rel, sha1):
        rec = {"op": op, "path": rel, "sha1": sha1, "ts": time.time()}
        with self._ops_lock:
            with open(self._ops_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def manifest(self):
        """Kayıtlı operasyonlar (eski→yeni). Yoksa []."""
        out = []
        try:
            with open(self._ops_path(), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
        except FileNotFoundError:
            pass
        return out

    # ---------- dosya işlemleri ----------

    def list_files(self, path="."):
        base = self._abs(path)
        out = []
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
            for f in fns:
                out.append(os.path.relpath(os.path.join(dp, f), self.root))
        return sorted(out)[:500]

    def read(self, rel, max_chars=MAX_READ_CHARS):
        p = self._abs(rel)
        with open(p, "rb") as f:
            raw = f.read(BINARY_CHECK)
        if _looks_binary(raw):
            raise ValueError(f"ikili dosya okunamaz: {rel}")
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)

    def write(self, rel, content):
        if isinstance(content, str):
            raw = content.encode("utf-8")
        else:
            raw = bytes(content)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError(
                f"dosya çok büyük ({len(raw)}B > {MAX_FILE_BYTES}B): {rel}")
        if _looks_binary(raw):
            raise ValueError(f"ikili içerik yazılamaz: {rel}")
        self._snapshot_orig(rel)
        p = self._abs(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(raw.decode("utf-8"))
        self._log_op("write", rel, hashlib.sha1(raw).hexdigest())

    def edit(self, rel, old, new):
        p = self._abs(rel)
        text = open(p, encoding="utf-8").read()
        if old not in text:
            return "hata: 'old' metni dosyada yok"
        n = text.count(old)
        self._snapshot_orig(rel)
        text = text.replace(old, new, 1)
        open(p, "w", encoding="utf-8").write(text)
        self._log_op("edit", rel,
                     hashlib.sha1(text.encode("utf-8")).hexdigest())
        return f"düzenlendi: {rel} ({n} eşleşmeden ilki değişti)"

    # ---------- diff (UI için) ----------

    def diff(self):
        """Workspace'in orijinale göre unified diff'i.

        Manifest'teki dosyalar + yeni oluşan dosyalar taranır. İkili/bozuk
        dosyalar diff'e alınmaz. Değişiklik yoksa boş metin döner.
        """
        touched = []
        seen = set()
        for rec in self.manifest():
            p = rec.get("path")
            if p and p not in seen:
                seen.add(p)
                touched.append(p)
        # manifest dışı yeni dosyalar da varsa ekle (harici komutla yazılmış)
        for rel in self.list_files():
            if rel not in seen:
                touched.append(rel)

        chunks = []
        for rel in touched:
            ap = os.path.join(self.root, rel)
            op = self._orig_path(rel)
            try:
                old = ""
                if os.path.exists(op):
                    old = open(op, encoding="utf-8", errors="replace").read()
                elif not os.path.exists(ap):
                    continue  # silinmiş, orijinali de yok
                new = ""
                if os.path.exists(ap):
                    with open(ap, "rb") as f:
                        raw = f.read(BINARY_CHECK)
                    if _looks_binary(raw):
                        continue
                    new = open(ap, encoding="utf-8",
                               errors="replace").read()
                if old == new:
                    continue
                chunks.append("".join(difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    fromfile=f"a/{rel}", tofile=f"b/{rel}")))
            except OSError:
                continue
        return "".join(chunks)

    # ---------- komut çalıştırma ----------

    def run(self, cmd, timeout=60, cancel: threading.Event = None):
        if self.runner is not None:
            return self.runner.run(cmd, timeout=timeout, cancel=cancel)
        return self._run_host(cmd, timeout=timeout, cancel=cancel)

    def _run_host(self, cmd, timeout=60, cancel=None):
        try:
            proc = subprocess.Popen(
                cmd, shell=True, cwd=self.root,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except Exception as e:
            return f"komut başlatılamadı: {e}"
        try:
            # 100 ms'lik dilimlerde bekle → iptal bayrağına duyarlı
            waited = 0.0
            while True:
                try:
                    proc.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    waited += 0.1
                    if cancel is not None and cancel.is_set():
                        proc.kill()
                        proc.wait()
                        return "komut iptal edildi"
                    if waited >= timeout:
                        proc.kill()
                        proc.wait()
                        return f"komut zaman aşımı ({timeout}s)"
            out, err = proc.communicate()
            txt = (out or "") + (("\nSTDERR:\n" + err) if err else "")
            return (txt[:6000] + f"\n[exit {proc.returncode}]").strip()
        finally:
            if proc.poll() is None:
                proc.kill()
