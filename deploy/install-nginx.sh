#!/usr/bin/env bash
# Publish the dakcoder gateway at https://ai.cept.gov.in/dakcoder/.
#
# Two steps, both idempotent:
#   1. copy deploy/nginx-dakcoder.conf to /etc/nginx/snippets/dakcoder.conf
#   2. add one `include` line to the 443 server block in sites-available/api.conf
#
# api.conf is edited exactly once and backed up first. `nginx -t` gates the
# reload, and a failed test restores the backup — a config that does not parse
# takes down every other service on this vhost, so the rollback is not optional.
#
#   deploy/install-nginx.sh            show what would change, change nothing
#   deploy/install-nginx.sh --apply    do it (asks for sudo)
#   deploy/install-nginx.sh --remove   take the include back out
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/deploy/nginx-dakcoder.conf"
SNIPPET=/etc/nginx/snippets/dakcoder.conf
SITE=/etc/nginx/sites-available/api.conf
INCLUDE="    include $SNIPPET;   # dakcoder gateway"
MODE="${1:---dry-run}"

[[ -f "$SRC" ]]  || { echo "missing $SRC"; exit 1; }
[[ -f "$SITE" ]] || { echo "missing $SITE — is this the right host?"; exit 1; }

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# -- preflight ---------------------------------------------------------------
# Publishing a gateway that is not running is how you find out about it from a
# user rather than from a check.
if ! curl -s -o /dev/null -m 3 --noproxy 127.0.0.1 http://127.0.0.1:8790/v1/health; then
  echo "!! the gateway is not answering on 127.0.0.1:8790 — start it with deploy/start.sh"
  [[ "$MODE" == "--apply" ]] && exit 1
fi

identity="$(curl -s -m 3 --noproxy 127.0.0.1 http://127.0.0.1:8790/v1/health \
            | python3 -c 'import json,sys; print(json.load(sys.stdin).get("capabilities",{}).get("identity","unknown"))' 2>/dev/null || echo unknown)"
say "gateway identity provider: $identity"
if [[ "$identity" != "gitlab" ]]; then
  cat <<'WARN'
  The gateway is NOT using a real identity provider. The snippet therefore
  returns 403 for /dakcoder/v1/auth/ — sign-in stays loopback-only, because a
  published dev IdP accepts any authorization code and would let anyone mint a
  session against the shared GPU budget.

  Everything else is published and still requires a dakcoder JWT. Mint one with:
      deploy/gateway_main.py --mint dev:<user> --mint-hours 12
WARN
fi

# -- what would change -------------------------------------------------------
if grep -qF "$SNIPPET" "$SITE"; then
  included=yes
else
  included=no
fi

say "plan"
echo "  snippet : $SRC  ->  $SNIPPET"
if [[ "$MODE" == "--remove" ]]; then
  echo "  api.conf: remove the include line (currently included: $included)"
elif [[ "$included" == yes ]]; then
  echo "  api.conf: already includes the snippet — no edit needed"
else
  echo "  api.conf: insert before the final closing brace of the 443 server block:"
  echo "            $INCLUDE"
fi

if [[ "$MODE" != "--apply" && "$MODE" != "--remove" ]]; then
  say "dry run — nothing changed. Re-run with --apply."
  exit 0
fi

# -- apply -------------------------------------------------------------------
# The snippet is cheap to rewrite and carries no state, so it is always
# refreshed. api.conf is only touched when the include is actually missing —
# and only then is it worth a backup.
if [[ "$MODE" == "--remove" || "$included" == no ]]; then
  stamp="$(date +%Y%m%d-%H%M%S)"
  backup="${SITE}.bak-${stamp}"
  say "backing up $SITE -> $backup"
  sudo cp -a "$SITE" "$backup"
fi

restore() {
  if [[ -n "${backup:-}" ]]; then
    echo "!! nginx -t failed; restoring $backup"
    sudo cp -a "$backup" "$SITE"
    sudo nginx -t
  else
    echo "!! nginx -t failed, but api.conf was not modified by this run."
    echo "   The problem is in $SNIPPET or elsewhere in the config."
  fi
  exit 1
}

if [[ "$MODE" == "--remove" ]]; then
  sudo sed -i "\|$SNIPPET|d" "$SITE"
  sudo rm -f "$SNIPPET"
else
  sudo install -m 0644 "$SRC" "$SNIPPET"

  if [[ "$included" == no ]]; then
    # Insert before the LAST closing brace in the file, which is the end of the
    # 443 server block. awk rather than sed because "the last line matching ^}"
    # needs a second pass, and a sed that gets it wrong lands the include
    # outside the server block, where nginx rejects `location`.
    #
    # The result is held in a variable and piped straight onto api.conf. The
    # obvious version — awk to a mktemp file, then sudo tee it — cannot work
    # here: fs.protected_regular=2 stops root writing to a file it does not own
    # in a sticky world-writable directory, so `sudo tee /tmp/tmp.XXXX` fails
    # with "Permission denied" on a file the invoking user just created.
    # Writing through tee also preserves api.conf's inode, owner and mode,
    # which a cp over the top does not.
    edited="$(awk -v line="$INCLUDE" '
      { rec[NR] = $0; if ($0 ~ /^}[[:space:]]*$/) last = NR }
      END {
        for (i = 1; i <= NR; i++) {
          if (i == last) print line
          print rec[i]
        }
      }' "$SITE")"

    # tee truncates before it writes, so anything short of a verified-correct
    # result must not reach it. Exactly one line longer, and the include
    # present: that is the whole edit, and either it happened or it did not.
    before="$(wc -l < "$SITE")"
    after="$(printf '%s\n' "$edited" | wc -l)"
    if [[ -z "$edited" ]] || (( after != before + 1 )) || ! printf '%s\n' "$edited" | grep -qF "$SNIPPET"; then
      echo "!! refusing to write: the edit produced $after lines from $before, or lost the include."
      echo "   $SITE is unchanged. Backup at $backup."
      exit 1
    fi
    printf '%s\n' "$edited" | sudo tee "$SITE" >/dev/null
  fi
fi

say "nginx -t"
sudo nginx -t || restore

say "reloading nginx"
sudo systemctl reload nginx

say "verifying through the vhost"
curl -s -o /dev/null -w '  /dakcoder/v1/health -> %{http_code}\n' https://ai.cept.gov.in/dakcoder/v1/health
curl -s -o /dev/null -w '  /dakcoder/v1/auth/start -> %{http_code} (403 expected while identity=dev)\n' \
     -X POST https://ai.cept.gov.in/dakcoder/v1/auth/start
say "done. Backup kept at $backup"
