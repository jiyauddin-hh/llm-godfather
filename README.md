# God Father

A local control panel for the applications you run on your own machine. Register each app once —
its folder, start command and port — and God Father runs it, captures its logs, and keeps an eye on
its port.

- **Applications** — start / stop / restart, live status, PID, uptime, CPU and memory
- **Logs** — stdout and stderr captured per app, streamed live over WebSocket, filterable and downloadable
- **Ports** — a map of every listening TCP port on the machine, with registered apps linked to theirs
- **Port conflicts** — refuses to start an app whose port is already taken, and names the culprit
- **Auto-restart** — optional, with a flap guard that gives up after 5 rapid crashes
- **Events** — an audit trail of every start, stop, crash and restart
- **AI models** — which application uses which model, whether it is talking to one right now, and how many tokens it spent doing it

Stack: **FastAPI** backend, **React (Vite)** dark-mode frontend, **SQLite** storage. No external
services.

---

## Always-on (recommended)

Install it once as a systemd user service and it runs whenever the machine is on, restarting itself
if it ever dies:

```bash
./deploy/install-service.sh          # or: PORT=9200 ./deploy/install-service.sh
sudo loginctl enable-linger $USER    # so it starts at boot, before/without login
```

The script builds the web interface, writes `~/.config/systemd/user/godfather.service`, and starts it.
**FastAPI then serves the built UI itself, so there is only one process and one port** — open
**http://127.0.0.1:9100**. Re-run the script any time to rebuild and restart; `deploy/uninstall-service.sh`
removes the service and leaves your data alone.

```bash
systemctl --user status godfather      # is it running?
systemctl --user restart godfather     # restart (also restarts managed apps)
journalctl --user -u godfather -f      # the service's own log
```

### Starting your apps automatically too

Each application has a **Start this app whenever God Father starts** toggle. Apps with it enabled are
launched a few seconds after the service comes up, with their logs captured from the first line. An
app whose port is already being served is skipped rather than crashed, and the reason is written to
its log.

Without that toggle an app stays stopped after a reboot — or after any `systemctl --user restart
godfather`, since stopping the service stops everything it manages.

## Running it for development

Two terminals, with hot-reload.

**Terminal 1 — backend**

```bash
cd backend
./run.sh 9100          # http://127.0.0.1:9100
```

First run creates a virtualenv and installs dependencies.

**Terminal 2 — frontend**

```bash
cd frontend
npm install
PORT=5180 BACKEND_PORT=9100 npm run dev    # http://localhost:5180
```

Open http://localhost:5180. Interactive API docs: http://127.0.0.1:9100/docs

### Choosing ports

Nothing is hard-coded — pick whatever is free on your machine.

| | How to set it | Default |
| --- | --- | --- |
| Backend port | `./run.sh <port>` or `PORT=<port> ./run.sh` | `8000` |
| Backend host | `HOST=0.0.0.0 ./run.sh` | `127.0.0.1` |
| Frontend port | `PORT=<port> npm run dev` | `5173` |
| Backend the frontend proxies to | `BACKEND_PORT=<port> npm run dev` | `8000` |

`BACKEND_PORT` must match the port the backend is actually on, otherwise the UI shows
*Backend unreachable*. The dev server uses `strictPort`, so if the port you pick is taken it fails
loudly rather than quietly moving to the next one and proxying to the wrong place.

The backend accepts any `localhost`/`127.0.0.1` origin, so no CORS change is needed when you move the
frontend port.

---

## Registering applications

### Scan this system (fastest)

Click **⌕ Scan this system**. God Father inspects running processes and listening ports and offers
everything it finds, grouped into **your projects**, **developer tools** and **system services**, with
the name, folder, command and port already filled in. Edit any of them inline, tick what you want, and
import in one click. Re-scanning later shows anything already registered as *already added* rather
than duplicating it.

Two details worth knowing about the scan:

- **Related ports are grouped.** A single `npm start` that runs an API on 4000 and Vite on 5173 is
  offered as *one* application, not two, because that is the one command you would re-type.
- **Services need a second pass.** MySQL, MongoDB and ollama run as their own users, so a normal user
  cannot see which process owns their socket. They are found by process name and matched to their
  default port instead.

### Add one by hand

Click **+ Add application** and fill in:

| Field | Meaning | Example |
| --- | --- | --- |
| Name | Unique label | `admin-dashboard` |
| Start command | Run through the shell, in the working directory | `npm run dev` |
| Working directory | Must already exist | `/home/hh/projects/admin` |
| Port | Optional; used for conflict checks and the port map | `3000` |
| Environment | One `KEY=value` per line, merged over the backend's own env | `NODE_ENV=development` |
| Auto-restart | Relaunch automatically after a crash | off |

The port field checks itself as you type and tells you if something already holds it.

---

## Reading the logs of an app you already started

This is the one thing to understand about the tool.

**God Father can only capture the output of processes it starts itself.** An app you launched from a
terminal has its stdout wired to that terminal; there is no way to attach to it afterwards.

Such an app shows the status **Running outside** — a hollow green dot — meaning its port is being
served but by a process God Father does not own. Its Logs tab stays empty, and a banner on the app
says so.

To start capturing its logs, once per app:

1. Stop it wherever it is running now (Ctrl-C in that terminal).
2. Press **Start** in God Father.

From then on everything is captured, and it survives you closing the terminal.

**Start and Restart both refuse** while the app is still running elsewhere, naming the process that
holds the port — nothing is killed behind your back, and nothing is launched into an occupied port.
Restart applies the check *after* releasing its own process, so restarting an app God Father owns
works normally.

### Take over

Rather than hunting for the terminal that owns it, press **⚡ Take over**. It stops whatever holds the
port and starts the app here in one step — the two-part migration done for you.

Because it kills a process, it shows exactly what it will kill first — name, pid, user and full
command — and asks. The process is signalled politely, then forcibly after 8 seconds, and God Father
waits for the port to come free before launching. The whole process group goes, so an `npm start`
that spawned several servers is stopped as a unit rather than leaving orphans.

It refuses rather than doing something destructive when:

| Situation | Why |
| --- | --- |
| Held by another app God Father manages | Stop that app instead — its name is given |
| Runs as another user (MySQL, MongoDB, ollama) | Needs root; use `systemctl` |
| A protected system process | Killing `sshd`, `systemd`, the desktop, your editor is never right |
| God Father itself | Obvious |

| Status | Dot | Meaning |
| --- | --- | --- |
| Running | solid green | Started by God Father; logs captured |
| Running outside | hollow green | Port served by a process we do not own; no logs |
| Stopped | grey | Not running |
| Crashed | red | Exited non-zero; the reason is in the log |

## The live wall

**Live wall** in the sidebar shows every running web app at once, each in its own frame — a video
wall of your applications. Frames render at desktop width and are scaled to the tile, so a tile looks
like a small monitor rather than a squashed mobile layout.

- **1× to 4× per row** — fewer, bigger tiles or more, smaller ones
- **Fit** — works out columns *and* rows together so every tile lands inside the window with no
  scrolling. The more tiles you have the smaller each gets, so Fit is for glancing at everything at
  once; 1× or 2× is what you want to actually read one
- **Refresh** — a dropdown from 10 seconds to 6 hours that reloads every tile on that interval, plus
  **Refresh now** for a one-off. Off by default, since reloading a tile discards whatever state the
  app had. The header shows when the last refresh happened. For an app you are actively editing,
  **Live reload** below is the better tool — it reloads one tile when that app changes, instead of
  all of them on a timer
- **Pause** a tile to stop it consuming resources; **reload** just that one; **↗** opens it in a tab
- Clicking a tile's name jumps to that application's logs
- The wall re-probes every 15 seconds, which does *not* reload the frames

Only apps that actually serve an embeddable page get a frame. God Father probes each one and says why
the others cannot be shown, rather than leaving you with a blank rectangle:

| Reported as | Means |
| --- | --- |
| *Refuses embedding* | The app sends `X-Frame-Options` or a `frame-ancestors` policy |
| *Not a web page* | Answers HTTP but not HTML — ollama and mongo do this |
| *Error response* | Returns 4xx/5xx — often the wrong port or path, see below |
| *Not running* | Nothing to show yet |

### Camera, microphone and screen capture

A cross-origin frame is granted no device permissions by default, so an app that asks for the camera
is refused with `NotAllowedError` before you ever see a prompt — the panel renders and then sits
dead. Wall tiles therefore delegate:

```
allow="camera; microphone; display-capture; fullscreen"
```

That restores the app's ability to *ask*. The browser still puts its own prompt in front of every
request, and anything not on that list — geolocation, clipboard, payment — stays denied. If you
would rather grant nothing, drop the `allow` attribute from `TileFrame` in
`frontend/src/components/WallView.jsx` and from `backend/app/static/wall.html`.

**On the network wall this cannot work, and that is not something God Father can fix.** Camera
access also needs a *secure context*, and that page is plain `http://` on a LAN address rather than
`localhost`. `navigator.mediaDevices` simply does not exist there, so a well-written app reports
that it is unavailable. Watch such an app on the machine itself, where `127.0.0.1` counts as secure.

### Live reload

A tile shows the page it loaded when you opened the wall, and God Father runs your app with no
reloader of its own. So an edit goes stale twice over: the process still holds the code it imported
at startup, and the frame still shows the page from before you saved. Tick **Live reload** in an
app's Edit dialog and both are handled.

It watches the app's working directory and, about a second after you stop typing:

| You edited | What happens |
| --- | --- |
| A page, stylesheet, image or bundle | That one tile reloads. The process is left alone — it reads those off disk per request, so it is already serving the new one |
| Anything the server loaded at startup (`.py`, `.js`, `.go`, a config file) | The app restarts, God Father waits for its port to answer, *then* the tile reloads |

Only the app that changed reloads; every other tile keeps its state. Watched apps carry a ⚡ in
their tile bar, and the wall header names the last file that moved and whether it caused a restart.
Every action is written to the app's own log too, so a restart is never a mystery.

Details worth knowing:

- **Off by default, per app.** Turn it on for what you are working on. There is no point watching
  mysql, mongo or ollama
- **A burst of writes is one reload.** Saving a file often writes it two or three times, and a
  formatter can touch thirty; the quiet window collapses all of it into a single action
- **`.git`, `node_modules`, `.venv`, `__pycache__`, `build` and friends are not walked**, nor are
  editor scratch files, `.pyc`, or `.log` — an app that logs its own requests would otherwise
  reload itself forever
- **`.md`, `.rst` and `.txt` are ignored**, so writing documentation does not restart anything
- **An app running outside God Father cannot be restarted** — its process belongs to whichever
  terminal launched it. The tile still reloads, and the log says so
- **If the restart fails** — the port got taken while it was down, say — the tile reloads anyway and
  the reason lands in the app's log rather than disappearing
- The network wall picks this up too: while something is watched it polls every 2.5s instead of 20s
  and reloads only the frame whose revision moved. It gets this from the tile data it already
  fetches, so that server gains no route for it

### Watching the wall from another device

**Open on local network** publishes a copy of the wall on its own port (9200 by default, editable).
Open `http://<lan-ip>:9200/` on a phone, a tablet or another machine and you get the same wall, with
the same 1×–4×, **Fit** and **Refresh** controls — Fit and a refresh interval being the useful pair
for a screen left on display. That page remembers its refresh interval across reloads, so a display
that reboots comes back with the same setting.

It has the **📊 Analytics** tab too, with the same KPI cards, charts and per-app cards as the
dashboard (see below) — read-only, so there is no *Scan now* button and app names are not links.
It remembers which tab and time window you left it on.

It stays on across restarts, and the button turns it off again.

**It is a separate, read-only server.** The dashboard itself never leaves loopback, because it runs
arbitrary shell commands with no authentication. The public port serves exactly four things — the
page, the tile data, the Analytics figures, and a health check. Every control route returns 404 there: no start, stop,
restart, kill, edit, delete, logs or API docs.

**Tile URLs are rewritten to the LAN address.** A viewer's browser resolves them itself, so
`127.0.0.1:4200` would mean *the phone* — the network wall serves `192.168.0.115:4200` instead. That
only works for apps reachable from the network, so an app bound to loopback alone shows a **Not on
the network** placeholder telling you to turn on its local network access. Apps that bind `0.0.0.0`
themselves need nothing extra.

The port still needs a firewall opening, the same as any shared app — see below.

### Preview URL

The port an app is registered under is not always the page you want to watch. A project whose API is
on 4000 and whose UI is on 5173 would show the API's 404.

Set **Preview URL** in the app's Edit dialog to fix that. It accepts a full URL
(`http://localhost:5173`) or just a path (`/dashboard`, combined with the registered port). Leave it
blank to use the port as-is.

Tick **Include unshowable** to see every app on the wall, placeholders and all.

### News ticker

A bar across the top of the wall scrolls right to left like a TV news ticker, showing what just
happened across every application, each item with the time and the app's name. It is on the
read-only network wall too.

| Colour | Means | For example |
| --- | --- | --- |
| Green | Good news | *Started*, *Payment received ₹25,000*, an app came back up |
| Grey | Routine | *Stopped*, *Page updated — style.css changed* |
| Amber | Worth a look | *Restarted*, a warning line, *Stopped responding on port 11434* |
| Red | Something broke | *Crashed — exit code 1*, an error line, *HTTP 500 on POST /api/pay* |

A new headline joins the tape right behind whatever is scrolling past, marked **NEW**, within a few
seconds. When nothing new is happening, the last 40 headlines loop. Hover to pause, and click one to
open that app.

The news comes from four places:

- **Start, stop, crash and restart** of every app God Father runs, plus Live reload updating a tile
- **Log lines worth reading**: errors, warnings and HTTP 5xx responses, plus any line containing one
  of the app's **News keywords** (set in its Edit dialog, e.g. `payment, order, signup`). An app gets
  at most 6 of these a minute, and the same message only once every 5 minutes, so a crash loop
  can't flood the bar
- **The app's own announcements**, which read best because only the app knows what an event means.
  Print a line starting with `[news]`:

  ```python
  print("[news] New lead added: Rahul Sharma")
  print("[news:good] Payment received ₹25,000 from Priya")   # also :warn and :error
  ```

  An app running outside God Father has no captured output, so it can post instead:

  ```bash
  curl -X POST http://127.0.0.1:9100/api/news -H 'Content-Type: application/json' \
       -d '{"app": "CRM Dashboard", "message": "3 new orders", "level": "good"}'
  ```

- **Apps running outside God Father** going quiet or coming back on their port, checked every 15
  seconds

Untick **Show this app on the news ticker** in an app's Edit dialog to leave it off the bar entirely.
Headlines are kept in the database, the latest 500.

### Live logs

**💬 Live logs** opens a panel beside the tiles that shows every line every app writes, like a live
chat: each line appears at the bottom and older ones drift up and fade out at the top. Each app has
its own coloured avatar with its initials, and error, warning and keyword lines get softly tinted
cards, so you can tell at a glance which app said what.

- Lines are let in one at a time at a readable pace. If an app prints faster than anyone could read,
  the oldest waiting lines are skipped and the panel says how many
- Hover over the panel, or scroll up, to hold it still; **N new lines ↓** takes you back to live
- The chips at the top show one app only
- Click a line to open that app
- The **💬 Live logs** button shows or hides the panel, and the choice is remembered

Lines are held in memory (the last 500), not stored; each app's Logs tab is still the full history.
Only apps started by God Father have lines to show. The read-only network wall has the same panel.

### Analytics

The wall's second tab, **📊 Analytics**, answers *how much traffic are my apps getting, and how well
are they coping* — as KPI cards over 1h, 6h, 24h or 7d:

- **Totals** — requests, requests per minute right now and on average, error rate, 5xx count,
  response-time p95, apps up/stopped/crashed, CPU and memory
- **Highlights** — the busiest app, the one with the highest error rate, the slowest
- **Charts** — requests over time (failed ones in red), response codes, where requests come from
  (this machine / local network / internet), each app's share of load, busiest hours of the day,
  top endpoints
- **One card per app** — its own trend line, requests, errors, p95, CPU, memory, uptime, crashes and
  top three endpoints. Click a name to jump to that app

Nothing is proxied. The counts come from the **access log each app already prints** — the
`"GET /api/jobs HTTP/1.1" 200` lines — read out of the log God Father captures, every 10 seconds.
uvicorn, Flask/Werkzeug, `http.server`, gunicorn, Express/morgan and gin are all understood. So:

- Only apps started **by God Father** can be counted; one running outside has no captured log
- A server that prints no access log (Vite, `uvicorn --no-access-log`) is listed as **not counted**,
  never shown as zero
- Response time exists only where the server prints a duration (morgan, gin). uvicorn does not, so
  its p95 reads *not logged*
- The wall's own reachability probes are recognised and left out
- IDs in paths are grouped, so `/users/41` and `/users/42` are one endpoint, `/users/:id`

The first run back-fills from whatever the log files still hold. Buckets are kept for 7 days.

## The camera log

A log line says an application stopped at 03:14. It does not say who was sitting there. **Camera
log** in the sidebar closes that gap — and it does it by keeping a rolling recording rather than by
taking a photograph when something happens.

ffmpeg holds the webcam open for as long as God Father is running and drops one 640×480 JPEG into
`backend/data/ring/` every ten seconds. The last twenty minutes are kept; everything older is swept.
When an application stops, crashes or is taken over, nothing is photographed — the frame nearest
that moment is **looked up** in the recording and copied into the permanent log.

**On by default**, on any machine that has a camera and ffmpeg. A machine with neither is left
switched off rather than filling its log with rows that say "no camera found".

| Filed on | Why it is worth a frame |
| --- | --- |
| **Crashed** | The unexplained deaths — an exit code with a face beside it |
| **Stopped** | A deliberate act, so somebody is very likely at the keyboard |
| **Taken over** | Someone reclaimed a port from another process |
| **God Father shutting down** | The `systemctl stop`, and who ran it |
| **Power cut** | The last frame before the machine died — see below |
| Started | Off by default. A boot autostarts every app in turn and each one photographs an empty room; tick it on in the view if you want them |

Newest first, click one to enlarge. Crashes are outlined in red, shutdowns amber, and a power cut
gets its own red badge.

### Watching it back

Two buttons sit at the top of the camera log, and they play footage from two different places:

| | Plays | Available |
| --- | --- | --- |
| **▶ Live · last 5 min** | The rolling recording, ending a few seconds ago | Whenever recording is on |
| **▶ Power cut · last 5 min** | The minutes leading up to the most recent power cut, ending at the instant the machine died | Only once a power cut has happened |

They are separate because they read from different places. The live button plays `ring/`, which is
swept continuously; the power-cut button plays a rescued copy in `incidents/`, which is not. For a
crash or a stop there is no second button and none is needed — those frames are still in the ring,
so the live clip already covers them for as long as they are held. A power cut is the one event
whose surroundings are destroyed before anyone can look at them.

Either way it is a timelapse — one recorded frame per frame of video, so five minutes of room
becomes about seven seconds of playback. The live player also offers 1 minute and the full window,
and both offer **Download** if you need to keep or send a copy.

The clip is rendered on request and never cached: the newest frame changes every ten seconds, so
anything kept would be stale before it was asked for twice. A five-minute clip takes about a tenth
of a second to encode and comes out around 500 KB.

**Every frame has its own capture time burnt into it.** Not a running clock — the actual wall-clock
second that frame was taken, which matters more than it sounds. The recording holds each frame for
the real gap until the next one, so if the stream was down the clock *jumps*, and the jump is how
you see the gap. A held frame with a clock ticking over it would say "the room did not change";
a jump says "we have no idea what happened here", which is the truth. Gaps are capped at two
intervals of playback so an hour-long outage cannot fill the clip with one stale image.

### Surviving a power cut

A power cut signals nothing. No handler runs, no code executes, the lights simply go out
mid-instruction. So the moment most worth a photograph is the one moment a camera triggered *by*
events can never catch, and no amount of "save on shutdown" can fix that — there is no shutdown.

The recording sidesteps the problem rather than solving it. Every frame is already on disk the
instant it is taken, so **there is nothing to save when the power goes**. The newest surviving file
simply *is* the last thing the machine saw.

What is left is a job for the next boot, not for the microsecond the power drops. God Father writes
a marker at startup and blanks it on a clean exit. Finding that marker still set means the last run
never reached its shutdown path, so the newest surviving frame is promoted to a **Power cut**
capture — stamped with the frame's own modification time, not with boot time. The log therefore says
*when* the machine died, to within ten seconds, and shows you the room. `journalctl` often cannot
tell you the first part and can never tell you the second.

**The run-up is rescued too, not just the final frame.** Every other kind of event keeps its
surroundings for free — they stay in the ring, and the clip player can reach them for as long as
they are held. A power cut cannot: within seconds of the next boot the sweeper deletes every frame
of what led to it. So recovery copies the last five minutes out of the ring first, into
`backend/data/incidents/`, and the capture gets a **▶ Watch the 5 minutes before the cut** button of
its own. About 30 frames, under a megabyte, kept for exactly as long as the capture is — delete the
capture and the footage goes with it. The window is set by **Rescue on cut (min)** in the view.

Recovery deliberately runs **before** the sweeper starts, and that ordering is the whole feature.
After a long outage every surviving frame is already older than the retention window, so a sweep
would delete the evidence within seconds of boot.

The rescue window is measured back from the **last frame**, not from the present moment — the machine
may have been off for days, and a window measured from now would come back empty at the exact
instant the footage was being saved.

Two honest limits. A frame written a second or two before the cut may still be in the page cache and
never reach the disk, so in practice the surviving photo is from **ten to forty seconds** before the
machine died rather than exactly ten. And if the outage lasted longer than the machine has been
back, the frame is still the right one — the ring is untouched while nothing is running.

### Why a recording rather than a shutter

A V4L2 webcam's opening frames are not exposed yet. On the camera this was built against, frame zero
comes back **uniformly black — a mean luma of 0.0 out of 255**, indistinguishable from a working
camera in a dark room. Discarding the first ten frames fixes it, and costs about 1.7 seconds.

The old design paid that 1.7s on every single photograph, which is why captures had to be queued and
taken by a background worker. A continuously running stream pays it **once, at startup** — every
frame after that comes off a sensor that is already warm. Which changes what the rest of the feature
has to be:

- **Filing an event is instant.** It copies a file that already exists. No queue, no worker thread,
  no lifecycle event ever waiting on a camera
- **The frame can pre-date the event.** The nearest frame is chosen in either direction, so a crash
  can be filed with the room as it was four seconds *before* it happened. A camera triggered by the
  event could only ever look forwards, and it looked forwards by two seconds of warm-up
- **A restart needs no special case.** `stopped` and `started` two seconds apart resolve to the same
  frame, and because a capture is named after the frame it came from they share one file on disk.
  Deleting either still leaves the other

### Things worth knowing

- **The camera is held open the whole time God Father runs.** A camera can only be opened by one
  program at a time, so nothing else on the machine can use it — a browser tab, Third Eye's
  server-camera view, anything. This is the one real cost of the design and it is not a small one
- **The activity LED will be lit permanently** on most webcams, and that is a hardware line you
  cannot switch off. Treat it as a feature: it is honest, visible notice that recording is happening
- **The stream is supervised.** A long-lived ffmpeg eventually dies — a USB reset, a knocked cable —
  and the failure is silent: God Father keeps running and the UI keeps looking healthy while nothing
  is being recorded. So it is restarted with backoff, and the status strip states whether frames are
  actually *landing* rather than only whether the process is alive. Trust the frame age, not the switch
- **Orphans are reaped.** SIGKILL does not propagate to children, so a hard-stopped God Father leaves
  ffmpeg alive holding the camera — which would lock the *next* God Father out of recording
  permanently, the feature failing in exactly the case it exists for. Any such orphan is identified
  by the ring path in its command line and killed before a new stream starts
- **Disk is nothing.** ~30 KB a frame, six frames a minute: twenty minutes of recording is about
  3.6 MB. The permanent log is capped separately, 500 MB by default, oldest deleted first
- **Deleting an application does not delete its captures.** They are the record of what happened; the
  app name is kept on each one so the history still reads properly
- **Test shot** proves the camera now rather than at the worst possible moment. If the recording is
  running it files the newest frame — which proves the recording is live, the thing actually worth
  knowing. If it is not, the device is free and one is taken directly

### Before you leave it on

This records **whoever** is at that machine, continuously, not only you — anyone who walks up to it
and anyone in frame behind them. That is a meaningfully bigger thing than photographing lifecycle
events, and on a shared or office machine it is a decision to make deliberately rather than discover
later. Shorten the window or switch it off in the view.

The images never leave the machine: they are written to `backend/data/ring/` and
`backend/data/captures/`, served only over loopback, and the read-only network wall has no route
that can reach them.

## The AI model log

The **AI models** tab answers a question the System tab is one layer below: not *what is this machine
doing*, but **which of my applications is talking to which model, and how much**.

On a developer machine there is no single source of truth for that, so it is assembled from four, and
the tab always says which one it is using:

| Source | What it means | How far to trust it |
| --- | --- | --- |
| **Declared** | You wrote it down against an application | The only source that can be right about an app that has not run yet |
| **Observed** | An open socket to that provider was actually seen | A fact about this machine |
| **Detected** | Read out of the application's own folder — its `*_API_KEY` names, its SDK imports, model ids in its source | A reading of the source, and labelled as one |
| **Measured** | A real token count, from the app reporting it or from its captured log | Exact where it exists, absent where it does not |

Expand any row to see the evidence behind every claim, down to the file it came from.

### What it does not do

It does not proxy, intercept or decrypt anything. That has a consequence worth stating plainly: God
Father can see **that** an application is talking to a model, because the socket is visible, but a
token count only ever exists inside the application. So an app it can see talking but not counting is
shown as **not counted** rather than as a zero — a zero would be a lie, and the whole tab is only
worth having if its numbers can be trusted.

One more consequence: a connection is not a call. HTTP/2 carries many requests down one socket, so
the live half says *talking*, never *talked N times*.

### Getting exact numbers

Two ways, neither of which needs a proxy.

**1 — report it.** One request after each call. The app is matched by its registered name, its id, or
the port it serves:

```python
response = client.messages.create(model=MODEL, ...)

requests.post("http://127.0.0.1:9100/api/ai/usage", json={
    "app": "my-app",
    "model": MODEL,
    "input_tokens": response.usage.input_tokens,
    "output_tokens": response.usage.output_tokens,
})
```

**2 — print it.** If the application simply prints its usage block, God Father reads it out of the log
it is already capturing — no code change beyond the print. The OpenAI, Anthropic, Gemini and Ollama
shapes are all understood, including Anthropic's cache token fields.

### Cost

Priced from a built-in table of per-million-token rates. A model missing from the table shows no cost
rather than a guessed one, and locally served models are shown as *runs here* — they cost VRAM, which
is what the System tab is for.

### Local models

If Ollama is running, the tab asks it directly what it has. **Loaded** is the interesting state: a
downloaded model costs disk, a loaded one is holding the VRAM the System tab is showing you.

### Things worth knowing

- Folders are re-read every 15 minutes, or on demand with **Re-scan folders**. Live connections are
  walked every 15 seconds.
- An application registered against `/` or your home directory — a database, a tunnel — has its
  folder scan skipped. Reading one would describe every project on the machine rather than that app.
- Detection deliberately errs towards missing an unusual model name over inventing one. It insists on
  the shape a real model id has: a version number, a family word, or an Ollama `name:tag`.
- Counted calls are kept a fortnight; connection history a day.

## Opening an app to your local network

Each application has a **Network** tab with one switch: *Open to local network*. Turn it on and the
app becomes reachable from other devices on your WiFi — your phone, another laptop — at
`http://<this machine's LAN ip>:<same port>`.

The port number does not change. God Father binds your LAN address on the *same* port and relays to
the app's loopback listener, which the kernel allows because `192.168.0.167:4200` and
`127.0.0.1:4200` are different sockets.

It picks the right method per app, and tells you which it used:

| The app binds | What happens |
| --- | --- |
| `127.0.0.1` only | God Father relays `<lan-ip>:port` → `127.0.0.1:port`. Works without touching the app's code |
| `0.0.0.0` / `*` already | No relay — it is already listening everywhere, so only the firewall rule matters |

Sharing survives restarts: an app left switched on has its relay reopened when the service starts.

### Firewall

ufw will still block the port unless a rule allows it. Let God Father manage that, once:

```bash
sudo ./deploy/install-firewall-rule.sh
```

This installs `/etc/sudoers.d/godfather-ufw` granting your user exactly three things: `ufw status`,
and adding/removing an `allow from <subnet> to any port N proto tcp` rule. Nothing else. Undo with
`sudo rm /etc/sudoers.d/godfather-ufw`.

Without it everything still works, but the Network tab shows you the `sudo ufw …` command to run by
hand for each port.

### Two things to know

- **The app sees `127.0.0.1` as the client address**, not the real device IP, because this is a plain
  TCP relay. If your app logs or rate-limits by IP, it will see the relay. Apps that already bind
  `0.0.0.0` are unaffected, since no relay is involved.
- **The dashboard itself stays on localhost.** God Father runs arbitrary shell commands with no
  authentication, so it deliberately does not offer to expose itself. Only your apps get shared.

## How it behaves

**Process groups.** Each app is launched via the shell in its own session
(`start_new_session=True`), so stopping it signals the whole process tree — `npm run dev` and the
node process it spawns both go down. Stop sends `SIGTERM`, waits 8 seconds, then `SIGKILL`.

**Logs.** stdout and stderr are captured separately (stderr renders red) and written to
`backend/data/logs/app-<id>.log`. The last 3000 lines per app stay in memory for instant replay when
you open the viewer; older lines are read back from the file. Log files rotate at 5 MB.

**Backend restarts stop your apps.** Children are deliberately shut down when the backend exits —
leaving them running with nobody capturing their output would orphan them. So `systemctl --user
restart godfather`, re-running the installer, or a crash of the service all stop every managed app.
Apps with **start on boot** enabled come back by themselves; the rest wait for you to press Start.

**Auto-restart guard.** After 5 restarts inside a 60-second window, auto-restart switches itself off
and says so in the log, rather than looping forever on an app that cannot start.

**Port detail needs permissions.** Ports owned by other users show as listening but without a process
name — reading those requires elevated privileges. Your own processes always show fully.

---

## Layout

```
backend/
  app/
    main.py         FastAPI app, CORS, lifespan
    manager.py      process supervision, log capture, auto-restart
    ai_monitor.py   model detection, live provider sockets, token accounting
    traffic.py      counts requests from each app's access log, per minute
    news.py         the news ticker feed: events, log lines, announcements
    discovery.py    scans processes/ports and infers importable applications
    routers/
      apps.py       CRUD + start/stop/restart + events
      logs.py       log snapshot, clear, WebSocket stream
      ports.py      port map and single-port inspection
      discover.py   system scan and bulk import
      ai.py         model usage state, usage ingest, declared links
      traffic.py    the Analytics tab: per-app requests, errors, load
      news.py       news ticker: recent headlines, app posts, WebSocket feed
    ports.py        psutil-backed port and process inspection
    database.py     SQLite schema and helpers
    models.py       request/response schemas
    config.py       paths and tunables
  data/             SQLite database + captured logs (created on first run)

frontend/
  src/
    App.jsx         layout, polling, notifications
    api.js          REST + WebSocket client
    components/
      Sidebar.jsx   nav, search, app list
      AppDetail.jsx header, stats, tabs
      LogViewer.jsx live log stream
      AppForm.jsx   add/edit modal
      PortsView.jsx port map table
      AiView.jsx    which app uses which model, and how much
      AnalyticsView.jsx  traffic and performance KPIs, on the wall's second tab
      NewsTicker.jsx     the scrolling news bar at the top of the wall
```

## Configuration

Environment variables read by the backend:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GODFATHER_DATA_DIR` | `backend/data` | Where the database and logs live |
| `GODFATHER_LOG_BUFFER` | `3000` | Lines kept in memory per app |
| `GODFATHER_MAX_LOG_BYTES` | `5242880` | Log size before rotation |
| `GODFATHER_STOP_GRACE` | `8` | Seconds before `SIGKILL` |
| `GODFATHER_MAX_RESTARTS` | `5` | Rapid restarts before auto-restart disables itself |

For the frontend, `VITE_API_BASE` points at a backend on a different host; leave it unset to use the
Vite proxy.

## A note on scope

Commands you register run with your own user's privileges — this is a tool for your own machine, and
it binds to `127.0.0.1` only. Don't expose the backend on a public interface: anything that can reach
it can run commands as you.
