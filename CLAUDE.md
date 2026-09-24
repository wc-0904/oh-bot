# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small bot that watches CMU's Office Hours Queue (OHQ, at `ohq.eberly.cmu.edu`) for a specific course via its Socket.IO backend, and posts a Discord notification whenever a new student joins the queue.

## Running

```bash
source venv/bin/activate
python oh_bot.py
```

`bot.sh` starts that same command under `nohup`, writing to `oh_bot.log`. `./bot.sh start` refuses to launch a second copy, `./bot.sh stop` stops the running `python oh_bot.py`, and `./bot.sh log` follows the log.

Requires a `.env` file with `DISCORD_WEBHOOK_URL` set, and a valid `cookie.txt` (see below). `DISCORD_ALERT_WEBHOOK_URL`, `REAUTH_PASSWORD`, and `REAUTH_BASE_URL` are only needed for headless remote login. `QUEUE_AUTO_OPEN` defaults to off. There is no test suite, linter, or build step configured.

To manually re-authenticate and refresh the session cookie:

```bash
python refresh_cookie.py
```

With `REAUTH_BASE_URL` unset, this launches a visible Chromium window, waits for Andrew ID and Duo, then saves `session_id` to `cookie.txt` after Enter is pressed in the terminal. With `REAUTH_BASE_URL` set, it opens a headless Chromium profile in `browser_profile/`. If CMU still accepts that profile, it writes `session_id` to `cookie.txt` and exits. Otherwise it serves a one-time, password-gated page at `REAUTH_BASE_URL` and posts the link to `DISCORD_ALERT_WEBHOOK_URL` so you can finish Andrew ID and Duo from another device.

## Architecture

- **`oh_bot.py`** — the bot's main loop (`run_forever`). It builds a fresh `socketio.Client` per connection attempt (`build_client`), connects to the `/queue` namespace using the cookie in `cookie.txt` as the auth header, and joins `COURSE_ID` (currently hardcoded to `"12"`). It listens for `questions_initial`/`questions` events, diffs the queue entries against an in-memory `seen_ids` set to detect newly-`on_queue` students, and posts one Discord message per new entry via `notify_discord`. While connected, `sync_queue_open` emits `open_queue` or `close_queue` on `/queue` only when `QUEUE_AUTO_OPEN` is true. That flag defaults to false, so a missing key leaves the queue alone. The window comes from `QUEUE_OPEN_DAYS`, `QUEUE_OPEN_TIME`, and `QUEUE_CLOSE_TIME` in `.env` (Sunday–Thursday 17:00–20:00 America/New_York when those keys are omitted) and is re-read when that file changes. `queue_should_be_open` also stays closed outside the fall 2026 and spring 2027 terms in `DEFAULT_TERMS`, and on the no-class dates in `DEFAULT_SKIP_DATES`. Extra days can be added with `QUEUE_SKIP_DATES`. `queue_meta` updates the known open state, and the Discord open/close message is sent only when that state changes to the value just requested. A boot during the window opens the queue immediately. Outside the window the bot emits `close_queue` only after `queue_meta` says the queue is open and this connection has received a question list with nobody `on_queue`. When `QUEUE_AUTO_OPEN` is false, the bot does not emit either event.
- **Auth failure recovery**: the OHQ session cookie expires periodically. When the socket rejects auth (`connect_error` firing with `"Not authorized"`), the module-level `auth_failed` flag is set. The main loop polls this flag (rather than blocking on `sio.wait()`) so it can notice the failure, disconnect, and call `refresh_session_cookie()`, which shells out to `refresh_cookie.py` and blocks until `cookie.txt` is rewritten. After an interactive login it posts "Logged in, reconnecting." to `DISCORD_ALERT_WEBHOOK_URL`. This poll-based design (instead of an async/event-driven wait) is intentional — it's what lets the main thread interrupt and trigger the reauth flow.
- **`refresh_cookie.py`** is a standalone script, invoked as a subprocess by `oh_bot.py`, not imported. With `REAUTH_BASE_URL` unset it opens a visible browser and waits for Enter in the terminal, then exits 2. With `REAUTH_BASE_URL` set it uses a persistent headless Chromium profile (`browser_profile/`). A live `session_id` that passes `GET /user` is written to `cookie.txt` with no Discord alert. The cookie the socket just rejected is not treated as success; that `session_id` is cleared and OHQ is loaded again so a still-valid Andrew/Duo session can mint a new one. If that fails, the script listens on the port in `REAUTH_BASE_URL` and posts a 15-minute, one-time link. The page requires `REAUTH_PASSWORD`, then forwards screenshots, clicks, and keystrokes to the Pi's browser. Exit codes: 0 silent refresh, 2 interactive login finished, 3 link expired, 1 error.
- **`cookie.txt`**, **`browser_profile/`**, and **`.env`** are gitignored — they hold the live session cookie, the Duo remember-this-browser profile, and the Discord webhook URLs respectively, and are the only required local setup besides `pip install`.
- **`test.py`** is a scratch script for manually checking that `cookie.txt` still authenticates (`GET /user`). It reads the cookie from `cookie.txt` like `oh_bot.py` does — never hardcode a session cookie in this file.
