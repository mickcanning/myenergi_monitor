"""Container entrypoint: make the data folder writable, drop root, then exec gunicorn.

One worker on purpose: the live CT chart history and the report caches are kept in process memory,
so several workers would each hold a different, partial history. Threads handle concurrent requests.
"""
import os

DATA = "/app/instance"
port = os.environ.get("PORT", "5001")
uid, gid = int(os.environ.get("PUID", "99")), int(os.environ.get("PGID", "100"))

os.makedirs(os.path.join(DATA, "uploads"), exist_ok=True)
if os.getuid() == 0:
    for root, dirs, files in os.walk(DATA):
        for name in [root] + [os.path.join(root, n) for n in dirs + files]:
            try:
                os.chown(name, uid, gid)
            except OSError:
                pass
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
os.environ["HOME"] = "/tmp"          # root's home isn't writable after dropping privileges (gunicorn writes there)

os.execvp("gunicorn", [
    "gunicorn", "--bind", f"0.0.0.0:{port}", "--workers", "1", "--threads", "4", "--timeout", "60",
    "--access-logfile", "-", "app:create_app()",
])
