# oh-bot

A bot that watches CMU's Office Hours Queue (OHQ) for a course and posts a Discord
notification whenever a new student joins the queue.

## Setup

1. Install dependencies:

   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   ```

   OHQ speaks Engine.IO 3. `requirements.txt` keeps `python-socketio` on 4.x,
   which is the client that can complete that handshake.

2. Create a `.env` file in the project root. Only the queue webhook is required:

   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
   ```

   On a headless machine such as a Pi, add the remote-login and schedule settings:

   ```
   DISCORD_ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...
   REAUTH_PASSWORD=choose-a-password
   REAUTH_BASE_URL=http://100.x.x.x:8787
   QUEUE_AUTO_OPEN=true
   QUEUE_OPEN_DAYS=Sun,Mon,Tue,Wed,Thu
   QUEUE_OPEN_TIME=17:00
   QUEUE_CLOSE_TIME=20:00
   ```

   `DISCORD_WEBHOOK_URL` is the shared channel for queue joins.
   `DISCORD_ALERT_WEBHOOK_URL` is a webhook in a channel only you can see.
   Webhooks cannot send DMs, so that private channel is where session alerts go.
   Leave `REAUTH_BASE_URL` unset to log in from a visible browser window on this
   machine. Set it, with `REAUTH_PASSWORD`, to log in from your phone instead.
   `REAUTH_PASSWORD` unlocks that page. `REAUTH_BASE_URL` is the address your
   phone uses to reach the machine running the bot, including the port. Use
   Tailscale or another private network. Do not port-forward this page to the
   public internet.
   Leave `QUEUE_AUTO_OPEN` unset to leave the queue alone. Set it to `true` to
   open and close on a schedule. `QUEUE_OPEN_DAYS`, `QUEUE_OPEN_TIME`, and
   `QUEUE_CLOSE_TIME` are that window, in 24-hour Eastern time. The close time
   has to be later on the same day. The bot re-reads these when `.env` changes,
   so you do not have to restart it. Leave the window unset to keep
   Sunday–Thursday, 5:00pm–8:00pm. The 2026–27 CMU calendar is built in: once
   automatic open is on, the queue stays closed outside fall and spring,
   including finals' surrounding breaks, and on Labor Day, fall break,
   Thanksgiving, spring break, and Spring Carnival. Democracy Day is not
   skipped, because evening classes after 5:00pm still meet. Add more days
   with `QUEUE_SKIP_DATES=2026-10-05`.

3. Log in once:

   ```bash
   python refresh_cookie.py
   ```

   With `REAUTH_BASE_URL` unset, this opens a Chromium window. Log in with your
   Andrew ID and Duo, wait until you see the queue page, then press Enter in
   the terminal. This saves `cookie.txt`.

   With `REAUTH_BASE_URL` set, Chromium stays headless and stores its profile in
   `browser_profile/`. If that profile is already logged in, this writes
   `cookie.txt` and exits. Otherwise it posts a one-time link to the private
   Discord channel. Open the link, enter `REAUTH_PASSWORD`, then finish Andrew
   ID and Duo on that page. The link expires in 15 minutes and stops working
   after a successful login.

## Running

```bash
source venv/bin/activate
python oh_bot.py
```

On the Pi, leave it running in the background instead:

```bash
./bot.sh start
./bot.sh log
./bot.sh stop
```

`start` does nothing if `oh_bot.py` is already running, including after the
reboot cron job. `log` follows `oh_bot.log`, and `Ctrl+C` only stops the
viewer.

The bot connects to the OHQ Socket.IO server and posts to Discord when someone
joins the queue. If the session cookie expires and `REAUTH_BASE_URL` is unset,
it opens a browser window for you to log in again. With `REAUTH_BASE_URL` set,
it opens the saved browser profile and writes a new `cookie.txt` when CMU still
accepts that login. When a real login is required, it sends a one-time link to
the private channel. If that link expires, it sends a new one 30 minutes later.
With `QUEUE_AUTO_OPEN=true`, it opens course 12 on the configured Eastern
schedule. After the close time it waits until nobody is left on the queue, then
closes it. The private channel is told only after OHQ confirms the change.
Starting the bot during that window opens the queue immediately. The account
in the saved browser profile is the one OHQ sees.
Leaving `QUEUE_AUTO_OPEN` unset, or setting it to `false`, stops the bot from
opening or closing the queue.
`browser_profile/` holds the Andrew session and the Duo remember-this-browser
cookie when remote login is configured. Do not commit it.

By default the bot watches the course with ID `12`. Change `COURSE_ID` in
`oh_bot.py` to watch a different course.
