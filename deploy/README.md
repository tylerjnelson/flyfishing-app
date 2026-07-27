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

## Not managed here (on purpose)
- **`/etc/flyfish/app.env`** — secrets; stays out of git. `app.env.example`
  tracks the required *keys* so the env can be rebuilt. Update it when a key
  is added/removed.
- **TLS certs** (`/etc/letsencrypt/…`) — managed by certbot, never in git.
- **App code / frontend** — deployed separately (`/opt/flyfish/backend`,
  `/var/www/flyfish/dist`). This dir is host/systemd/nginx config only.

## Notes
- The drift-check units point at `/home/ubuntu/flyfish/deploy/sync.sh` — the git
  checkout on this server. If the checkout moves, update those unit paths.
- To reconstruct the box from git: copy `app.env.example`→`/etc/flyfish/app.env`
  and fill secrets, run `sync.sh deploy`, `systemctl enable --now flyfish
  flyfish-drift-check.timer`, provision certs with certbot.
