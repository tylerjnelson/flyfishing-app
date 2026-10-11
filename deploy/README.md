# deploy/ — server config as code

**Git is the single source of truth for server configuration.** The files here
are the canonical copies of what runs on the box; the copies under `/etc` are
deployment artifacts. This directory exists so the service can be reviewed in
PRs and rebuilt from git if the server is lost.

## The rule
1. **Change in git, then deploy.** Edit the file in `deploy/`, commit, then run
   `sudo ./deploy/sync.sh deploy`. Never hand-edit `/etc` as the workflow.
2. **If you must edit live** (incident firefighting), immediately run
   `sudo ./deploy/sync.sh pull` to copy the change back into `deploy/`, then
   commit it. A live edit is not "done" until it's in git.
3. **Drift is caught automatically.** `flyfish-drift-check.timer` runs
   `sync.sh check` hourly; any live-vs-git divergence makes the unit fail
   (visible in `systemctl --failed` and the journal).

## sync.sh
| Command | What it does | Root |
|---|---|---|
| `sync.sh deploy` | For each managed file: back up live, copy repo→`/etc`, then `nginx -t` + reload (nginx) or `daemon-reload` (systemd). Aborts + restores if `nginx -t` fails. Skips files already in sync. | yes |
| `sync.sh check` | Diffs every managed file against its `/etc` copy. Prints drift, exits non-zero if any. (Used by the hourly timer.) | yes* |
| `sync.sh pull` | Copies live `/etc` files back into `deploy/` to reconcile an emergency edit. Then `git diff` + commit. | yes |

\* some `/etc` files are root-readable only, so `check` needs root too.

Backups are written next to each live file as `<file>.bak_synctool_<timestamp>`.

## Managed files (the manifest in sync.sh)
| repo | live |
|---|---|
| `nginx/flyfish.conf` | `/etc/nginx/sites-available/flyfish` |
| `nginx/conf.d/flyfish-ratelimit.conf` | `/etc/nginx/conf.d/flyfish-ratelimit.conf` |
| `systemd/flyfish.service` | `/etc/systemd/system/flyfish.service` |
| `systemd/flyfish-drift-check.{service,timer}` | `/etc/systemd/system/…` |
| `llama-chat.service`, `llama-util.service`, `flyfish-llama-prewarm.service` | `/etc/systemd/system/…` |
| `logrotate.flyfish` | `/etc/logrotate.d/flyfish` |
| `letsencrypt/reload-nginx.sh` (0755) | `/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh` |

## TLS renewal
Certbot renews via the `dns-cloudflare` authenticator, which has no nginx
installer, so nothing reloads nginx on its own: nginx keeps serving the old
cert from memory. (This is how the site served an expired cert from 2026-09-02
until 2026-10-11 despite a successful renewal on 2026-10-02.)
`letsencrypt/reload-nginx.sh` is a certbot **deploy hook**. Certbot runs it only
after a cert is actually renewed, and it runs `nginx -t` + `systemctl reload nginx`.

`sync.sh check` (and so the hourly timer) also checks the **served** cert and
fails if it differs from certbot's current cert (renewed, not reloaded) or has
fewer than `TLS_MIN_DAYS` (14) days left (renewal itself failing).

Test end-to-end (staging, no real cert change; certbot adds a random delay of up to ~8 min):
`sudo certbot renew --dry-run --run-deploy-hooks --cert-name waflyfish.com`

## Not managed here (on purpose)
- **`/etc/flyfish/app.env`** — secrets; stays out of git. `app.env.example`
  tracks the required *keys* so the env can be rebuilt. Update it when a key
  is added/removed.
- **TLS certs** (`/etc/letsencrypt/…`) — managed by certbot, never in git
  (only the deploy hook above is tracked).
- **App code / frontend** — deployed separately (`/opt/flyfish/backend`,
  `/opt/flyfish/prompts`, `/var/www/flyfish/dist`); see the next section.

## Deploying app code (backend)
Prod runs from `/opt/flyfish/backend` (systemd `WorkingDirectory`, `User=flyfish`,
env from `/etc/flyfish/app.env`). The prompts it imports live at `/opt/flyfish/prompts`
(the repo-root `prompts/`, not `backend/prompts/`). Deploys copy changed files
from this git checkout. There is no build step.

1. **Commit first**, and run the suite offline:
   `cd backend && sudo unshare -n /opt/flyfish/venv/bin/pytest tests/ -q`
2. **Back up what you'll overwrite.** Copy each prod file into
   `/opt/flyfish/_predeploy_backup_<topic>_<timestamp>/code/`. Before any migration
   that deletes or rewrites data, also `pg_dump -Fc -t <table>` the affected tables
   into that directory.
3. **Install** each file as `flyfish:flyfish`, mode `664`:
   `sudo install -D -m 0664 -o flyfish -g flyfish backend/<f> /opt/flyfish/backend/<f>`
4. **Migrate before restarting**, from the prod tree, with the prod env:
   `sudo bash -c 'set -a; . /etc/flyfish/app.env; set +a; cd /opt/flyfish/backend && /opt/flyfish/venv/bin/alembic upgrade head'`
   Copy the new `db/migrations/versions/*.py` in step 3, or alembic won't see them.
5. **Restart** `sudo systemctl restart flyfish`, then check the journal from the
   restart on: `"Application startup complete"` ×3, no `[ERROR]`, and the WDFW and
   InciWeb jobs (they run at startup) logging `job_done`.
6. **Verify prod matches git:**
   `sudo diff -rq backend /opt/flyfish/backend -x __pycache__ -x '*.bak*' -x .pytest_cache -x tests`
   (`scripts/batch_results*` directories are expected to differ.)

Gotchas:
- `/opt/flyfish/backend` isn't readable by `ubuntu`, so every step, including
  `stat` and `[ -f ]` tests, needs `sudo`. Chain commands with `set -e` so a failed
  install never reaches the restart.
- Workers that are shutting down can import files that were just copied (fixed for
  the scheduler in 81ec46a). Any other traceback from an *exiting* PID during a
  restart is the same mixed-version effect, not a bug in the new code. Check that
  the *new* PIDs booted cleanly.
- Rollback: copy the backup's `code/` back, `alembic downgrade <prev>`, restart.

## Notes
- The drift-check units point at `/home/ubuntu/flyfish/deploy/sync.sh` — the git
  checkout on this server. If the checkout moves, update those unit paths.
- To reconstruct the box from git: copy `app.env.example`→`/etc/flyfish/app.env`
  and fill secrets, run `sync.sh deploy`, `systemctl enable --now flyfish
  flyfish-drift-check.timer`, provision certs with certbot.
