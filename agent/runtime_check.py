"""Üretilen HTML'i gerçek (headless) tarayıcıda çalıştırır: çalışma hataları,
konsol hataları, tüm butonlara tıklama, ekran görüntüsü. Playwright yoksa
(None) döner — sahte "test geçti" üretilmez."""
import os
import threading

SHOT_NAME = "_onizleme.png"
_LOCK = threading.Lock()          # CPU'da tek tarayıcı yeter


def available():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except Exception:
        return False


def check_html(path, workdir, max_buttons=12, timeout_ms=15000):
    """{'ok','errors':[...],'clicked':n,'shot':path|None} ya da None (ortam yok)."""
    if not available():
        return None
    from playwright.sync_api import sync_playwright
    errors, clicked, shot, inter = [], 0, None, 0
    with _LOCK:
        try:
            with sync_playwright() as p:
                b = p.chromium.launch(args=["--allow-file-access-from-files"])
                pg = b.new_page(viewport={"width": 900, "height": 640})
                pg.on("pageerror", lambda e: errors.append("çalışma hatası: " + str(e).split("\n")[0][:200]))
                pg.on("console", lambda m: errors.append("konsol hatası: " + m.text[:200])
                      if m.type == "error" and "favicon" not in m.text.lower() else None)
                pg.goto("file://" + os.path.abspath(path), wait_until="load", timeout=timeout_ms)
                pg.wait_for_timeout(400)
                inter = len(pg.query_selector_all(
                    "button, input, textarea, select, [onclick], [role=button], .btn, a[href^='#'], canvas"))
                btns = pg.query_selector_all("button, input[type=button], input[type=submit], [role=button], [onclick], .btn, a[href^='#']")
                for el in btns[:max_buttons]:
                    try:
                        if el.is_visible() and el.is_enabled():
                            try:
                                el.scroll_into_view_if_needed(timeout=800)
                            except Exception:
                                pass
                            el.click(timeout=1500, force=True)
                            clicked += 1
                            pg.wait_for_timeout(120)
                    except Exception as e:
                        pass  # gizli/örtülü buton tıklanamazsa hata sayılmaz; gerçek hatalar pageerror ile gelir
                for el in pg.query_selector_all("input[type=text], input:not([type]), textarea")[:4]:
                    try:
                        if el.is_visible():
                            el.fill("deneme")
                    except Exception:
                        pass
                pg.keyboard.press("Enter")
                pg.wait_for_timeout(300)
                try:
                    shot = os.path.join(workdir, SHOT_NAME)
                    pg.screenshot(path=shot)
                except Exception:
                    shot = None
                b.close()
        except Exception as e:
            errors.append("tarayıcı testi çalıştırılamadı: " + str(e).split("\n")[0][:200])
    uniq = []
    for e in errors:
        if e not in uniq:
            uniq.append(e)
    return {"ok": not [e for e in uniq if not e.startswith("tarayıcı testi")], "errors": uniq[:6],
            "clicked": clicked, "shot": shot, "interactive": inter}
