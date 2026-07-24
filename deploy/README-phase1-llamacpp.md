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

## Artifacts (pinned — Phase 0e)

- Binary + shared libs: llama.cpp **b10091** (staged at
  `dev-instructions/build-log/llamacpp-test/bin/llama-b10091/`).
- Weights: Google QAT **`gemma-4-E4B_q4_0-it.gguf`** (staged at
  `dev-instructions/build-log/llamacpp-test/models/`).
- Do **not** use ollama's vendored binary/blob — it fails tool fidelity (Phase 0e E1).

## Install (maintenance window, as root unless noted)

```bash
# 1. Lay out the engine under /opt/flyfish (owned by flyfish)
install -d -o flyfish -g flyfish /opt/flyfish/llamacpp/bin \
                                  /opt/flyfish/llamacpp/models \
                                  /opt/flyfish/llamacpp/slots
cp dev-instructions/build-log/llamacpp-test/bin/llama-b10091/*        /opt/flyfish/llamacpp/bin/
cp dev-instructions/build-log/llamacpp-test/models/gemma-4-E4B_q4_0-it.gguf /opt/flyfish/llamacpp/models/
chown -R flyfish:flyfish /opt/flyfish/llamacpp

# 2. Install the units + pre-warm oneshot
cp deploy/llama-chat.service deploy/llama-util.service \
   deploy/flyfish-llama-prewarm.service /etc/systemd/system/
systemctl daemon-reload

# 3. Boot the servers (flag still ollama — app unaffected)
systemctl enable --now llama-chat.service llama-util.service
systemctl start flyfish-llama-prewarm.service   # after chat /health is green

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
  frozen-context prefix will extend the pre-warm target.
- Utility endpoint is templated `/v1/chat/completions --jinja` (Phase 0g); raw
  `/completion` was rejected (untemplated → debrief hallucinated dialogue).
