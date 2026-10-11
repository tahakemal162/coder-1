"""coder-1 2. kat — anla → sor → planla → yaz → doğrula → onar → raporla.

Rakip ürünlerden farkı: belirsiz istekte önce seçenekli sorular, kullanıcı
cevaplarına birebir özel üretim, gerçek çalıştırma/testle doğrulama, hatada
kendi kendine onarım ve 'neyi test ettim' raporu. Kullanıcı tercihleri
(prefs) hatırlanır, aynı şey iki kez sorulmaz.
"""
import html.parser
import json
import os
import re
import shutil
import tempfile
import time

from . import brain
from . import runtime_check
from .workspace import Workspace

MAX_REPAIR = 2
FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.S)

UNDERSTAND_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["chat", "web", "python", "other"]},
        "clear": {"type": "boolean"},
        "summary": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "q": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["key", "q", "options"],
            },
        },
    },
    "required": ["kind", "clear", "summary", "questions"],
}
DISCOVER_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "q": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["key", "q", "options"],
            },
        },
    },
    "required": ["complete", "questions"],
}
BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "goal": {"type": "string"},
        "audience": {"type": "string"},
        "features": {"type": "array", "items": {"type": "string"}},
        "pages": {"type": "array", "items": {"type": "string"}},
        "look": {"type": "string"},
        "behavior": {"type": "array", "items": {"type": "string"}},
        "language": {"type": "string"},
        "out_of_scope": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "goal", "features", "look", "language"],
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"missing": {"type": "array", "items": {"type": "string"}}},
    "required": ["missing"],
}
SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    "required": ["items"],
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "files": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "purpose": {"type": "string"}},
            "required": ["path", "purpose"]}},
        "steps": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "files", "steps", "tests"],
}


def _code_of(text):
    """Model çıktısından dosya içeriğini al: en büyük çit bloğu, yoksa tümü."""
    blocks = FENCE_RE.findall(text)
    if blocks:
        return max(blocks, key=len).strip("\n") + "\n"
    return text.strip() + "\n"


class _HTMLCheck(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.scripts, self.in_script, self.problems = [], [], False, []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "script" and not dict(attrs).get("src"):
            self.in_script = True
            self.scripts.append("")

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script and self.scripts:
            self.scripts[-1] += data


def _check_html(content, must_have):
    p = _HTMLCheck()
    try:
        p.feed(content)
    except Exception as e:
        return [f"HTML ayrıştırılamadı: {e}"], []
    probs = []
    if "html" not in p.tags or "body" not in p.tags:
        probs.append("eksik <html>/<body> yapısı")
    low = content.lower()
    for kw in must_have:
        if kw.lower() not in low:
            probs.append(f"kullanıcının istediği '{kw}' kodda geçmiyor")
    return probs, p.scripts


def _node_ok():
    node = shutil.which("node")
    if not node:
        return False
    try:
        import subprocess
        v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5).stdout
        return int(v.lstrip("v").split(".")[0]) >= 16
    except Exception:
        return False


def _check_js(scripts, ws: Workspace):
    if not _node_ok():
        return []
    probs = []
    for i, src in enumerate(scripts):
        if not src.strip():
            continue
        # sadece sözdizimi kontrolü: kod çalıştırılmaz, bu yüzden host node'u yeterli
        # (sandbox imajında node olmayabilir)
        import subprocess
        fd, path = tempfile.mkstemp(suffix=".js", dir=ws.root)
        with os.fdopen(fd, "w") as f:
            f.write(src)
        try:
            r = subprocess.run([shutil.which("node"), "--check", path],
                               capture_output=True, text=True, timeout=20)
            out = (r.stdout + r.stderr).replace(path, "script%d.js" % (i + 1))
            code = r.returncode
        except Exception as e:  # node çöktü/zaman aşımı → kontrolü atla
            out, code = str(e), 0
        finally:
            os.unlink(path)
        if code != 0:
            probs.append("JavaScript sözdizimi hatası:\n" + out[:800])
    return probs


BUILD_WORDS = ("yap", "oluştur", "yaz", "kodla", "geliştir", "site", "sayfa", "uygulama",
               "fonksiyon", "program", "oyun", "hesap", "liste", "form", "html", "css", "python",
               "javascript", "script", "api", "bot", "sayaç", "portfol", "tasarla", "kur", "ekle",
               "düzelt", "değiştir", "build", "create", "make", "write", "app", "website", "page")
PY_WORDS = ("python", "fonksiyon", "def ", "doctest", "script", ".py", "pytest", "class ", "algoritma")


def _guess_kind(text, model_kind):
    low = text.lower()
    build = any(w in low for w in BUILD_WORDS)
    if model_kind == "chat" and not build:
        return "chat"
    if any(w in low for w in PY_WORDS) and not any(w in low for w in ("site", "sayfa", "html", "web")):
        return "python"
    if model_kind in ("web", "python", "other") and model_kind != "chat":
        return model_kind if model_kind != "other" or not build else "web"
    return "web"


DETAIL_WORDS = ("renk", "mavi", "kırmızı", "yeşil", "mor", "siyah", "beyaz", "turuncu", "sarı", "pembe",
                "koyu", "açık", "modern", "minimal", "sade", "animasyon", "responsive", "mobil", "tema",
                "font", "başlık", "buton", "özellik", "ile", "olsun", "içinde", "tarz", "stil", "ingilizce",
                "türkçe", "dark", "light", "color", "style", "with")


def _detail_score(text):
    low = text.lower()
    score = sum(1 for w in DETAIL_WORDS if w in low)
    score += low.count(",") + (1 if any(ch.isdigit() for ch in low) else 0)
    score += 1 if len(low) > 90 else 0
    return score


def _user_keywords(text, answers):
    kws = []
    for a in (answers or {}).values():
        a = str(a).strip()
        if 2 < len(a) <= 24 and " " not in a and not a.isdigit():
            kws.append(a)
    return kws[:4]


ASK_MARK = "Tam istediğin gibi olsun diye birkaç soru: "
BRIEF_MARK = "Proje özeti: "
MAX_ROUNDS = 2          # keşif turu üst sınırı
MAX_BRIEF_EDITS = 2     # özet düzeltme turu üst sınırı
PREF_KEYS = ("renk", "tema", "stil", "dil", "görün", "gorun", "font", "tasar", "mod")
COVERAGE = {
    "web": ["site/işletme adı ve amacı", "sayfalar ve her sayfadaki bölümler",
            "ana özellikler (ziyaretçi neler yapabilecek)", "görünüm: renk/tema/stil",
            "iletişim yolu (form / WhatsApp / telefon)", "metinler ve dil", "telefon uyumu"],
    "python": ["ne girip ne çıkacak", "ana işlevler", "hatalı girişte ne olacak",
               "kullanım şekli: fonksiyon mu komut satırı mı", "örnek girdi/çıktı"],
    "other": ["amaç", "ana özellikler", "girdi/çıktı", "görünüm"],
}
CONFIRM_Q = {"key": "onay", "q": "Böyle başlayayım mı?",
             "options": ["Evet, böyle başla", "Evet ama önerilerini çıkar", "Birkaç şeyi değiştirmek istiyorum"]}
CONFIRM_INTRO = "Her şeyi doğru anladım mı? Onaylarsan bundan sonra soru sormam; projeyi baştan sona ben götürürüm."
APPROVE_RE = re.compile(r"^\s*(evet|tamam|ok|okey|olur|başla|basla|aynen|onay|devam|hadi|yap)\b", re.I)


def _pref_items(answers):
    return {k: v for k, v in (answers or {}).items()
            if any(t in str(k).lower() for t in PREF_KEYS) and str(v).strip()}


def _collect_answers(history):
    out = {}
    for m in history:
        c = str(m.get("content", ""))
        if m.get("role") == "user" and c.startswith("Cevaplarım: "):
            try:
                out.update(json.loads(c[len("Cevaplarım: "):]))
            except Exception:
                pass
    return out


def _marks(history, mark):
    out = []
    for m in history:
        c = str(m.get("content", ""))
        if m.get("role") == "assistant" and c.startswith(mark):
            try:
                out.append(json.loads(c[len(mark):]))
            except Exception:
                out.append({})
    return out


def _is_approval(answers, task_text):
    if answers:
        return str(answers.get("onay", "")).strip().lower().startswith("evet")
    return bool(APPROVE_RE.match(task_text or ""))


def _brief_text(b):
    lines = [f"**Proje özeti — {b.get('title') or 'proje'}**",
             f"Amaç: {b.get('goal', '')}"]
    if b.get("audience"):
        lines.append(f"Kimin için: {b['audience']}")
    if b.get("pages"):
        lines.append("Sayfalar: " + ", ".join(b["pages"]))
    if b.get("features"):
        lines.append("Özellikler:")
        lines += [f"- {f}" for f in b["features"]]
    if b.get("look"):
        lines.append(f"Görünüm: {b['look']}")
    if b.get("behavior"):
        lines.append("Davranış:")
        lines += [f"- {x}" for x in b["behavior"]]
    if b.get("language"):
        lines.append(f"Dil: {b['language']}")
    if b.get("out_of_scope"):
        lines.append("Kapsam dışı: " + ", ".join(b["out_of_scope"]))
    return "\n".join(lines)


DET_RULES = [
    (r"sakla|localstorage|kalsın|kaydet", r"localStorage|sessionStorage|indexedDB"),
    (r"mavi", r"blue|#0{0,2}[0-9a-f]{0,2}ff\b|#1e40af|#2563eb|#3b82f6|rgb\(\s*\d+,\s*\d+,\s*2[0-9]{2}"),
    (r"\bmor\b", r"purple|violet|#7c3aed|#8b5cf6|#a855f7|#6d28d9|#9333ea"),
    (r"yeşil|yesil", r"green|#16a34a|#22c55e|#10b981|#15803d"),
    (r"kırmızı|kirmizi", r"\bred\b|#dc2626|#ef4444|#b91c1c|crimson"),
    (r"koyu tema|karanlık|dark", r"#0[0-9a-f]{5}|#1[0-9a-f]{5}|dark|#111|#222"),
    (r"türkçe|turkce", r"[çğışöüÇĞİŞÖÜ]"),
    (r"klavye|kısayol", r"keydown|keyup|keypress"),
    (r"sıfırla|sifirla", r"sıfırla|sifirla|reset"),
    (r"azalt", r"azalt|decrement|-\s*1|--"),
]


def _det_check(item, code):
    """Belirli maddeler için deterministik kontrol: True/False, bilinmiyorsa None."""
    low = item.lower()
    for need, pat in DET_RULES:
        if re.search(need, low):
            return bool(re.search(pat, code, re.I))
    return None


def _brief_items(b):
    items = list(b.get("features") or []) + list(b.get("behavior") or [])
    if b.get("look"):
        items.append("görünüm: " + b["look"])
    if b.get("language"):
        items.append("dil: " + b["language"])
    return [str(x).strip() for x in items if str(x).strip()][:12]


SKELETON = {
    "web": [
        {"key": "isim", "q": "Sitenin / işletmenin adı ne olsun? (yaz ya da seç)", "options": ["sen uydur"]},
        {"key": "sayfalar", "q": "Hangi sayfalar olsun?",
         "options": ["tek sayfa (her şey alt alta)", "Hakkımızda", "Hizmetler / Menü / Ürünler", "Galeri", "İletişim", "Blog"],
         "multi": True},
        {"key": "renk", "q": "Renk / tema nasıl olsun?", "options": ["mavi", "mor", "yeşil", "koyu tema", "sen seç"]},
        {"key": "stil", "q": "Görünüm tarzı?", "options": ["sade ve modern", "büyük ve belirgin", "renkli / eğlenceli", "lüks / şık", "sen seç"]},
        {"key": "iletisim", "q": "Ziyaretçi sana nasıl ulaşsın?",
         "options": ["iletişim formu", "WhatsApp butonu", "telefon + e-posta", "gerekmez"], "multi": True},
    ],
    "python": [
        {"key": "kullanim", "q": "Nasıl kullanılacak?", "options": ["fonksiyon olarak", "komut satırından", "ikisi de"]},
        {"key": "hata", "q": "Hatalı girişte ne olsun?", "options": ["anlaşılır hata mesajı", "varsayılan değer kullan", "sen seç"]},
    ],
}
SKELETON["other"] = SKELETON["web"][:2]
FINAL_Q = {"key": "ek", "q": "Eklemek istediğin başka bir detay var mı? (yoksa 'yok' seç)",
           "options": ["yok, bu kadar"]}


def _skeleton(kind, orig_req, all_answers, prefs, feature_q):
    """1. tur: modelin özellik sorusu + garanti kapsama sağlayan sabit sorular."""
    out = []
    if feature_q and feature_q.get("options"):
        out.append({"key": "ozellik", "q": feature_q.get("q") or "Hangi özellikler olsun? (birden fazla seçebilirsin)",
                    "options": [str(o) for o in feature_q["options"]][:5], "multi": True})
    for q in SKELETON.get(kind, SKELETON["other"]):
        if q["key"] in all_answers or q["key"] in prefs:
            continue
        out.append(dict(q))
    return _filter_questions(out, orig_req, prefs)[:6]


def _discover(orig_req, all_answers, prefs, kind, rnd, prev_qs, cancel):
    """Keşif turu: henüz netleşmemiş başlıklar için somut seçenekli sorular."""
    n = {1: "3-4", 2: "1-3"}.get(rnd, "1-2")
    asked = "; ".join(q.get("q", "") for q in prev_qs) or "—"
    plan_hint = (
        "1. turda mutlaka şunlar olsun: (a) ana işlev/özellikler (bu TÜR istek için gerçekçi seçenekler, "
        "ör. sayaç → 'artır-azalt-sıfırla / sadece artır / birden çok sayaç'), (b) görünüm (renk/tema), "
        "(c) ekstra davranış (ör. 'sayfa yenilenince değer kalsın / ses-animasyon / klavye kısayolu'). "
        "Basit bir uygulama için 'kimin için' ya da 'amaç' sorma; isteğin ne olduğu zaten belli."
        if rnd == 1 else
        "2. tur: bu TÜR projeye özgü, henüz sorulmamış 2 DERİN detay sorusu üret (ör. sayaç → başlangıç değeri, "
        "artış adımı, klavye kısayolu, ses/animasyon; yapılacaklar → öncelik, bitiş tarihi, kategori; oyun → zorluk, "
        "skor kaydı). Zaten cevaplanan ya da önceki tur sorulan konuları (özellik, renk, görünüm, veri saklama) "
        "yeniden sorma. complete=false." if rnd == 2 else
        "Sadece hâlâ boş kalan başlıkları sor; önceki cevapları yeniden sorma. Her şey netse questions boş, complete=true.")
    try:
        r = brain.chat([{"role": "user", "content": (
            "Sen kıdemli bir ürün geliştiricisisin; müşterinin hayalini eksiksiz çıkarmak için keşif görüşmesi yapıyorsun. "
            "İsteği olduğu gibi anla (ör. 'sayaç' = tıklayınca artan sayı; yanlış yorumlama).\n"
            f"Müşterinin isteği: {orig_req}\n"
            f"Şimdiye kadar aldığın cevaplar: {json.dumps(all_answers, ensure_ascii=False) or '—'}\n"
            f"Bilinen kalıcı tercihleri: {json.dumps(prefs, ensure_ascii=False) or '—'}\n"
            f"Daha önce sorduğun sorular (TEKRAR SORMA): {asked}\n"
            f"Netleşmesi gereken başlıklar: {'; '.join(COVERAGE.get(kind, COVERAGE['other']))}\n"
            f"Bu {rnd}. tur. {plan_hint}\n"
            f"{n} Türkçe soru üret; her soruya 3-4 kısa, somut, birbirinden farklı ve bu isteğe uygun seçenek yaz "
            "(müşteri dilinde, teknik jargon yok; son seçenek 'sen seç' olabilir). "
            "key: kısa ascii anahtar (ozellik, renk, davranis...). SADECE JSON.")}],
            schema=DISCOVER_SCHEMA, max_tokens=600, temperature=0.3, cancel=cancel, model=brain.FAST_MODEL) or {}
    except Exception:
        return []
    if r.get("complete") and rnd > 2:
        return []
    qs = _filter_questions(r.get("questions"), orig_req, prefs)
    seen = {q.get("q", "").strip().lower() for q in prev_qs}
    qs = [q for q in qs if q.get("q", "").strip().lower() not in seen and (q.get("options") or [])]
    return qs[:4]


_TRMAP = str.maketrans("çşığöüÇŞİĞÖÜ", "csigoucsigou")


def _tr(x):
    return str(x).lower().translate(_TRMAP)


def _make_brief(orig_req, all_answers, prefs, kind, cancel, prev=None, change=""):
    ctx = (f"Müşterinin isteği: {orig_req}\n"
           f"Keşif cevapları: {json.dumps(all_answers, ensure_ascii=False) or '—'}\n"
           f"Kalıcı tercihleri: {json.dumps(prefs, ensure_ascii=False) or '—'}\n")
    if prev:
        ctx += (f"Önceki özet: {json.dumps(prev, ensure_ascii=False)}\n"
                f"Müşterinin değişiklik isteği: {change}\nSADECE istenen değişikliği uygula, kalanı koru.\n")
    try:
        b = brain.chat([{"role": "user", "content": ctx + (
            "Bunlardan kıdemli bir geliştirici gibi net bir proje özeti çıkar. KURAL: müşterinin söylemediği renk, "
            "özellik veya amaç UYDURMA; 'sen seç' dediği yerde makul bir seçim yap ve '(senin için seçtim)' diye belirt; "
            "isteği olduğu gibi anla (sayaç = tıklayınca artan sayı). Türkçe, kısa ve somut: "
            "title (site/işletme adı; 'sen uydur' dediyse işe uygun özgün bir ad seç), goal (1 cümle, isteği olduğu gibi anlat), audience (kısa), "
            "pages (sayfalar; 'tek sayfa' dediyse ['Ana sayfa'] ve bölümleri features'a yaz: ör. 'Hero', 'Menü bölümü'), "
            "features (3-7 madde: SADECE kullanıcının yapabileceği işlevler — renk/saklama gibi şeyler buraya girmez; "
            "müşterinin söylediği her işlev mutlaka olsun, söylemediği abartılı şeyler ekleme), "
            "look (renk/tema/stil/rakam boyutu tek cümle), behavior (2-4 madde: veri saklama, hatalı giriş, "
            "telefon uyumu gibi somut davranışlar), language, out_of_scope (bu sürümde bilerek yapılmayacak "
            "1-3 gerçekçi şey, ör. 'giriş sistemi', 'bulut senkronu'). SADECE JSON.")}],
            schema=BRIEF_SCHEMA, max_tokens=700, temperature=0.2, cancel=cancel, model=brain.FAST_MODEL) or {}
    except Exception:
        b = {}
    if not b.get("features"):
        b = {"title": orig_req[:50], "goal": orig_req, "features": list(all_answers.values())[:6] or [orig_req],
             "look": all_answers.get("renk", "") or prefs.get("renk", "") or "sade, modern", "language": "Türkçe"}
    b["kind"] = kind
    look = [str(all_answers.get(k, "")).strip() for k in ("renk", "stil") if str(all_answers.get(k, "")).strip()
            and "sen seç" not in str(all_answers.get(k, "")).lower()]
    if look:
        b["look"] = ", ".join(look)
    ozl = str(all_answers.get("ozellik", "")).strip()
    user_feats = [x.strip() for x in re.split(r"\s*\+\s*|\s*,\s*|\s*/\s*", ozl) if x.strip()] if ozl else []
    said = _tr(orig_req + " " + " ".join(str(v) for v in all_answers.values()))
    def _said(f):
        toks = [t for t in re.findall(r"\w+", _tr(f)) if len(t) >= 4]
        return bool(toks) and sum(t[:5] in said for t in toks) >= max(1, len(toks) // 2)
    utoks = set(t for u in user_feats for t in re.findall(r"\w+", _tr(u)))
    ustems = {t[:5] for t in utoks if len(t) >= 4}
    extras = [f for f in (b.get("features") or [])
              if f.lower() not in [u.lower() for u in user_feats] and not _said(f)
              and not any(t[:5] in ustems for t in re.findall(r"\w+", _tr(f)) if len(t) >= 4)]
    kept = [f for f in (b.get("features") or []) if f not in extras
            and not (len(re.findall(r"\w+", f)) > 1
                     and set(re.findall(r"\w+", _tr(f))) <= utoks)
            and not any(t[:5] in ustems for t in re.findall(r"\w+", _tr(f)) if len(t) >= 4)]
    ftoks = {t[:5] for f in kept + user_feats for t in re.findall(r"\w+", _tr(f)) if len(t) >= 4}
    b["out_of_scope"] = [o for o in (b.get("out_of_scope") or [])
                         if "arayüz" not in str(o).lower() and "tasarım" not in str(o).lower()
                         and not any(t[:5] in ftoks for t in re.findall(r"\w+", str(o).lower()) if len(t) >= 4)][:3]
    b["features"] = user_feats + [f for f in kept if f.lower() not in [u.lower() for u in user_feats]] \
        + [f + " (önerim)" for f in extras[:2]]
    if not b["features"]:
        b["features"] = [orig_req]
    ek = str(all_answers.get("ek", "")).strip()
    if ek and not ek.lower().startswith("yok") and ek.lower() not in json.dumps(b, ensure_ascii=False).lower():
        b.setdefault("behavior", []).append(ek)
    veri = str(all_answers.get("veri", "")).lower()
    if veri.startswith("kalsın") and "sakla" not in json.dumps(b.get("behavior") or [], ensure_ascii=False).lower():
        b.setdefault("behavior", []).append("Veriler tarayıcıda (localStorage) saklanır")
    return b


def _filter_questions(qs, task_text, prefs):
    """Cevabı zaten istekte ya da tercihlerde olan soruları ele."""
    low = task_text.lower()
    known = " ".join(str(v).lower() for v in (prefs or {}).values())
    out = []
    for q in qs or []:
        if not q.get("q"):
            continue
        opts = [str(o).lower() for o in (q.get("options") or []) if str(o).strip()]
        if any(len(o) >= 3 and o in low for o in opts):
            continue
        if str(q.get("key", "")).lower() in (prefs or {}) and any(o in known for o in opts):
            continue
        out.append(q)
    return out[:3]


def _project_files(workdir):
    """Çalışma alanındaki kaynak dosyalar (gizli/klasör dışı), en fazla 3."""
    out = []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith(".") or not f.endswith((".html", ".py", ".js", ".css")):
                continue
            out.append(os.path.relpath(os.path.join(root, f), workdir))
    return sorted(out)[:3]


DEFAULT_STEPS = {
    "web": ["karanlık mod ekle", "mobil görünümü iyileştir", "verileri tarayıcıda sakla"],
    "python": ["hatalı girişleri yakala", "komut satırı arayüzü ekle", "daha fazla test ekle"],
}


VERBISH = ("ekle", "yap", "koy", "göster", "kaydet", "iyileştir", "düzelt", "değiştir", "sırala",
           "filtrele", "ayır", "destek", "mod", "animasyon", "büyüt", "küçült", "renk", "ara", "sil",
           "kullan", "çevir", "tut", "say", "hesapla", "paylaş", "yazdır", "indir", "oyna", "sesli")


def _next_steps(history, kind, cancel=None):
    """Bitmiş proje için 3 somut 'sonraki adım' önerisi (emir kipinde, kısa)."""
    try:
        sg = brain.chat(history + [{"role": "user", "content": (
            "Az önce bitirdiğin bu projeyi kullanıcı bir adım ileri taşımak istese ne isteyebilir? "
            "3 kısa, somut, EMİR kipinde Türkçe geliştirme önerisi yaz (2-6 kelime; her biri bir özellik "
            "eklemeli ya da bir şeyi değiştirmeli). Örnek biçim: 'karanlık mod ekle', 'görevleri tarihe göre sırala'. "
            "SADECE JSON: {\"items\": [\"...\", \"...\", \"...\"]}")}],
            schema=SUGGEST_SCHEMA, max_tokens=150, temperature=0.4, cancel=cancel, model=brain.FAST_MODEL) or {}
        items = [x.strip().rstrip(".").lower() for x in (sg.get("items") or []) if isinstance(x, str) and x.strip()]
        items = [x for x in items if 6 <= len(x) <= 60 and " " in x and any(v in x for v in VERBISH)][:3]
    except Exception:
        items = []
    defaults = DEFAULT_STEPS.get(kind, DEFAULT_STEPS["web"])
    return (items + [d for d in defaults if d not in items])[:3]


def agent_task2(task_text, workdir, on_event=None, cancel=None, sandbox=None,
                history=None, prefs=None, answers=None, user=""):
    """Bir konuşma turunu çalıştır. Soru sorulursa {"waiting": True} döner."""
    ws = Workspace(workdir, runner=sandbox)
    history = list(history or [])
    prefs = dict(prefs or {})
    trace = []

    def emit(t, **kw):
        ev = {"t": t, **kw}
        trace.append(ev)
        if on_event:
            on_event(ev)

    def stopped():
        return cancel is not None and cancel.is_set()

    def model_text(text):
        emit("model", text=text)

    emit("start", task=task_text)
    history.append({"role": "user", "content": task_text})
    if answers:
        history.append({"role": "user", "content":
                        "Cevaplarım: " + json.dumps(answers, ensure_ascii=False)})
        prefs.update(_pref_items(answers))

    # ---------- 1. anla + keşif + özet ----------
    brain.TLS.on_wait = lambda n: emit(
        "phase", name="sira",
        text=f"model şu an başka bir görevle meşgul, sırada bekliyorum ({n} görev önde)")
    emit("phase", name="anla", text="isteği anlıyorum")
    pref_txt = ("Kullanıcının bilinen tercihleri (bunları tekrar sorma): "
                + json.dumps(prefs, ensure_ascii=False)) if prefs else ""
    orig_req = next((m["content"] for m in history if m["role"] == "user"
                     and not m["content"].startswith("Cevaplarım:")), task_text)
    asks = _marks(history, ASK_MARK)
    briefs = _marks(history, BRIEF_MARK)
    all_answers = _collect_answers(history)
    asked_before = bool(asks or briefs)
    prev_qs = [q for a in asks for q in (a.get("questions") if isinstance(a, dict) else a or [])]

    done_before = any(m.get("role") == "assistant" and
                      str(m.get("content", "")).startswith(("**Hazır.**", "**Bitti"))
                      for m in history[:-1])
    existing = _project_files(workdir)
    revision = bool(done_before and existing and not answers)

    brief = briefs[-1] if (revision and briefs) else None
    summary = task_text
    questions = []
    if revision:
        kind = "web" if any(p.endswith(".html") for p in existing) else "python"
    elif briefs:
        kind = briefs[-1].get("kind", "web")
    elif asks and isinstance(asks[-1], dict) and asks[-1].get("kind"):
        kind = asks[-1]["kind"]
    else:
        try:
            u = brain.chat(history + [{"role": "user", "content": (
                "Yukarıdaki isteği değerlendir ve SADECE JSON ver.\n"
                "kind: 'chat' SADECE selamlaşma veya bilgi sorusuysa (bir şey yapılması/yazılması isteniyorsa asla chat değil), "
                "'web' (HTML/CSS/JS sayfa-uygulama), 'python' (python kodu/fonksiyon), 'other'.\n"
                "clear: istek üretime başlayacak kadar net mi? "
                "questions: web/python ise TAM 1 soru: key 'ozellik', q 'Hangi özellikler olsun?', options: bu istek için "
                "3-4 gerçekçi özellik seti (kısa, Türkçe, birbirinden farklı; ör. sayaç → 'artır / azalt / sıfırla', "
                "'sadece artır', 'birden çok sayaç'). chat ise boş liste. "
                + pref_txt)}],
                schema=UNDERSTAND_SCHEMA, max_tokens=500, cancel=cancel, model=brain.FAST_MODEL)
        except Exception as e:
            emit("error", text=f"beyin servisi: {e}")
            emit("done", ok=False, reason="model_error")
            return {"ok": False, "trace": trace, "history": history, "prefs": prefs}
        kind = _guess_kind(task_text, u.get("kind", "other"))
        if kind == "python":
            emit("answer", text=("coder-1 yalnızca **web sitesi** yapar (tanıtım sitesi, portföy, kafe/restoran, "
                                 "mağaza vitrini, blog, küçük web uygulaması). Bu isteği bir web sayfası olarak "
                                 "yapabilirim — istersen 'bunu web sitesi olarak yap' de, hemen sorularla başlayayım."))
            emit("done", ok=True, steps=1)
            return {"ok": True, "trace": trace, "history": history, "prefs": prefs}
        summary = u.get("summary") or task_text
        questions = _filter_questions(u.get("questions"), task_text, prefs)
    if stopped():
        emit("done", ok=False, reason="stopped")
        return {"ok": False, "stopped": True, "trace": trace, "history": history, "prefs": prefs}

    if kind == "chat" and not revision:
        acc = []
        reply = brain.chat(history + [{"role": "user", "content":
                           "Kısa, samimi ve Türkçe cevap ver. Kod üretme; gerekirse ne yapabileceğini bir cümleyle söyle."}],
                           max_tokens=300, temperature=0.6, cancel=cancel,
                           on_delta=lambda d: acc.append(d))
        emit("answer", text=reply.strip())
        history.append({"role": "assistant", "content": reply.strip()})
        emit("done", ok=True, steps=1)
        return {"ok": True, "answer": reply, "trace": trace, "history": history, "prefs": prefs}

    def ask_confirm(b):
        emit("brief", text=_brief_text(b), brief=b)
        emit("ask", questions=[CONFIRM_Q], intro=CONFIRM_INTRO, summary=b.get("title", ""))
        history.append({"role": "assistant", "content": BRIEF_MARK + json.dumps(b, ensure_ascii=False)})
        return {"ok": True, "waiting": True, "trace": trace, "history": history, "prefs": prefs}

    if not revision:
        if briefs:
            brief = briefs[-1]
            onay_txt = str((answers or {}).get("onay", "")).strip().lower()
            if onay_txt.startswith("evet ama"):
                brief = dict(brief)
                brief["features"] = [f for f in brief.get("features") or [] if "(önerim)" not in f]
                history.append({"role": "assistant", "content": BRIEF_MARK + json.dumps(brief, ensure_ascii=False)})
                emit("answer", text="Önerilerimi çıkardım; sadece senin dediklerinle, soru sormadan başlıyorum.")
            elif not _is_approval(answers, task_text):
                cikar = str((answers or {}).get("cikar", "")).strip()
                serbest = " ".join(str(v) for k, v in (answers or {}).items()
                                   if k not in ("onay", "cikar") and str(v).strip()) if answers else task_text
                if not answers or (not cikar and (not serbest.strip() or serbest.strip().startswith("Birkaç şeyi"))):
                    items = _brief_items(brief)[:8]
                    emit("ask", questions=[
                        {"key": "cikar", "q": "Çıkarmak istediğin maddeler (tıkla):", "options": items, "multi": True},
                        {"key": "degisiklik", "q": "Eklenecek / değişecek bir şey varsa yaz:", "options": []}],
                        intro="Tamam, özeti birlikte düzeltelim.", summary=brief.get("title", ""))
                    return {"ok": True, "waiting": True, "trace": trace, "history": history, "prefs": prefs}
                change = ((f"ŞUNLARI ÇIKAR: {cikar}. " if cikar else "") + serbest).strip()
                brief = _make_brief(orig_req, all_answers, prefs, kind, cancel, prev=brief, change=change)
                if cikar:
                    drop = [x.strip().lower() for x in cikar.split(" + ")]
                    for fld in ("features", "behavior"):
                        brief[fld] = [f for f in brief.get(fld) or [] if f.strip().lower() not in drop]
                if len(briefs) < MAX_BRIEF_EDITS:
                    return ask_confirm(brief)
                emit("answer", text="Değişikliği özete işledim; artık soru sormadan başlıyorum.")
            else:
                emit("answer", text="Özet onaylandı — bundan sonra soru yok, projeyi baştan sona ben götürüyorum.")
        else:
            rnd = len(asks) + 1
            very_detailed = rnd == 1 and _detail_score(orig_req) >= 5 and not questions
            if rnd == 1 and not very_detailed:
                feat = next((q for q in questions if q.get("key") == "ozellik"), questions[0] if questions else None)
                questions = _skeleton(kind, orig_req, all_answers, prefs, feat)
            elif rnd == 2 and not very_detailed:
                prev_qs = [q for a in asks for q in (a.get("questions") or [])]
                deep = _discover(orig_req, all_answers, prefs, kind, 2, prev_qs, cancel)
                known = set(all_answers) | set(prefs) | {"ozellik", "renk", "stil", "veri", "ek", "onay"}
                prev_txt = " ".join(q.get("q", "") for q in prev_qs).lower() + " " + " ".join(
                    str(v) for v in all_answers.values()).lower()
                TOPIC = ("görünüm", "tarz", "renk", "tema", "özellik", "stil", "yenilen", "sakla")
                def _dup(q):
                    ql = q.get("q", "").lower()
                    if any(t in ql and t in prev_txt for t in TOPIC):
                        return True
                    return any(str(o).strip().lower() in prev_txt for o in (q.get("options") or [])[:3]
                               if len(str(o).strip()) > 3)
                deep = [q for q in deep if q.get("key") not in known and not _dup(q)][:2]
                questions = deep + [FINAL_Q]
            else:
                questions = []
            if questions:
                emit("ask", questions=questions, summary=summary,
                     intro=("Tam hayalindeki gibi olsun diye birkaç soru:" if rnd == 1
                            else f"Birkaç detay daha ({rnd}. tur), sonra özeti onayına sunacağım:"))
                history.append({"role": "assistant", "content":
                                ASK_MARK + json.dumps({"kind": kind, "questions": questions}, ensure_ascii=False)})
                return {"ok": True, "waiting": True, "trace": trace, "history": history, "prefs": prefs}
            emit("phase", name="anla", text="proje özetini hazırlıyorum")
            brief = _make_brief(orig_req, all_answers, prefs, kind, cancel)
            return ask_confirm(brief)
        summary = _brief_text(brief)
        history.append({"role": "assistant", "content": "Onaylı özet:\n" + summary})

    if revision:
        # ---------- 2b. düzenle ----------
        emit("phase", name="duzenle", text="mevcut projeyi düzenliyorum")
        plan = {"title": "Düzenleme: " + task_text[:70]}
        steps = [f"{p} dosyasında isteği uygula" for p in existing[:3]]
        for i, st in enumerate(steps):
            emit("todo", i=i, state="todo", text=st)
        written = {}
        for i, path in enumerate(existing[:3]):
            if stopped():
                break
            emit("todo", i=i, state="active", text=steps[i])
            cur = ws.read(path, max_chars=7000)
            emit("tool", call={"name": "edit_file", "args": {"path": path}})
            t0 = time.time()
            txt = brain.chat(history + [{"role": "user", "content": (
                f"Kullanıcının değişiklik isteği: {task_text}\n" + (pref_txt + "\n" if pref_txt else "")
                + f"Mevcut '{path}' dosyası:\n```\n{cur}\n```\n"
                "SADECE bu isteği uygula, geri kalan her şeyi aynen koru. İstenen her öğe kodda somut "
                "olarak bulunsun (ör. 'kutucuk' → <input type=\"checkbox\">, 'kırmızı' → gerçek kırmızı renk). "
                "Dosyanın TAM yeni halini tek kod bloğunda ver, açıklama yazma.")}],
                max_tokens=1800, temperature=0.2, cancel=cancel)
            new = _code_of(txt)
            if new.strip() and new.strip() != cur.strip():
                ws.write(path, new)
                written[path] = new
                emit("file", path=path, op="edit")
                emit("tool_result", name="edit_file", out=f"düzenlendi: {path} ({len(new)} karakter)",
                     dur_ms=int((time.time() - t0) * 1000))
            else:
                emit("tool_result", name="edit_file", out=f"değişiklik gerekmedi: {path}")
            emit("todo", i=i, state="done", text=steps[i])
        if written:
            try:
                d = ws.diff()
                if d and d.strip():
                    emit("tool", call={"name": "run_command", "args": {"cmd": "git diff"}})
                    emit("tool_result", name="run_command", out=d[:2000])
            except Exception:
                pass
            if brief:
                brief = dict(brief)
                brief["behavior"] = list(brief.get("behavior") or []) + [task_text.strip()[:120]]
                history.append({"role": "assistant", "content": BRIEF_MARK + json.dumps(brief, ensure_ascii=False)})
                emit("tool_result", name="edit_file", out="özet güncellendi: bu değişiklik de artık projenin maddesi")
    else:
        # ---------- 2. planla ----------
        emit("phase", name="planla", text="planlıyorum")
        plan = brain.chat(history + [{"role": "user", "content": (
            f"Onaylı proje özeti:\n{summary}\nBu özetin TAMAMINI karşılayan, çalışan bir proje planla ve SADECE JSON ver. "
            "files: " + ("index.html + özetteki her ek sayfa için ayrı .html (ör. hakkimizda.html, iletisim.html) "
                         "+ ortak style.css + script.js (en fazla 6 dosya; tek sayfalık site ise sadece index.html + style.css + script.js)" if kind == "web" else
                         "tek bir .py dosyası (içinde doctest)" if kind == "python" else "en fazla 3 dosya")
            + ". İstenmeyen ekstra dosya/arayüz ekleme. steps: 3-5 kısa adım. tests: nasıl doğrulanacağı (2-4 madde).")}],
            schema=PLAN_SCHEMA, max_tokens=600, cancel=cancel, model=brain.FAST_MODEL) or {}
        files = [f for f in (plan.get("files") or []) if f.get("path")][:6]
        if kind == "python":
            files = [f for f in files if f["path"].endswith(".py")][:1]
        elif kind == "web":
            files = [f for f in files if f["path"].endswith((".html", ".css", ".js"))][:6]
            if not any(f["path"] == "index.html" for f in files):
                files = [{"path": "index.html", "purpose": "ana sayfa"}] + [f for f in files if not f["path"].endswith(".html")]
            files.sort(key=lambda f: (0 if f["path"] == "index.html" else 1 if f["path"].endswith(".html") else 2))
        if not files:
            files = [{"path": "index.html" if kind == "web" else "main.py", "purpose": summary}]
        steps = plan.get("steps") or [f"{f['path']} dosyasını yaz" for f in files]
        for i, s in enumerate(steps):
            emit("todo", i=i, state="todo", text=s)
        model_text(f"Plan: {plan.get('title') or summary}")

        # ---------- 3. yaz ----------
        emit("phase", name="yaz", text="kodu yazıyorum")
        written = {}
        for i, f in enumerate(files):
            if stopped():
                break
            path = f["path"].lstrip("./")
            emit("todo", i=min(i, len(steps) - 1), state="active", text=steps[min(i, len(steps) - 1)])
            emit("tool", call={"name": "write_file", "args": {"path": path}})
            ctx = "".join(f"\n--- {p} ---\n{c[:3000]}\n" for p, c in written.items())
            ask = (f"Onaylı proje özeti:\n{summary}\n" + (pref_txt + "\n" if pref_txt else "")
                   + f"Şimdi '{path}' dosyasının TAM içeriğini yaz ({f.get('purpose','')}). "
                   "Tek bir kod bloğu içinde ver, açıklama yazma. Özetteki HER özellik ve detay "
                   "(renk, özellik, davranış, dil) kodda somut olarak bulunsun; kıdemli bir geliştirici gibi "
                   "temiz, çalışır, eksiksiz kod yaz; yer tutucu veya TODO yok."
                   + ((" Sayfa responsive olsun (telefon ve masaüstü). Gerçek, işe uygun Türkçe içerik yaz (lorem ipsum, 'örnek metin', "
                       "sahte telefon/adres yerine özetle tutarlı metin); işletme adı özetteki başlık. "
                       + ("Ortak CSS için <link rel='stylesheet' href='style.css'>, JS için <script src='script.js'></script> kullan; "
                          "diğer sayfalara giden gezinme menüsü olsun (" + ", ".join(sorted(fp for fp in [x['path'] for x in files] if fp.endswith('.html'))) + ")."
                          if any(x["path"].endswith((".css", ".js")) for x in files) or sum(x["path"].endswith(".html") for x in files) > 1
                          else "Tüm CSS ve JS aynı dosyada olsun.")) if path.endswith(".html") else "")
                   + (" Bu dosya diğer sayfaların kullandığı ortak dosya; HTML'deki sınıf/id adlarıyla birebir uyumlu olsun." if path.endswith((".css", ".js")) else "")
                   + (" Fonksiyonlara doctest ekle ve dosya sonuna `if __name__ == '__main__': import doctest; doctest.testmod()` koy." if path.endswith(".py") else "")
                   + (f"\nÖnceki dosyalar:{ctx}" if ctx else ""))
            acc = []
            t0 = time.time()
            txt = brain.chat(history + [{"role": "user", "content": ask}], max_tokens=1800,
                             temperature=0.3, cancel=cancel,
                             on_delta=lambda d: acc.append(d))
            content = _code_of(txt)
            ws.write(path, content)
            written[path] = content
            emit("file", path=path, op="write")
            emit("tool_result", name="write_file", out=f"yazıldı: {path} ({len(content)} karakter)",
                 dur_ms=int((time.time() - t0) * 1000))
            emit("todo", i=min(i, len(steps) - 1), state="done", text=steps[min(i, len(steps) - 1)])

    # ---------- 4. doğrula + onar ----------
    def _runtime(path):
        """Gerçek tarayıcıda aç, butonlara tıkla; hataları döndür, ekran görüntüsü yayınla."""
        if not path.endswith(".html"):
            return []
        emit("tool", call={"name": "run_command", "args": {"cmd": f"browser-test {path}"}})
        rc = runtime_check.check_html(os.path.join(workdir, path), workdir)
        if rc is None:
            emit("tool_result", name="run_command", out="tarayıcı testi: ortam yok, atlandı")
            return []
        if rc["shot"]:
            emit("shot", path=runtime_check.SHOT_NAME)
        if rc["ok"] and rc.get("interactive", 1) == 0 and kind == "web":
            rc["ok"] = False
            rc["errors"].append("sayfada hiç etkileşimli öğe yok (buton / giriş alanı / form) — uygulama işlevsiz; "
                                "özetteki özellikleri gerçekten çalışan buton ve alanlarla ekle")
        if rc["ok"]:
            emit("tool_result", name="run_command",
                 out=f"tarayıcıda açıldı · {rc['clicked']} buton denendi · çalışma hatası yok")
            return []
        emit("tool_result", name="run_command", out="tarayıcı testi:\n" + "\n".join(rc["errors"]))
        return [e for e in rc["errors"] if not e.startswith("tarayıcı testi")]

    emit("phase", name="dogrula", text="test ediyorum")
    tested = []
    must = _user_keywords(orig_req + " " + " ".join(str(v) for v in all_answers.values()), answers)
    for path in list(written):
        for rnd in range(MAX_REPAIR + 1):
            if stopped():
                break
            content = written[path]
            problems = []
            if path.endswith(".html"):
                probs, scripts = _check_html(content, must)
                hard = [x for x in probs if not x.startswith("kullanıcının")]
                notes = [x for x in probs if x.startswith("kullanıcının")]
                problems += hard
                emit("tool", call={"name": "run_command", "args": {"cmd": f"html-check {path}"}})
                js = _check_js(scripts, ws)
                problems += js
                emit("tool_result", name="run_command",
                     out="HTML yapısı " + ("OK" if not hard else "; ".join(hard))
                     + (" · JS sözdizimi " + ("OK" if not js else "HATA\n" + "\n".join(js)) if scripts else "")
                     + ("\nnot: " + "; ".join(notes) if notes else ""))
                if not problems:
                    problems += _runtime(path)
            elif path.endswith(".js"):
                emit("tool", call={"name": "run_command", "args": {"cmd": f"node --check {path}"}})
                js = _check_js([content], ws)
                problems += js
                emit("tool_result", name="run_command", out="JS sözdizimi " + ("OK" if not js else "HATA\n" + "\n".join(js)))
            elif path.endswith(".py"):
                cmd = f"python3 -m py_compile {path} && python3 -m doctest {path} && echo DOCTEST_OK"
                emit("tool", call={"name": "run_command", "args": {"cmd": cmd}})
                out = ws.run(cmd, timeout=60, cancel=cancel)
                emit("tool_result", name="run_command", out=out[:2000])
                if "DOCTEST_OK" not in out:
                    problems.append(out[:1500])
            if not problems:
                tested.append(path)
                break
            if rnd == MAX_REPAIR:
                emit("error", text=f"{path}: onarım denemeleri bitti — {problems[0][:200]}")
                break
            emit("phase", name="onar", text=f"{path} hatasını düzeltiyorum ({rnd + 1}/{MAX_REPAIR})")
            emit("tool", call={"name": "edit_file", "args": {"path": path}})
            fix = brain.chat(history + [{"role": "user", "content": (
                f"'{path}' dosyasında şu sorunlar var:\n" + "\n".join(problems)
                + f"\n\nMevcut dosya:\n```\n{content[:6000]}\n```\n"
                "Sorunları gider ve dosyanın TAM düzeltilmiş halini tek kod bloğunda ver.")}],
                max_tokens=1800, temperature=0.2, cancel=cancel)
            written[path] = _code_of(fix)
            ws.write(path, written[path])
            emit("file", path=path, op="edit")
            emit("tool_result", name="edit_file", out=f"düzeltildi: {path}")

    # ---------- 4b. öz-denetim: özetteki her madde kodda var mı ----------
    checklist = []
    if brief and written and not stopped():
        emit("phase", name="kontrol", text="özetteki her maddeyi kodda kontrol ediyorum")
        items = _brief_items(brief)
        code_ctx = "".join(f"\n--- {p} ---\n{c[:5000]}\n" for p, c in written.items())
        try:
            rv = brain.chat([{"role": "user", "content": (
                "Gereksinimler:\n" + "\n".join(f"- {x}" for x in items)
                + f"\n\nKod:{code_ctx}\nHer gereksinimin kodda SOMUT karşılığı var mı? "
                "Kesinlikle eksik olanları gereksinimin tam metniyle listele; şüphelileri ekleme. SADECE JSON.")}],
                schema=REVIEW_SCHEMA, max_tokens=300, temperature=0.1, cancel=cancel, model=brain.FAST_MODEL) or {}
            missing = [str(m).strip() for m in (rv.get("missing") or []) if str(m).strip()][:4]
        except Exception:
            missing = []
        allcode = "\n".join(written.values())
        low_items = {x.lower(): x for x in items}
        missing = [low_items.get(m.lower(), m) for m in missing]
        missing = [m for m in missing if m in items or any(
            m.lower() in x.lower() or x.lower() in m.lower() for x in items)]
        for it in items:
            d = _det_check(it, allcode)
            if d is None:
                words = [w for w in re.findall(r"\w+", it.lower()) if len(w) >= 4]
                if words and all(w[:5] in allcode.lower() for w in words):
                    d = True
            if d is True and it in missing:
                missing.remove(it)
            elif d is False and it not in missing:
                missing.append(it)
        missing = missing[:4]
        emit("tool", call={"name": "run_command", "args": {"cmd": "brief-check"}})
        emit("tool_result", name="run_command",
             out=(f"öz-denetim: {len(items) - len(missing)}/{len(items)} madde kodda ✓"
                  + ("\neksik: " + "; ".join(missing) if missing else "")))
        if missing and not stopped():
            emit("phase", name="onar", text="eksik maddeleri tamamlıyorum")
            path = next(iter(written))
            before = written[path]
            emit("tool", call={"name": "edit_file", "args": {"path": path}})
            fix = brain.chat(history + [{"role": "user", "content": (
                "Bu dosyada şu gereksinimler eksik:\n" + "\n".join(f"- {m}" for m in missing)
                + f"\n\nMevcut '{path}':\n```\n{before[:6000]}\n```\n"
                "Eksikleri somut olarak ekle, kalan her şeyi koru. Dosyanın TAM yeni halini tek kod bloğunda ver.")}],
                max_tokens=1800, temperature=0.2, cancel=cancel)
            newc = _code_of(fix)
            okfix = bool(newc.strip())
            if okfix and path.endswith(".html"):
                pr, sc = _check_html(newc, [])
                okfix = not [x for x in pr if not x.startswith("kullanıcının")] and not _check_js(sc, ws)
                if okfix:
                    okfix = not _runtime(path)
            if okfix and path.endswith(".py"):
                ws.write(path, newc)
                okfix = "DOCTEST_OK" in ws.run(f"python3 -m py_compile {path} && python3 -m doctest {path} && echo DOCTEST_OK",
                                                timeout=60, cancel=cancel)
            if okfix:
                ws.write(path, newc)
                written[path] = newc
                emit("file", path=path, op="edit")
                emit("tool_result", name="edit_file", out=f"eksikler tamamlandı: {path}")
                try:
                    rv2 = brain.chat([{"role": "user", "content": (
                        "Gereksinimler:\n" + "\n".join(f"- {x}" for x in missing)
                        + f"\n\nKod:\n--- {path} ---\n{newc[:5000]}\nHer gereksinimin kodda SOMUT karşılığı var mı? "
                        "Kesinlikle eksik olanları tam metniyle listele. SADECE JSON.")}],
                        schema=REVIEW_SCHEMA, max_tokens=200, temperature=0.1, cancel=cancel, model=brain.FAST_MODEL) or {}
                    missing = [m for m in missing if m in (rv2.get("missing") or [])]
                except Exception:
                    missing = []
                missing = [m for m in missing if _det_check(m, newc) is not True] + \
                          [m for m in items if _det_check(m, newc) is False and m not in missing]
                emit("tool_result", name="run_command",
                     out="yeniden kontrol: " + ("tüm maddeler kodda ✓" if not missing else "hâlâ eksik: " + "; ".join(missing)))
            else:
                ws.write(path, before)
                emit("tool_result", name="edit_file", out="tamamlama testi geçemedi, önceki sürüm korundu")
        _n = lambda x: re.sub(r"\s*\(önerim\)\s*$", "", str(x)).strip().lower()
        _miss = {_n(m) for m in missing}
        checklist = [(x, _n(x) not in _miss) for x in items]

    # ---------- 5. raporla ----------
    emit("phase", name="rapor", text="raporluyorum")
    unresolved = [e["text"] for e in trace if e["t"] == "error"]
    rep = (("**Hazır.** " if not unresolved else "**Bitti, ama bir uyarı var.** ") + (plan.get("title") or summary) + "\n\n"
           + "Dosyalar: " + ", ".join(f"`{p}`" for p in written) + "\n"
           + "Test edilenler: " + (", ".join(f"`{p}` ✓" for p in tested) if tested else "—") + "\n"
           + ("Doğrulama: " + "; ".join(plan.get("tests") or []) + "\n" if plan.get("tests") else "")
           + ("Özet kontrolü:\n" + "\n".join(("- ✓ " if ok_ else "- ⚠ ") + x for x, ok_ in checklist) + "\n" if checklist else "")
           + ("Çözemediğim: " + "; ".join(u[:160] for u in unresolved) + "\n" if unresolved else "")
           + ("Önizleme sağda açılıyor." if any(p.endswith(".html") for p in written)
              else "Çalıştır: `python3 " + next(iter(written)) + "`" if written else ""))
    emit("answer", text=rep)
    history.append({"role": "assistant", "content": rep})
    if written and not stopped():
        emit("suggest", items=_next_steps(history, kind, cancel))
    ok = bool(written) and len(tested) == len(written)
    emit("done", ok=ok, steps=len(trace))
    return {"ok": ok, "answer": rep, "trace": trace, "history": history, "prefs": prefs,
            "files": list(written), "tested": tested}
