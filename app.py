#!/usr/bin/env python3
import html
import json
import os
import subprocess
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


HOST = os.environ.get("MEDIA_VAULT_UI_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEDIA_VAULT_UI_PORT", "8127"))
CIPHER_DIR = Path(os.environ.get("MEDIA_VAULT_CIPHER_DIR", "/home/user/media-vault/cipher"))
MOUNT_DIR = Path(os.environ.get("MEDIA_VAULT_MOUNT_DIR", "/home/user/media-vault/mount"))
JELLYFIN_DIR = Path(os.environ.get("JELLYFIN_COMPOSE_DIR", "/home/user/docker/jellyfin"))
READY_FILES = [
    MOUNT_DIR / ".media-vault-ready",
    MOUNT_DIR / "main/.media-vault-ready",
    MOUNT_DIR / "private/.media-vault-ready",
]


def run(cmd, *, input_text=None, timeout=60):
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def command_ok(cmd):
    return run(cmd).returncode == 0


def mount_options():
    result = run(["findmnt", "-n", "-o", "OPTIONS", "--target", str(MOUNT_DIR)])
    if result.returncode != 0:
        return ""
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""


def is_mounted():
    return command_ok(["mountpoint", "-q", str(MOUNT_DIR)])


def containers():
    result = run(
        [
            "docker",
            "compose",
            "-f",
            str(JELLYFIN_DIR / "docker-compose.yml"),
            "ps",
            "--all",
            "--format",
            "json",
        ],
        timeout=20,
    )
    items = []
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            items.append(
                {
                    "name": row.get("Name") or row.get("Service"),
                    "service": row.get("Service"),
                    "state": row.get("State"),
                    "health": row.get("Health") or "",
                }
            )
    return items


def current_status():
    mounted = is_mounted()
    options = mount_options() if mounted else ""
    ready = {str(path.relative_to(MOUNT_DIR)): path.exists() for path in READY_FILES}
    return {
        "mounted": mounted,
        "allow_other": mounted and "allow_other" in options.split(","),
        "ready": ready,
        "containers": containers(),
    }


def ensure_ready_files():
    for path in READY_FILES:
        if not path.exists():
            raise RuntimeError(f"Startschutz-Datei fehlt: {path}")
    for path in [MOUNT_DIR, MOUNT_DIR / "main", MOUNT_DIR / "private"]:
        path.chmod(0o755)
    for path in READY_FILES:
        path.chmod(0o644)


def unmount_vault():
    if not is_mounted():
        return "Media-Vault ist bereits gesperrt."
    messages = []
    for _ in range(5):
        if not is_mounted():
            break
        result = run(["fusermount", "-u", str(MOUNT_DIR)], timeout=20)
        if result.stdout.strip():
            messages.append(result.stdout.strip())
        if result.stderr.strip():
            messages.append(result.stderr.strip())
    if is_mounted():
        raise RuntimeError("Unmount fehlgeschlagen; der Media-Vault ist weiterhin gemountet.")
    return "\n".join(messages) or "Media-Vault gesperrt."


def stop_jellyfin():
    result = run(
        ["docker", "compose", "-f", str(JELLYFIN_DIR / "docker-compose.yml"), "stop"],
        timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "docker compose stop fehlgeschlagen").strip())


def start_jellyfin():
    result = run(
        ["docker", "compose", "-f", str(JELLYFIN_DIR / "docker-compose.yml"), "up", "-d"],
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "docker compose up fehlgeschlagen").strip())


def unlock_vault(password):
    if not password:
        raise RuntimeError("Passwort fehlt.")
    if not (CIPHER_DIR / "gocryptfs.conf").exists():
        raise RuntimeError("Media-Vault ist nicht initialisiert.")

    MOUNT_DIR.mkdir(parents=True, exist_ok=True)

    if is_mounted() and "allow_other" not in mount_options().split(","):
        stop_jellyfin()
        unmount_vault()

    if not is_mounted():
        runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        tmp_dir = runtime_dir if runtime_dir.is_dir() else Path("/tmp")
        with tempfile.NamedTemporaryFile("w", dir=tmp_dir, prefix="media-vault-pass.", delete=False) as passfile:
            passfile.write(password)
            passfile.write("\n")
            passfile_path = Path(passfile.name)
        passfile_path.chmod(0o600)
        try:
            result = run(
                [
                    "gocryptfs",
                    "-allow_other",
                    "-passfile",
                    str(passfile_path),
                    str(CIPHER_DIR),
                    str(MOUNT_DIR),
                ],
                timeout=45,
            )
        finally:
            try:
                passfile_path.unlink()
            except FileNotFoundError:
                pass
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "Entsperren fehlgeschlagen.").strip())

    ensure_ready_files()
    start_jellyfin()
    return "Media-Vault entsperrt, Jellyfin wurde gestartet."


PAGE = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Media-Vault</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, -apple-system, Segoe UI, sans-serif; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #101820; color: #f4f7f8; }
    main { width: min(560px, calc(100vw - 32px)); }
    section { background: #17232d; border: 1px solid #2e4556; border-radius: 8px; padding: 24px; box-shadow: 0 18px 60px rgba(0,0,0,.28); }
    h1 { margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }
    p { margin: 0 0 18px; color: #bfd0d8; line-height: 1.5; }
    label { display: block; margin-bottom: 8px; font-weight: 650; }
    input { box-sizing: border-box; width: 100%; height: 44px; border-radius: 6px; border: 1px solid #5d7586; background: #0f171e; color: #fff; padding: 0 12px; font-size: 16px; }
    .row { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
    button, a.button { border: 0; border-radius: 6px; background: #e7f0f4; color: #10202a; min-height: 42px; padding: 0 14px; font-weight: 700; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; }
    button.secondary { background: #304758; color: #f4f7f8; }
    button.danger { background: #ffd7d2; color: #4e100b; }
    button:disabled { opacity: .62; cursor: wait; }
    .status { margin: 16px 0; padding: 12px; border-radius: 6px; background: #0f171e; border: 1px solid #2e4556; white-space: pre-line; }
    .links { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 16px; }
    .ok { color: #8ee6a8; }
    .bad { color: #ffb4aa; }
  </style>
</head>
<body>
  <main>
    <section>
      <h1>Media-Vault</h1>
      <p id="intro">Entsperrt den verschlüsselten Medienbereich und startet beide Jellyfin-Instanzen.</p>
      <form id="unlock">
        <label for="password">Vault-Passwort</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
        <div class="row">
          <button type="submit">Entsperren</button>
          <button class="secondary" type="button" id="refresh">Status</button>
          <button class="danger" type="button" id="lock">Sperren</button>
        </div>
      </form>
      <div class="status" id="status">Status wird geladen...</div>
      <div class="links">
        <a class="button" href="/jellyfin/">Filme/Serien</a>
        <a class="button" href="/jellyfin-private/">Persönliche Videos</a>
      </div>
    </section>
  </main>
<script>
const statusEl = document.querySelector('#status');
const buttons = [...document.querySelectorAll('button')];
const params = new URLSearchParams(window.location.search);
const nextPath = params.get('next') || '';
const safeNext = /^\\/jellyfin(?:-private)?\\//.test(nextPath) ? nextPath : '';
if (safeNext) {
  document.querySelector('#intro').textContent = 'Entsperrt den verschlüsselten Medienbereich und öffnet danach Jellyfin.';
}
function busy(on) { buttons.forEach(b => b.disabled = on); }
function render(data, message='') {
  const containers = (data.containers || []).map(c => `${c.name}: ${c.state}${c.health ? ' (' + c.health + ')' : ''}`).join('\\n') || 'Keine Containerdaten';
  statusEl.innerHTML = `${message ? message + '\\n\\n' : ''}Vault: <span class="${data.mounted ? 'ok' : 'bad'}">${data.mounted ? 'entsperrt' : 'gesperrt'}</span>\\nallow_other: ${data.allow_other ? 'ja' : 'nein'}\\n\\n${containers}`;
}
async function api(path, options={}) {
  const res = await fetch(path, options);
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.error || 'Anfrage fehlgeschlagen');
  return data;
}
async function refresh(message='') {
  const data = await api('api/status');
  render(data.status, message);
}
document.querySelector('#unlock').addEventListener('submit', async (event) => {
  event.preventDefault();
  busy(true);
  try {
    const body = new URLSearchParams(new FormData(event.target));
    const data = await api('api/unlock', {method:'POST', body});
    render(data.status, data.message);
    event.target.password.value = '';
    if (safeNext) {
      window.setTimeout(() => { window.location.href = safeNext; }, 900);
    }
  } catch (err) {
    statusEl.textContent = err.message;
  } finally {
    busy(false);
  }
});
document.querySelector('#lock').addEventListener('click', async () => {
  busy(true);
  try {
    const data = await api('api/lock', {method:'POST'});
    render(data.status, data.message);
  } catch (err) {
    statusEl.textContent = err.message;
  } finally {
    busy(false);
  }
});
document.querySelector('#refresh').addEventListener('click', () => refresh().catch(err => statusEl.textContent = err.message));
refresh().catch(err => statusEl.textContent = err.message);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "MediaVaultUI/1.0"

    def log_message(self, _format, *_args):
        return

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            body = PAGE.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/status":
            self.send_json({"ok": True, "status": current_status()})
            return
        if path == "/healthz":
            self.send_json({"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/unlock":
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(min(length, 8192)).decode("utf-8")
                password = parse_qs(raw).get("password", [""])[0]
                message = unlock_vault(password)
                self.send_json({"ok": True, "message": message, "status": current_status()})
                return
            if path == "/api/lock":
                stop_jellyfin()
                message = unmount_vault()
                self.send_json({"ok": True, "message": message, "status": current_status()})
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json(
                {"ok": False, "error": html.escape(str(exc)), "status": current_status()},
                status=HTTPStatus.BAD_REQUEST,
            )


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"media-vault-ui listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
