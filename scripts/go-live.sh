#!/usr/bin/env bash
# contextctl go-live: ONE step that makes the project public.
#
#   DO NOT RUN without Ayan's explicit "go live". Read docs/GO_LIVE.md first.
#
# What it does, in order (each step is idempotent):
#   1. Makes the GitHub repo public.
#   2. Flips the site to indexable (removes noindex meta, robots.txt Disallow,
#      X-Robots-Tag header), marks the repo public on the site, commits, pushes.
#   3. Builds the site and deploys it to Netlify PRODUCTION.
#   4. Attaches SITE_DOMAIN and www.SITE_DOMAIN as custom domains on the Netlify site.
#   5. Prints the exact DNS records to set at the registrar (DNS is NOT changed by
#      this script) and, with --provision-tls, asks Netlify for the TLS certificate
#      once DNS resolves.
#
# Usage: scripts/go-live.sh --yes [--provision-tls]
#   Values come from site/site.config.json (SITE_DOMAIN, GITHUB_REPO,
#   NETLIFY_SITE_ID); env vars of the same name override them.
# Requires: gh (logged in, admin on the repo), netlify CLI (logged in), python3, git.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CFG="$ROOT/site/site.config.json"
cfg() { python3 -c "import json,sys;print(json.load(open('$CFG')).get('$1',''))"; }
SITE_DOMAIN="${SITE_DOMAIN:-$(cfg SITE_DOMAIN)}"
GITHUB_REPO="${GITHUB_REPO:-$(cfg GITHUB_REPO)}"
NETLIFY_SITE_ID="${NETLIFY_SITE_ID:-$(cfg NETLIFY_SITE_ID)}"
NETLIFY_SITE_NAME="${NETLIFY_SITE_NAME:-$(cfg NETLIFY_SITE_NAME)}"

YES=0; TLS=0
for a in "$@"; do
  case "$a" in
    --yes) YES=1 ;;
    --provision-tls) TLS=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done
if [[ $YES -ne 1 ]]; then
  echo "Refusing to run without --yes. This makes $GITHUB_REPO public and deploys" >&2
  echo "https://$SITE_DOMAIN to production. Read docs/GO_LIVE.md." >&2
  exit 1
fi
for bin in gh netlify python3 git; do command -v "$bin" >/dev/null || { echo "missing $bin" >&2; exit 1; }; done
[[ -n "$SITE_DOMAIN" && -n "$GITHUB_REPO" && -n "$NETLIFY_SITE_ID" ]] || { echo "config incomplete" >&2; exit 1; }

echo "==> 1/5 Make github.com/$GITHUB_REPO public"
gh repo edit "$GITHUB_REPO" --visibility public --accept-visibility-change-consequences

echo "==> 2/5 Make the site indexable and mark the repo public"
python3 - "$CFG" <<'PY'
import json, sys
p = sys.argv[1]
c = json.load(open(p))
c["INDEXABLE"] = True
c["REPO_PUBLIC"] = True
open(p, "w").write(json.dumps(c, indent=2) + "\n")
PY
cd "$ROOT"
if ! git diff --quiet -- "$CFG"; then
  git add "$CFG"
  git commit -q -m "Go live: site indexable on $SITE_DOMAIN, repo public"
  git push -q origin HEAD
fi

echo "==> 3/5 Build and deploy to Netlify production"
python3 site/build.py
grep -q 'content="index,follow"' site/dist/index.html
! grep -q 'Disallow: /' site/dist/robots.txt
netlify deploy --prod --dir site/dist --site "$NETLIFY_SITE_ID" --message "go-live $(git rev-parse --short HEAD)"

echo "==> 4/5 Attach $SITE_DOMAIN and www.$SITE_DOMAIN to the Netlify site"
netlify api updateSite --data "{\"site_id\":\"$NETLIFY_SITE_ID\",\"body\":{\"custom_domain\":\"$SITE_DOMAIN\",\"domain_aliases\":[\"www.$SITE_DOMAIN\"]}}" >/dev/null
echo "    custom_domain=$SITE_DOMAIN alias=www.$SITE_DOMAIN"

echo "==> 5/5 DNS (set these at the registrar; this script does not touch DNS)"
cat <<TXT
    Remove: URL Redirect record on @, CNAME www -> parkingpage.namecheap.com, parking page.
    Add:    ALIAS  @    apex-loadbalancer.netlify.com   TTL 5 min
            (fallback if ALIAS is unavailable: A  @  75.2.60.5)
    Add:    CNAME  www  ${NETLIFY_SITE_NAME}.netlify.app.  TTL 5 min
    Check:  dig +short $SITE_DOMAIN ; dig +short www.$SITE_DOMAIN CNAME
TXT
if [[ $TLS -eq 1 ]]; then
  echo "==> Requesting the Let's Encrypt certificate (DNS must already resolve to Netlify)"
  netlify api provisionSiteTLSCertificate --data "{\"site_id\":\"$NETLIFY_SITE_ID\"}" >/dev/null \
    && echo "    TLS requested" || echo "    TLS not ready yet; re-run with --provision-tls after DNS propagates"
fi
echo "Done. Verify: curl -sI https://$SITE_DOMAIN | head -1"
