"""HTTP client for my.epitech.eu: Bearer auth, rotating refresh, SSO fallback (stdlib only)."""
import base64
import fcntl
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://my.epitech.eu/api"
TOKEN_FILE = Path(os.environ.get("EPIPLAN_TOKENS", "~/.config/epiplan/tokens.json")).expanduser()
HERE = Path(__file__).resolve().parent


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class AuthError(Exception):
    """No usable credentials left (API refresh and SSO fallback both failed)."""


def jwt_claims(token):
    try:
        return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    except Exception:
        return {}


class Client:
    def __init__(self):
        self.access = os.environ.get("EPITECH_ACCESS_TOKEN")
        self.refresh_token = os.environ.get("EPITECH_REFRESH_TOKEN")
        self._profile = None
        if not (self.access or self.refresh_token):
            self._load()
        if not (self.access or self.refresh_token):
            self._sso_login("no tokens")

    def _load(self):
        if TOKEN_FILE.exists():
            data = json.loads(TOKEN_FILE.read_text())
            self.access, self.refresh_token = data.get("access_token"), data.get("refresh_token")

    def _save(self):
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"access_token": self.access, "refresh_token": self.refresh_token}, f)

    def _raw(self, method, path, params=None, body=None, auth=True):
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if auth and self.access:
            headers["Authorization"] = f"Bearer {self.access}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode(errors="replace")[:500])

    def refresh(self):
        """One refresh at a time (file lock); another process may already have done it."""
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            stale = self.refresh_token
            self._load()
            if self.refresh_token != stale:
                return
            try:
                t = self._raw("POST", "/auth/refresh", body={"refresh_token": self.refresh_token}, auth=False)
            except ApiError as e:
                self._sso_login(str(e))
                return
            self.access, self.refresh_token = t["access_token"], t["refresh_token"]
            self._save()

    def _sso_login(self, reason):
        """Refresh is dead: silent Microsoft SSO via epilogin.py, which rewrites tokens.json."""
        venv_py = HERE / ".venv" / "bin" / "python"
        r = subprocess.run([str(venv_py) if venv_py.exists() else sys.executable, str(HERE / "epilogin.py"), "refresh"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise AuthError(f"Login failed ({reason}); SSO fallback: {(r.stdout + r.stderr).strip()[:300]}")
        self._load()

    def call(self, method, path, **kw):
        try:
            return self._raw(method, path, **kw)
        except ApiError as e:
            if e.status != 401:
                raise
        self.refresh()
        return self._raw(method, path, **kw)

    @property
    def profile(self):
        """/auth/verify: login, cursus, semester, ... (cached)."""
        if not self._profile:
            self._profile = self.call("GET", "/auth/verify")
        return self._profile

    @property
    def login(self):
        return self.profile["login"]
