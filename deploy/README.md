# Private hosting on the Mac (Tailscale + launchd)

Two resident processes: the static site server and the backend. Expose both to
**only your own devices** via Tailscale — never the public internet.

Before installing launchd agents, set the machine-local wiki folder in
`backend/.env`:

```sh
PW_CONTENT_DIR=/absolute/path/to/your/wiki-content
```

The site rebuild and backend both read this same setting.

## 1. Serve

```sh
# Site (from repo root) — after `npm run build`
node scripts/serve.mjs --host 127.0.0.1 --port 4321   # static server + security headers

# Backend
cd backend && ./.venv/bin/python -m app.serve   # 127.0.0.1:8787
```

For development, `cd backend && bash run.sh` still creates/updates `.venv` and
then delegates to the same Python entrypoint. Launchd should call the Python
entrypoint directly after dependencies are installed.

`scripts/serve.mjs` serves `dist/` and sets `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff`, and `Referrer-Policy: no-referrer` — the
clickjacking/header protections the app's `<meta>` CSP cannot express (`frame-ancestors`
is ignored in `<meta>`). `tailscale serve` proxies these headers through unchanged.
(`npm run preview` still works for quick local viewing but does not set them.)

For a one-shot local session instead of resident launchd processes, run
`python3 scripts/app_start.py` from the repo root. `./run.sh` is now only a
compatibility wrapper for that Python startup manager.

## 2. Tailscale (private access, your devices only)

```sh
tailscale serve --bg --https 443 http://127.0.0.1:4321   # reading site
tailscale serve --bg --https 8443 http://127.0.0.1:8787  # backend API
# Do NOT use `tailscale funnel` — that would expose it publicly.
```

Confirm your tailnet ACLs restrict these to your own devices. In the site's
**Ingest console → Backend**, set the backend URL to your `https://…:8443` tailnet
address and the auth token.

## 3. Keep-alive (launchd)

Copy the example plists, edit the paths/user, then load:

```sh
cp deploy/com.personalwiki.*.plist.example ~/Library/LaunchAgents/  # drop .example
launchctl load ~/Library/LaunchAgents/com.personalwiki.site.plist
launchctl load ~/Library/LaunchAgents/com.personalwiki.backend.plist
```

Run these as your **login user** (not root) so git ownership matches the vault.
After each ingest the backend runs `REBUILD_CMD` to refresh the site.
