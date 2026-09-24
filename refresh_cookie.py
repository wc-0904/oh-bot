import hashlib
import hmac
import json
import os
import queue
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

OHQ_URL = "https://ohq.eberly.cmu.edu"
PROFILE_DIR = "browser_profile"
COOKIE_FILE = "cookie.txt"
VIEWPORT = {"width": 420, "height": 780}
LINK_TTL_SECONDS = 15 * 60
SILENT_WAIT_SECONDS = 15
# Same cookie the socket just rejected is not a successful refresh.
PREVIOUS_COOKIE = None

EXIT_SILENT = 0
EXIT_ERROR = 1
EXIT_INTERACTIVE = 2
EXIT_EXPIRED = 3

CHROME_UA = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Filled in only while the one-time login page is running.
STATE = {
    "token": "",
    "expires": 0,
    "password": "",
    "session": "",
    "failures": 0,
    "done": False,
    "closed": False,
}
JOBS = queue.Queue()
SERVER = None

ALLOWED_KEYS = {
    "Enter",
    "Backspace",
    "Tab",
    "Escape",
    "Delete",
    "ArrowLeft",
    "ArrowRight",
    "ArrowUp",
    "ArrowDown",
    "ScrollUp",
    "ScrollDown",
}

VIEWER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>OHQ login</title>
<style>
  body { margin: 0; font-family: sans-serif; background: #111; color: #eee; }
  p { margin: 8px 12px; font-size: 14px; }
  #shot { width: 100%; height: auto; display: block; background: #000; touch-action: manipulation; }
  #bar, #keys { display: flex; gap: 8px; padding: 8px; background: #1c1c1c; }
  #keys { padding-top: 0; }
  input { flex: 1; font-size: 16px; padding: 10px; min-width: 0; }
  button { font-size: 16px; padding: 10px 12px; }
  #done { padding: 24px; }
</style>
</head>
<body>
<p>Tap a field in the page, type below, then Send. Approve Duo on your phone.</p>
<img id="shot" alt="OHQ login page">
<div id="bar">
  <input id="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Type, then Send">
  <button type="button" id="send">Send</button>
</div>
<div id="keys">
  <button type="button" id="enter">Enter</button>
  <button type="button" id="back">Delete</button>
  <button type="button" id="tab">Tab</button>
  <button type="button" id="up">Up</button>
  <button type="button" id="down">Down</button>
</div>
<script>
const base = location.pathname.replace(/\/$/, "");
const shot = document.getElementById("shot");
function post(path, body) {
  return fetch(base + path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
}
async function refresh() {
  try {
    const status = await fetch(base + "/status");
    if (status.status === 410) {
      document.body.innerHTML = "<p id='done'>This link has expired.</p>";
      return;
    }
    if (status.ok) {
      const body = await status.json();
      if (body.done) {
        document.body.innerHTML = "<p id='done'>Logged in. You can close this tab.</p>";
        return;
      }
    }
    const resp = await fetch(base + "/shot?t=" + Date.now());
    if (resp.ok) {
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const old = shot.src;
      shot.src = url;
      if (old.startsWith("blob:")) URL.revokeObjectURL(old);
    }
  } catch (e) {}
  setTimeout(refresh, 700);
}
refresh();
shot.addEventListener("click", (e) => {
  const rect = shot.getBoundingClientRect();
  if (!shot.naturalWidth || !rect.width || !rect.height) return;
  const x = (e.clientX - rect.left) * (shot.naturalWidth / rect.width);
  const y = (e.clientY - rect.top) * (shot.naturalHeight / rect.height);
  post("/click", {x, y});
});
async function sendText() {
  const input = document.getElementById("text");
  const text = input.value;
  if (!text) return;
  await post("/type", {text});
  input.value = "";
}
document.getElementById("send").onclick = sendText;
document.getElementById("enter").onclick = () => post("/key", {key: "Enter"});
document.getElementById("back").onclick = () => post("/key", {key: "Backspace"});
document.getElementById("tab").onclick = () => post("/key", {key: "Tab"});
document.getElementById("up").onclick = () => post("/key", {key: "ScrollUp"});
document.getElementById("down").onclick = () => post("/key", {key: "ScrollDown"});
document.getElementById("text").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); sendText(); }
});
</script>
</body>
</html>
"""


def passwords_match(given, expected):
    return hmac.compare_digest(
        hashlib.sha256(given.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    )


def post_alert(text):
    print(text)
    url = os.environ.get("DISCORD_ALERT_WEBHOOK_URL")
    if not url:
        return
    try:
        resp = requests.post(url, json={"content": text}, timeout=15)
    except requests.RequestException as e:
        print(f"Alert post failed: {e}")
        return
    if resp.status_code >= 300:
        print(f"Alert post failed: {resp.status_code} {resp.text}")


def load_previous_cookie():
    try:
        with open(COOKIE_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def save_cookie(header):
    with open(COOKIE_FILE, "w") as f:
        f.write(header)
    print("Saved fresh cookie to cookie.txt")


def on_ohq(page):
    parsed = urlparse(page.url)
    return parsed.scheme == "https" and parsed.hostname == "ohq.eberly.cmu.edu"


def session_header(context):
    for cookie in context.cookies():
        if cookie["name"] == "session_id" and cookie["value"]:
            return f"session_id={cookie['value']}"
    return None


def live_session_header(page):
    """Return the OHQ session header when /user accepts it and the page is OHQ."""
    if not on_ohq(page):
        return None
    header = session_header(page.context)
    if not header:
        return None
    try:
        resp = page.request.get(
            OHQ_URL + "/user",
            max_redirects=0,
            timeout=5000,
        )
    except Exception as e:
        print(f"Session check failed: {e}")
        return None
    if resp.status != 200:
        return None
    return header


def wait_for_new_session(page, seconds):
    """Return a live session that is not the cookie the socket just rejected.

    A still-valid rejected cookie means the profile should drop session_id and
    load OHQ again, so this returns immediately in that case.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        header = live_session_header(page)
        if header and header != PREVIOUS_COOKIE:
            return header
        if header and PREVIOUS_COOKIE and header == PREVIOUS_COOKIE:
            return None
        page.wait_for_timeout(1000)
    return None


def clear_session_cookie(context):
    context.clear_cookies(name="session_id")


def open_browser(playwright):
    context = playwright.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=True,
        viewport=VIEWPORT,
        user_agent=CHROME_UA,
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.set_viewport_size(VIEWPORT)
    return context, page


def token_ok(token):
    if not token or not STATE["token"] or STATE["closed"]:
        return False
    if time.time() >= STATE["expires"] or STATE["failures"] >= 8:
        return False
    return hmac.compare_digest(
        hashlib.sha256(token.encode()).digest(),
        hashlib.sha256(STATE["token"].encode()).digest(),
    )


def parse_route(path):
    """Return (token, action) for /r/<token>/ and /r/<token>/<action>."""
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2 or parts[0] != "r":
        return None, None
    token = parts[1]
    action = parts[2] if len(parts) > 2 else ""
    if len(parts) > 3:
        return None, None
    return token, action


def request_session(handler):
    raw = handler.headers.get("Cookie", "")
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "reauth_session":
            return value
    return ""


def unlocked(handler, token):
    session = request_session(handler)
    return bool(session) and hmac.compare_digest(
        hashlib.sha256(session.encode()).digest(),
        hashlib.sha256(STATE["session"].encode()).digest(),
    ) and token_ok(token)


def send_html(handler, status, html):
    body = html.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.end_headers()
    handler.wfile.write(body)


def send_bytes(handler, status, content_type, payload, extra_headers=None):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Cache-Control", "no-store")
    if extra_headers:
        for key, value in extra_headers:
            handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(payload)


def password_form(message):
    note = f"<p>{message}</p>" if message else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>OHQ login</title>
<style>
  body {{ margin: 0; font-family: sans-serif; background: #111; color: #eee; }}
  form {{ padding: 24px; display: flex; flex-direction: column; gap: 12px; max-width: 360px; }}
  input {{ font-size: 16px; padding: 10px; }}
  button {{ font-size: 16px; padding: 10px 12px; }}
</style>
</head>
<body>
<form method="POST" action="unlock">
  <label>Reauth password</label>
  {note}
  <input type="password" name="password" autocomplete="current-password" autofocus>
  <button type="submit">Continue</button>
</form>
</body>
</html>
"""


def read_body(handler, limit=10_000):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length < 0 or length > limit:
        return None
    return handler.rfile.read(length)


def enqueue(kind, payload):
    if STATE["closed"]:
        raise RuntimeError("closed")
    job = {"kind": kind, "payload": payload, "result": queue.Queue(maxsize=1)}
    JOBS.put(job)
    try:
        return job["result"].get(timeout=20)
    except queue.Empty:
        raise TimeoutError("timed out waiting for the browser")


def run_job(page, job):
    kind = job["kind"]
    payload = job["payload"]
    try:
        if kind == "shot":
            result = page.screenshot(type="png")
        elif kind == "click":
            page.mouse.click(payload["x"], payload["y"])
            result = True
        elif kind == "type":
            page.keyboard.type(payload["text"])
            result = True
        elif kind == "key":
            key = payload["key"]
            if key == "ScrollUp":
                page.mouse.wheel(0, -350)
            elif key == "ScrollDown":
                page.mouse.wheel(0, 350)
            else:
                page.keyboard.press(key)
            result = True
        else:
            result = RuntimeError("unknown job")
    except Exception as e:
        result = e
    job["result"].put(result)


class ReauthServer(ThreadingHTTPServer):
    allow_reuse_address = True


class ReauthHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        token, action = parse_route(urlparse(self.path).path)
        label = action or "page"
        if token is None:
            label = "unknown"
        if label in {"shot", "status"}:
            return
        print(f"reauth {self.command} {label}")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        parsed = urlparse(self.path)
        token, action = parse_route(parsed.path)
        if token is None or not token_ok(token):
            send_html(self, 410, "<p>This link has expired.</p>")
            return
        if action == "" and method == "GET":
            if not parsed.path.endswith("/"):
                self.send_response(302)
                self.send_header("Location", parsed.path + "/")
                self.end_headers()
                return
            if STATE["done"]:
                send_html(self, 200, "<p>Logged in. You can close this tab.</p>")
                return
            if unlocked(self, token):
                send_html(self, 200, VIEWER_HTML)
                return
            send_html(self, 200, password_form(""))
            return
        if action == "unlock" and method == "POST":
            self._unlock(token)
            return
        if action == "status" and method == "GET":
            if not unlocked(self, token):
                send_bytes(self, 401, "application/json", b"{}")
                return
            body = json.dumps({"done": STATE["done"]}).encode()
            send_bytes(self, 200, "application/json", body)
            return
        if not unlocked(self, token):
            send_html(self, 401, "<p>Enter the reauth password first.</p>")
            return
        if STATE["done"]:
            send_html(self, 410, "<p>Logged in. You can close this tab.</p>")
            return
        if action == "shot" and method == "GET":
            self._shot()
            return
        if action == "click" and method == "POST":
            self._click()
            return
        if action == "type" and method == "POST":
            self._type()
            return
        if action == "key" and method == "POST":
            self._key()
            return
        send_html(self, 404, "<p>Not found.</p>")

    def _unlock(self, token):
        raw = read_body(self)
        if raw is None:
            send_html(self, 413, "<p>Request too large.</p>")
            return
        given = parse_qs(raw.decode("utf-8", errors="replace")).get("password", [""])[0]
        if not passwords_match(given, STATE["password"]):
            STATE["failures"] += 1
            if STATE["failures"] >= 8:
                send_html(self, 410, "<p>This link has expired.</p>")
                return
            send_html(self, 401, password_form("Wrong password."))
            return
        STATE["session"] = secrets.token_urlsafe(32)
        body = b""
        self.send_response(302)
        self.send_header("Location", f"/r/{token}/")
        self.send_header(
            "Set-Cookie",
            f"reauth_session={STATE['session']}; HttpOnly; SameSite=Lax; Path=/r/{token}/",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.wfile.write(body)

    def _shot(self):
        try:
            result = enqueue("shot", None)
        except (RuntimeError, TimeoutError):
            send_html(self, 503, "<p>Screenshot failed.</p>")
            return
        if isinstance(result, Exception):
            send_html(self, 503, "<p>Screenshot failed.</p>")
            return
        send_bytes(self, 200, "image/png", result)

    def _json_body(self):
        raw = read_body(self)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode())
        except json.JSONDecodeError:
            return None

    def _click(self):
        data = self._json_body()
        if not isinstance(data, dict):
            send_html(self, 400, "<p>Bad request.</p>")
            return
        try:
            x = float(data["x"])
            y = float(data["y"])
        except (KeyError, TypeError, ValueError):
            send_html(self, 400, "<p>Bad request.</p>")
            return
        if not (0 <= x <= VIEWPORT["width"] and 0 <= y <= VIEWPORT["height"]):
            send_html(self, 400, "<p>Bad request.</p>")
            return
        try:
            result = enqueue("click", {"x": x, "y": y})
        except (RuntimeError, TimeoutError):
            send_html(self, 503, "<p>Browser unavailable.</p>")
            return
        if isinstance(result, Exception):
            send_html(self, 503, "<p>Click failed.</p>")
            return
        send_bytes(self, 204, "text/plain", b"")

    def _type(self):
        data = self._json_body()
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            send_html(self, 400, "<p>Bad request.</p>")
            return
        text = data["text"]
        if not text or len(text) > 500:
            send_html(self, 400, "<p>Bad request.</p>")
            return
        try:
            result = enqueue("type", {"text": text})
        except (RuntimeError, TimeoutError):
            send_html(self, 503, "<p>Browser unavailable.</p>")
            return
        if isinstance(result, Exception):
            send_html(self, 503, "<p>Typing failed.</p>")
            return
        send_bytes(self, 204, "text/plain", b"")

    def _key(self):
        data = self._json_body()
        if not isinstance(data, dict) or data.get("key") not in ALLOWED_KEYS:
            send_html(self, 400, "<p>Bad request.</p>")
            return
        try:
            result = enqueue("key", {"key": data["key"]})
        except (RuntimeError, TimeoutError):
            send_html(self, 503, "<p>Browser unavailable.</p>")
            return
        if isinstance(result, Exception):
            send_html(self, 503, "<p>Key failed.</p>")
            return
        send_bytes(self, 204, "text/plain", b"")


def reauth_base_url():
    base = os.environ.get("REAUTH_BASE_URL", "").rstrip("/")
    password = os.environ.get("REAUTH_PASSWORD", "")
    parsed = urlparse(base)
    if not password:
        raise RuntimeError("Set REAUTH_PASSWORD in .env before a manual login is required.")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        raise RuntimeError(
            "Set REAUTH_BASE_URL to an address your phone can open, including the port. "
            "Example: http://100.x.x.x:8787"
        )
    return base, parsed.port, password


def serve_reauth(page):
    global SERVER
    base, port, password = reauth_base_url()
    token = secrets.token_urlsafe(32)
    STATE.update(
        {
            "token": token,
            "expires": time.time() + LINK_TTL_SECONDS,
            "password": password,
            "session": "",
            "failures": 0,
            "done": False,
            "closed": False,
        }
    )
    try:
        SERVER = ReauthServer(("0.0.0.0", port), ReauthHandler)
    except OSError as e:
        raise RuntimeError(f"Could not listen on port {port}: {e}") from e

    thread = threading.Thread(target=SERVER.serve_forever, daemon=True)
    thread.start()
    url = f"{base}/r/{token}/"
    post_alert(
        "OHQ login needed. Open this link within 15 minutes and enter the reauth password:\n"
        + url
    )

    last_check = 0
    try:
        while True:
            if time.time() >= STATE["expires"] or STATE["failures"] >= 8:
                print("Login link expired.")
                return EXIT_EXPIRED
            try:
                job = JOBS.get(timeout=0.5)
            except queue.Empty:
                job = None
            if job is not None:
                run_job(page, job)
            now = time.time()
            if now - last_check >= 2:
                last_check = now
                if on_ohq(page):
                    header = session_header(page.context)
                else:
                    header = None
                if header:
                    save_cookie(header)
                    STATE["done"] = True
                    time.sleep(2)
                    return EXIT_INTERACTIVE
    finally:
        STATE["closed"] = True
        while True:
            try:
                job = JOBS.get_nowait()
            except queue.Empty:
                break
            job["result"].put(RuntimeError("closed"))
        SERVER.shutdown()
        SERVER.server_close()
        SERVER = None


def visible_login():
    """Open a normal browser window and wait for Enter. No saved profile or login page."""
    print("Log in with your Andrew ID + Duo in the browser window.")
    print("Once you see the actual queue/course page, come back here and press Enter.")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        try:
            page.goto(OHQ_URL)
            input()
            header = session_header(page.context)
            if not header:
                raise RuntimeError("No session_id cookie found. Finish login, then press Enter.")
            save_cookie(header)
        finally:
            browser.close()
    return EXIT_INTERACTIVE


def main():
    global PREVIOUS_COOKIE
    load_dotenv()
    if not os.environ.get("REAUTH_BASE_URL", "").strip():
        return visible_login()
    PREVIOUS_COOKIE = load_previous_cookie()
    print("Opening the saved browser...", flush=True)
    with sync_playwright() as playwright:
        context, page = open_browser(playwright)
        try:
            print("Loading OHQ...", flush=True)
            page.goto(OHQ_URL, wait_until="domcontentloaded", timeout=45000)
            header = wait_for_new_session(page, SILENT_WAIT_SECONDS)
            if not header and session_header(context):
                print("Saved session was rejected. Loading OHQ again...", flush=True)
                clear_session_cookie(context)
                page.goto(OHQ_URL, wait_until="domcontentloaded", timeout=45000)
                header = wait_for_new_session(page, SILENT_WAIT_SECONDS)
            if header:
                save_cookie(header)
                return EXIT_SILENT
            print("Saved profile is not logged in. Opening the one-time login page.", flush=True)
            return serve_reauth(page)
        finally:
            context.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"Cookie refresh failed: {e}")
        if isinstance(e, RuntimeError):
            post_alert(f"OHQ cookie refresh failed. {e}")
        else:
            post_alert("OHQ cookie refresh failed. Check the Pi log.")
        sys.exit(EXIT_ERROR)
