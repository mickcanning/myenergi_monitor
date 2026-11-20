"""Optional single-password login.

Enabled only when APP_PASSWORD is set. Signing in stores a signed session cookie (30 days) that is
HttpOnly, SameSite=Lax, and Secure whenever the request arrived over HTTPS (directly or via a proxy with
BEHIND_PROXY=1). Changing APP_PASSWORD signs everyone out. Repeated wrong passwords from one address are
throttled. /healthz and static files stay open so Docker health checks and the login page itself work.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import timedelta
from pathlib import Path

from flask import abort, redirect, render_template, request, session, url_for
from flask.sessions import SecureCookieSessionInterface

OPEN_ENDPOINTS = {"login", "healthz", "static"}
MAX_FAILURES = 5              # wrong passwords allowed per address...
FAILURE_WINDOW_S = 15 * 60    # ...within this window, before further attempts are refused
FAIL_DELAY_S = 1.0            # slows down guessing even below the limit


class _SessionInterface(SecureCookieSessionInterface):
    """Mark the cookie Secure when the page was served over HTTPS, so direct http://ip:port use still works."""

    def get_cookie_secure(self, app):
        return request.is_secure


def _secret_key(app):
    """SECRET_KEY from the environment, else a random one kept in the instance folder (so restarts keep you signed in)."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = Path(app.config.get("SECRET_KEY_FILE") or Path(app.instance_path) / "secret_key")
    try:
        key = path.read_text().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key)
    os.chmod(path, 0o600)
    return key


def _safe_next(target):
    """Only follow same-site relative paths after login (no //evil.com or http://...)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def init_auth(app):
    password = app.config.get("APP_PASSWORD", os.environ.get("APP_PASSWORD", ""))
    app.config["AUTH_ENABLED"] = bool(password)

    @app.context_processor
    def auth_context():
        return {"auth_enabled": app.config["AUTH_ENABLED"], "signed_in": session.get("pw") is not None}

    if not password:
        return

    app.secret_key = app.config.get("SECRET_KEY") or _secret_key(app)
    app.session_interface = _SessionInterface()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=30))
    # A fingerprint of the current password goes in the session, so changing the password signs everyone out.
    fingerprint = hmac.new(app.secret_key.encode(), password.encode(), hashlib.sha256).hexdigest()[:32]
    failures, lock = {}, threading.Lock()

    def signed_in():
        return hmac.compare_digest(str(session.get("pw", "")), fingerprint)

    def recent_failures(addr, now):
        with lock:
            times = [t for t in failures.get(addr, []) if now - t < FAILURE_WINDOW_S]
            failures[addr] = times
            return len(times)

    @app.before_request
    def require_login():
        if request.endpoint in OPEN_ENDPOINTS or signed_in():
            return None
        # Background calls (live refresh, API, form posts) get a plain 401; the page reloads into the login form.
        wants_page = request.method == "GET" and request.headers.get("X-Requested-With") != "fetch" \
            and not request.path.startswith(("/api/", "/partials/"))
        if not wants_page:
            abort(401)
        nxt = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=nxt))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        nxt = _safe_next(request.values.get("next")) or url_for("overview")
        if signed_in():
            return redirect(nxt)
        error, status = None, 200
        if request.method == "POST":
            addr, now = request.remote_addr or "?", time.time()
            if recent_failures(addr, now) >= MAX_FAILURES:
                error, status = "Too many wrong attempts. Wait 15 minutes and try again.", 429
            elif hmac.compare_digest(request.form.get("password", "").encode(), password.encode()):
                with lock:
                    failures.pop(addr, None)
                session.clear()
                session.permanent = True
                session["pw"] = fingerprint
                return redirect(nxt)
            else:
                with lock:
                    failures.setdefault(addr, []).append(now)
                time.sleep(app.config.get("AUTH_FAIL_DELAY", FAIL_DELAY_S))
                error, status = "That password isn't right.", 401
        return render_template("login.html", error=error, next=nxt), status

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))
