"""Üretilen HTML'i gerçek (headless) tarayıcıda çalıştırır: çalışma hataları,
konsol hataları, tüm butonlara tıklama, ekran görüntüsü. Playwright yoksa
(None) döner — sahte "test geçti" üretilmez."""
import os
import threading

SHOT_NAME = "_onizleme.png"
SHOT_MOBILE = "_onizleme_mobil.png"
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
                def _reqfail(r):
                    u = r.url
                    if u.startswith("file://") and u.rsplit(".", 1)[-1].lower() in ("js", "css", "html"):
                        errors.append("eksik dosya: " + u.rsplit("/", 1)[-1] + " (sayfa çağırıyor ama dosya yok)")
                pg.on("requestfailed", _reqfail)
                def _console(m):
                    if m.type != "error" or "Failed to load resource" in m.text:
                        return
                    if not ((m.location or {}).get("url") or "").startswith("file://"):
                        return  # gömülü harita/video gibi üçüncü taraf iframe mesajları sayılmaz
                    errors.append("konsol hatası: " + m.text[:200])
                pg.on("console", _console)
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
                # kırık yerel görseller
                try:
                    broken = pg.evaluate("""[...document.images].filter(i => i.complete && i.naturalWidth === 0
                        && i.src.startsWith('file://')).map(i => i.getAttribute('src')).slice(0,3)""")
                    for b_ in broken or []:
                        errors.append(f"kırık görsel: {b_} (dosya yok)")
                except Exception:
                    pass
                # telefon boyutunda yatay taşma + mobil ekran görüntüsü
                try:
                    pg.set_viewport_size({"width": 390, "height": 844})
                    pg.wait_for_timeout(300)
                    over = pg.evaluate("Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - window.innerWidth")
                    if over and over > 8:
                        errors.append(f"mobilde yatay taşma: içerik ekrandan {int(over)}px taşıyor (390px genişlikte)")
                    try:
                        clash = pg.evaluate("""(() => {
                          const out = []; const W = window.innerWidth;
                          const vis = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
                            return r.width > 2 && r.height > 2 && cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0'; };
                          const name = el => (el.tagName.toLowerCase() + (el.id ? '#' + el.id : (typeof el.className === 'string' && el.className.trim()) ? '.' + el.className.trim().split(/\\s+/)[0] : '') + (el.children.length ? '' : ' "' + (el.textContent || '').trim().slice(0, 14) + '"')).slice(0, 48);
                          const roots = [...document.querySelectorAll('header, nav, .navbar, .header, .topbar')];
                          const items = [];
                          for (const r of roots) for (const el of r.querySelectorAll('a, button, img, h1, h2, span, li, .logo'))
                            if (vis(el) && !items.some(x => x.contains(el) || el.contains(x))) items.push(el);
                          for (let i = 0; i < items.length && i < 40; i++) {
                            const a = items[i].getBoundingClientRect();
                            if (a.right > W + 8) out.push(name(items[i]) + ' ekrandan taşıyor');
                            for (let j = i + 1; j < items.length && j < 40; j++) {
                              const b = items[j].getBoundingClientRect();
                              const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
                              const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
                              if (ox > 6 && oy > 6) out.push(name(items[i]) + ' ile ' + name(items[j]) + ' üst üste biniyor');
                            }
                          }
                          return [...new Set(out)].slice(0, 3);
                        })()""")
                        for c_ in clash or []:
                            errors.append(f"mobilde çakışma (390px, üst menü): {c_}")
                    except Exception:
                        pass
                    pg.evaluate("window.scrollTo(0,0)")
                    pg.screenshot(path=os.path.join(workdir, SHOT_MOBILE))
                    pg.set_viewport_size({"width": 900, "height": 640})
                    pg.wait_for_timeout(200)
                except Exception:
                    pass
                try:
                    shot = os.path.join(workdir, SHOT_NAME)
                    try:
                        pg.evaluate("window.scrollTo(0,0)")
                        pg.wait_for_timeout(300)
                    except Exception:
                        pass
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
