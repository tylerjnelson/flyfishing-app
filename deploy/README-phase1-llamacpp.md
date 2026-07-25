# Phase 1 — llama.cpp engine switchover (Option B) — deploy notes

Moves **all** gemma-4-E4B usage off ollama onto llama.cpp `llama-server`.
Embeddings (`nomic-embed-text`) and vision (`llama3.2-vision`) stay on ollama.
Controlled by a single flag, **`FLYFISH_CHAT_ENGINE`** (`ollama` default | `llamacpp`),
which flips the chat path **and** the three background utility sites together —
rollback is atomic.

The application code ships **inert**: with the flag unset (or `ollama`) nothing
changes. These artifacts + the steps below stand the engine up; the flip is the
last step, done in a maintenance window after the S-gates pass.

> ⚠️ This is the **live prod box**. Booting `llama-server` costs RAM and the
> S-gate prefills saturate all 4 cores — run staging + S1–S8 in a maintenance
> window. Keep `FLYFISH_CHAT_ENGINE=ollama` until every gate is green.

## Two instances (weight-sharing, same GGUF)

| unit | port | role | key flags |
|------|------|------|-----------|
| `llama-chat.service` | 8080 | interactive chat: planning (tools) + streaming generation | `--jinja --reasoning on --swa-full --slot-save-path … -np 1 -c 16384 -t 4` |
| `llama-util.service` | 8081 | background field/location extraction + debrief | `--jinja --reasoning off -np 1 -c 4096 -t 2` (mmap default, no slot persistence) |

Both point at the **same** `gemma-4-E4B_q4_0-it.gguf`; the second instance loads
the weights as shared read-only mmap pages (~0 extra RAM, ~1 GB for its own KV).
Steady state ≈ **10 GB / 23 GB**.

## Artifacts (pinned — Phase 0e) — RE-FETCH AT DEPLOY

The binary + GGUF are **not** in the repo and are **no longer staged on the box**
(they were removed to reclaim ~4.9 GB). Re-fetch them fresh at deploy time from the
pinned sources below (box arch is **aarch64**). Both are large-ish one-time downloads;
the box needs outbound HTTPS to GitHub + Hugging Face.

- **Binary + shared libs:** llama.cpp **b10091**, **Linux aarch64** build — asset
  **`llama-b10091-bin-ubuntu-arm64.tar.gz`** from
  `https://github.com/ggml-org/llama.cpp/releases/tag/b10091` (a `.tar.gz`, not a zip;
  the `.so` shared libs — incl. the per-uarch `libggml-cpu-*` variants — ship in the
  same archive). Do **not** use ollama's vendored binary/blob — it fails tool fidelity
  (Phase 0e E1).
  - **Extra runtime dep — `libgomp.so.1` (GNU OpenMP):** NOT in the tarball and NOT
    installed system-wide on this box, so `llama-server` fails to start with
    `error while loading shared libraries: libgomp.so.1`. Stage it into `bin/` (on the
    server's `LD_LIBRARY_PATH`) from ollama's bundled copy — no apt / no system change:
    `cp -L /usr/local/lib/ollama/libgomp.so.1 /opt/flyfish/llamacpp/bin/`
    (`-L` dereferences — it's a symlink). Alternatively `apt-get install libgomp1`.
- **Weights:** Google QAT **`gemma-4-E4B_q4_0-it.gguf`** from Hugging Face repo
  `google/gemma-4-E4B-it-qat-q4_0-gguf` — **5,154,941,280 bytes** (~4.9 GB). The repo
  is **not gated** (anonymous download works; no HF token needed).

## Install (maintenance window, as root unless noted)

```bash
# 1. Lay out the engine under /opt/flyfish (owned by flyfish)
install -d -o flyfish -g flyfish /opt/flyfish/llamacpp/bin \
                                  /opt/flyfish/llamacpp/models \
                                  /opt/flyfish/llamacpp/slots

# 1a. Binary — llama.cpp b10091, Linux aarch64 (see Artifacts). Extract the
#     llama-server binary + all *.so flat into bin/ (--strip-components=1).
curl -L --fail -o /tmp/llama-b10091.tar.gz \
  https://github.com/ggml-org/llama.cpp/releases/download/b10091/llama-b10091-bin-ubuntu-arm64.tar.gz
tar xzf /tmp/llama-b10091.tar.gz --strip-components=1 -C /opt/flyfish/llamacpp/bin
chmod +x /opt/flyfish/llamacpp/bin/llama-server
#     Runtime dep libgomp.so.1 is not in the tarball — stage it (see Artifacts):
cp -L /usr/local/lib/ollama/libgomp.so.1 /opt/flyfish/llamacpp/bin/
#     Sanity: the binary must print its version (proves libs resolve):
LD_LIBRARY_PATH=/opt/flyfish/llamacpp/bin /opt/flyfish/llamacpp/bin/llama-server --version

# 1b. Weights — Google QAT GGUF (~4.9 GB) straight into models/.
/opt/flyfish/venv/bin/python - <<'PY'
from huggingface_hub import hf_hub_download
p = hf_hub_download(
    repo_id="google/gemma-4-E4B-it-qat-q4_0-gguf",
    filename="gemma-4-E4B_q4_0-it.gguf",
    local_dir="/opt/flyfish/llamacpp/models",
)
print("GGUF at:", p)
PY

chown -R flyfish:flyfish /opt/flyfish/llamacpp
# Sanity: bin/llama-server is executable and models/gemma-4-E4B_q4_0-it.gguf exists.

# 2. Install the units + pre-warm oneshot
cp deploy/llama-chat.service deploy/llama-util.service \
   deploy/flyfish-llama-prewarm.service /etc/systemd/system/
systemctl daemon-reload

# 3. Boot the servers (flag still ollama — app unaffected)
systemctl enable --now llama-chat.service llama-util.service
systemctl enable --now flyfish-llama-prewarm.service   # after chat /health is green
#    NOTE: prewarm is a RemainAfterExit oneshot — to RE-run it use
#    `systemctl restart flyfish-llama-prewarm.service` (a bare `start` is a no-op).

# 4. Run S1–S8 (incl. S2b) against the staged servers, HERE-stubbed. Rehearse S8.

# 5. Flip the engine (edit /etc/flyfish/app.env), then restart the app
#    FLYFISH_CHAT_ENGINE=llamacpp
systemctl restart flyfish.service
```

## Env (`/etc/flyfish/app.env`)

```ini
FLYFISH_CHAT_ENGINE=llamacpp          # ollama (default) | llamacpp
# Optional overrides (defaults shown):
# FLYFISH_LLAMA_CHAT_URL=http://127.0.0.1:8080
# FLYFISH_LLAMA_UTIL_URL=http://127.0.0.1:8081
```

## Rollback (S8 — atomic)

Set `FLYFISH_CHAT_ENGINE=ollama` (or remove it), `systemctl restart flyfish.service`.
The chat path **and** the three utility sites revert to ollama together; gemma
returns to ollama's resident set. Optionally `systemctl stop llama-chat llama-util`
to free RAM. Rehearse this **before** the cutover.

## Notes

- `prewarm_llama.py` warms only the **planning** static prefix (system + tools) —
  the sole fully-static shared prefix under the current two-pass app. Phase 4's
  frozen-context prefix will extend the pre-warm target. Its prefill also warms the
  live slot directly, so the disk slot-save (below) is a restore-time optimization,
  not a prerequisite for the prefix-reuse benefit.
- **Slot-save API (b10091):** the filename goes in the **JSON body**, not the query
  string — `POST /slots/0?action=save` with `{"filename": "..."}`. The old query-param
  form (`?action=save&filename=...`) returns `500 "attempting to parse an empty input"`.
  `prewarm_llama.py` uses the body form; keep it that way if the endpoint is re-touched.
- Utility endpoint is templated `/v1/chat/completions --jinja` (Phase 0g); raw
  `/completion` was rejected (untemplated → debrief hallucinated dialogue).
