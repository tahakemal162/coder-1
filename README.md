# coder-1

Kendi eğittiğimiz kod yapay zekası — model, tokenizer, eğitim kodu, ajan döngüsü,
kod editörü ve çalışma alanı; hepsi bu repo içinde, hazır model yok.

Kurulum ve yol haritası: [SPEC.md](SPEC.md)

## Hızlı başlangıç

```bash
pip install -r requirements.txt
python tests/smoke.py          # uçtan uca duman testi (CPU, ~2 dk)
```

## Parçalar

- `model/` — BPE tokenizer, transformer mimarisi, pretrain/SFT/eval
- `serve/` — OpenAI-uyumlu model sunucusu
- `agent/` — araç çağıran ajan döngüsü + çalışma alanı + ürün backend'i
- `web/` — sohbet + Monaco editör + dosya ağacı + terminal arayüzü
- `deploy/` — Vercel (site) + Kaggle (GPU eğitim) betikleri
