# Media Downloader

Small self-hosted Flask app around `yt-dlp` (video/audio) and `gallery-dl`
(images/galleries) for downloading from various platforms — styled as a
retro-futuristic "Outrun Aero" glass UI (80s/90s synthwave grid-and-sun
background, Frutiger Aero-style glossy glass panels and buttons).

- Video formats: mp4, mkv, webm (with quality cap: 2160/1440/1080/720/480/360/best)
- Audio formats: mp3, opus, m4a, wav, flac
- **Images**: Instagram, Facebook, Twitter/X, Pinterest, Reddit, Tumblr, and
  everything else `gallery-dl` supports — downloads originals as-is, no
  format conversion
- Playlists are auto-detected and saved into `downloads/<username>/<playlist title>/`
- Single videos go into `downloads/<username>/`
- Plain HTML + minimal CSS, no JS framework
- Per-user login backed by a local `users.json` file (no external database)
- Admin dashboard: create/delete users, set/change per-user storage quotas, reset passwords
- Each user can browse and delete their own downloaded files; admins can browse/delete anyone's
- Live progress: filename, size, playlist/gallery position, and speed
- Stop button to cancel an in-progress download
- Storage quotas enforced in real time, not just at the start of a download

## Accounts & quotas

User accounts live in `users.json` (created automatically next to `app.py`,
password hashed with Werkzeug's `generate_password_hash` — never stored in
plain text).

On first run, if `users.json` doesn't exist yet, a default **admin** account
is created automatically and the password is printed once to the console:

```
============================================================
Created default admin account
  username: admin
  password: <randomly generated>
Log in and change this, or set ADMIN_PASSWORD before first run.
============================================================
```

To set your own admin password instead of a random one, set `ADMIN_PASSWORD`
before the first run:

```bash
ADMIN_PASSWORD=yourpassword python3 app.py
```

Log in as admin, go to **Admin** in the nav bar, and from there you can:
- Create new users (username, password, role, quota in MB — 0 = unlimited)
- Change any user's storage quota
- Reset any user's password
- Delete a user (their downloaded files are left on disk, not deleted)

Regular users see a storage usage bar on the main page and get blocked from
starting new downloads once they're at/over quota — they just need to delete
some files first (via **My files**) to free up space.

**Quota enforcement:** checked before a download starts, and again in real
time during the download itself — once a user's usage would cross their
quota, the in-progress file is aborted and its partial data is cleaned up.
One trade-off: to make that hard stop reliable across a playlist, a failed
item (network hiccup, region-blocked video, etc.) will now stop the rest
of the playlist too, instead of being skipped — predictable quota
enforcement was prioritized over skip-and-continue resilience.

## 1. Server prerequisites

```bash
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg
```

`ffmpeg` is required for merging video+audio and for audio conversion.

## 2. Install

```bash
sudo mkdir -p /opt/media-dl
sudo cp -r ./* /opt/media-dl/
cd /opt/media-dl
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 3. Run it

Quick test (foreground):

```bash
cd /opt/media-dl
ADMIN_PASSWORD=yourpassword ./venv/bin/python app.py
```

Then visit `http://<server-ip>:5000` from any device on your network and
log in with `admin` / the password you set (or the auto-generated one
printed to the console — see "Accounts & quotas" below).

## 4. Run as a systemd service (recommended)

Edit `media-dl.service` (set your real `APP_PASSWORD`, and `User=` to your
Linux username instead of `%i`), then:

```bash
sudo cp media-dl.service /etc/systemd/system/media-dl.service
sudo systemctl daemon-reload
sudo systemctl enable --now media-dl
sudo systemctl status media-dl
```

Logs: `journalctl -u media-dl -f`

## 5. Reverse proxy (optional, if exposing outside your LAN)

Put nginx or Caddy in front with HTTPS rather than exposing port 5000
directly. Example Caddyfile:

```
downloads.yourdomain.com {
    reverse_proxy localhost:5000
}
```

## Image downloads (Instagram, Facebook, etc.)

The "Images" mode uses `gallery-dl`, a separate tool from `yt-dlp` built for
image posts and galleries rather than video. It's installed automatically
via `requirements.txt`. It works on public posts/profiles without any setup
for most sites.

Some sites (Instagram in particular) increasingly require being logged in
to reliably fetch content, especially whole profiles. If you hit login
walls or rate limits, export your browser cookies for that site to a
Netscape-format `cookies.txt` file (browser extensions like "Get
cookies.txt" can do this) and place it at:

```
/opt/media-dl/cookies.txt
```

If that file exists, it's automatically passed to `gallery-dl` for every
image download. Treat it like a password — it grants access to your
logged-in session on that site.

## Notes

- Files land in `/opt/media-dl/downloads/<username>/`. Point Samba/NFS/Jellyfin
  at that folder if you want the family to browse the results directly, or use
  the built-in `/files` browser in the app (scoped to each user's own folder;
  admins can browse everyone's).
- `users.json` sits next to `app.py`. Back it up along with your downloads if
  you care about not having to recreate accounts after a reinstall.
- yt-dlp is updated frequently as sites change their pages. Keep it fresh:
  `./venv/bin/pip install -U yt-dlp`
- Downloads run one thread per request with no queue limit — for a small
  family/friends group that's fine, but if several big playlists get
  kicked off at once it'll just use more CPU/bandwidth concurrently.
- Respect the terms of service of whatever platforms you're downloading from.
