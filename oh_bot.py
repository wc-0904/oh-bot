import os
import sys
import time
import logging
import subprocess
from datetime import date, datetime
from zoneinfo import ZoneInfo

import socketio
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)

seen_ids = set()
COURSE_ID = "12"

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
DISCORD_ALERT_WEBHOOK_URL = os.environ.get("DISCORD_ALERT_WEBHOOK_URL")

# Set by connect_error when the server rejects our auth, read by the main loop
auth_failed = False
# refresh_cookie.py exits 2 after you finish the one-time login page, and 3 when that link expires.
EXIT_INTERACTIVE = 2
EXIT_EXPIRED = 3
EASTERN = ZoneInfo("America/New_York")
QUEUE_EMIT_COOLDOWN = 60
# Sunday is 6. Default is Sunday through Thursday, 5:00pm to 8:00pm.
DAY_NAMES = {
    "sun": 6, "sunday": 6,
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
}
WEEKDAY_ORDER = (6, 0, 1, 2, 3, 4, 5)
WEEKDAY_LABELS = {6: "Sun", 0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat"}
# Fall 2026 through makeup finals, then spring 2027 through makeup finals.
# Summer is left closed. Official no-class days inside the terms are skipped.
DEFAULT_TERMS = (
    (date(2026, 8, 24), date(2026, 12, 14)),
    (date(2027, 1, 19), date(2027, 5, 11)),
)
DEFAULT_SKIP_DATES = frozenset({
    date(2026, 9, 7),  # Labor Day
    date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14),
    date(2026, 10, 15), date(2026, 10, 16),  # Fall break
    date(2026, 11, 25), date(2026, 11, 26), date(2026, 11, 27),  # Thanksgiving
    date(2027, 1, 18),  # Martin Luther King Jr. Day
    date(2027, 3, 8), date(2027, 3, 9), date(2027, 3, 10),
    date(2027, 3, 11), date(2027, 3, 12),  # Spring break
    date(2027, 4, 15), date(2027, 4, 16), date(2027, 4, 17),  # Spring Carnival
})
DEFAULT_QUEUE_SCHEDULE = (
    False, frozenset({6, 0, 1, 2, 3}), (17, 0), (20, 0),
    DEFAULT_SKIP_DATES, DEFAULT_TERMS,
)
_SCHEDULE_UNSET = object()
_schedule_mtime = _SCHEDULE_UNSET
_schedule = DEFAULT_QUEUE_SCHEDULE
_schedule_error = None
# None until queue_meta says whether the joined course's queue is open.
queue_is_open = None
# True/False after we emit open_queue or close_queue, until queue_meta answers.
pending_queue_target = None
last_queue_emit = 0.0
# False until this connection has received a question list. seen_ids starts empty.
questions_seen = False


def load_session_cookie():
    try:
        with open("cookie.txt") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def notify_alert(msg):
    print(msg)
    if not DISCORD_ALERT_WEBHOOK_URL:
        return
    try:
        resp = requests.post(
            DISCORD_ALERT_WEBHOOK_URL,
            json={"content": msg},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"Alert post failed: {e}")
        return
    if resp.status_code >= 300:
        print(f"Alert post failed: {resp.status_code} {resp.text}")


def refresh_session_cookie():
    """Blocks until cookie.txt is refreshed.

    With REAUTH_BASE_URL unset, refresh_cookie.py opens a visible browser and
    waits for Enter. With it set, the saved browser profile or the one-time
    login page is used instead.
    """
    print("\n=== Refreshing OHQ session. ===")
    result = subprocess.run([sys.executable, "refresh_cookie.py"], check=False)
    if result.returncode == 0:
        print("=== Cookie refreshed, reconnecting... ===\n")
        return
    if result.returncode == EXIT_INTERACTIVE:
        print("=== Cookie refreshed, reconnecting... ===\n")
        notify_alert("Logged in, reconnecting.")
        return
    if result.returncode == EXIT_EXPIRED:
        print("Login link expired. Retrying in 30 minutes.")
        notify_alert("Login link expired. I'll send a new one in 30 minutes.")
        time.sleep(30 * 60)
        return
    print(f"Cookie refresh failed (exit {result.returncode}). Retrying in 60 seconds.")
    time.sleep(60)


def notify_discord(entry):
    name = f'{entry["first_name"]} {entry["last_name"]}'.strip()
    msg = f'🎓 **{name}** joined the queue — *{entry["topic"]}* ({entry["location"]})'
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": msg})
    if resp.status_code >= 300:
        print(f"Discord post failed: {resp.status_code} {resp.text}")


def read_env_values(path):
    values = {}
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def parse_enabled(text):
    value = text.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"QUEUE_AUTO_OPEN must be true or false, got {text!r}")


def parse_days(text):
    parts = [part.strip().lower() for part in text.split(",")]
    parts = [part for part in parts if part]
    if not parts:
        raise ValueError("QUEUE_OPEN_DAYS is empty")
    days = set()
    for part in parts:
        if part not in DAY_NAMES:
            raise ValueError(f"Unknown day in QUEUE_OPEN_DAYS: {part}")
        days.add(DAY_NAMES[part])
    return frozenset(days)


def parse_hhmm(text, key):
    try:
        hour_text, minute_text = text.strip().split(":")
        hour, minute = int(hour_text), int(minute_text)
    except ValueError:
        raise ValueError(f"{key} must be HH:MM, got {text!r}") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and len(minute_text) == 2):
        raise ValueError(f"{key} must be HH:MM, got {text!r}")
    return hour, minute


def parse_dates(text):
    dates = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            dates.add(date.fromisoformat(part))
        except ValueError:
            raise ValueError(f"QUEUE_SKIP_DATES entries must be YYYY-MM-DD, got {part!r}") from None
    return dates


def parse_queue_schedule(values):
    """Return (enabled, weekdays, open_at, close_at, skip_dates, terms)."""
    enabled_text = values.get("QUEUE_AUTO_OPEN")
    days_text = values.get("QUEUE_OPEN_DAYS")
    open_text = values.get("QUEUE_OPEN_TIME")
    close_text = values.get("QUEUE_CLOSE_TIME")
    extra_skip_text = values.get("QUEUE_SKIP_DATES")
    enabled, weekdays, open_at, close_at, skip_dates, terms = DEFAULT_QUEUE_SCHEDULE
    if enabled_text is not None:
        enabled = parse_enabled(enabled_text)
    if days_text is not None:
        weekdays = parse_days(days_text)
    if open_text is not None:
        open_at = parse_hhmm(open_text, "QUEUE_OPEN_TIME")
    if close_text is not None:
        close_at = parse_hhmm(close_text, "QUEUE_CLOSE_TIME")
    if extra_skip_text is not None:
        skip_dates = frozenset(set(skip_dates) | parse_dates(extra_skip_text))
    if enabled and open_at >= close_at:
        raise ValueError("QUEUE_CLOSE_TIME must be later the same day than QUEUE_OPEN_TIME")
    return enabled, weekdays, open_at, close_at, skip_dates, terms


def describe_schedule(schedule):
    enabled, weekdays, open_at, close_at, _skip_dates, _terms = schedule
    if not enabled:
        return "Queue auto-open is off."
    days = ", ".join(WEEKDAY_LABELS[day] for day in WEEKDAY_ORDER if day in weekdays)
    open_label = f"{open_at[0]:02d}:{open_at[1]:02d}"
    close_label = f"{close_at[0]:02d}:{close_at[1]:02d}"
    return (
        f"Queue auto-open is on, {days}, {open_label}–{close_label} Eastern, "
        "skipping CMU 2026-27 breaks."
    )


def current_queue_schedule(path=".env"):
    """Re-read schedule settings when .env changes, without restarting the bot."""
    global _schedule_mtime, _schedule, _schedule_error
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if mtime == _schedule_mtime:
        return _schedule
    try:
        parsed = parse_queue_schedule(read_env_values(path))
    except ValueError as e:
        message = str(e)
        if message != _schedule_error:
            print(f"Queue schedule config ignored: {message}")
            _schedule_error = message
        _schedule_mtime = mtime
        return _schedule
    if _schedule_mtime is _SCHEDULE_UNSET or parsed != _schedule or _schedule_error:
        print(describe_schedule(parsed))
    _schedule_error = None
    _schedule = parsed
    _schedule_mtime = mtime
    return _schedule


def queue_should_be_open(now, weekdays, open_at, close_at, skip_dates, terms):
    """True from open_at until close_at Eastern on configured weekdays in term."""
    today = now.date()
    if not any(start <= today <= end for start, end in terms):
        return False
    if today in skip_dates:
        return False
    if now.weekday() not in weekdays:
        return False
    current = (now.hour, now.minute)
    return open_at <= current < close_at


def remember_queue_open(data):
    global queue_is_open, pending_queue_target
    payload = data.get("payload") if isinstance(data, dict) else None
    if not payload or "open" not in payload[0]:
        return
    new_open = bool(payload[0]["open"])
    if pending_queue_target is not None and new_open == pending_queue_target:
        if new_open != queue_is_open:
            notify_alert("Queue opened." if new_open else "Queue closed.")
        pending_queue_target = None
    queue_is_open = new_open


def sync_queue_open(sio):
    """Open or close course 12 so it matches the schedule in .env."""
    global pending_queue_target, last_queue_emit
    enabled, weekdays, open_at, close_at, skip_dates, terms = current_queue_schedule()
    if not enabled:
        return
    desired = queue_should_be_open(
        datetime.now(EASTERN), weekdays, open_at, close_at, skip_dates, terms,
    )
    # Outside the window, wait until OHQ tells us the queue is actually open.
    if queue_is_open is None and not desired:
        return
    if queue_is_open == desired:
        return
    # A close waits until this connection has seen a question list with nobody waiting.
    if not desired and (not questions_seen or seen_ids):
        return
    if time.time() - last_queue_emit < QUEUE_EMIT_COOLDOWN:
        return
    event = "open_queue" if desired else "close_queue"
    try:
        sio.emit(event, namespace="/queue")
    except Exception as e:
        print(f"Queue {event} failed: {e}")
        return
    last_queue_emit = time.time()
    pending_queue_target = desired


def handle_questions(data):
    global seen_ids, questions_seen
    entries = data.get("payload", [])
    current_ids = {e["id"] for e in entries if e["state"] == "on_queue"}
    new_ids = current_ids - seen_ids

    for entry in entries:
        if entry["id"] in new_ids:
            notify_discord(entry)

    seen_ids = current_ids
    questions_seen = True


def build_client():
    """Creates a fresh Socket.IO client with all handlers attached.
    Rebuilt on each reconnect attempt rather than reusing one client instance."""
    sio = socketio.Client(logger=False, engineio_logger=False)

    @sio.on("questions_initial", namespace="/queue")
    def on_questions_initial(data):
        handle_questions(data)

    @sio.on("questions", namespace="/queue")
    def on_questions(data):
        handle_questions(data)

    @sio.on("queue_meta", namespace="/queue")
    def on_queue_meta(data):
        remember_queue_open(data)

    @sio.event(namespace="/queue")
    def connect():
        print("Connected to /queue namespace, joining course...")
        sio.emit("join_course", COURSE_ID, namespace="/queue")

    @sio.event(namespace="/queue")
    def connect_error(data):
        global auth_failed
        print(f"Connection to /queue rejected: {data}")
        if data == "Not authorized":
            auth_failed = True

    @sio.event
    def disconnect():
        print("Disconnected from server.")

    return sio


def run_forever():
    global auth_failed, questions_seen

    while True:
        auth_failed = False
        questions_seen = False
        session_cookie = load_session_cookie()

        if session_cookie is None:
            print("No cookie.txt found — need to log in first.")
            refresh_session_cookie()
            continue

        sio = build_client()

        try:
            sio.connect(
                "https://ohq.eberly.cmu.edu",
                namespaces=["/queue"],
                headers={"Cookie": session_cookie},
                transports=["polling"],
            )
        except Exception as e:
            print(f"Connection attempt failed: {e}")
            time.sleep(5)
            continue

        # Poll instead of sio.wait(), so we can notice an auth failure
        # flagged by connect_error and break out to trigger a refresh.
        while True:
            time.sleep(2)
            if auth_failed:
                sio.disconnect()
                break
            if not sio.connected:
                break
            sync_queue_open(sio)

        if auth_failed:
            refresh_session_cookie()
        else:
            print("Connection lost, retrying in 5s...")
            time.sleep(5)


if __name__ == "__main__":
    run_forever()