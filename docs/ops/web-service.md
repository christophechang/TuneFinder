# Running the TuneFinder web service

The web API (`tunefinder serve`, FastAPI) runs on the Mac mini next to `data/`
— the same checkout the Sunday launchd run uses. Architecture:
`docs/architecture/tunefinder-web.md`.

## 1. Environment

In the checkout's `.env` (see `.env.example`):

```
TUNEFINDER_API_SECRET=<long random string>     # required (or TUNEFINDER_WEB_INSECURE=1 on a trusted LAN)
TUNEFINDER_WEB_STATIC_DIR=/path/to/tunefinder-web/dist   # optional: serve the SPA from the same origin
TUNEFINDER_WEB_BASE_URL=https://tunefinder.example.com   # optional: Discord reports link here
TUNEFINDER_WEB_ALLOWED_ORIGINS=https://tunefinder.example.com  # only for a separately-hosted SPA
```

Generate a secret: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.

Sanity check: `./venv/bin/python -m tunefinder serve` then open
`http://127.0.0.1:8420/api/health` (no auth needed) and `/docs`.

## 2. launchd unit (keep-alive)

A ready-to-edit unit ships in the repo root as `com.openclaw.tunefinder-web.plist`
(alongside the weekly-run `com.openclaw.tune-finder.plist`). Copy it to
`~/Library/LaunchAgents/`, replace every `YOUR_ADMIN_USER` with your macOS username,
and confirm the paths match your checkout. For reference, its contents:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.openclaw.tunefinder-web</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOUR_USER/OpenClaw/Automations/TuneFinder/venv/bin/python</string>
    <string>-m</string><string>tunefinder</string><string>serve</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8420</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/YOUR_USER/OpenClaw/Automations/TuneFinder</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/YOUR_USER/OpenClaw/Automations/TuneFinder/logs/web.log</string>
  <key>StandardErrorPath</key><string>/Users/YOUR_USER/OpenClaw/Automations/TuneFinder/logs/web.log</string>
</dict>
</plist>
```

`launchctl load ~/Library/LaunchAgents/com.openclaw.tunefinder-web.plist`.
(Env vars come from `.env` via the app's `load_dotenv`; no need to inline them.)

The Sunday weekly run and web-triggered runs share the `data/` run lock — a
collision fails cleanly with "another TuneFinder run is in progress" instead
of corrupting stores.

## 3. Access from anywhere — Cloudflare Tunnel (recommended)

Outbound-only, no port forwarding, same "no inbound connections to the home
network" property as MixLab's queue:

```bash
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create tunefinder
# ~/.cloudflared/config.yml → ingress: tunefinder.yourdomain.com → http://127.0.0.1:8420
cloudflared tunnel route dns tunefinder tunefinder.yourdomain.com
sudo cloudflared service install   # keep-alive daemon
```

Optionally put Cloudflare Access (email OTP) in front of the hostname —
bearer auth still applies behind it. Alternatives: Tailscale (zero config,
private) or plain LAN.

## 4. The SPA

Simplest (one origin, zero CORS): build tunefinder-web and point
`TUNEFINDER_WEB_STATIC_DIR` at its `dist/`. The tunnel hostname then serves
both app and API.

Alternative: Cloudflare Pages hosting for the SPA (`tunefinder-web` README §
Deployment) — then add the Pages origin to `TUNEFINDER_WEB_ALLOWED_ORIGINS`
and point the SPA's Settings at the tunnel hostname.

## 5. Operational notes

- `data/web_jobs.json` — recent web-triggered runs (survives restarts;
  anything caught mid-run at restart is marked failed/interrupted).
- Report artifacts (`data/reports/report_*.json`) are never pruned — they are
  the web app's browsing history. Audition HTML keeps its 26-run retention.
- Secret rotation: change `TUNEFINDER_API_SECRET` in `.env`, restart the
  service (`launchctl kickstart -k gui/$UID/com.openclaw.tunefinder-web`),
  re-enter in the SPA's Settings.
- The service starts (with a logged warning) without Discord env vars —
  browsing and feedback work; live runs would skip Discord delivery.
