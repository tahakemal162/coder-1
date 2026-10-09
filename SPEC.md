# coder-1 — Teknik Şartname (SPEC)

> Kullanıcı isteğinin profesyonel hali: *"Web sitesinde çalışan, kendi kod editörü ve
> bilgisayarı olan, kodlama için eğitilmiş yapay zeka. Modeli kendimiz yapacağız —
> hazır/yerel model yok, her şey bizim. Kaliteli olana kadar, haftalarca sürse de
> çalış."*

## 1. Vizyon

**coder-1**: tarayıcıda çalışan bir kodlama ajanı. Kullanıcı doğal dille istek yazar;
coder-1 kendi **kod editöründe** dosyaları açar/düzenler, kendi **bilgisayarında**
(yalıtılmış konteyner) komut çalıştırır, sonucu test eder ve raporlar. Beyni baştan
sonra **bizim eğittiğimiz** bir modeldir — hazır model, harici LLM API'si, yerel
Ollama modeli yok.

## 2. Araştırma özeti — 2026'da kod AI nasıl yapılır

### 2.1 Üretim hattı (gerçek laboratuvar yöntemi)

| Aşama | Ne yapılır | Bizdeki karşılığı |
|---|---|---|
| **Tokenizer** | Ham metin → token. Byte-level BPE, ~32k vocab | `model/tokenizer/` — kendi BPE eğiticimiz |
| **Pre-training** | Milyarlarca token kod üzerinde "sonraki token'ı tahmin" | `model/pretrain.py` + `model/data/` |
| **SFT** | Talimat→kod örnekleriyle ince ayar (ChatML benzeri format) | `model/sft.py` — gerçek docstring→fonksiyon çiftleri + araç çağrısı kayıtları |
| **Değerlendirme** | HumanEval tarzı gizli testler, pass@1 | `model/eval.py` — kendi test setimiz |
| **Ajanlaştırma** | Modeli araç çağıracak şekilde eğitme / sarma | `agent/` döngüsü + SFT'de araç formatı |
| **Servis** | Streaming API | `serve/` FastAPI, OpenAI-uyumlu |

### 2.2 Kaç ekip lazım? (gerçek dünya cevabı)

Bir AI ürününü gerçekte taşıyan 4 işlev: **veri mühendisi** (kayıp giderken fark
edilmeyen tek şey veri kalitesidir), **eğitim mühendisi**, **ürün/ajan mühendisi**,
**değerlendirme mühendisi**. Endüstride bu minimum 3-4 kişi; model sıfırdan
eğitilecekse ayrıca ML araştırmacısı gerekir.

**Bizdeki karşılığı:** Devin ajanları — veri boru hattı, eğitim, web/ürün, agent
backend için paralel ajanlar; koordinasyon + entegrasyon ana oturumda.

### 2.3 Hesap gerçekleri (dürüst tablo)

- GPT/Claude sınıfı model: milyonlarca $ → **gerçekçi değil, hedef de değil.**
- Ücretsiz GPU: **Kaggle 30 saat/hafta** (P100 veya T4x2), Colab free (T4,
  değişken kota), ~12 saat/oturum sınırı.
- Bu bütçeyle sıfırdan gerçekçi hedef: **~120M–350M parametre**, haftada
  ~2–5 milyar token. GPT-2-small sınıfı bir *kod* modeli: basit fonksiyonları
  yazar, gerçek öğrenir. Haftalar ilerledikçe parametre/token artar.
- GPU hesabı yokken bile boru hattı CPU'da mini boyutta uçtan uca doğrulanır.

## 3. Mimari

```
coder-1/
├── model/          # %100 bizim: tokenizer, mimari, eğitim, eval
│   ├── tokenizer/  # byte-level BPE eğitici + kodlayıcı
│   ├── architecture.py   # decoder-only transformer (RoPE, RMSNorm, SwiGLU, GQA)
│   ├── config.py   # nano/mini/small/base/pro boyutları
│   ├── data/       # kaynaklar, temizleme, ön-tokenleme, torch Dataset
│   ├── pretrain.py # AMP, cosine LR, grad-accum, checkpoint-resume
│   ├── sft.py      # talimat + araç-çağrısı ince ayarı (sadece cevap öğrenir)
│   ├── generate.py # top-p/top-k/temp, repetition penalty, streaming
│   └── eval.py     # kendi HumanEval-mini setimiz, sandbox'lı yürütme
├── serve/          # FastAPI: /v1/chat/completions (SSE), /generate
├── agent/          # araç döngüsü + görev başına yalıtılmış çalışma alanı
├── web/            # site: sohbet + Monaco editör + dosya ağacı + terminal + ekran
├── deploy/         # Vercel (site) + Kaggle (eğitim) scriptleri
└── tests/          # uçtan uca duman testi
```

### API sözleşmesi (ajanlar arası kontrat)

- `POST /v1/chat/completions` — OpenAI şeması, `stream:true` SSE.
- Sohbet formatı: `<|im_start|>rol\n...<|im_end|>\n` (kendi tokenizer'ımızda özel
  token'lar).
- Araç çağrısı formatı (ajan SFT'si): `<|tool|>{"name":..,"args":{..}}<|im_end|>` →
  sistem `<|tool_result|>...<|im_end|>` döner.
- Ajan araçları: `read_file, write_file, edit_file, run_command, list_files,
  create_project, open_editor` (workspace'e yansır), `done`.

## 4. Yol haritası

- **Faz A — Yürüyen iskelet (bu oturum):** tüm kod var; CPU'da ~20M model birkaç
  adım eğitilir → serve → ajan → site uçtan uca çalışır.
- **Faz B — İlk gerçek eğitim:** Kaggle T4x2, small (124M) model, milyarlarca
  token; her hafta checkpoint + eval tablosu.
- **Faz C — Ajan SFT:** araç formatında ince ayar; site üzerinde gerçek görevlerde
  eval; "kaliteli olana kadar" döngüsü burada.
- **Faz D — Ürünleşme:** Docker çalışma alanı + kod editörü senkron + ekran
  görüntüsü; çok kullanıcı; Vercel + backend.
- **Faz E — Büyütme:** base/pro boyutlar, RL (çalışan test = ödül) hazır
  altyapıyla.

## 5. Dürüst sınırlar

- İlk coder-1 GPT seviyesinde olmaz; hedef: **kendi kendine basit programlar
  yazabilen, gerçekten bizim** bir model. Kalite haftalar içinde eval'le ölçülerek
  artar.
- Eğitim için tek dış bağımlılık bedava GPU hesabı (Kaggle). Anahtar gelene kadar
  Faz A/B boru hattı CPU'da küçük ölçekte doğrulanmış bekler.
