import json
import os
import secrets
import threading
from pathlib import Path

from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = Path(__file__).resolve().parent / "users.json"
_lock = threading.Lock()


def _load():
    if not DB_PATH.exists():
        return {"users": {}}
    with open(DB_PATH) as f:
        return json.load(f)


def _save(data):
    tmp = DB_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(DB_PATH)  # atomic on POSIX


def get_user(username):
    with _lock:
        return _load()["users"].get(username)


def all_users():
    with _lock:
        return _load()["users"]


def create_user(username, password, role="user", quota_mb=1000):
    username = username.strip()
    if not username or not password:
        raise ValueError("Username and password are required")
    with _lock:
        data = _load()
        if username in data["users"]:
            raise ValueError("User already exists")
        data["users"][username] = {
            "password_hash": generate_password_hash(password),
            "role": role,
            "quota_mb": quota_mb,
        }
        _save(data)


def delete_user(username):
    with _lock:
        data = _load()
        data["users"].pop(username, None)
        _save(data)


def update_quota(username, quota_mb):
    with _lock:
        data = _load()
        if username in data["users"]:
            data["users"][username]["quota_mb"] = quota_mb
            _save(data)


def set_password(username, password):
    with _lock:
        data = _load()
        if username in data["users"]:
            data["users"][username]["password_hash"] = generate_password_hash(password)
            _save(data)


def verify_password(username, password):
    user = get_user(username)
    if not user:
        return False
    return check_password_hash(user["password_hash"], password)


def ensure_default_admin():
    """Create a default admin account on first run if the DB is empty."""
    with _lock:
        data = _load()
        if data["users"]:
            return
        password = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
        data["users"]["admin"] = {
            "password_hash": generate_password_hash(password),
            "role": "admin",
            "quota_mb": 0,  # 0 = unlimited
        }
        _save(data)
        print("=" * 60)
        print("Created default admin account")
        print("  username: admin")
        print(f"  password: {password}")
        print("Log in and change this, or set ADMIN_PASSWORD before first run.")
        print("=" * 60)
