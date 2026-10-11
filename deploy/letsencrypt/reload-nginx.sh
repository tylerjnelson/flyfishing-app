#!/usr/bin/env bash
#
# certbot deploy hook — reload nginx after a successful renewal.
#
# Certbot here uses the dns-cloudflare authenticator (no nginx installer), so
# nothing else tells nginx a new cert exists; without this hook nginx keeps
# serving the old cert from memory until its next restart. That is exactly how
# waflyfish.com served an expired cert 2026-09-02 → 2026-10-11 despite a
# successful renewal on 2026-10-02.
#
# Installed to /etc/letsencrypt/renewal-hooks/deploy/ by `sync.sh deploy`.
# Certbot runs every executable there only after a cert is actually renewed.

set -euo pipefail

if nginx -t -q; then
  systemctl reload nginx
  echo "reload-nginx: reloaded nginx for renewed cert(s): ${RENEWED_DOMAINS:-unknown}"
else
  echo "reload-nginx: nginx -t FAILED — not reloading; old cert still served" >&2
  exit 1
fi
