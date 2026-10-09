"""Görev başına yalıtılmış çalışma alanı — v0: hostta dizin; Faz D: Docker."""
import os
import subprocess


class Workspace:
    """Bir görevin dosya/komut alanı. Yol escape'i engellenir."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def _abs(self, rel):
        p = os.path.abspath(os.path.join(self.root, rel.lstrip("/")))
        if not p.startswith(self.root):
            raise ValueError("yol workspace dışına çıkıyor")
        return p

    def list_files(self, path="."):
        base = self._abs(path)
        out = []
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d not in (".git", "node_modules", "__pycache__")]
            for f in fns:
                out.append(os.path.relpath(os.path.join(dp, f), self.root))
        return sorted(out)[:500]

    def read(self, rel, max_chars=8000):
        with open(self._abs(rel), encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)

    def write(self, rel, content):
        p = self._abs(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)

    def edit(self, rel, old, new):
        p = self._abs(rel)
        text = open(p, encoding="utf-8").read()
        if old not in text:
            return "hata: 'old' metni dosyada yok"
        n = text.count(old)
        text = text.replace(old, new, 1)
        open(p, "w", encoding="utf-8").write(text)
        return f"düzenlendi: {rel} ({n} eşleşmeden ilki değişti)"

    def run(self, cmd, timeout=60):
        try:
            r = subprocess.run(cmd, shell=True, cwd=self.root, timeout=timeout,
                               capture_output=True, text=True)
            out = (r.stdout or "") + (("\nSTDERR:\n" + r.stderr) if r.stderr else "")
            return (out[:6000] + f"\n[exit {r.returncode}]").strip()
        except subprocess.TimeoutExpired:
            return f"komut zaman aşımı ({timeout}s)"
