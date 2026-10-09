"""coder-1 "kendi bilgisayarı" — görev başına yalıtılmış komut ortamı.

Docker varsa her görev `coder1-<tid>` adlı bir konteynerde çalışır
(workdir /work'e bağlanır; dosya işlemleri host'tan görünür kalır).
Docker yoksa/host erişilemezse HostSandbox'a düşülür — dürüst fallback,
sahte yalıtım iddiası yok.

`screenshot()` yalnızca gerçek bir X ekranı varsa JPEG döndürür
(konteynerde Xvfb + ImageMagick `import`, ya da host'ta DISPLAY + import);
yoksa None → API 404 verir. Sahte ekran üretilmez.
"""
import os
import shutil
import subprocess
import threading

DOCKER_IMAGE = "ubuntu:22.04"
_CONTAINER_PREFIX = "coder1-"

_docker_checked = None       # (bool, neden) — daemon durumu tek seferlik ölçülür


def _docker_ok():
    """(True, '') ya da (False, neden). Sonuç önbelleğe alınır."""
    global _docker_checked
    if _docker_checked is not None:
        return _docker_checked
    if not shutil.which("docker"):
        _docker_checked = (False, "docker yok")
        return _docker_checked
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            _docker_checked = (True, "")
        else:
            _docker_checked = (False, (r.stderr or "docker daemon yok").strip()[:200])
    except Exception as e:
        _docker_checked = (False, str(e)[:200])
    return _docker_checked


def docker_status():
    ok, why = _docker_ok()
    return {"available": ok, "reason": why}


class HostSandbox:
    """Yalıtımsız fallback: komutlar host'ta workdir içinde koşar."""

    kind = "host"

    def __init__(self, workdir):
        self.workdir = os.path.abspath(workdir)
        os.makedirs(self.workdir, exist_ok=True)
        self._procs = set()
        self._lock = threading.Lock()

    def run(self, cmd, timeout=60, cancel=None):
        try:
            proc = subprocess.Popen(
                cmd, shell=True, cwd=self.workdir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except Exception as e:
            return f"komut başlatılamadı: {e}"
        with self._lock:
            self._procs.add(proc)
        try:
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
            with self._lock:
                self._procs.discard(proc)
            if proc.poll() is None:
                proc.kill()

    def screenshot(self):
        """Host'ta gerçek DISPLAY varsa ImageMagick ile kök pencere JPEG'i."""
        display = os.environ.get("DISPLAY")
        imp = shutil.which("import")
        if not display or not imp:
            return None
        try:
            r = subprocess.run([imp, "-display", display, "-window", "root",
                                "jpg:-"], capture_output=True, timeout=15)
            if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8":
                return r.stdout
        except Exception:
            pass
        return None

    def close(self):
        with self._lock:
            for p in list(self._procs):
                if p.poll() is None:
                    p.kill()
            self._procs.clear()


class DockerSandbox:
    """Görev başına `ubuntu:22.04` konteyneri; exec ile komut."""

    kind = "docker"

    def __init__(self, workdir, tid):
        self.workdir = os.path.abspath(workdir)
        os.makedirs(self.workdir, exist_ok=True)
        self.name = f"{_CONTAINER_PREFIX}{tid}"
        self.cid = None
        self.display = os.environ.get("CODER1_DISPLAY", ":99")
        self._closed = False
        self._lock = threading.Lock()
        self._exec_procs = set()

    def start(self):
        if self.cid:
            return self
        r = subprocess.run(
            ["docker", "run", "-d", "--rm", "-w", "/work",
             "-v", f"{self.workdir}:/work", "--name", self.name,
             DOCKER_IMAGE, "sleep", "infinity"],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"container başlatılamadı: {(r.stderr or r.stdout).strip()[:300]}")
        self.cid = r.stdout.strip()[:12]
        return self

    def run(self, cmd, timeout=60, cancel=None):
        if not self.cid:
            return "sandbox çalışmıyor"
        # içeride coreutils timeout: üst sınır garantili; dışarıda backstop
        args = ["docker", "exec", "-w", "/work", self.cid,
                "timeout", "-k", "2", str(int(timeout)), "sh", "-c", cmd]
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        except Exception as e:
            return f"komut başlatılamadı: {e}"
        with self._lock:
            self._exec_procs.add(proc)
        try:
            waited = 0.0
            outer = timeout + 15          # iç timeout'un bitmesini bekle
            while True:
                try:
                    proc.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    waited += 0.1
                    if cancel is not None and cancel.is_set():
                        self._kill_inner()
                        proc.kill()
                        proc.wait()
                        return "komut iptal edildi"
                    if waited >= outer:
                        self._kill_inner()
                        proc.kill()
                        proc.wait()
                        return f"komut zaman aşımı ({timeout}s)"
            out, err = proc.communicate()
            rc = proc.returncode
            if rc == 124 or rc == 137:    # coreutils timeout / SIGKILL
                return f"komut zaman aşımı ({timeout}s)"
            txt = (out or "") + (("\nSTDERR:\n" + err) if err else "")
            return (txt[:6000] + f"\n[exit {rc}]").strip()
        finally:
            with self._lock:
                self._exec_procs.discard(proc)
            if proc.poll() is None:
                proc.kill()

    def _kill_inner(self):
        """İptalde konteyner içindeki komutu best-effort öldür."""
        try:
            subprocess.run(
                ["docker", "exec", self.cid,
                 "pkill", "-KILL", "-f", "sh -c"],
                capture_output=True, timeout=10)
        except Exception:
            pass

    def screenshot(self):
        """Konteynerde X ekranı + ImageMagick varsa gerçek JPEG; yoksa None."""
        if not self.cid:
            return None
        probe = (f"command -v import >/dev/null 2>&1 && "
                 f"(xdpyinfo -display {self.display} >/dev/null 2>&1 || "
                 f"pgrep -x Xvfb >/dev/null 2>&1)")
        try:
            r = subprocess.run(["docker", "exec", self.cid, "sh", "-c", probe],
                               capture_output=True, timeout=10)
            if r.returncode != 0:
                return None
            r = subprocess.run(
                ["docker", "exec", self.cid,
                 "import", "-display", self.display, "-window", "root", "jpg:-"],
                capture_output=True, timeout=20)
            if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8":
                return r.stdout
        except Exception:
            pass
        return None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for p in list(self._exec_procs):
                if p.poll() is None:
                    p.kill()
            self._exec_procs.clear()
        if self.cid:
            try:
                subprocess.run(["docker", "rm", "-f", self.cid],
                               capture_output=True, timeout=15)
            except Exception:
                pass


def start_sandbox(workdir, tid, prefer_docker=True):
    """(sandbox, kind, reason) döndürür. Docker tercih edilir, host'a düşülür."""
    if prefer_docker:
        ok, why = _docker_ok()
        if ok:
            try:
                sb = DockerSandbox(workdir, tid).start()
                return sb, "docker", ""
            except Exception as e:
                why = str(e)[:200]
        sb = HostSandbox(workdir)
        return sb, "host", why
    sb = HostSandbox(workdir)
    return sb, "host", "docker devre dışı"


def cleanup_stale_containers(max_age_s=0):
    """Yarım kalan coder1-* konteynerlerini süpür (başlangıç/dönemsel iş)."""
    ok, _ = _docker_ok()
    if not ok:
        return 0
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={_CONTAINER_PREFIX}",
             "--format", "{{.ID}} {{.CreatedAt}} {{.Names}}"],
            capture_output=True, text=True, timeout=10)
        n = 0
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 1:
                subprocess.run(["docker", "rm", "-f", parts[0]],
                               capture_output=True, timeout=10)
                n += 1
        return n
    except Exception:
        return 0
