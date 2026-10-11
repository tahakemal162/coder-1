"""Web sitesi kalite kapıları (statik) + uzun çalışma turları için yardımcılar.

audit(written) -> {"hard": [...], "soft": [...]}
  hard: teslimi engelleyen (kırık sayfa linki, tanımsız onclick fonksiyonu, tekrar eden id,
        eksik viewport/title, JS'in aradığı id HTML'de yok)
  soft: kalite (alt metni olmayan görsel, lang yok, meta description yok, boş link, form doğrulaması yok)
"""
import re
from html.parser import HTMLParser

ROUND_THEMES = [
    ("mobil", "telefon ekranında (390px) düzen, dokunma hedefleri (min 44px), yatay taşma, menü kullanılabilirliği"),
    ("erisilebilirlik", "erişilebilirlik: alt metinleri, form etiketleri (label/for), kontrast, klavye odak görünürlüğü, başlık hiyerarşisi (tek h1)"),
    ("icerik", "içerik kalitesi: gerçekçi ve ikna edici Türkçe metinler, boş/yer tutucu metin kalmasın, tutarlı marka adı"),
    ("etkilesim", "etkileşim: tüm buton ve bağlantılar çalışsın, form doğrulama ve başarı mesajı, boş durumlar, hata mesajları"),
    ("seo", "SEO/meta: title, description, og etiketleri, anlamlı başlıklar, semantik etiketler (header/nav/main/section/footer)"),
    ("gorsel", "görsel incelik: tutarlı boşluk/tipografi, hover/odak geçişleri, kart hizaları, footer ve ayırıcılar"),
]


class _P(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.links, self.imgs, self.onclicks, self.tags = [], [], [], [], []
        self.title = False
        self.viewport = False
        self.lang = False
        self.desc = False
        self.forms = 0
        self.inputs_wo_label = 0
        self.labels_for = set()
        self.input_ids = []
        self.h1 = 0
        self.hrefs_empty = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append(tag)
        if a.get("id"):
            self.ids.append(a["id"])
        if tag == "a":
            h = (a.get("href") or "").strip()
            if h in ("", "#"):
                self.hrefs_empty += 1
            elif not re.match(r"^(https?:|//|mailto:|tel:|#|javascript:|data:)", h, re.I):
                self.links.append(h.split("#")[0].split("?")[0])
        if tag == "img":
            self.imgs.append((a.get("src") or "", a.get("alt")))
        if a.get("onclick"):
            self.onclicks.append(a["onclick"])
        if tag == "title":
            self.title = True
        if tag == "meta":
            if (a.get("name") or "").lower() == "viewport":
                self.viewport = True
            if (a.get("name") or "").lower() == "description" and (a.get("content") or "").strip():
                self.desc = True
        if tag == "html" and a.get("lang"):
            self.lang = True
        if tag == "form":
            self.forms += 1
        if tag == "label" and a.get("for"):
            self.labels_for.add(a["for"])
        if tag in ("input", "textarea", "select") and (a.get("type") or "").lower() not in ("hidden", "submit", "button", "checkbox", "radio"):
            self.input_ids.append(a.get("id") or "")
        if tag == "h1":
            self.h1 += 1


def audit(written):
    """written: {path: content}. Sadece web dosyalarına bakar."""
    hard, soft = [], []
    files = set(written)
    htmls = {p: c for p, c in written.items() if p.endswith((".html", ".htm"))}
    js_all = "\n".join(c for p, c in written.items() if p.endswith(".js"))
    for p, c in htmls.items():
        js_all += "\n" + "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", c, re.S | re.I))
    js_funcs = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js_all))
    js_funcs |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", js_all))
    js_funcs |= set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", js_all))
    all_ids = set()
    parsed = {}
    for p, c in htmls.items():
        q = _P()
        try:
            q.feed(c)
        except Exception as e:
            hard.append(f"{p}: HTML ayrıştırılamadı ({str(e)[:80]})")
            continue
        parsed[p] = q
        all_ids |= set(q.ids)
        dup = sorted({i for i in q.ids if q.ids.count(i) > 1})
        if dup:
            hard.append(f"{p}: tekrar eden id: {', '.join(dup[:5])}")
        for h in q.links:
            if h and h not in files and not h.endswith("/") and "." in h.rsplit("/", 1)[-1]:
                hard.append(f"{p}: kırık bağlantı '{h}' (böyle bir dosya yok)")
        for oc in q.onclicks:
            m = re.match(r"\s*([A-Za-z_$][\w$]*)\s*\(", oc)
            if m and m.group(1) not in js_funcs and m.group(1) not in ("alert", "location", "window", "document", "history", "print", "confirm", "open"):
                hard.append(f"{p}: onclick '{m.group(1)}()' fonksiyonu JS'te tanımlı değil")
        if not q.viewport:
            hard.append(f"{p}: <meta name=\"viewport\"> yok (mobilde bozuk görünür)")
        if not q.title:
            hard.append(f"{p}: <title> yok")
        if not q.lang:
            soft.append(f"{p}: <html lang=\"tr\"> eksik")
        if not q.desc:
            soft.append(f"{p}: meta description yok (SEO)")
        noalt = [s for s, alt in q.imgs if alt is None]
        if noalt:
            soft.append(f"{p}: alt metni olmayan {len(noalt)} görsel")
        if q.hrefs_empty > 2:
            soft.append(f"{p}: {q.hrefs_empty} boş bağlantı (href=\"#\") — gerçek hedef ver")
        if q.h1 == 0:
            soft.append(f"{p}: h1 başlık yok")
        elif q.h1 > 1:
            soft.append(f"{p}: {q.h1} adet h1 (tek olmalı)")
        unl = [i for i in q.input_ids if not i or i not in q.labels_for]
        if q.forms and unl:
            soft.append(f"{p}: {len(unl)} form alanının <label for> etiketi yok")
        if q.forms and not re.search(r"required|checkValidity|preventDefault", c + js_all):
            soft.append(f"{p}: formda doğrulama yok (required / JS kontrolü)")
    # JS'in aradığı id'ler HTML'de var mı
    for m in set(re.findall(r"getElementById\(\s*['\"]([\w\-]+)['\"]\s*\)", js_all)):
        if htmls and m not in all_ids:
            soft.append(f"script: getElementById('{m}') — bu id hiçbir sayfada yok")
    # CSS'te tanımsız sınıflar (bilgi amaçlı, en fazla 1 madde)
    css_all = "\n".join(c for p, c in written.items() if p.endswith(".css"))
    for p, c in htmls.items():
        css_all += "\n" + "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", c, re.S | re.I))
    if css_all.strip():
        used = set()
        for c in htmls.values():
            for cls in re.findall(r'class=["\']([^"\']+)["\']', c):
                used |= set(cls.split())
        defined = set(re.findall(r"\.([A-Za-z_][\w\-]*)", css_all))
        undefined = sorted(u for u in used if u not in defined and not u.startswith(("fa", "bi", "material")))
        if len(undefined) >= 4:
            soft.append(f"CSS'te tanımı olmayan {len(undefined)} sınıf (ör. {', '.join(undefined[:4])})")
    return {"hard": hard[:8], "soft": soft[:10]}


def fmt(a):
    return ("ciddi: " + "; ".join(a["hard"]) if a["hard"] else "ciddi sorun yok") + \
           (" | iyileştirme: " + "; ".join(a["soft"]) if a["soft"] else "")
