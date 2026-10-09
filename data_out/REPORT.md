# coder-1 data report

Data artifacts for the first real training run. **All content is real code** —
no synthetic/mock/generated text anywhere.

## A. Corpus — `data_out/raw_code.txt` (~250MB)

Documents separated by `\n\n<<<CODER1_DOC_BREAK>>>\n\n`. Every doc passed
`looks_like_code` + `Dedup` (sha1 content hash) from `model/data/clean.py`.

| source | docs | MB | notes |
|---|---|---|---|
| GitHub codeload tarballs (repos.txt) | 12,657 files | 115.7 | 56/57 repos ok; `redis/redis` tarball fetch failed (skipped) |
| CodeSearchNet `python` | 26,012 funcs | 33.6 | `code` field, real functions |
| CodeSearchNet `javascript` | 34,420 funcs | 33.6 | |
| CodeSearchNet `go` | 50,825 funcs | 33.6 | |
| CodeSearchNet `java` | 45,939 funcs | 33.6 | |
| **total** | **169,853** | **~250** | |

Notes:
- HF `code_search_net` no longer loads on `datasets>=3` (script datasets were
  removed). Used `Nan-Do/code-search-net-{python,javascript,go,java}` mirrors —
  same real CodeSearchNet records (`code`/`docstring`/`repo`/`sha`/`func_name`).
- GitHub phase capped at 150MB; CSN balanced ~33.6MB/lang (~54% CSN, 46% GH).
- `model/data/sft_data.py` patched: `_csn_rows()` tries `code_search_net` first,
  falls back to the Nan-Do mirrors (only file touched under `model/`).

## B. Tokenizer — `data_out/tok16k/`

`model/tokenizer/bpe.py` `BPE().train(vocab_size=16000, min_freq=3)` on a
17.3MB stratified sample of the corpus (every 15th doc — 11,324 docs across
both sources; byte-level BPE trains fine on a sample). ~4MB chunks streamed.

- vocab_size: **16,000** (all merges learned, min_freq=3 held to the end)
- merges learned: 15,798 | special tokens: 11 | base chars: ~191
- round-trip sanity: PASS (`encode`→`decode` identity on code sample)
- training time: 93.4 min | files: vocab.json 286KB + merges.txt 152KB
- first merges: `ĠĠ`, `ĠĠĠĠ`, `er`, `in`, `ĊĠĠĠĠĠĠĠĠ`, `re`, `on`, `,Ġ`, `at`, `ĉĉ`

## C. Pretokenized shards — `data_out/shards/`

`pretokenize_stream(docs, tok, shard_tokens=8_000_000)`, `<|im_end|>` appended
per document.

- total tokens: **67,941,584** (~3.7 bytes/token on code)
- shards: 9 (8 × ~8.0M + 1 × 3.93M) uint16 .bin + index.json, ~136MB
- wall time: 4.9 min

## D. SFT dataset — `data_out/sft.jsonl`

`python -m model.data.sft_data --out data_out/sft.jsonl --per-lang 2000`
→ 8,000 lines (2,000/lang). Real docstring→function pairs from CodeSearchNet.
Format: `{"messages":[{"role":"user","content":"<instruction+docstring>"},
{"role":"assistant","content":"```lang\n<function>\n```"}]}`

## Failures / caveats

- `redis/redis`: tarball fetch failed → 0 files. 56/57 repos contributed.
- CSN phase hit the volume cap — all four languages still fully represented
  (rebalanced pass).
- Tokenizer trained on a 17.3MB sample (full-corpus training of a pure-Python
  BPE is O(merges × unique pretokens) — ~2.4h even on the sample).
- (crash noise) `PyGILState_Release` at interpreter exit in `datasets`/hf-xet
  teardown — harmless, fires after files are written.
