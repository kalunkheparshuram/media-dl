import os
import re
import uuid
import secrets
import shutil
import subprocess
import threading
import functools
import time
from pathlib import Path

from flask import (
    Flask, render_template, request, jsonify, send_from_directory,
    session, redirect, url_for, abort, flash
)
import yt_dlp

import users as userdb

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

userdb.ensure_default_admin()

# in-memory job store: job_id -> dict(status, percent, message, error, username)
JOBS = {}
JOBS_LOCK = threading.Lock()

# username -> most recent job_id they started, so the index page can
# reconnect to an in-progress download after navigating away and back
LAST_JOB_BY_USER = {}

VIDEO_FORMATS = ["mp4", "mkv", "webm"]
VIDEO_QUALITIES = ["best", "2160", "1440", "1080", "720", "480", "360"]
AUDIO_FORMATS = ["mp3", "opus", "m4a", "wav", "flac"]


# ------------------------------------------------------------- helpers ----

def user_dir(username: str) -> Path:
    d = DOWNLOAD_DIR / username
    d.mkdir(parents=True, exist_ok=True)
    return d


def dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def safe_resolve(base: Path, subpath: str) -> Path:
    """Resolve subpath under base, refusing to escape it."""
    target = (base / subpath).resolve()
    base = base.resolve()
    if target != base and base not in target.parents:
        abort(403)
    return target


# --------------------------------------------------------------- auth -----

def current_user():
    username = session.get("username")
    if not username:
        return None
    u = userdb.get_user(username)
    if not u:
        session.clear()
        return None
    u = dict(u)
    u["username"] = username
    return u


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        u = current_user()
        if not u or u["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if userdb.verify_password(username, password):
            session.clear()
            session["username"] = username
            return redirect(request.args.get("next") or url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------- job runner ----
#
#class QuotaExceededError(Exception):
#    """Raised from inside a progress hook to hard-stop a download that
#    would push the user over their storage quota."""
#    def __init__(self, message, partial_path=None):
#        super().__init__(message)
#        self.partial_path = partial_path


#class JobCancelledError(Exception):
#    """Raised from inside a progress hook when the user hits Stop."""
#    def __init__(self, message, partial_path=None):
#        super().__init__(message)
#        self.partial_path = partial_path

class QuotaExceededError(BaseException):
    """Raised from inside a progress hook to hard-stop a download that
    would push the user over their storage quota."""
    def __init__(self, message, partial_path=None):
        super().__init__(message)
        self.partial_path = partial_path


class JobCancelledError(BaseException):
    """Raised from inside a progress hook when the user hits Stop."""
    def __init__(self, message, partial_path=None):
        super().__init__(message)
        self.partial_path = partial_path


def is_playlist(url: str) -> bool:
    opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return bool(info.get("entries"))


def cleanup_partial(path_str):
    """Best-effort removal of a partially-downloaded file and its
    in-progress fragments after a quota abort."""
    if not path_str:
        return
    p = Path(path_str)
    for candidate in [p, Path(str(p) + ".part"), Path(str(p) + ".ytdl")]:
        try:
            if candidate.exists():
                candidate.unlink()
        except OSError:
            pass


def make_progress_hook(job_id, quota_bytes, base_usage, state):
    """quota_bytes <= 0 means unlimited: no enforcement.
    base_usage is the user's disk usage measured once, right before the
    job starts; we track bytes finished/downloading this job on top of it
    so we don't have to re-walk the whole directory tree on every tick.
    `state` is shared with the caller so it can report a final summary
    (e.g. playlist item count) once the job completes."""

    def hook(d):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            return

        if job.get("cancel_requested"):
            raise JobCancelledError(
                "Cancelled by user.",
                partial_path=d.get("tmpfilename") or d.get("filename"),
            )

        info = d.get("info_dict", {}) or {}
        idx = info.get("playlist_index")
        total_items = info.get("n_entries")
        if idx and total_items:
            state["playlist_idx"] = idx
            state["playlist_total"] = total_items
        item = (
            f"file {state['playlist_idx']}/{state['playlist_total']}"
            if state["playlist_total"] else None
        )

        raw_name = d.get("filename") or d.get("tmpfilename") or info.get("title") or "download"
        name = Path(raw_name).name

        if d["status"] == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate")
            speed = d.get("speed")

            if quota_bytes > 0:
                projected = base_usage + state["finished_bytes"] + downloaded
                if projected > quota_bytes:
                    raise QuotaExceededError(
                        "Storage quota exceeded — download stopped.",
                        partial_path=d.get("tmpfilename") or d.get("filename"),
                    )

            size_str = format_size(downloaded)
            if total_bytes:
                size_str += f" of {format_size(total_bytes)}"
            speed_str = f"{format_size(speed)}/s" if speed else "starting..."
            pct = d.get("_percent_str", "").strip()

            # filename | file size | file number (playlists only) | speed
            fields = [name, size_str]
            if item:
                fields.append(item)
            fields.append(speed_str)

            with JOBS_LOCK:
                job["percent"] = pct
                job["message"] = "  |  ".join(fields)

        elif d["status"] == "finished":
            state["finished_bytes"] += d.get("downloaded_bytes") or d.get("total_bytes") or 0
            suffix = f" ({item})" if item else ""
            with JOBS_LOCK:
                job["message"] = f"{name} downloaded{suffix}, processing..."

    return hook


def make_postprocessor_hook(job_id, state):
    """Reports what's happening during merge/convert/finalize steps in the
    same filename | ... | file X/Y style as the download progress line,
    instead of a bare 'Processing (ExtractAudio)...' with no context."""
    NAMES = {
        "Merger": "Merging video and audio",
        "FFmpegExtractAudio": "Converting to audio format",
        "ExtractAudio": "Converting to audio format",
        "FFmpegVideoConvertor": "Converting video format",
        "VideoConvertor": "Converting video format",
        "FFmpegVideoRemuxer": "Remuxing video",
        "VideoRemuxer": "Remuxing video",
        "FFmpegMetadata": "Writing metadata",
        "Metadata": "Writing metadata",
        "MoveFiles": "Finalizing file",
        "FixupM3u8": "Fixing up video",
        "FixupMP4": "Fixing up video",
    }

    def hook(d):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            return
        if job.get("cancel_requested"):
            raise JobCancelledError("Cancelled by user.")
        if d["status"] != "started":
            return

        pp = d.get("postprocessor", "")
        action = NAMES.get(pp, f"Processing ({pp})")

        info = d.get("info_dict", {}) or {}
        raw_name = info.get("filepath") or info.get("_filename") or info.get("title") or "file"
        name = Path(raw_name).name

        fields = [name, action]
        if state["playlist_total"]:
            fields.append(f"file {state['playlist_idx']}/{state['playlist_total']}")

        with JOBS_LOCK:
            job["message"] = "  |  ".join(fields)

    return hook


# def run_image_job(job_id, url, udir, quota_bytes):
    # """Downloads images/galleries (Instagram, Facebook, Twitter/X, Pinterest,
    # Reddit, Tumblr, and everything else gallery-dl supports) via the
    # gallery-dl CLI, since these are image posts, not video/audio, and
    # yt-dlp doesn't cover them. Returns the number of files saved.

    # gallery-dl organizes its own subfolders per site/user under -D, similar
    # in spirit to how playlists get their own folder for video/audio."""
    # if shutil.which("gallery-dl") is None:
        # raise Exception(
            # "gallery-dl is not installed on the server. Run: pip install gallery-dl"
        # )

    # cmd = ["gallery-dl", "-D", str(udir), "--no-colors"]
    # cookies_path = BASE_DIR / "cookies.txt"
    # if cookies_path.exists():
        # cmd += ["--cookies", str(cookies_path)]
    # cmd.append(url)

    # proc = subprocess.Popen(
        # cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # text=True, bufsize=1,
    # )

    # file_count = 0
    # recent_lines = []

    # start_time = time.time()
    # last_speed_check_time = start_time
    # last_speed_check_size = 0
    # speed_bps = None

    # try:
        # for line in proc.stdout:
            # line = line.rstrip("\n")
            # if not line:
                # continue
            # recent_lines.append(line)
            # if len(recent_lines) > 20:
                # recent_lines.pop(0)

            # with JOBS_LOCK:
                # job = JOBS.get(job_id)
            # if not job:
                # proc.terminate()
                # return file_count

            # if job.get("cancel_requested"):
                # proc.terminate()
                # try:
                    # proc.wait(timeout=5)
                # except subprocess.TimeoutExpired:
                    # proc.kill()
                # raise JobCancelledError("Cancelled by user.")

            # # gallery-dl prints the destination path of each file it saves
            # is_saved_file = not line.startswith("[") and "." in line.rsplit("/", 1)[-1]
            # file_size_str = None
            # if is_saved_file:
                # file_count += 1
                # try:
                    # file_size_str = format_size(Path(line).stat().st_size)
                # except OSError:
                    # file_size_str = None

            # # dir_size_bytes() walks the whole folder, so only re-measure
            # # every couple seconds instead of on every single output line —
            # # this doubles as both the quota check and the speed estimate
            # now = time.time()
            # if now - last_speed_check_time > 2:
                # current_size = dir_size_bytes(udir)
                # elapsed = now - last_speed_check_time
                # if elapsed > 0:
                    # speed_bps = max(0, current_size - last_speed_check_size) / elapsed
                # last_speed_check_time = now
                # last_speed_check_size = current_size

                # if quota_bytes > 0 and current_size >= quota_bytes:
                    # proc.terminate()
                    # try:
                        # proc.wait(timeout=5)
                    # except subprocess.TimeoutExpired:
                        # proc.kill()
                    # raise QuotaExceededError("Storage quota exceeded — download stopped.")

            # # filename | file size | image count | speed — same style as
            # # the video/audio progress line
            # name = Path(line).name if is_saved_file else "gallery-dl"
            # speed_str = f"{format_size(speed_bps)}/s" if speed_bps else "..."
            # fields = [name]
            # if file_size_str:
                # fields.append(file_size_str)
            # fields.append(f"image {file_count}")
            # fields.append(speed_str)

            # with JOBS_LOCK:
                # job["message"] = "  |  ".join(fields)

        # proc.wait()
        # if proc.returncode != 0:
            # tail = "\n".join(recent_lines[-5:]) or "no output"
            # raise Exception(f"gallery-dl exited with an error:\n{tail}")

    # finally:
        # if proc.poll() is None:
            # proc.terminate()

    # return file_count


def run_image_job(job_id, url, dest_dir, user_root, quota_bytes):
    """Downloads images/galleries using gallery-dl."""
    if shutil.which("gallery-dl") is None:
        raise Exception(
            "gallery-dl is not installed on the server. Run: pip install gallery-dl"
        )

    # Use dest_dir instead of udir for the download destination
    cmd = ["gallery-dl", "-D", str(dest_dir), "--no-colors"]
    cookies_path = BASE_DIR / "cookies.txt"
    if cookies_path.exists():
        cmd += ["--cookies", str(cookies_path)]
    cmd.append(url)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )

    file_count = 0
    recent_lines = []

    start_time = time.time()
    last_speed_check_time = start_time
    last_speed_check_size = 0
    speed_bps = None

    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            recent_lines.append(line)
            if len(recent_lines) > 20:
                recent_lines.pop(0)

            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                proc.terminate()
                return file_count

            if job.get("cancel_requested"):
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise JobCancelledError("Cancelled by user.")

            is_saved_file = not line.startswith("[") and "." in line.rsplit("/", 1)[-1]
            file_size_str = None
            if is_saved_file:
                file_count += 1
                try:
                    file_size_str = format_size(Path(line).stat().st_size)
                except OSError:
                    file_size_str = None

            now = time.time()
            if now - last_speed_check_time > 2:
                # IMPORTANT: Measure the user_root so quota includes everything, not just images
                current_size = dir_size_bytes(user_root)
                elapsed = now - last_speed_check_time
                if elapsed > 0:
                    speed_bps = max(0, current_size - last_speed_check_size) / elapsed
                last_speed_check_time = now
                last_speed_check_size = current_size

                if quota_bytes > 0 and current_size >= quota_bytes:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise QuotaExceededError("Storage quota exceeded — download stopped.")

            name = Path(line).name if is_saved_file else "gallery-dl"
            speed_str = f"{format_size(speed_bps)}/s" if speed_bps else "..."
            fields = [name]
            if file_size_str:
                fields.append(file_size_str)
            fields.append(f"image {file_count}")
            fields.append(speed_str)

            with JOBS_LOCK:
                job["message"] = "  |  ".join(fields)

        proc.wait()
        if proc.returncode != 0:
            tail = "\n".join(recent_lines[-5:]) or "no output"
            raise Exception(f"gallery-dl exited with an error:\n{tail}")

    finally:
        if proc.poll() is None:
            proc.terminate()

    return file_count

# def run_job(job_id, username, url, mode, fmt, quality):
    # try:
        # udir = user_dir(username)

        # user = userdb.get_user(username) or {}
        # quota_mb = user.get("quota_mb", 0)
        # quota_bytes = quota_mb * 1024 * 1024 if quota_mb else 0
        # base_usage = dir_size_bytes(udir)

        # if quota_bytes > 0 and base_usage >= quota_bytes:
            # raise QuotaExceededError("Storage quota already reached. Delete some files first.")

        # if mode == "image":
            # file_count = run_image_job(job_id, url, udir, quota_bytes)
            # with JOBS_LOCK:
                # JOBS[job_id]["status"] = "done"
                # JOBS[job_id]["message"] = f"Done! Downloaded {file_count} image(s)."
                # JOBS[job_id]["percent"] = "100%"
            # return

        # playlist = is_playlist(url)

        # with JOBS_LOCK:
            # if JOBS[job_id].get("cancel_requested"):
                # raise JobCancelledError("Cancelled by user.")

        # if playlist:
            # outtmpl = str(udir / "%(playlist_title)s/%(title).150s.%(ext)s")
        # else:
            # outtmpl = str(udir / "%(title).150s.%(ext)s")

        # progress_state = {"finished_bytes": 0, "playlist_idx": None, "playlist_total": None}

        # ydl_opts = {
            # "outtmpl": outtmpl,
            # "progress_hooks": [make_progress_hook(job_id, quota_bytes, base_usage, progress_state)],
            # "postprocessor_hooks": [make_postprocessor_hook(job_id, progress_state)],
            # "quiet": True,
            # "no_warnings": True,
            # # False (not "ignoreerrors": True) on purpose: a quota abort raised
            # # from the progress hook must propagate and stop the whole job,
            # # including the rest of a playlist, rather than being swallowed
            # # and silently skipped to the next item.
            # "ignoreerrors": False,
            # "noplaylist": False,
        # }

        # if mode == "video":
            # if quality == "best":
                # ydl_opts["format"] = "bestvideo+bestaudio/best"
            # else:
                # h = quality
                # ydl_opts["format"] = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
            # ydl_opts["merge_output_format"] = fmt
        # else:
            # ydl_opts["format"] = "bestaudio/best"
            # ydl_opts["postprocessors"] = [{
                # "key": "FFmpegExtractAudio",
                # "preferredcodec": fmt,
                # "preferredquality": "192",
            # }]

        # with JOBS_LOCK:
            # JOBS[job_id]["message"] = "Fetching info..."

        # with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # ydl.download([url])

        # if progress_state["playlist_total"]:
            # done_msg = f"Done! Downloaded {progress_state['playlist_total']} file(s)."
        # else:
            # done_msg = "Done!"

        # with JOBS_LOCK:
            # JOBS[job_id]["status"] = "done"
            # JOBS[job_id]["message"] = done_msg
            # JOBS[job_id]["percent"] = "100%"

    # except JobCancelledError as e:
        # cleanup_partial(e.partial_path)
        # with JOBS_LOCK:
            # JOBS[job_id]["status"] = "cancelled"
            # JOBS[job_id]["message"] = str(e)

    # except QuotaExceededError as e:
        # cleanup_partial(e.partial_path)
        # with JOBS_LOCK:
            # JOBS[job_id]["status"] = "error"
            # JOBS[job_id]["error"] = str(e)

    # except Exception as e:
        # with JOBS_LOCK:
            # JOBS[job_id]["status"] = "error"
            # JOBS[job_id]["error"] = str(e)

def run_job(job_id, username, url, mode, fmt, quality):
    try:
        udir = user_dir(username)
        
        # --- NEW: Create specific subfolder based on the mode ---
        target_dir = udir / mode
        target_dir.mkdir(parents=True, exist_ok=True)

        user = userdb.get_user(username) or {}
        quota_mb = user.get("quota_mb", 0)
        quota_bytes = quota_mb * 1024 * 1024 if quota_mb else 0
        base_usage = dir_size_bytes(udir)

        if quota_bytes > 0 and base_usage >= quota_bytes:
            raise QuotaExceededError("Storage quota already reached. Delete some files first.")

        if mode == "image":
            # Pass BOTH the target_dir for saving, and udir for checking global quota
            file_count = run_image_job(job_id, url, target_dir, udir, quota_bytes)
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["message"] = f"Done! Downloaded {file_count} image(s)."
                JOBS[job_id]["percent"] = "100%"
            return

        playlist = is_playlist(url)

        with JOBS_LOCK:
            if JOBS[job_id].get("cancel_requested"):
                raise JobCancelledError("Cancelled by user.")

        # --- NEW: Save video/audio into target_dir instead of udir ---
        if playlist:
            outtmpl = str(target_dir / "%(playlist_title)s/%(title).150s.%(ext)s")
        else:
            outtmpl = str(target_dir / "%(title).150s.%(ext)s")

        progress_state = {"finished_bytes": 0, "playlist_idx": None, "playlist_total": None}

        ydl_opts = {
            "outtmpl": outtmpl,
            "progress_hooks": [make_progress_hook(job_id, quota_bytes, base_usage, progress_state)],
            "postprocessor_hooks": [make_postprocessor_hook(job_id, progress_state)],
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "noplaylist": False,
        }

        if mode == "video":
            if quality == "best":
                ydl_opts["format"] = "bestvideo+bestaudio/best"
            else:
                h = quality
                ydl_opts["format"] = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
            ydl_opts["merge_output_format"] = fmt
        else:
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt,
                "preferredquality": "192",
            }]

        with JOBS_LOCK:
            JOBS[job_id]["message"] = "Fetching info..."

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if progress_state["playlist_total"]:
            done_msg = f"Done! Downloaded {progress_state['playlist_total']} file(s)."
        else:
            done_msg = "Done!"

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["message"] = done_msg
            JOBS[job_id]["percent"] = "100%"

    except JobCancelledError as e:
        cleanup_partial(e.partial_path)
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "cancelled"
            JOBS[job_id]["message"] = str(e)

    except QuotaExceededError as e:
        cleanup_partial(e.partial_path)
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)

    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)
# -------------------------------------------------------------- routes ----

@app.route("/")
@login_required
def index():
    u = current_user()
    used = dir_size_bytes(user_dir(u["username"]))
    quota_mb = u["quota_mb"]
    return render_template(
        "index.html",
        user=u,
        used_fmt=format_size(used),
        quota_fmt=("Unlimited" if quota_mb == 0 else format_size(quota_mb * 1024 * 1024)),
        percent_used=(0 if quota_mb == 0 else min(100, round(used / (quota_mb * 1024 * 1024) * 100, 1))),
        video_formats=VIDEO_FORMATS,
        video_qualities=VIDEO_QUALITIES,
        audio_formats=AUDIO_FORMATS,
    )


@app.route("/start", methods=["POST"])
@login_required
def start():
    u = current_user()
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    mode = data.get("mode")
    fmt = data.get("format")
    quality = data.get("quality", "best")

    if not url:
        return jsonify({"error": "URL is required"}), 400
    if mode not in ("video", "audio", "image"):
        return jsonify({"error": "Invalid mode"}), 400
    if mode == "video" and fmt not in VIDEO_FORMATS:
        return jsonify({"error": "Invalid video format"}), 400
    if mode == "audio" and fmt not in AUDIO_FORMATS:
        return jsonify({"error": "Invalid audio format"}), 400

    quota_mb = u["quota_mb"]
    if quota_mb > 0:
        used = dir_size_bytes(user_dir(u["username"]))
        if used >= quota_mb * 1024 * 1024:
            return jsonify({"error": "Storage quota reached. Delete some files first."}), 403

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "percent": "0%", "message": "Queued",
            "error": None, "username": u["username"], "cancel_requested": False,
        }
        LAST_JOB_BY_USER[u["username"]] = job_id

    t = threading.Thread(
        target=run_job, args=(job_id, u["username"], url, mode, fmt, quality), daemon=True
    )
    t.start()
    return jsonify({"job_id": job_id})


# @app.route("/status/<job_id>")
# @login_required
# def status(job_id):
    # u = current_user()
    # with JOBS_LOCK:
        # job = JOBS.get(job_id)
        # if not job:
            # return jsonify({"error": "unknown job"}), 404
        # if job["username"] != u["username"] and u["role"] != "admin":
            # abort(403)
        # return jsonify(job)
@app.route("/status/<job_id>")
@login_required
def status(job_id):
    u = current_user()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if job["username"] != u["username"] and u["role"] != "admin":
            abort(403)
        
        # Calculate dynamic live storage info
        used = dir_size_bytes(user_dir(job["username"]))
        quota_mb = u["quota_mb"]
        quota_bytes = quota_mb * 1024 * 1024
        percent_used = 0 if quota_mb == 0 else min(100, round(used / quota_bytes * 100, 1))
        
        # Create a response copy and append the live storage metrics
        job_copy = dict(job)
        job_copy["storage"] = {
            "used_fmt": format_size(used),
            "quota_fmt": "Unlimited" if quota_mb == 0 else format_size(quota_bytes),
            "percent_used": percent_used
        }
        return jsonify(job_copy)


@app.route("/stop/<job_id>", methods=["POST"])
@login_required
def stop_job(job_id):
    u = current_user()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if job["username"] != u["username"] and u["role"] != "admin":
            abort(403)
        if job["status"] != "running":
            return jsonify({"error": "Job already finished"}), 400
        job["cancel_requested"] = True
        job["message"] = "Stopping..."
    return jsonify({"ok": True})


# @app.route("/current-job")
# @login_required
# def current_job():
    # """Lets the index page reconnect to whatever job this user last
    # started, so progress keeps showing even after navigating away and
    # back (the download itself never stopped — only the browser lost
    # track of which job to poll). Only returns it while still running —
    # once a job is done/errored/cancelled it's been shown already, so a
    # fresh page load starts with a clean status area instead of
    # resurrecting an old result."""
    # u = current_user()
    # with JOBS_LOCK:
        # job_id = LAST_JOB_BY_USER.get(u["username"])
        # if not job_id or job_id not in JOBS or JOBS[job_id]["status"] != "running":
            # return jsonify({"job_id": None})
        # return jsonify({"job_id": job_id, **JOBS[job_id]})

@app.route("/current-job")
@login_required
def current_job():
    u = current_user()
    with JOBS_LOCK:
        job_id = LAST_JOB_BY_USER.get(u["username"])
        if not job_id or job_id not in JOBS or JOBS[job_id]["status"] != "running":
            return jsonify({"job_id": None})
        
        # Calculate dynamic live storage info
        used = dir_size_bytes(user_dir(u["username"]))
        quota_mb = u["quota_mb"]
        quota_bytes = quota_mb * 1024 * 1024
        percent_used = 0 if quota_mb == 0 else min(100, round(used / quota_bytes * 100, 1))
        
        job_copy = dict(JOBS[job_id])
        job_copy["job_id"] = job_id
        job_copy["storage"] = {
            "used_fmt": format_size(used),
            "quota_fmt": "Unlimited" if quota_mb == 0 else format_size(quota_bytes),
            "percent_used": percent_used
        }
        return jsonify(job_copy)


@app.route("/files")
@app.route("/files/")
@app.route("/files/<path:subpath>")
@login_required
def files(subpath=""):
    u = current_user()
    if u["role"] == "admin":
        base = DOWNLOAD_DIR
    else:
        base = user_dir(u["username"])

    target = safe_resolve(base, subpath)
    if target.is_dir():
        entries = []
        for p in sorted(target.iterdir()):
            entries.append({
                "name": p.name,
                "is_dir": p.is_dir(),
                "path": str(p.relative_to(base)),
                "size": format_size(dir_size_bytes(p) if p.is_dir() else p.stat().st_size),
            })
        rel = target.relative_to(base)
        parent = str(rel.parent) if target != base else None

        quota_mb = u["quota_mb"]
        total_used = dir_size_bytes(user_dir(u["username"]))
        usage_summary = (
            f"{format_size(total_used)} used of Unlimited"
            if quota_mb == 0 else
            f"{format_size(total_used)} used of {format_size(quota_mb * 1024 * 1024)}"
        )

        return render_template(
            "files.html", entries=entries, current=str(rel), parent=parent,
            user=u, usage_summary=usage_summary,
        )
    else:
        return send_from_directory(target.parent, target.name, as_attachment=True)


@app.route("/files/delete/<path:subpath>", methods=["POST"])
@login_required
def delete_file(subpath):
    u = current_user()
    base = DOWNLOAD_DIR if u["role"] == "admin" else user_dir(u["username"])
    target = safe_resolve(base, subpath)

    if target == base:
        abort(403)  # never let anyone wipe the whole downloads root this way

    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()

    parent = str(target.parent.relative_to(base))
    if parent == ".":
        return redirect(url_for("files"))
    return redirect(url_for("files", subpath=parent))


# -------------------------------------------------------------- admin -----

@app.route("/admin")
@admin_required
def admin_dashboard():
    rows = []
    for username, u in userdb.all_users().items():
        used = dir_size_bytes(user_dir(username))
        rows.append({
            "username": username,
            "role": u["role"],
            "quota_mb": u["quota_mb"],
            "used_fmt": format_size(used),
            "quota_fmt": "Unlimited" if u["quota_mb"] == 0 else f"{u['quota_mb']} MB",
        })
    return render_template("admin.html", rows=rows, user=current_user())


@app.route("/admin/users", methods=["POST"])
@admin_required
def admin_create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    try:
        quota_mb = int(request.form.get("quota_mb", "1000"))
    except ValueError:
        quota_mb = 1000
    try:
        userdb.create_user(username, password, role=role, quota_mb=quota_mb)
        flash(f"Created user '{username}'")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<username>/quota", methods=["POST"])
@admin_required
def admin_update_quota(username):
    try:
        quota_mb = int(request.form.get("quota_mb", "0"))
    except ValueError:
        quota_mb = 0
    userdb.update_quota(username, quota_mb)
    flash(f"Updated quota for '{username}'")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<username>/password", methods=["POST"])
@admin_required
def admin_reset_password(username):
    new_password = request.form.get("password", "")
    if new_password:
        userdb.set_password(username, new_password)
        flash(f"Password reset for '{username}'")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<username>/delete", methods=["POST"])
@admin_required
def admin_delete_user(username):
    u = current_user()
    if username == u["username"]:
        flash("You can't delete your own account while logged in.", "error")
        return redirect(url_for("admin_dashboard"))
    userdb.delete_user(username)
    flash(f"Deleted user '{username}' (their files were left in place)")
    return redirect(url_for("admin_dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
