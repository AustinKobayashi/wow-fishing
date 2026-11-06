# The Angler's Rest

A local Python web controller for the WoW fishing bot. The Warcraft-themed page controls the real bot, edits runtime settings, shows live logs, and previews timing distributions using the same SciPy model as the bot.

## Docker on the Raspberry Pi

The Compose setup runs the UI and bot in one container. Keep `pigpiod` on the Pi host. Linux host networking exposes the UI to the configured LAN and Tailscale networks and connects to pigpiod on `localhost:8888`. Using `localhost` supports the daemon's IPv4 or IPv6 loopback listener. Audio access is limited to `/dev/snd`, without privileged mode. The process runs as an unprivileged container user.

With Docker Engine and the Compose plugin installed on the Pi:

```bash
# Install the host daemon's boot service once.
sudo install -m 644 deploy/pigpiod.service /etc/systemd/system/pigpiod.service
sudo systemctl daemon-reload
sudo systemctl enable --now pigpiod
sudo systemctl enable docker

# Use the group that owns the Pi's audio devices.
export AUDIO_GID=$(stat -c '%g' /dev/snd/controlC0)
docker compose up -d --build
docker compose logs -f
```

If your audio device has a different control number, use the matching `/dev/snd/controlC*` path. Compose defaults to audio group 29 when `AUDIO_GID` is not set. The USB audio device must already be attached. PyAudio uses the container's default ALSA input. Host-specific ALSA or PulseAudio configuration is not copied into the image. Stop any native bot or controller first to avoid competing for the same hardware and port.

Open **http://192.168.1.233:8766** on the LAN or **http://100.104.134.32:8766** through Tailscale. Compose allows clients from `192.168.1.0/24` and Tailscale's `100.64.0.0/10` range, plus loopback. Adjust the LAN subnet in `command` if deploying elsewhere. Do not add a port mapping with host networking. This Compose file targets a Linux Pi, not Docker Desktop's audio devices.

Settings, catch logs, logs, and stats are kept in the `fishing-data` named volume at `/data`. Rebuilding the image or running `docker compose down` preserves them. `docker compose down -v` deletes them. Existing native files are not imported automatically. To copy a native configuration before the first session:

```bash
docker compose cp config.json fishing:/data/config.json
docker compose restart fishing
```

Stop fishing in the UI before replacing an existing configuration. For a full shutdown:

```bash
docker compose down
```

Docker sends SIGTERM to the Python process and allows 30 seconds for cleanup before forcing termination. A stuck native device call can still outlast that timeout. Restarting the container starts only the controller, not a fishing session.

### Boot behavior

After a Pi reboot, systemd starts the host's `pigpiod` service and Docker restores the web controller through `restart: unless-stopped`. Fishing remains stopped until you click **Cast a line**. A session interrupted by a reboot or container crash does not resume automatically. Settings stay in the named volume. Tailscale must also have its service enabled and remain signed in for remote access.

The service expects the existing pigpio installation at `/usr/local/bin/pigpiod`. It runs in the foreground under systemd, allows only local socket clients, and disables the unused FIFO control interface. Starting the daemon does not request servo pulses. Do not run a second manual `pigpiod` instance alongside the service.

If Start reports that it cannot connect to pigpiod, check the host service:

```bash
systemctl status pigpiod --no-pager
journalctl -u pigpiod -n 30 --no-pager
sudo systemctl restart pigpiod
```

Stop any fishing session before restarting the daemon. Manually stopping the container with `docker compose stop` keeps it stopped across reboots. Use `docker compose up -d` to restore it. To disable the GPIO service as well, first stop fishing and then run `sudo systemctl disable --now pigpiod`.

On the current Wi-Fi Pi, the generic network readiness check times out for two minutes because `wlan0` is marked optional. The deployed override waits explicitly for its routable IPv4 connection:

```bash
sudo mkdir -p /etc/systemd/system/systemd-networkd-wait-online.service.d
sudo install -m 644 deploy/wait-online-wifi.conf /etc/systemd/system/systemd-networkd-wait-online.service.d/wifi.conf
sudo systemctl daemon-reload
```

This override is specific to the current `wlan0` deployment. Remove it if switching to Ethernet. It changes boot readiness checking, not network configuration or credentials.

Build and run the hardware-free test suite on any supported Docker host:

```bash
docker build -t wow-fishing:local .
docker run --rm wow-fishing:local python -m unittest discover -s tests -v
```

The image includes PortAudio, PyAudio, NumPy, SciPy, and the pigpio Python client. Its build context excludes local settings, logs, Git history, and hardware-design files. The optional `FISHING_DATA_DIR` environment variable moves all runtime data together. Without it, native runs retain their existing repository-local paths.

## Run on the Raspberry Pi

From this repository with the Pi's Python environment active:

```bash
python3 -m pip install -r requirements.txt
sudo pigpiod
python3 webui.py
```

If pigpiod is already running, skip that command. The existing GPIO wiring remains on pin 27. PyAudio needs PortAudio on the Pi. If its installation fails on Ubuntu or Debian, install `portaudio19-dev` and `python3-dev` through apt, then retry pip.

`bash start.sh` activates `venv` when present and starts the web server. It does not kill Python processes or start pigpiod. Run one controller and do not run `main.py` at the same time.

For native runs, network access is opt-in:

```bash
python3 webui.py --allow-network 192.168.1.0/24 --allow-network 100.64.0.0/10
```

Then open the Pi's LAN or Tailscale IPv4 address on port 8766. The server rejects clients outside the allowed networks, foreign Host/Origin headers, and control requests without the controller's token. Use the IP address in the URL. This personal controller has no account system, so anyone on the allowed networks can use it. Keep public reverse proxies and Tailscale Funnel disabled.

Without `--allow-network`, the server remains loopback-only at **http://127.0.0.1:8766**. Use `--port 8767` if another port is needed.

## Controls

- Edit settings, inspect the updated graph, then **Save settings**. Settings are validated and atomically saved to ignored `config.json`. Graph edits are drafts until saved. Top summary cards show saved settings.
- **Cast a line** starts one hardware session with the saved values. No GPIO or audio connection is made just by opening the server or page.
- **Stop fishing** interrupts waits and disables servo pulses during cleanup. Another session cannot start until the worker exits. Native device initialization or reads must return before cleanup can finish, so a stuck driver can leave the status at Stopping.
- Closing the page does not stop the bot. The configured runtime limit and Ctrl+C/server shutdown also request a stop.
- The journal polls every second and retains the latest 200 entries in memory. Filter, clear the local view, or export the recent entries. Full bot logs remain in `logs/DD-MM-YYYY-log.txt`. Device-library output written directly to stderr remains in the server terminal.
- Session summaries are written to `stats.txt`. Timing histories retain the latest 10,000 samples of each kind. Volume statistics and detection counts cover the complete session.
- **Restore defaults** creates an unsaved draft. Stop the bot and save to apply it.

You can still run `python3 main.py` for a terminal-only session using the same saved configuration. Ctrl+C requests cleanup.

### Log a catch

Each saved fish row has **Edit** and **Remove** buttons. **Edit** opens that row in place to change its fish name, total amount, or Stacks flag. **Save row** updates the all-session rates, while **Cancel** discards the draft. Rows that match the same fish and Stacks flag are combined. A session's location, duration, and creation date cannot be edited. **Remove** asks for confirmation before removing a fish row. **Delete session** asks for confirmation before permanently removing the entire session and its catches. After a successful **Log a catch**, the fish name clears, the amount resets to 1, and **Stacks** resets to checked. Failed saves keep the inputs so you can retry.

Open **Log a catch** from the sidebar or expand its panel beneath Tackle & settings. After fishing, enter your location and session duration in whole seconds, then click **Create session log**. The creation date is recorded automatically. These are manual logs, independent of starting or stopping the bot.

Select a saved log to view its location, creation date, duration, fish counts, and total. Enter a fish name and an amount, then click **Log a catch** to save it immediately. **Stacks** is checked by default. Uncheck it for fish that cannot stack. Logging the same fish with the same flag adds to that line's count. Different flags stay on separate lines and do not change earlier catches. Fish names from all saved logs appear as suggestions, and capitalization differences reuse the original name. **Export session JSON** downloads the selected log:

```json
{
  "date": "2026-11-06T18:00:00+00:00",
  "location": "Stormwind",
  "sessionLength": 7200,
  "catches": [
    {"fish": "Bristle Whisker Catfish", "amount": 3, "stacks": true}
  ]
}
```

All logs are stored as an expandable JSON array in `catches.json`, beside `config.json`. In Docker this is `/data/catches.json` in the persistent volume. Native runs use the repository folder unless `FISHING_DATA_DIR` is set. Writes replace the file atomically. Logs and fish names survive controller restarts. Dates are stored in UTC and displayed in your browser's local time. Reopen the panel to refresh logs added from another browser.

The **Catch rates** tab in **Log a catch** shows the combined fish per hour, total catches, total hours, and a per-fish table across every saved session. Each rate is `total fish * 3600 / total session seconds`, including time from sessions with no catches of that fish. Longer sessions contribute proportionally. Rates are calculated from saved logs and are not stored separately. With no recorded time, rates show **Not available**. Logs with catches but zero duration are flagged because they can overstate rates. Switch back to **Session logs** to view or add entries.

**Stacks/hour** uses 20 fish per stack for lines with **Stacks** checked, with each species rounded up separately after combining its stacking catches across all sessions. Non-stacking catches each occupy one slot. For example, 23 stacking fish A and 50 stacking fish B occupy 2 + 3 = 5 stacks. Three non-stacking fish add another 3 slots. Divide that total by total recorded hours for the rate. Hover, focus, or tap the stat to see full, partial, and non-stacking counts. Press Escape or move focus away to dismiss the breakdown. Older entries without a `stacks` flag default to stacking and do not need migration.

## Timing semantics

The UI and runtime share `normal_distribution.py`. Samples follow a normal distribution truncated below the minimum, with no upper cap. The spread reference and tail parameter set sigma. Direct SciPy truncated-normal sampling replaces the old recursive rejection while preserving its distribution.

The independent `DONT_REEL_TIME` cutoff and existing fishing-window check are preserved. The runtime waits R, then checks elapsed + R against the fishing window. For detection age A and negligible overhead, the combined delay threshold is `min(DONT_REEL_TIME, (WOW_FISHING_TIME - A) / 2)`.

Defaults predict approximately **4.08945% cutoff skips per eligible detected event**, not per cast and not a catch-failure rate. Actual detection ages and processing overhead affect the window check. The scenario warns when the post-cast guard or recast timeout prevents the hypothetical event.

Cast delay uses the previous reel sample as its underlying mean. The cast graph is explicitly conditional on the chosen reel sample. Probability calculations include the whole unbounded tail even when the plot viewport does not.

Static values such as GPIO pin, sample format, buffer size and log paths are not editable. The unused under-minimum modifiers and overridden cast mean modifier are omitted.

## Development and verification

The UI can run without hardware packages until Start is pressed:

```bash
python -m pip install numpy scipy
python webui.py
python -m unittest discover -s tests -v
node --check web/app.js
```

Tests use fake hardware and cover cleanup, interruptible waits, the preserved timing checks, numerical probabilities, persistence, request protection, and session lifecycle. They do not certify physical servo calibration or audio detection on the Pi.

`web/` is the working UI. `mockup/` preserves the original standalone simulation. Artwork is local, with provenance in `web/assets/sources.json` and credits in the page footer.
