#!/usr/bin/env bash
#
# sync.sh — keep git-tracked infra config in sync with the live server.
#
# Git is the single source of truth. Managed files (nginx, systemd units,
# logrotate) live in this deploy/ dir; the copies under /etc are deployment
# artifacts. Changes flow git -> prod via `deploy`. `check` is the drift
# backstop (run hourly by flyfish-drift-check.timer). `pull` reconciles an
# emergency live edit back into the repo so it can be committed.
#
#   sudo ./sync.sh deploy   repo -> /etc, validate, reload   (needs root)
#   sudo ./sync.sh check    diff live vs repo; exit 1 on drift
#   sudo ./sync.sh pull     /etc -> repo (then: git diff && commit)
#
# See deploy/README.md for the full process.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS="$(date +%Y%m%d_%H%M%S)"

# Managed files: "<repo-relative path>|<live path>|<kind>"
# kind ∈ nginx | systemd | plain  (drives the post-deploy reload action)
MANIFEST=(
  "nginx/flyfish.conf|/etc/nginx/sites-available/flyfish|nginx"
  "nginx/conf.d/flyfish-ratelimit.conf|/etc/nginx/conf.d/flyfish-ratelimit.conf|nginx"
  "systemd/flyfish.service|/etc/systemd/system/flyfish.service|systemd"
  "systemd/flyfish-drift-check.service|/etc/systemd/system/flyfish-drift-check.service|systemd"
  "systemd/flyfish-drift-check.timer|/etc/systemd/system/flyfish-drift-check.timer|systemd"
  "llama-chat.service|/etc/systemd/system/llama-chat.service|systemd"
  "llama-util.service|/etc/systemd/system/llama-util.service|systemd"
  "flyfish-llama-prewarm.service|/etc/systemd/system/flyfish-llama-prewarm.service|systemd"
  "logrotate.flyfish|/etc/logrotate.d/flyfish|plain"
)

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "${RED}error:${RST} '$1' needs root — re-run with sudo." >&2
    exit 2
  fi
}

# ---------------------------------------------------------------------------
cmd_check() {
  local drift=0 repo live kind rel
  for entry in "${MANIFEST[@]}"; do
    IFS='|' read -r rel live kind <<<"$entry"
    repo="$DEPLOY_DIR/$rel"
    if [[ ! -f "$repo" ]]; then
      echo "${YEL}?? repo-missing${RST}  $rel (tracked file absent)"; drift=1; continue
    fi
    if [[ ! -f "$live" ]]; then
      echo "${YEL}!! not-on-live${RST}  $live  (repo has it; never deployed?)"; drift=1; continue
    fi
    if diff -q "$repo" "$live" >/dev/null 2>&1; then
      echo "${GRN}ok${RST}            $live"
    else
      echo "${RED}DRIFT${RST}         $live  <-- differs from deploy/$rel"
      diff -u "$repo" "$live" | sed 's/^/    /' || true
      drift=1
    fi
  done
  if [[ $drift -ne 0 ]]; then
    echo "${RED}drift detected — reconcile with 'sync.sh deploy' (git wins) or 'sync.sh pull' (live wins).${RST}" >&2
    return 1
  fi
  echo "${GRN}no drift — live matches git.${RST}"
}

# ---------------------------------------------------------------------------
cmd_deploy() {
  require_root deploy
  local repo live kind rel nginx_changed=0 systemd_changed=0
  local -a nginx_backups=()
  for entry in "${MANIFEST[@]}"; do
    IFS='|' read -r rel live kind <<<"$entry"
    repo="$DEPLOY_DIR/$rel"
    [[ -f "$repo" ]] || { echo "${YEL}skip${RST} (repo missing) $rel"; continue; }
    if [[ -f "$live" ]] && diff -q "$repo" "$live" >/dev/null 2>&1; then
      continue  # already in sync
    fi
    if [[ -f "$live" ]]; then
      cp -a "$live" "${live}.bak_synctool_${TS}"
      [[ "$kind" == nginx ]] && nginx_backups+=("$live")
    fi
    install -m 0644 "$repo" "$live"
    echo "${GRN}deployed${RST}     $rel -> $live"
    [[ "$kind" == nginx ]] && nginx_changed=1
    [[ "$kind" == systemd ]] && systemd_changed=1
  done

  if [[ $nginx_changed -eq 1 ]]; then
    if ! nginx -t; then
      echo "${RED}nginx -t FAILED — restoring backups, NOT reloading.${RST}" >&2
      for f in "${nginx_backups[@]}"; do cp -a "${f}.bak_synctool_${TS}" "$f"; done
      exit 1
    fi
    systemctl reload nginx && echo "${GRN}nginx reloaded.${RST}"
  fi
  if [[ $systemd_changed -eq 1 ]]; then
    systemctl daemon-reload && echo "${GRN}systemd daemon-reloaded.${RST}"
    echo "${YEL}note:${RST} unit files changed — restart affected services if needed"
    echo "      (e.g. 'systemctl restart flyfish'); a reload does not bounce them."
  fi
  echo "${GRN}deploy complete.${RST}"
}

# ---------------------------------------------------------------------------
cmd_pull() {
  require_root pull
  local repo live kind rel n=0
  for entry in "${MANIFEST[@]}"; do
    IFS='|' read -r rel live kind <<<"$entry"
    repo="$DEPLOY_DIR/$rel"
    [[ -f "$live" ]] || { echo "${YEL}skip${RST} (not on live) $live"; continue; }
    if [[ -f "$repo" ]] && diff -q "$repo" "$live" >/dev/null 2>&1; then continue; fi
    mkdir -p "$(dirname "$repo")"
    cp -a "$live" "$repo"
    echo "${GRN}pulled${RST}       $live -> deploy/$rel"; n=$((n+1))
  done
  # Files pulled from /etc are root-owned; hand them back to the repo owner.
  if [[ -n "${SUDO_USER:-}" ]]; then chown -R "$SUDO_USER" "$DEPLOY_DIR"; fi
  echo "${GRN}pulled $n file(s).${RST} Review with 'git -C $DEPLOY_DIR/.. diff' and commit."
}

case "${1:-}" in
  check)  cmd_check  ;;
  deploy) cmd_deploy ;;
  pull)   cmd_pull   ;;
  *) echo "usage: sudo $0 {deploy|check|pull}" >&2; exit 2 ;;
esac
