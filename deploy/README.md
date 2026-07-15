# Private hosting on the Mac (Tailscale + launchd)

One resident FastAPI process serves both the built site and the API. Expose it
to **only your own devices** via Tailscale — never the public internet.

Before installing launchd agents, set the machine-local wiki folder in
`backend/.env`:

```sh
PW_CONTENT_DIR=/absolute/path/to/your/wiki-content
```

The site build and backend both read this same setting.

## 1. Serve

```sh
npm run build
cd backend && ./.venv/bin/python -m app.serve   # site + API on 127.0.0.1:8787
```

For development, `cd backend && bash run.sh` still creates/updates `.venv` and
then delegates to the same Python entrypoint. Launchd should call the Python
entrypoint directly after dependencies are installed.

FastAPI serves `dist/` and adds `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and
`Cross-Origin-Opener-Policy: same-origin`. `tailscale serve` proxies these
headers through unchanged.

For a one-shot local session instead of resident launchd processes, run
`python3 scripts/app_start.py` from the repo root. `./run.sh` is now only a
compatibility wrapper for that Python startup manager.

## 2. Tailscale (private access, your devices only)

```sh
tailscale serve --bg --https 443 http://127.0.0.1:8787
# Do NOT use `tailscale funnel` — that would expose it publicly.
```

Confirm your tailnet ACLs restrict this to your own devices. In the site's
ingest console, set the auth token; the API is same-origin.

## 3. Keep-alive (launchd)

Copy the backend example plist, edit the paths/user, then load:

```sh
cp deploy/com.personalwiki.backend.plist.example \
  ~/Library/LaunchAgents/com.personalwiki.backend.plist
launchctl load ~/Library/LaunchAgents/com.personalwiki.backend.plist
```

Run these as your **login user** (not root) so git ownership matches the vault.
After each ingest the backend runs `REBUILD_CMD` to refresh the site.
