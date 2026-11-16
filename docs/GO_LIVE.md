# Go-live runbook (contextctl.sh)

Everything is staged so going public is **one command** plus registrar DNS edits.
Nothing here has been run. Run it only after Ayan says "go live".

```bash
scripts/go-live.sh --yes                 # steps 1-5 below
# ...set the DNS records at Namecheap (section "DNS"), wait for propagation...
scripts/go-live.sh --yes --provision-tls # idempotent re-run; requests the TLS cert
```

The domain is a single config value, `SITE_DOMAIN` in `site/site.config.json`
(default `contextctl.sh`). It drives the canonical URL, `sitemap.xml`, Open Graph
tags, the BibTeX `url`, and the go-live script. Override per run with
`SITE_DOMAIN=example.org scripts/go-live.sh --yes`.

## Current state (before go-live)

| Item | State |
|---|---|
| GitHub repo `AyanPutatunda/contextctl` | **private** |
| Netlify site `contextctl` (id `79f2acb7-1b55-4e97-b321-77750a8b9281`, team AyanPutatunda) | draft deploys only, **no production deploy** |
| Indexing | `<meta name="robots" content="noindex,nofollow">`, `robots.txt` = `Disallow: /`, `X-Robots-Tag: noindex, nofollow` |
| Custom domains | none attached |
| DNS for contextctl.sh | untouched (Namecheap PremiumDNS; URL redirect on `@`, `www` CNAME to `parkingpage.namecheap.com`, parking page on) |

## What `scripts/go-live.sh --yes` does

1. **Repo public:** `gh repo edit AyanPutatunda/contextctl --visibility public --accept-visibility-change-consequences`
2. **Indexable site:** sets `INDEXABLE: true` and `REPO_PUBLIC: true` in
   `site/site.config.json`, which removes the noindex meta tag, replaces
   `robots.txt` with `Allow: /` + `Sitemap:`, drops the `X-Robots-Tag` header and
   removes the "access on request" note. Commits and pushes that one-file change.
   (Deploy previews and branch deploys stay `noindex` via `netlify.toml`.)
3. **Production deploy:** `python3 site/build.py` then
   `netlify deploy --prod --dir site/dist --site 79f2acb7-1b55-4e97-b321-77750a8b9281`.
   It asserts the built page says `index,follow` before deploying.
4. **Custom domains:** `netlify api updateSite` with
   `custom_domain = contextctl.sh` and `domain_aliases = [www.contextctl.sh]`
   (apex is primary; Netlify redirects `www` to the apex).
5. **Prints the DNS records** below. The script never changes DNS. With
   `--provision-tls` it also calls `netlify api provisionSiteTLSCertificate`
   (Let's Encrypt), which only succeeds after DNS points at Netlify.

## DNS: contextctl.sh on Namecheap (PremiumDNS)

Namecheap → Domain List → contextctl.sh → **Manage**.

1. **Domain** tab: under *Redirect Domain*, delete any redirect for `contextctl.sh`.
   Keep the nameservers on Namecheap PremiumDNS (no nameserver change needed).
2. **Advanced DNS** tab → *Host Records*:
   * **Delete** the `URL Redirect Record` with host `@`.
   * **Delete** the `CNAME Record` with host `www` → `parkingpage.namecheap.com.`
   * **Delete** any other parking records (e.g. a parking `A`/`CNAME` on `@`).
   * **Add** `ALIAS Record` · Host `@` · Value `apex-loadbalancer.netlify.com` · TTL 5 min
     (Netlify's recommended apex target; if ALIAS is not offered, add an
     `A Record` · Host `@` · Value `75.2.60.5` instead).
   * **Add** `CNAME Record` · Host `www` · Value `contextctl.netlify.app.` · TTL 5 min
3. Leave MX/TXT records alone (email, verification).
4. Check propagation:
   ```bash
   dig +short contextctl.sh            # -> Netlify load balancer IP(s)
   dig +short www.contextctl.sh CNAME  # -> contextctl.netlify.app.
   ```
5. Re-run `scripts/go-live.sh --yes --provision-tls`, then confirm
   `curl -sI https://contextctl.sh | head -1` returns `HTTP/2 200` and
   `https://www.contextctl.sh` redirects to the apex.

Alternative: switch the nameservers to Netlify DNS (Netlify → Domains → Add
domain → set the nameservers Netlify shows at Namecheap) and Netlify
manages the records itself. Not needed for the steps above.

(contextctl.com is registered elsewhere and is out of scope.)

## Rollback

* Unpublish: `netlify api updateSite --data '{"site_id":"79f2acb7-1b55-4e97-b321-77750a8b9281","body":{"custom_domain":null,"domain_aliases":[]}}'`
  and restore the Namecheap records.
* Repo private again: `gh repo edit AyanPutatunda/contextctl --visibility private --accept-visibility-change-consequences`
  (stars/forks may be lost; that is what the flag acknowledges).
* Re-hide the site: set `INDEXABLE: false` and redeploy.

## Before running: checklist for Ayan

- [ ] Confirm the paper PDF may be public as is (its affiliation line is shown on page 1).
- [ ] Confirm the co-author listing on the site/BibTeX.
- [ ] Camera-ready (due Nov 25) final: replace `paper/contextctl_AAIBD2026.pdf` if it changes.
