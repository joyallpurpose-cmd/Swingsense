# Hosting SwingSense on GitHub

Two things GitHub does for you here:

1. **GitHub Pages** serves a read-only copy of the app at
   `https://YOUR-USERNAME.github.io/YOUR-REPO/` - open it on your phone
   from anywhere (mobile data, office, travel). Installable as a PWA too.
2. **GitHub Actions** attempts the 6:40 PM scan in the cloud, so signals
   publish even when your PC is off. (Caveat below.)

Your PC app stays the full control center: Run scan button, Settings,
closing positions. The Pages copy is for *viewing* signals on the go.

## PRIVACY - read this first

Free GitHub Pages requires a **public repository**. Your signals, open
positions, and trade history in `docs/data.json` are visible to anyone
who finds the URL. There are no passwords or API keys in it (.gitignore
keeps `.env` and the live `data.json` out), but your trading activity is
public. If that's unacceptable, don't use Pages - ask for the
private-hosting alternative instead.

## One-time setup (15 minutes)

1. Install Git for Windows if needed: https://git-scm.com/download/win
2. Create a repo on github.com: **New repository** -> name it something
   non-obvious (e.g. `ss-data-7x`) -> Public -> Create (no README)
3. In PowerShell, in the swingsense folder:
   ```
   git init
   git add .
   git commit -m "SwingSense AI"
   git branch -M main
   git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
   git push -u origin main
   ```
   (Git will ask you to sign in to GitHub in the browser the first time.)
4. On github.com -> your repo -> **Settings -> Pages** ->
   Source: *Deploy from a branch* -> Branch: `main`, Folder: `/docs` -> Save
5. Wait ~2 minutes, then open
   `https://YOUR-USERNAME.github.io/YOUR-REPO/` - your app, from anywhere.
6. (Optional, for cloud scans with AI ranking) repo **Settings ->
   Secrets and variables -> Actions -> New repository secret**:
   name `GEMINI_API_KEY`, value = your key.

## Daily flow

**Path 1 - PC publishes (reliable):** after your evening scan finishes,
double-click **publish.bat**. It rebuilds the static site from the fresh
data and pushes. Phone URL updates in ~1 minute.

**Path 2 - cloud scans (automatic, experimental):** the included workflow
(`.github/workflows/scan.yml`) runs every trading day at 6:40 PM IST and
on demand (repo -> Actions -> Daily scan -> Run workflow). **Honest
caveat:** NSE often blocks datacenter IPs, and GitHub runners are
datacenter IPs. The first run downloads ~1 year of history (10-20 min,
cached afterward) - watch its log. If downloads 403 consistently, cloud
scanning is off the table for NSE data and Path 1 is your pipeline;
Pages hosting still works perfectly either way.

Note: cloud scans and PC scans each maintain their own price cache but
share `docs/data.json` as the published state - the workflow restores
from it before scanning, so recommendations and history stay continuous.

## What's NOT on the Pages copy (by design)

Run scan, Settings, and Close-position buttons - those need the server.
Close positions in the PC app; the next publish reflects it.

## Phone install

Open the Pages URL -> browser menu -> Add to Home Screen. Same PWA,
now reachable from anywhere with internet.
