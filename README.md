# Local Copilot session monitor

A standalone Windows tray app and local web page for watching Copilot sessions. See which observed session families are working, keep finished entries until you dismiss them, and receive native notifications when an observed family's current runs finish.

The monitor runs independently of your Copilot chats and browser tabs. It has no npm dependencies, cloud service, or installer. **Coverage is this Windows machine only.** This is an unofficial, version-sensitive prototype, not an official Copilot live-status API.

## Requirements

- Windows with an interactive desktop and Windows PowerShell.
- [Node.js](https://nodejs.org/) **24 or newer**, available on `PATH`.
- A local Copilot desktop installation that writes the supported metadata and event files under `%USERPROFILE%\.copilot`. The adapter was verified against desktop 1.1.24 / CLI 1.0.90-0; other versions may change the format.
- A current Edge or Chromium browser with CSS `light-dark()` support.
- Git if cloning the repository rather than downloading its source.

There is **no `npm install` step**. The app uses Node.js built-in modules and Windows components.

## Quick start

Run these commands in PowerShell from a directory where you keep projects:

```powershell
git clone https://github.com/rchiodo/copilot-session-monitor.git
cd .\copilot-session-monitor
.\Start-Monitor.ps1
```

Open **http://127.0.0.1:43187**. The start script launches a separate background Node process, waits for observation to become healthy, and opens the browser. Running the script again finds the existing monitor instead of starting another copy.

To start without opening a browser:

```powershell
.\Start-Monitor.ps1 -NoBrowser
```

The Windows tray icon provides **Open observed sessions**, **Test notification**, and **Stop monitor**. Closing the browser or terminal does not stop the background monitor. The tray icon may be in the notification area's overflow menu.

To stop it, use the tray menu or run this from the cloned directory:

```powershell
.\Stop-Monitor.ps1
```

Stopping the monitor does not stop, resume, or modify any Copilot session. No Windows startup registration is installed; start it again after signing in or rebooting.

If PowerShell blocks an unsigned local script and your organization's policy permits a one-process override:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Start-Monitor.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Stop-Monitor.ps1
```

These commands do not change saved execution policy. Do not bypass an organization's enforced policy.

For foreground operation, use `npm start` and stop with Ctrl+C. To use another port, stop the existing monitor, set `$env:MONITOR_PORT = '43188'`, then start it again. The start script prints the actual URL. Run only one instance per checkout.

## Using the monitor

| UI element | Meaning |
| --- | --- |
| **Running** column | A parent or at least one observed descendant has current execution evidence. Attached background commands can keep a family Working after the foreground model stops. |
| **Finished / Needs input** column | Retained non-running families, with distinct finished, input/approval, error/interrupted, and **Unconfirmed** labels. Being in this column alone does not mean finished. |
| **Run finished** | The monitor observed the current runs finishing. This is **not** a claim that the whole task, tests, or pull request succeeded. The observed completion time is displayed explicitly. |
| **Parent alert** | The parent's own latest observed monitor alert and its time, independent of aggregate family status. A child alert never replaces it. **No parent alert observed** means none was recorded, not that an old alert was reconstructed. |
| **Working 1/3** | One of three retained family members is working. Dormant children listed only as metadata are not included in the working count. |
| **Not observed** | A linked child's name is known, but this monitor has not observed its execution. It is not assumed idle or finished. |

One compact card represents a top-level parent and its linked descendants. Standalone sessions have one card each. Canonical app records determine relationships; names, repositories, and working directories do not.

Click a card's disclosure, or focus it with Tab and press Enter/Space, to expand the full parent and child names, nested relationships, member statuses, machine/source, response time, and completion/alert details. Long names wrap in the details. Disclosure and focus are preserved through normal polling and reordering.

Each column sorts by the **parent's latest assistant response timestamp**, not tool activity or polling time. If no response time is available, a labeled, stable first-observed time is used. Dates use your browser's local time zone.

Rows are compact by default: approximately 56px on desktop, with about ten visible per column at a 1200x900 CSS-pixel viewport. Expanded details and narrow screens can be taller. At widths of 900px or less, the columns stack without horizontal overflow.

The app follows the Windows user's **app light/dark preference**, including changes while running. It reads `AppsUseLightTheme` without changing it. If Windows preference reading is unavailable, the UI labels its browser-theme fallback.

### Dismiss finished entries

**Dismiss** hides one safely finished family; **Clear finished** hides the visible qualifying finished families. These controls affect **only this monitor's list**. They do not delete Copilot sessions, files, history, worktrees, source-database rows, or Windows notifications.

Working, waiting, error, and unconfirmed families cannot be dismissed. The server refreshes source state and checks each completion revision before accepting an action, so stale controls cannot hide resumed work. Bulk actions report skipped entries.

Dismissal survives browser refresh and monitor restart without resetting observation or notification dedupe. New observed work by the parent or any descendant restores the family. Changed membership or uncertain state can also make a card visible conservatively. There is no automatic expiration or trash folder.

### Check native notifications

Select **Test notification** on the page or tray menu. It sends a clearly labeled test through the same Windows notification mechanism, independent of Copilot and browser notification permissions.

Windows **Focus / Do not disturb**, notification policy, or other Windows settings can suppress tray balloons. The page distinguishes queued, submitted, and Windows-reported shown; even "shown" is not proof that you personally saw a banner. The monitor does not change notification settings.

Completion alerts are grouped by family: a parent's run ending does not announce family completion while another observed member still works. Input, error, and unavailable-status alerts are distinct from successful completion.

## How observation works

The app opens `%USERPROFILE%\.copilot\data.db` read-only and reads named session/hierarchy metadata fields. It incrementally reduces `%USERPROFILE%\.copilot\session-state\<id>\events.jsonl` into lifecycle metadata. Live owners are matched through `inuse.<pid>.lock`, Windows process identity, and process creation times. Desktop owners must have a live Copilot desktop parent process.

Hierarchy resolution uses `workspaces.session_id`, `workspace_parent_links`, `workspace_session_aliases`, chat creator links, and recorded side-chat links. Older runtime IDs are resolved without importing unrelated historical sessions.

**The persisted `is_running` flag is not full-session activity.** A foreground model loop can stop while attached PowerShell commands or task agents remain running. The monitor independently discovers live local owners and tracks supported background-work lifecycle evidence. A tool call returning does not mean its command exited. Explicitly detached services do not hold a run open.

An observed desktop completion requires same-run terminal evidence, a cleared foreground flag, no outstanding or unconfirmed supported background work, continuous healthy observation, and a live unchanged owner. Unsupported shell-status formats, missing exits, failures, cancellation, input gates, and observation gaps do not establish success.

The monitor never resumes, aborts, sends prompts to, or modifies Copilot sessions to discover state. It does not scrape Copilot UI, connect to authenticated cloud endpoints, read Copilot authentication tokens, or rely on an AI chat polling status tools.

See [GitHub's session data documentation](https://docs.github.com/en/copilot/concepts/security-governance-and-network-settings/session-data). The database schema and runtime status envelopes used here remain **undocumented/version-sensitive implementation details**.

## Reliability and limitations

- **Local Windows only.** Other machines, remote hosts, and cloud execution are not monitored. A known active nonlocal relative can appear as an unavailable completion blocker, not as verified remote activity.
- **Standalone CLI is activity-only.** Supported live root turns can appear, but full CLI-run completion notifications are deliberately unsupported. The SDK's true `session.idle` and `assistant.idle` signals are ephemeral, not recoverable from JSONL. Unsupported background-work mechanisms may require a future public standalone status interface.
- Startup and reconnect baseline existing state. Old finished runs do not trigger a notification storm. Runs that finish while the monitor is stopped or disconnected are not retroactively declared successful.
- Polling is approximately every 1.5 seconds, with process snapshots every 2 seconds and CLI directory discovery every 5 seconds. Short transitions entirely between observations can be missed. Silence, file age, and process existence alone are not proof of completion.
- Sleep, clock rollback, polling gaps over 15 seconds, dead/replaced owners, event rotation, malformed data, or reader failures discard completion authority. UI snapshots older than 10 seconds are unavailable rather than falsely healthy.
- A final marker already present when observation begins cannot confirm a later finish by itself. Unsupported final-response shapes remain unconfirmed. Partial JSONL writes wait for a complete line; malformed or oversized records fail closed.
- Missing parents, ambiguous identities, conflicting links, and hierarchy cycles block family completion. Dormant linked names are metadata, not proof of observed work.
- Native input gates that were never persisted and internal deadlocks may not be observable. The monitor cannot promise exact full-session completion for every runtime/version.
- Notifications are at-most-once: dedupe is saved before delivery, so a crash can lose an alert rather than replay it.

## Local storage and privacy

All runtime data is kept under **`.local\` in your checkout**, which is excluded from Git:

| File | Contents |
| --- | --- |
| `sessions.json` | Retained metadata: session IDs/titles, machine/source, relationships, states, timestamps, parent alerts, and dismissed-run hashes. |
| `notifications.json` | Hashed notification dedupe keys. |
| `runtime.json` | This monitor's PID, instance identity, loopback URL, and control token. Treat this file as private. |
| `monitor.log`, `monitor-error.log` | Start-script process output and diagnostics. |

Retained metadata accumulates locally; dismissal hides cards but does not erase internal observations. Do not publish `.local`, Copilot databases/event files, screenshots, or diagnostic snapshots.

Transcript text, commands, prompts, and tool output are not displayed or retained by the monitor. Raw event bytes are temporarily read to extract lifecycle facts; only required metadata is kept. Source databases/settings are never modified.

The server binds only to **127.0.0.1**, makes no external network requests, and uses Host/Origin checks and a control token for mutations. Other programs running as your Windows user can access the local UI. This is not an authenticated multi-user service.

## Troubleshooting

- **Start fails:** check `node --version`, the PowerShell error, and `.local\monitor-error.log`. A missing or incompatible Copilot database is an observation error, not an empty successful result.
- **No cards:** idle historical sessions are deliberately not imported. Let normal Copilot work run; do not treat an empty list as proof that unsupported remote work is idle.
- **Unconfirmed:** expand the card and check the health message. Restore the missing source or restart the monitor after an app update; do not treat an uncertain state as completion.
- **Port already in use:** stop the existing monitor or select another `MONITOR_PORT`. Do not delete runtime files belonging to a still-running instance.
- **No notification banner:** use Test notification and check Windows notification policy yourself.

## Development and tests

No dependency installation is required:

```powershell
npm test
```

For targeted lifecycle, discovery, family, and dismissal checks:

```powershell
node --disable-warning=ExperimentalWarning --test .\test\background.test.mjs .\test\source.test.mjs .\test\families.test.mjs .\test\dismiss.test.mjs
```

Tests use synthetic metadata and temporary directories, not your real Copilot sessions. The Windows HTTP integration test starts a separate copied server/tray helper with an empty synthetic Copilot home and tests authorization, dismissal, and restart. It never sends dismissal requests to your production monitor.

For browser layout checks:

```powershell
node .\scripts\verify-ui.mjs
```

Open the printed fixture URL in Edge. This separate loopback harness serves synthetic families using the real UI assets, checking both themes, compactness, long names, disclosure/focus, finished-only dismissal, and wide/narrow layouts. It never reads Copilot data or connects to the production monitor. Results are shown on the page and at its `/results` endpoint. Stop the fixture server with Ctrl+C.

## License

[MIT](LICENSE). Copyright (c) 2026 Rich Chiodo.
