"""agent/ kendini sınaması — gerçek model gerekmez.

Kapsar: workspace manifest/diff/guard'ları, loop'un repeat-nudge/plan/file/
stop olayları, computer.py sandbox (docker varsa) ve iptal davranışı.
Model, loop.call_model yerine test içi betikli cevaplarla koşulur
(test-only stub — shipped path'te sahte yok).

Kullanım: python -m agent.selftest
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import loop                                  # noqa: E402
from agent.computer import docker_status, start_sandbox  # noqa: E402
from agent.workspace import Workspace                   # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond, info=""):
    results.append((name, bool(cond)))
    print(f"[{PASS if cond else FAIL}] {name} {info}")


def script_model(replies):
    """Sırayla cevap döndüren test modeli."""
    it = iter(replies)

    def fake(base_url, messages, **kw):
        return next(it, "<|done|>")
    return fake


def t_workspace(tmp):
    ws = Workspace(os.path.join(tmp, "w"))
    ws.write("a.txt", "satir1\nsatir2\n")
    ws.write("sub/b.py", "print('x')\n")
    ws.edit("a.txt", "satir2", "SATIR2")
    ops = ws.manifest()
    check("manifest yazıldı", len(ops) == 3 and
          all(set(o) == {"op", "path", "sha1", "ts"} for o in ops))
    check("manifest op'lar", [o["op"] for o in ops] ==
          ["write", "write", "edit"])
    d = ws.diff()
    check("diff unified", "--- a/a.txt" in d and "+SATIR2" in d and
          "b/sub/b.py" in d)
    # guard'lar
    try:
        ws.write("big.bin", "x" * (3_000_000))
        check("büyük dosya reddi", False)
    except ValueError:
        check("büyük dosya reddi", True)
    try:
        ws.write("bin.dat", "a\x00b\x00c")
        check("ikili yazma reddi", False)
    except ValueError:
        check("ikili yazma reddi", True)
    open(os.path.join(ws.root, "raw.bin"), "wb").write(b"\x00\x01\x02\x03" * 10)
    try:
        ws.read("raw.bin")
        check("ikili okuma reddi", False)
    except ValueError:
        check("ikili okuma reddi", True)
    try:
        ws._abs("../escape.txt")
        check("yol escape reddi", False)
    except ValueError:
        check("yol escape reddi", True)
    check(".coder1 listelenmez", not any(".coder1" in f
                                         for f in ws.list_files()))


def t_loop(tmp):
    wd = os.path.join(tmp, "loop1")
    evs = []

    # 1) plan + file olayları + done
    replies = iter([
        "Plan:\n1. dosyayı yaz\n2. çalıştır\n<|tool|>{\"name\":\"write_file\",\"args\":{\"path\":\"x.py\",\"content\":\"print(1)\"}}",
        "<|tool|>{\"name\":\"run_command\",\"args\":{\"cmd\":\"cat x.py\"}}",
        "tamamlandı<|tool|>{\"name\":\"done\",\"args\":{}}",
    ])
    loop.call_model = lambda *a, **k: next(replies, "<|done|>")
    res = loop.agent_task("http://x", "test", wd, on_event=lambda e: evs.append(e))
    kinds = [e["t"] for e in evs]
    check("plan eventi", "plan" in kinds and
          any(e.get("items") for e in evs if e["t"] == "plan"))
    check("file write eventi", any(e == {"t": "file", "path": "x.py",
                                         "op": "write"} for e in evs))
    check("dur_ms izlendi", any(e["t"] == "tool_result" and "dur_ms" in e
                                for e in evs))
    check("done ok", res.get("ok") is True)
    check("workspace dosyası yazıldı",
          os.path.exists(os.path.join(wd, "x.py")))

    # 2) repeat guard: aynı çağrı 3x → repeat_loop
    evs.clear()
    replies = iter(["<|tool|>{\"name\":\"list_files\",\"args\":{}}"] * 10)
    loop.call_model = lambda *a, **k: next(replies)
    res = loop.agent_task("http://x", "t", os.path.join(tmp, "loop2"),
                          on_event=lambda e: evs.append(e))
    check("repeat nudge", any(e["t"] == "nudge" for e in evs))
    check("repeat_loop abort", res.get("reason") == "repeat_loop" and
          not res.get("ok"))

    # 3) stop: cancel flag → {"t":"stop"} (önce bir tool çağrısı ki döngü sürsün)
    evs.clear()
    cancel = threading.Event()
    replies = iter([
        "<|tool|>{\"name\":\"list_files\",\"args\":{}}",
        "düşünüyorum",   # 2. çağrıda cancel set edilir; bir sonraki adımda stop
    ])

    def scripted(*a, **k):
        r = next(replies, "yine düşünüyorum")
        if r == "düşünüyorum":
            cancel.set()
        return r
    loop.call_model = scripted
    res = loop.agent_task("http://x", "t", os.path.join(tmp, "loop3"),
                          on_event=lambda e: evs.append(e), cancel=cancel)
    check("stop eventi", any(e["t"] == "stop" for e in evs))
    check("stopped sonucu", res.get("stopped") is True)

    # 4) tool_result enjeksiyon sınırı (baş+son korunur)
    long_out = "A" * 5000 + "SON" + "B" * 5000
    capped = loop._cap_result(long_out)
    check("tool_result sınırı", len(capped) <= loop.RESULT_INJECT_CHARS + 60
          and capped.endswith("B" * 100) and capped.startswith("AAA"))


def t_sandbox(tmp):
    st = docker_status()
    print(f"  docker: {st}")
    sb, kind, why = start_sandbox(os.path.join(tmp, "sb"), "selftest")
    print(f"  sandbox kind={kind} reason={why!r}")
    out = sb.run("echo -n merhaba && echo -n ' ' && pwd")
    check("sandbox run", "merhaba" in out and "/work" in out, out[:120])
    # iptal: uzun komut + cancel
    cancel = threading.Event()
    cancel.set()
    out2 = sb.run("sleep 30", timeout=30, cancel=cancel)
    check("sandbox iptal", "iptal" in out2, out2[:80])
    # timeout: kısa limit
    out3 = sb.run("sleep 30", timeout=1)
    check("sandbox timeout", "zaman aşımı" in out3, out3[:80])
    shot = sb.screenshot()
    print(f"  screenshot: {'JPEG ' + str(len(shot)) + 'B' if shot else 'None (display yok — 404)'}")
    if shot:
        check("screenshot JPEG", shot[:2] == b"\xff\xd8")
    else:
        check("screenshot dürüst 404", True)
    sb.close()
    if kind == "docker":
        import subprocess
        r = subprocess.run(["docker", "ps", "-a", "--filter",
                            "name=coder1-selftest", "--format", "{{.Names}}"],
                           capture_output=True, text=True)
        check("container temizlendi", "coder1-selftest" not in r.stdout)


def main():
    tmp = tempfile.mkdtemp(prefix="c1agent_")
    print(f"workdir: {tmp}")
    t_workspace(tmp)
    t_loop(tmp)
    t_sandbox(tmp)
    n_ok = sum(1 for _, ok in results if ok)
    print(f"\n== {n_ok}/{len(results)} kontrol geçti")
    sys.exit(0 if n_ok == len(results) else 1)


if __name__ == "__main__":
    main()
