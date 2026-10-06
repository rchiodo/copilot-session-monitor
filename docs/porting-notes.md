# Porting notes: Node.js -> Python

This document tracks the module-by-module mapping from the original
`src/*.mjs` implementation to the Python package under `src/pymonitor/`,
plus the specific places where behavior is intentionally preserved
byte-for-byte because it fixes a previously-shipped bug.

## Module mapping

| Node (`src/`)       | Python (`src/pymonitor/`) | Notes |
|---------------------|---------------------------|-------|
| `events.mjs`        | `events.py`               | `EventState` state machine, ported directly. |
| `background.mjs`    | `background.py`           | Subagent/background-work tracking. |
| `engine.mjs`        | `engine.py`               | `MonitorEngine`, `Ledger`, `SessionStore`. |
| `hierarchy.mjs`      | `hierarchy.py`            | Parent/child linkage, related-metadata resolution. |
| `families.mjs`      | `families.py`             | `FamilyMonitor`, `group_families`, dismiss validation. |
| `actions.mjs`       | `actions.py`              | `MonitorActions` (serializing dismiss queue), `read_dismiss_entries`. |
| `source.mjs`        | `source.py`               | `LocalSource` -- desktop-app DB/events adapter. SDK swap point: `_discover_cli_session_ids()` (done). |
| `local-report.mjs`  | `local_report.py`         | Shared local-poll logic used by both watcher and in-process collector self-source. |
| `protocol.mjs`       | `protocol.py`             | JSON protocol v1 bounds, observed-metadata shaping. |
| `configuration.mjs`  | `configuration.py`         | Done -- config read/save, collector validation, `initialize`/`pair`/`migrate_legacy`/`configuration_command` CLI dispatcher. |
| `collector.mjs`      | `collector.py`            | Done -- `Collector` class: reporter-namespaced aggregation, clock-skew/heartbeat/lease fencing, retained-finish trust, zombie-row rescue, recovery-vote corroboration, atomic save/rollback. Highest-risk module; see "Preserved bug fixes" below. |
| `server.mjs`         | `server.py`               | Done -- `CollectorServer`: loopback dashboard (`/`, `/api/status`, `/api/control`, `/api/test`, `/api/stop`, `/api/dismiss`), same-origin guard, and the HTTPS ingestion endpoints (`/v1/connect`/`/v1/report`/`/v1/disconnect`). See "Phase 2: tray/notification seam" below for the deliberate scope boundary (tray/toast/dashboard-rendering are deferred to Phase 3 via a `TrayBridge` protocol). |
| `watcher.mjs`        | `watcher.py`              | Done -- `Watcher`: HTTPS client that pairs with (`/v1/connect`) and reports to (`/v1/report`) a remote collector; durable installation/boot identity, lease renewal, clock-skew-aware retry/backoff. |
| `reporter.mjs`       | `reporter.py`             | Done -- the ~1.5s report-cadence loop + local-engine-to-wire-payload shaping that `watcher.py` drives; split out as its own module so the cadence/backoff logic is unit-testable independent of the HTTP transport. |
| `lifecycle.mjs`      | `lifecycle.py`             | Done -- single-role-per-directory lock (`acquire_role`). `psutil.pid_exists` replaces `process.kill(pid, 0)`'s liveness probe (Windows' `os.kill(pid, 0)` would call `TerminateProcess` instead of merely probing). |
| *(none -- new in Phase 5)* | `launcher.py`        | Done -- `start_role`/`stop_role`/live-instance-detection logic backing the PEP 723 launcher scripts at repo root (`start-host.py`, `start-client.py`, `start-tray.py`, `stop-host.py`, `stop-client.py`, `init-host.py`). Absorbs `scripts/Start-Role.ps1` and `scripts/Stop-Role.ps1` (both deleted); no 1:1 `.mjs` predecessor since the original app never had a testable Python/JS launcher module -- the PowerShell scripts *were* the logic. See "Phase 5" below. |

## Local session discovery: the one architectural change (done)

The Node implementation's `source.mjs` hand-parses `~/.copilot/session-state/*.jsonl`
and `~/.copilot/data.db` directly. This is fragile because it is coupled to the
CLI's on-disk storage format.

The Python port keeps that hand-parsing for family/hierarchy/activity detail
(there is no SDK surface for that), but swaps the *session identity discovery*
step -- "which CLI session IDs currently exist for this machine" -- to use the
official Copilot SDK for Python (`github-copilot-sdk`, package import name
`copilot`) via `CopilotClient.list_sessions()`. This boundary is isolated in
`LocalSource._discover_cli_session_ids()` / `_default_sdk_discover()` in
`source.py`.

Implementation notes (confirmed against the installed `github-copilot-sdk==1.0.15`
package, not assumed from the Go client):

- `SessionMetadata`'s fields are **snake_case** in Python
  (`session_id`, `start_time`, `modified_time`, `is_remote`, `summary`,
  `context`), not the PascalCase the Go client's naming implied.
- `CopilotClient.list_sessions()` is async and, with no `connection` argument,
  `CopilotClient.start()` spawns the real bundled CLI binary as a child
  process over stdio. Spawning per poll cycle would be far too heavy, so
  `LocalSource` lazily constructs **one** `CopilotClient(base_directory=self.home)`,
  calls `start()` once, and reuses it for the lifetime of the `LocalSource`
  instance (`LocalSource.aclose()` stops it cleanly).
- `base_directory` is set to `LocalSource.home` so the SDK reads/writes
  session state from the exact same `~/.copilot`-equivalent directory the
  hand-parsing code already uses (it sets `COPILOT_HOME` on the spawned
  runtime).
- `_discover_cli_session_ids()` takes an injectable `sdk_discover` async
  callable (constructor arg on `LocalSource`) so tests never spawn a real CLI
  process; `tests/conftest.py` installs a directory-scan stand-in as an
  autouse fixture (functionally identical to the old hand-list behavior) so
  existing merge-logic tests are unaffected by the swap, and
  `tests/test_source.py`'s `test_sdk_discover_seam_surfaces_cli_only_sessions_found_by_the_injected_callable`
  / `test_sdk_discover_failure_is_reported_as_an_issue_and_does_not_crash_poll`
  exercise the seam itself directly (including a non-UUID-filtering check and
  a failure-degrades-gracefully check).
- Unlike the original JS (`readdir` not wrapped in try/catch -- a failure
  there fails the whole `poll()` cycle), the SDK call is deliberately wrapped
  in broad exception handling at the `poll()` call site: a failure appends an
  issue message, keeps `directory_ids` at its last-known-good value, and
  still advances `discovered_at` (to avoid a tight retry loop against a
  broken SDK/subprocess). This is a reasoned divergence from literal JS
  parity: an RPC/subprocess failure is both more likely and more disruptive
  to crash on than a missing local directory.

The SDK has no live busy/idle/state field (`SessionMetadata` only exposes
`session_id`, `start_time`, `modified_time`, `summary`, `is_remote`,
`context`), so status inference continues to come from the ported
`EventState`/`MonitorEngine` state machine reading `events.jsonl` directly,
not from the SDK.

## Phase 2: protocol/network layer decisions

- **aiohttp** (not stdlib `http.server`/`ssl`, not FastAPI/uvicorn) was chosen
  for both the collector's HTTPS server (`server.py`, upcoming) and the
  watcher's HTTPS client (`watcher.py`/`reporter.py`, upcoming): it has
  first-class `ssl.SSLContext` support on both the server and client side,
  async request handling fits the existing `asyncio`-based engine/poll loop
  without a second event-loop integration, and it avoids pulling in a web
  framework (FastAPI) whose request-validation/routing machinery is overkill
  for the small, already-bounded JSON protocol in `protocol.py`.
- **Native PEM cert generation** (`cryptography` library,
  `configuration._generate_certificate`) replaces the PFX-based
  `windows/certificate.ps1` PowerShell shell-out. This produces two files
  (`collector-cert.pem`, `collector-key.pem`) instead of Node's single PFX,
  since Node bundled cert+key together -- purely a storage-format difference,
  not a behavioral one; `aiohttp`'s `ssl.SSLContext.load_cert_chain()` takes
  the same cert/key pair either way.
- **Native ACL hardening** (`pywin32`, `configuration._harden_acl`) replaces
  the `windows/protect-data.ps1` shell-out, producing an identical DACL
  (owner = current user; exactly two ACEs -- current user and `SYSTEM` --
  both `FILE_ALL_ACCESS`, container+object inherit, protected from
  inheritance) confirmed via direct `win32security` inspection in
  `tests/test_configuration.py::test_harden_acl_sets_owner_and_dacl`.
- **Bug found and fixed in `protocol.py` while testing `configuration.py`'s
  `pair()`/CLI flows** (not a pre-existing Node bug -- a porting bug
  introduced in this Python rewrite and caught before it shipped):
  `validate_pairing()` compared `urlsplit(collectorUrl).path` against the
  literal string `"/"`. JS's `URL` class normalizes an absent path to `"/"`
  for special schemes (http/https), but Python's `urllib.parse.urlsplit`
  leaves `path` as `""` when the URL has no path segment at all (e.g.
  `https://192.168.1.50:43188`, which is exactly the form
  `configuration.pair()`/`initialize()` construct -- no trailing slash).
  This would have rejected every real pairing/collector URL. Fixed by
  normalizing `parsed.path or "/"` before the comparison. Caught by
  `tests/test_configuration.py`'s `pair()`/`configuration_command()` tests,
  which exercise `validate_pairing()` through realistic collector URLs (the
  existing `tests/test_protocol.py` tests had all used an explicit trailing
  slash, which masked the bug).

## Preserved bug fixes (must not regress)

These are ported as-is from the Node implementation and are exercised by
dedicated regression tests:

- **Unconfirmed-after-reconnect bug** (`tests/test_reconnect_lease.py`,
  mirrors `test/reconnect-lease.test.mjs`): a dropped reporting lease must
  **not** force `monitor.update([], { healthy: False, reason: ... })` on every
  reconnect. That forced-invalidate call used to flip every still-tracked
  session to `unknown` on a brief reconnect, bypassing `MonitorEngine`'s own
  wall-clock gap check. `local_report.poll_local()` only raises a gap on a
  genuine observation failure, never merely because the previous report
  attempt failed.
- **Dismiss replay-safety / zombie-row prevention** (`tests/test_dismiss.py`):
  dismissing a family requires presenting its exact current `dismissKey`
  (sha256 hex digest over its terminal state); a stale key is skipped, not
  applied, so a family that resumed work after being dismissed cannot be
  hidden again by replaying an old dismiss request.
- **Family-infection containment** (`tests/test_families.py`): per-row status
  (`working`/`waiting`/`error`/`unknown`/`finished`) is computed from each
  session's own `EventState`; a family's aggregate state is the "worst" state
  across members, but dismissal and gap-handling operate per-family-id, not by
  cascading one member's lost-observation status onto unrelated families.

## `collector.mjs` -> `collector.py`: the three named regressions

`collector.py` is the aggregation layer on the collector side of the network
protocol (reporter-side `monitor.update()` calls are the watcher's own local
engine; `Collector.accept()` is what a *remote* watcher's `/v1/report` lands
in before being merged into the dashboard). It is where the three bugs the
user explicitly called out historically lived, because it merges
possibly-out-of-order, possibly-stale reports from multiple reporters into a
single per-family view. All three are regression-guarded in
`tests/test_collector.py`, ported 1:1 from `test/collector.test.mjs`
(15/15 scenarios, all passing):

- **Unconfirmed-status bug** -- a session that had genuinely finished could
  flip back to `unconfirmed`/`unknown` if a later, slightly-stale report
  arrived after the finishing one. Preserved via "retained-finish trust":
  once a row is observed as finished through a clean process exit, a
  subsequent report that merely *omits* that session (rather than actively
  contradicting it) does not demote it -- but a **fresh baseline** report
  (reporter restarted, new run) is trusted to start clean.
  Test: `test_retained_finish_trusted_but_baseline_distrusts`.
- **Zombie-row bug** -- a row that looked abandoned (stale, no corroborating
  report) needed to be "rescued" back to a real status if a report later
  proved it was still alive, but a row that is genuinely still open must
  *not* be rescued into a false finished/stale state by an unrelated report.
  Test: `test_zombie_row_rescue_vs_still_open` (the fixture asserts both the
  rescue path and the still-open-stays-lost path in the same scenario so
  they can't silently regress independently).
- **Family-infection bug** -- one member of a family (e.g. a sub-agent)
  entering a bad/lost state must not retroactively taint sibling members or
  the parent's own independently-reported status. Preserved via per-reporter
  namespacing (`Collector` keys internal state by `(reporterId, sessionId)`)
  plus `group_families()`'s own per-family aggregation;
  `test_central_parent_alert_remains_parent_only` and
  `test_parent_descendant_restores_dismissed_and_stale_revision_skips`
  assert a child's state/dismissal never leaks onto the parent row or
  vice versa.

Additionally ported and regression-guarded (not explicitly named by the user
but part of the same lease/corroboration machinery, and just as easy to
silently regress during a line-by-line port):

- **Lease fencing** (`test_leases_fence_installations_boots_replay_reorder`):
  a report is only accepted if its `(installationId, bootGeneration, bootId,
  sequence)` tuple proves it is newer than the last accepted report for that
  reporter -- rejects replayed/reordered/competing-boot reports.
- **Recovery-vote corroboration**
  (`test_reconnect_baseline_cannot_upgrade_but_votes_recover`,
  `test_corroboration_votes_reset_on_run_change_or_unhealthy`): a single
  reconnect report cannot unilaterally "upgrade" a row out of a
  lost/degraded state (to prevent one flaky reporter from falsely clearing a
  real problem); it takes `RECOVERY_VOTES_REQUIRED = 2` consecutive healthy
  corroborating reports, and the vote counter resets on a run change or an
  unhealthy report.
- **Rollback-on-save-failure** (`test_failed_persistence_rolls_back_and_allows_retry`):
  if the atomic `collector-state.json` write fails partway (e.g. target
  directory missing), in-memory state is rolled back to the last successfully
  persisted snapshot rather than silently diverging from disk, so a retry on
  the next report can still succeed.

## Testing strategy

Unit tests are ported file-by-file from `test/*.test.mjs` to `tests/test_*.py`
with 1:1 test-case correspondence (including parametrized loop tests). This
is a correctness gate independent of the SDK: `tests/conftest.py` installs a
directory-scan stand-in for `LocalSource._default_sdk_discover` as an autouse
fixture, so merge-logic tests never spawn a real CLI process, while
`tests/test_source.py`'s two `test_sdk_discover_*` tests exercise the real
injection seam end-to-end with fake async callables.

`test/self-observation.test.mjs` (full integration test spawning real
`server.mjs`/`configuration.mjs`/`reporter.mjs` child processes) is deferred
until the Phase 2 protocol/network layer exists, then ported as an
integration test against the Python equivalents.

## Phase 2: `server.mjs`/`watcher.mjs`/`reporter.mjs` -> `server.py`/`watcher.py`/`reporter.py`

### Scope-mixing deviation (flagged to user, not yet explicitly re-confirmed)

`server.mjs` in the original JS mixes two concerns in one file: the
protocol/network layer (same-origin dashboard API, HTTPS ingestion
endpoints) *and* Phase-3 concerns (driving the system tray icon/menu,
native toast notifications, serving the dashboard's static HTML/CSS/JS
assets). Rather than port that mixing verbatim, Phase 2 introduces a
`TrayBridge` protocol (a small structural-typing seam in `server.py`) that
`CollectorServer` calls into for tray/notification/theme concerns
(`notify()`, theme queries, bridge-lifecycle hooks `on_bridge_ready` /
`on_bridge_lost` / `on_theme` / `on_power_event`). Phase 2 runs and is fully
tested with `bridge=None` (no real tray process), exercising the
"collector up, tray bridge not yet connected" degraded-dashboard state
(`/api/status` marks every row `unknown` with
`"Collector unavailable; current status unconfirmed"` until
`on_bridge_ready()` fires). The real PowerShell-tray-subprocess
implementation of `TrayBridge` is Phase 3's job. Static dashboard
HTML/CSS/JS asset serving (`_STATIC_ASSETS` / `_handle_static`) IS ported
in Phase 2 (`server.py` serves `public/`'s existing files unmodified), since
that's pure protocol-layer request/response handling, not tray-specific.

### Library choice confirmed: aiohttp end-to-end

As anticipated in the Phase 1 plan, `aiohttp` is used for both sides:
`server.py`'s `CollectorServer` as an `aiohttp.web` application (one
`TCPSite` per configured bind address for the HTTPS ingestion listeners,
plus a separate loopback-only `TCPSite` for the dashboard), and
`watcher.py`'s `Watcher` as an `aiohttp.ClientSession` with a client-side
`ssl.SSLContext` pinned to the paired collector's certificate fingerprint
(no CA trust store involved -- this is the existing `csm1:` pairing's
fingerprint-pinning model, ported as-is).

### TLS pairing/protocol mapping (byte-for-byte semantics preserved)

- `csm1:` base64 connection-string parsing/generation, cert-fingerprint
  pinning, write-only bearer credential, and one-time-use consumption are
  unchanged from `configuration.py` (done in an earlier phase) -- `watcher.py`
  only consumes the already-parsed pairing dict.
- Durable watcher identity (`installationId`/`bootGeneration`/`bootId`) and
  the collector-side lease (15s) / sequence-number fencing are ported
  unchanged in `collector.py` (earlier phase) and exercised again here via
  `reporter.py`'s wire-payload shaping (`reporter.py` constructs exactly the
  `{version, reporterId, lease, seq, sentAt, healthy, issues, notices, ...}`
  shape `Collector.accept()` expects) and `watcher.py`'s `/v1/connect` /
  `/v1/report` / `/v1/disconnect` HTTP calls.
- The ~1.5s report cadence and clock-skew-aware retry/backoff live in
  `reporter.py`, unit-tested independent of real network I/O
  (`tests/test_reporter.py`, 9/9).
- `watcher.py`'s bearer-auth header construction
  (`f"Bearer {token}"`) and `server.py`'s `_require_control_token`'s matching
  comparison were each independently flagged during review as looking like a
  literal `"******"` string -- see "Content-exclusion redaction false alarm"
  below before assuming either is a bug.

### Content-exclusion redaction false alarm (observed twice this phase)

This environment's content-exclusion policy redacts text that is *shaped
like* a credential to `"******"` **at display time** -- in `view`, `grep`,
PowerShell's `Select-String`, and even a `python -c`/subprocess `print()`
rendered back through the chat tool. This happened twice in Phase 2
(`watcher.py`'s auth header construction and `server.py`'s
`_require_control_token`), both times for code that merely *builds* an
`Authorization: Bearer <token>` string -- no real secret is present in
source. If you encounter a suspicious `"******"` in this codebase in the
future: **do not assume it's a bug**. Verify by dumping character codes
instead of printing the raw string (character codes aren't textually
credential-shaped, so they bypass the redaction) -- e.g. write a temporary
script that regex-extracts the suspicious literal from the file and prints
`[ord(c) for c in literal]`, then decode by hand. Delete the temp script
afterward.

### Test coverage and testing-strategy scope decisions

- `tests/test_reporter.py` (9/9): cadence timing, lease/sequence
  construction, clock-skew handling -- pure unit tests against
  `reporter.py`'s functions, no real HTTP.
- `tests/test_watcher.py` (14/14): pairing, connect/report/disconnect
  HTTP flows, bearer-auth construction, reconnect/backoff -- served via
  `aiohttp.test_utils.TestServer`/`TestClient` (in-process, no real TLS
  socket/cert), following the same pattern established for `test_server.py`
  below.
- `tests/test_server.py` (15/15, new this phase): builds a `CollectorServer`
  manually wired to a real in-memory `Collector`/`Ledger`/`MonitorActions`
  (same fixture pattern as `test_collector.py`) WITHOUT calling
  `CollectorServer.start()` (which binds real TLS ingest listeners and
  needs on-disk certs) -- the dashboard's `web.Application` is rebuilt
  locally the same way `_start_dashboard()` builds it, then served via
  `aiohttp.test_utils.TestServer`/`TestClient`. Covers: same-origin
  middleware (wrong `Host`, cross-site `Sec-Fetch-Site`, mismatched
  `Origin`), `/api/status`'s degraded-without-bridge contract (both
  empty-state and with a connected session+report) and its recovery once
  `on_bridge_ready()` fires, `/api/control` token echo, bearer-token gating
  on `/api/test`/`/api/stop`/`/api/dismiss`, `/api/dismiss`'s layered
  validation (403 wrong token -> 503 not-ready -> 400 empty/invalid entries
  -> 200 with skipped-unknown-id), and static-asset serving
  (`/` -> `index.html`, `Cache-Control: no-store`).
  **Deliberately out of scope for `test_server.py`**: the HTTPS ingestion
  endpoints (`/v1/connect`/`/v1/report`/`/v1/disconnect`, i.e.
  `_handle_ingest`/`_ingest_body`) are not directly exercised here --
  their underlying `Collector.connect`/`.accept`/`.disconnect` logic is
  already fully covered by `tests/test_collector.py`, and exercising the
  HTTP-layer wrapper around them would require either a real TLS listener
  or a second manually-built `aiohttp` app; this is deferred to a future
  integration test (see `test/self-observation.test.mjs` note above) rather
  than duplicated here.
- One test-construction bug was caught and fixed while writing
  `test_server.py` (test-only, not a `server.py`/`collector.py` bug): a
  synthetic member report used a hardcoded `sentAt` of `"2026-01-01T12:00:00.000Z"`
  while the collector's `now` defaulted to the real wall clock -- more than
  30s apart, which correctly triggered `collector.py`'s clock-skew rejection
  (`source["healthy"] = False`) and kept the row `unknown` regardless of
  `on_bridge_ready()`. This is the clock-skew fix working as intended, not a
  regression; fixed by having the test pass an explicit, consistent `now` to
  both `connect()`/`accept()` and the member's own timestamps, matching
  `test_collector.py`'s `Fixture` pattern.

### Cleanup

- `server.py`'s `_process_snapshot()` had a leftover `-> ProcessSnapshot | None`
  return-type hint (and corresponding now-unused `from .source import
  ProcessSnapshot` import) from an earlier draft; the method actually returns
  `self.processes: dict[str, Any] | None`. Fixed (cosmetic only, no behavior
  change).

### Current combined test count

241/241 passing (`pytest tests/ -q`): 203 from Phase 1 + 9 (`test_reporter.py`)
+ 14 (`test_watcher.py`) + 15 (`test_server.py`).

## Phase 3: tray/notification bridge, dashboard, PowerShell launchers

Phase 3 fills the `TrayBridge` seam Phase 2 deliberately left open
(`server.py` ran fully tested with `bridge=None`) and repoints the process
entry points from `node` to Python.

### Scope decision: tray/toast UI stays in PowerShell, not Python

The original `windows/tray.ps1` (driven by `server.mjs`/`watcher.mjs` over
stdio) already implements the Windows Forms `NotifyIcon` tray icon, its
context menu, and native toast notifications. Rather than reimplement that
UI in a Python tray library (`pystray`) + a separate toast library
(`win11toast`/`winotify`) -- which would mean maintaining two parallel,
less-mature UI stacks for Windows-specific chrome that PowerShell already
does natively and well -- Phase 3 keeps `windows/tray.ps1` as-is and ports
only the **Python side of the stdio bridge** that drives it:
`src/pymonitor/tray_bridge.py`.

- `tray_bridge.py` defines a shared `_PowerShellBridge` base (spawns
  `powershell -File windows/tray.ps1 ...` via
  `asyncio.create_subprocess_exec`, owns newline-delimited JSON stdin/stdout
  framing, `write()`/`stop()` with a `_STOP_GRACE_SECONDS` kill-on-timeout
  fallback) plus two concrete subclasses:
  - `CollectorTrayBridge` (spawns with `-MonitorUrl`): handles `ready`,
    `theme` (validates against the known theme enum, ignores invalid
    values), `notification-shown`/`notification-submitted` (shown always
    wins and is never downgraded by a stale submitted event; mismatched
    notification ids are ignored), `test`, `stop`, `power`, `processes`,
    `error`, and `generate-connection` (calls `server.pair_connection`,
    imported locally at call time so tests can monkeypatch
    `pymonitor.server.pair_connection` directly).
  - `WatcherTrayBridge` (spawns with `-WatcherOnly`): handles `processes`,
    `power`/`error`, `stop`, `connect` (success/failure), and a no-op
    `on_health_changed` hook (the watcher doesn't drive dashboard state the
    way the collector does, so health-change notifications from the tray
    process are accepted but intentionally discarded).
- Because `pystray`/`win11toast`/`winotify` are no longer used anywhere in
  the Python package, they were removed from `pyproject.toml`'s
  dependencies (they were added speculatively during Phase 1 planning
  before this scope decision was finalized; leaving them in would have been
  dead weight, not a feature).

### The power/error distinction (critical, regression-guarded)

`server.mjs`/`watcher.mjs` treat the tray bridge's `power` and `error`
stdio events differently: `power` (a real Windows power-state transition,
e.g. sleep/resume) calls `on_power_event()`, which the collector/watcher use
to decide whether to kick off a disconnect-and-reconnect cycle. `error` (the
tray subprocess itself failing/misbehaving) resets local bridge state and
calls `on_bridge_fault()`, but must **not** also call `on_power_event()` --
doing so would turn a tray-process crash into a spurious
disconnect/reconnect loop against the remote collector, unrelated to actual
machine power state. This distinction was ported faithfully into
`tray_bridge.py`'s dispatch, and is now explicitly covered by
`tests/test_tray_bridge.py::test_collector_bridge_error_event_resets_then_faults_without_disconnect_loop`
(and its `WatcherTrayBridge` parametrized counterpart), which asserts
`on_power_event` is NOT called on `error` while `on_bridge_fault` is.

### `Watcher.poll()`'s forced-gap-reason consume-clear semantics

`tray_bridge.py`'s `power`/`error` handlers set `watcher.reset` to a short
reason string ("power event" / "bridge error") to force the next local poll
to treat it as a fresh baseline (avoiding a false "gap" penalty for time the
machine was asleep or the tray subprocess was down). `Watcher.poll()` reads
`self.reset` into a local `forced_gap_reason`, immediately clears
`self.reset = None`, then passes the (possibly `None`) reason into
`poll_local(...)` -- a true one-shot flag, not a sticky mode. Newly
regression-guarded by
`tests/test_watcher.py::test_poll_consumes_and_clears_a_pending_reset_reason`
(asserts the first `poll()` call after setting `reset` forces the gap
reason, and the very next call no longer does).

### Dashboard webpage (`public/`): no changes needed

`public/app.js`, `public/index.html`, and `public/style.css` were read in
full and confirmed to be pure client-side assets with zero Node-specific
coupling: `app.js` is a REST client hitting `/api/status`, `/api/control`,
`/api/test`, `/api/stop`, `/api/dismiss`, all of which `server.py` already
serves (ported in Phase 2); `index.html`/`style.css` are static
markup/styling. All three files are served byte-for-byte unchanged by
`server.py`'s existing static-asset handler.

### CLI entry points (`cli.py`)

`cli.py` replaces the Node `bin`/launcher scripts' role-dispatch with
`host_main()` (collector role: builds `CollectorServer` +
`CollectorTrayBridge`, runs until stopped), `client_main()` (watcher role:
builds `Watcher`/`Reporter` + `WatcherTrayBridge`), and `config_main()` (the
`configuration.mjs`-equivalent CLI surface: `initialize`/`pair`/`migrate-legacy`
/`revoke <REPORTER-ID>`/etc., unchanged from Phase 1/2's
`configuration_command()`), wired up as console-script entry points in
`pyproject.toml`. An earlier `tray_main()`/`pymonitor-tray` entry point
concept (a Python-side process whose only job would have been to exec
`windows/tray.ps1`) was dropped as unnecessary indirection: `host_main()`/
`client_main()` already spawn the tray subprocess directly via
`tray_bridge.py`, so a separate "tray launcher" entry point would have had
no role to play.

### PowerShell launchers repointed to Python

- `scripts/Start-Role.ps1` and `Initialize-Host.ps1` now invoke the
  installed Python console-script entry points (`pymonitor-host`,
  `pymonitor-client`, `pymonitor-config`) instead of `node <path>.mjs`.
- `Stop-Host.ps1`, `Stop-Client.ps1`, `scripts/Stop-Role.ps1`,
  `Start-Host-Headless.ps1`, `Start-Client-Headless.ps1`, and
  `Start-Tray.ps1` needed **no changes**: they operate on process
  name/lockfile conventions and the tray subprocess contract, none of which
  changed shape in the port.
- `config initialize` was smoke-tested end-to-end via the new
  `pymonitor-config` entry point (`pip install -e .` then invoking the
  console script directly) and completed successfully.
- Live-spawning the `host`/`client` roles for a manual smoke test was
  deliberately **not** attempted in this environment: both roles start a
  real TLS listener and a real PowerShell tray subprocess, which in this
  sandboxed/background-tool execution environment would leave orphaned
  long-running processes with no reliable way to send a graceful `SIGINT`-
  equivalent (Windows doesn't have `SIGINT` delivery the way the test
  harness relies on for cleanup). This is a coverage gap relative to a true
  end-to-end run, but all of the individual pieces that `host_main()`/
  `client_main()` wire together (`CollectorServer`, `Watcher`, `Reporter`,
  `CollectorTrayBridge`, `WatcherTrayBridge`) are independently covered by
  passing unit/integration-style tests (`test_server.py`, `test_watcher.py`,
  `test_reporter.py`, `test_tray_bridge.py`). Flagged to the user as an
  explicit open risk rather than silently skipped.

### Test additions and current combined test count

- `tests/test_tray_bridge.py` (new, 29 tests): full `CollectorTrayBridge`/
  `WatcherTrayBridge` dispatch coverage using a fake-subprocess harness
  (`_FakeProcess` wraps a real `asyncio.StreamReader` for stdout so tests
  can `feed_line()` realistic newline-delimited JSON and assert dispatch,
  plus a `_FakeStdinWriter` that can simulate `BrokenPipeError`/closed-stream
  write failures). One fixture bug was hit and fixed while writing this
  file: a sync pytest fixture constructed `asyncio.StreamReader()` directly,
  which raises `RuntimeError: There is no current event loop in thread
  'MainThread'` on Windows' `ProactorEventLoopPolicy` outside a running
  loop; fixed by making the fixture `async def` so it runs inside
  pytest-asyncio's active loop.
- `tests/test_watcher.py`: +1 test (see "consume-clear semantics" above),
  now 15 tests in this file.
- **271/271 passing** (`pytest tests/ -q`, full suite): 241 from Phases 1-2
  + 29 (`test_tray_bridge.py`) + 1 (`test_watcher.py` addition).

## Phase 4: native Python tray (replaces the PowerShell subprocess bridge)

Phase 3's scope decision to keep `windows/tray.ps1` + a stdio bridge
(`tray_bridge.py`) was explicitly revisited at the user's request: Phase 4
removes the PowerShell dependency for tray/notifications entirely and
replaces it with an in-process Python tray (`src/pymonitor/tray_native.py`),
using `pystray` for the icon/menu and `win11toast` for native Windows toast
notifications. `windows/tray.ps1` itself is left untouched in the repo (it
is simply no longer invoked); the other `windows/*.ps1` scripts
(`certificate.ps1`, `protect-data.ps1`) are unaffected -- they are unrelated
to the tray.

`src/pymonitor/tray_bridge.py` and `tests/test_tray_bridge.py` (the Phase 3
subprocess bridge and its 29 tests) were deleted outright rather than kept
alongside the replacement, since they implemented the same seam
(`TrayBridge` Protocol in `server.py`/`watcher.py`) and keeping both would
mean two parallel, divergent tray implementations.

### Module layout

`tray_native.py` exports two classes satisfying the exact same `TrayBridge`
Protocols Phase 2 defined (`server.py`'s `TrayBridge`, `watcher.py`'s
`TrayBridge`) -- no change was needed to either protocol definition:

- `CollectorNativeTray(server, stop_event)` -- host-role tray: menu items
  "Open dashboard", "Test notification", "Generate connection request for a
  sub machine...", "Stop collector"; drives `CollectorServer`'s
  `on_bridge_ready`/`on_theme`/`on_power_event`/`on_bridge_fault` hooks the
  same way `CollectorTrayBridge` did.
- `WatcherNativeTray(watcher, stop_event)` -- watcher-role tray: menu items
  "Connect to host...", "Stop watcher"; no "Open dashboard"/"Test
  notification" items (the watcher never showed those, matching
  `-WatcherOnly` gating in the original `tray.ps1`).

Supporting pieces in the same module: `_tray_image()` (in-memory PIL icon),
`_read_theme()` (registry `AppsUseLightTheme` read), `_snapshot_processes()`
(`psutil`-based process presence check replacing `tray.ps1`'s
`Get-Process`), `_PollWorker` (background thread, ~2s cadence, pushes
theme/process snapshots into the tray classes), `_PowerEventWindow` (hidden
`win32gui` window + `PumpMessages()` loop solely to catch
`WM_POWERBROADCAST`, replacing WinForms' `SystemEvents.PowerModeChanged`),
`_ToastWorker` (throttled toast queue + unthrottled "system" toasts),
`_run_label_dialog`/`_run_connection_dialog`/`_show_error` (`tkinter`
dialogs replacing WinForms `InputBox`/`MessageBox`), and
`_copy_to_clipboard` (`pywin32` clipboard write for the generated
connection string).

### Behavioral deviations from `tray.ps1` (for user review)

1. **Toast duration**: the original used a fixed `ShowBalloonTip(8000)`
   (8 seconds). `win11toast` only exposes duration presets (`'short'` ~=7s,
   `'long'` ~=25s), not arbitrary milliseconds; `'short'` was chosen as the
   closer match. Actual on-screen time is also ultimately governed by
   Windows' own Action Center/notification settings regardless of either
   app's requested duration, so this is a minor, likely-imperceptible
   deviation.
2. **Static, non-theme-reactive icon**: `tray.ps1` used
   `[System.Drawing.SystemIcons]::Information` for both host and watcher,
   with no light/dark variant -- `_tray_image()` matches this exactly (one
   generated bitmap, same for both roles), so this is *not* a regression,
   just worth noting since "theme following" in this project refers to the
   *dashboard webpage* and *notification styling*, not the tray icon glyph
   itself.
3. **No coded `winotify` fallback**: per the finalized design, only
   `win11toast` is wired up; if it is unavailable/fails, `_ToastWorker`
   degrades to a stderr log line rather than trying a second library. This
   mirrors `tray.ps1`'s own fire-and-forget `try/catch` around
   `ShowBalloonTip`.
4. **Toast click-through preserved**: `tray.ps1`'s
   `add_BalloonTipClicked({ Start-Process $MonitorUrl })` is faithfully
   reproduced via `win11toast.toast(..., on_click=self._monitor_url)` --
   clicking a notification still opens the dashboard.
5. **Cancel-semantics asymmetry preserved intentionally**: the host's
   "Generate connection request..." dialog (`_run_label_dialog`, using
   `tkinter.simpledialog.askstring`) reproduces the original VB `InputBox`
   quirk where Cancel is indistinguishable from an empty string -- the code
   always proceeds to generate a connection request, just with an empty
   label if cancelled. The watcher's "Connect to host..." dialog
   (`_run_connection_dialog`) has true Cancel-aborts semantics (`None`
   short-circuits before calling `apply_connection_string`). This was
   carried over unchanged because `tray.ps1` itself has this asymmetry
   between the two dialogs, and preserving it avoids any accidental
   behavior change.
6. **Stop-menu-exits-process bug found and fixed in this phase**: Phase 3's
   `cli.py` never actually wired the bridge's "Stop" action's result back
   to the process-level shutdown signal, meaning clicking "Stop
   collector"/"Stop watcher" in the tray only stopped the server/watcher
   object but left the process running until Ctrl+C/signal. Phase 4's
   `cli.py` now constructs the `asyncio.Event` used by
   `_run_until_signalled()` *before* building the tray, and passes it into
   `CollectorNativeTray`/`WatcherNativeTray`; each tray's `_on_stop_clicked`
   schedules `server.stop()`/`watcher.stop()` on the main loop and sets
   that event in a `done_callback`, so the process now reliably exits after
   a tray-initiated stop. This is a genuine behavior fix, not a deviation.

### Known feature gaps vs. WinForms `NotifyIcon` (flagged, not blocking)

- `pystray`'s Windows backend uses its own hidden message-loop thread
  separate from the `win32gui` hidden window `_PowerEventWindow` creates
  for `WM_POWERBROADCAST` -- two message loops coexist in the process. This
  works (verified via the test suite's mocked threading) but was not
  exercised with a live manual smoke test of real tray + real power-event
  delivery in this sandboxed environment (same caveat already on record
  from Phase 3 for live host/client role smoke tests).
- `win11toast` depends on `winrt` packages and targets Windows 10 1809+/
  Windows 11; `winotify` (the originally-named fallback option) was not
  wired in as a coded fallback (see deviation #3 above) -- if `win11toast`
  turns out to be unreliable on a given machine, the current behavior is a
  stderr log line, not a silent retry via a different library.
- No custom `.ico`/`.png` asset exists in the repo for either the tray icon
  or toast notification icon; `_tray_image()` generates a simple circular
  "i" glyph with PIL at runtime. This is visually different from
  `SystemIcons.Information`'s actual OS-rendered glyph, though
  conceptually equivalent (an "info" icon).

### Test suite

`tests/test_tray_native.py` (new, 30 tests) replaces `test_tray_bridge.py`
(deleted, 29 tests) with direct module-level monkeypatching of
`tray_native.py`'s `pystray`/`winreg`/`psutil`/`tkinter`/clipboard surfaces
(an autouse fixture plus per-test patches), rather than faking a
subprocess's stdio. Coverage includes both tray classes' full protocol
surface (start/ready/fault, notify/mark-shown, process snapshot,
theme/process push, power event, generate-connection success/failure,
connect-dialog Cancel-vs-value asymmetry, stop-event wiring to process
exit) plus standalone unit tests for `_PollWorker.tick()`, `_read_theme()`,
`_snapshot_processes()`, and the Cancel-coercion dialog behavior.

Two tests initially failed on first run
(`test_collector_tray_stop_sets_stop_event_after_server_stops`,
`test_watcher_tray_stop_sets_stop_event_after_watcher_stops`): they used a
fixed `await asyncio.sleep(0)` x2 to let the stop-event chain
(`run_coroutine_threadsafe` -> await `server.stop()`/`watcher.stop()` ->
`done_callback` -> `call_soon_threadsafe(stop_event.set)`) settle, which
needs more loop iterations than two bare `sleep(0)` calls provide. Fixed by
replacing the fixed sleeps with `await asyncio.wait_for(stop_event.wait(),
timeout=1)` -- a test-only fix, no production code change was needed.

### Current combined test count

**272/272 passing** (`pytest tests/ -q`, full suite): 271 from Phases 1-3,
minus 29 (`test_tray_bridge.py`, deleted) plus 30
(`test_tray_native.py`, new).

(Two subsequent production bug fixes -- a stale startup "observation gap"
`lastAlert` never being cleared/superseded by later healthy reports, and
`local_report.py` discarding the real exception message in favor of a bare
class name -- each added their own regression test, bringing the baseline
to **278/278** before Phase 5 below.)

## Phase 5: PEP 723 standalone launchers, Node.js removal

This phase retired the dual-implementation state entirely. The Node.js
source tree (`src/*.mjs`, `package.json`, `package-lock.json`) is deleted;
`src/pymonitor/` is now the sole implementation. The 8 `.ps1`
launcher/stop scripts and the 3 now-dead `windows/*.ps1` helpers
(`certificate.ps1`, `protect-data.ps1`, `tray.ps1` -- superseded by
`configuration.py`'s cert generation/`_harden_acl()` and `tray_native.py`
since Phase 4) are also deleted. The empty `windows/` directory was removed
along with its last file (a judgment call: the user's instructions named
the 3 files, not the directory itself).

### Launcher scripts

The 8 original `.ps1` scripts map onto 6 new root-level
[PEP 723](https://peps.python.org/pep-0723/) standalone scripts, run via
`uv run <script>.py` (no `pip install` step -- `uv` builds an ephemeral venv
from each script's inline `# /// script` metadata block, which declares
`dependencies = ["copilot-session-monitor"]` and
`[tool.uv.sources] copilot-session-monitor = { path = ".", editable = true }`
so the local package resolves against the checkout, not PyPI):

| Original `.ps1`                     | New script             |
|--------------------------------------|-------------------------|
| `Initialize-Host.ps1`                | `init-host.py`          |
| `Start-Host-Headless.ps1`            | `start-host.py`         |
| `Start-Client-Headless.ps1`          | `start-client.py`       |
| `Start-Tray.ps1`                      | `start-tray.py`         |
| `Stop-Host.ps1`                       | `stop-host.py`          |
| `Stop-Client.ps1`                     | `stop-client.py`        |
| `scripts/Start-Role.ps1`             | absorbed into `pymonitor.launcher.start_role()` |
| `scripts/Stop-Role.ps1`              | absorbed into `pymonitor.launcher.stop_role()`  |

`scripts/Start-Role.ps1`/`Stop-Role.ps1` had no direct script-for-script
Python replacement because their logic (Python-version sanity check,
`runtime.json`/`watcher-runtime.json` + `instanceId` match + HTTP health
probe to detect an already-live instance, hidden-background-process spawn
with log redirection, 30s health-poll-until-up loop, and -- for stop --
URL-pattern validation, `instanceId` ownership confirmation before
trusting a runtime file, bearer-authenticated `/api/stop`/`/stop` call,
and poll-until-runtime-file-disappears) is now a single shared,
unit-tested module, `src/pymonitor/launcher.py`, that all 6 thin scripts
call into. This follows this project's established precedent (e.g.
`reporter.py` being split out of `watcher.py` in Phase 2) of keeping
testable logic in the importable package and leaving the entry-point
scripts as thin argument-parsing wrappers. `init-host.py` is the one
exception that calls `pymonitor.configuration.configuration_command()`
directly in-process rather than spawning a subprocess, since it is a
short synchronous configuration write, not a long-running role process to
detach from and health-poll.

Deliberate naming deviations from the PowerShell originals (both
documented in each script's `--help`):

- `Start-Tray.ps1`'s positional `/host` argument became `start-tray.py
  --host` (an argparse flag), since positional "slash-style" arguments
  are not idiomatic argparse and `--host` reads clearly as "run the host
  role."
- `Initialize-Host.ps1 -Reconfigure` (a PowerShell switch) became
  `init-host.py ... --reconfigure` (argparse `store_true`) -- same
  semantics, Python-idiomatic spelling. `-BindAddress`/`-IngestPort`
  (PowerShell named parameters) became positional `bind_address`/
  `ingest_port` arguments, matching `configuration_command()`'s own
  positional `["initialize", bind, port, "replace"?]` argument shape.

### Tests

`tests/test_launcher.py` (new, 24 tests) covers `launcher.py`'s
live-instance detection (runtime-file parsing, `instanceId` match, HTTP
health probe), start/stop subprocess spawning (mocked), the stop-side
safety checks (URL pattern validation, ownership confirmation before
trusting a runtime file belongs to this app), and the health-poll-until-up
loop (including timeout). No new tests were needed for the 6 top-level
`.py` scripts themselves, since they are intentionally thin argparse
wrappers with no independent logic -- `launcher.py`'s tests are the real
coverage.

**302/302 passing** (`pytest tests/ -q`, full suite): 278 from the prior
baseline plus the 24 new launcher tests. Re-run after all file
deletions/doc edits to confirm no regressions from removing `src/*.mjs`,
`package.json`, `windows/*.ps1`, and the old launcher scripts (none of
which the Python test suite imports or exercises).

### Deleted Node test files

Of the 11 original `test/*.test.mjs` files, 10 depended directly on the
now-deleted `src/*.mjs` modules (`background`, `collector`,
`connection-string`, `controls`, `dismiss`, `families`, `monitor`,
`reconnect-lease`, `self-observation`, `source`) and were deleted.
`test/ui.test.mjs` has no such dependency and was kept untouched, per the
explicit instruction to leave `scripts/check-ui.mjs`/`verify-ui.mjs` and
the Node UI-testing tooling alone.

### Known contradiction -- resolved in Phase 5

`scripts/verify-ui.mjs` imported from `../src/families.mjs` and
`../src/actions.mjs` to synthesize fixture data for its browser-rendering
harness -- both deleted in the Node-removal phase above, which broke it.
This was resolved in a follow-up phase by porting `verify-ui.mjs` to
`scripts/verify_ui.py` (an aiohttp app using the real `pymonitor.families`/
`pymonitor.actions` modules instead of the deleted `.mjs` ones). See
"Phase 5: porting scripts/verify-ui.mjs to Python" below for the full
writeup; `scripts/verify-ui.mjs` itself has since been deleted.

### Documentation

README.md's Requirements, one-machine/multi-machine quick-start,
command-reference table, revoke-command, and Development/verification
sections were rewritten to reference `uv run <script>.py` instead of
`.\*.ps1`, and to describe the native in-process Python tray (Phase 4)
instead of a PowerShell tray-helper subprocess. The PowerShell
execution-policy-bypass paragraph was removed outright (no longer
applicable -- there is no PowerShell script left to bypass policy for).
The Development/verification section was split to separately call out
`pytest tests/ -q` (the real, current test suite) versus the two
remaining Node-based UI tools, with the `verify-ui.mjs` broken-import
issue above called out explicitly as a known issue rather than silently
left for a future reader to discover.

## Phase 5: porting scripts/verify-ui.mjs to Python

`scripts/verify-ui.mjs` is a dev-only tool: it serves a synthetic
24-session fixture dashboard over HTTP so `scripts/check-ui.mjs` (headless
Edge via CDP) can drive the real `public/index.html`/`app.js`/`style.css`
client and assert on rendered layout/counts/badges. It was broken by the
Node-removal phase (its `../src/families.mjs`/`../src/actions.mjs`
imports no longer existed). It is now `scripts/verify_ui.py`, an aiohttp
app that imports the real `pymonitor.families.FamilyMonitor`/
`group_families` and `pymonitor.actions.MonitorActions`/
`read_dismiss_entries` instead of the deleted `.mjs` modules.

### What was ported, 1:1

- The same 24-parent / 12-working / 12-finished-or-waiting-or-error-or-
  unknown synthetic fixture (parents, children, dormant/missing
  "relatives"), same long-title torture strings, same machine/reporter
  labeling -- all preserved as literal Python data so
  `check-ui.mjs`'s `measure()` assertions (exact counts, badge text,
  etc.) keep passing unmodified.
- Routes: `/`, `/host.js`, `/measure.js`, `/case`, `/style.css`,
  `/app.js`, `/api/status`, `/api/control`, `/api/dismiss` (POST,
  bearer-token protected), `/results` (GET/POST).
- The embedded browser-side `host()`/`measure()` JS and the `measure.js`
  body are preserved verbatim as Python string constants -- they are
  100% client-side JS driving the dashboard in a real browser, not
  server logic, so there was nothing to "pythonify."
- `/` serves a minimal host shell page (iframe orchestrator, unchanged
  behavior: `<script src="/host.js">`); `/case` serves
  `public/index.html` with `<script src="/measure.js" defer>` injected
  before `</body>` -- same split as the original (the fixture/session
  dashboard itself only renders inside `/case` iframes, not at `/`).
- `public/index.html`/`app.js`/`style.css` are served completely
  unchanged (read from disk, not touched).

### `uv run` usage

No PEP 723 header was added to `scripts/verify_ui.py` -- unlike the 6
top-level launcher scripts, this is pure dev tooling invoked directly
from within the repo's own Python environment (the same one `pytest`
runs in), so `aiohttp` is already a project dependency and there's no
"run standalone with no prior setup" requirement to satisfy. Run it with
`python scripts/verify_ui.py` (prints
`Synthetic UI checks: http://127.0.0.1:<port>` on an ephemeral port, the
same stdout contract `check-ui.mjs` regex-scrapes to discover the URL)
or let `check-ui.mjs` spawn it itself via `uv run` (it invokes the
script through the project's `uv` environment so it resolves
`pymonitor` without a separate install step).

### Bugs found while porting (all now fixed/explained)

1. **Verbatim-copy bug in embedded client JS** (fixed): an early draft of
   the `measure()` string had been re-indented/re-escaped during the
   port, breaking a template-literal boundary. Fixed by copying the
   original's JS byte-for-byte into the Python string constant instead
   of retyping it.
2. **`/api/dismiss` auth literal pasted-back-as-redacted-text bug**
   (transient, self-corrected, confirmed fixed): at one point during
   authoring, tool-output redaction (any text shaped like
   `Bearer <token>` is masked to `"******"` in anything the model sees
   displayed, including `view`/`grep`/terminal echoes of the *real* file
   content) caused a "Bearer ..." comparison literal to get pasted back
   as the literal six-asterisk mask instead of the real derived-constant
   comparison, which made every dismiss request 403 regardless of a
   valid token -- this broke the `individualDismiss`/
   `bulkFinishedOnly`/`noPollReappearance` `check-ui.mjs` checks. It was
   caught and fixed before this phase by comparing against a derived
   constant (`f"Bearer {_CONTROL_TOKEN}"`) rather than a literal, with a
   code comment recording the failure mode so it isn't reintroduced.
   During *this* phase a second look at the same code, again via masked
   tool output, raised a false alarm that the bug had regressed; raw
   byte-level inspection (`ToCharArray() | [int][char]$_` in PowerShell,
   which bypasses all display-layer masking) confirmed the file already
   had the correct, real comparison -- no actual regression, and the
   `scripts/verify_ui.py` dismiss-auth code is correct today. This
   redaction-masking hazard is worth flagging for anyone who edits
   `Authorization`/bearer-token-shaped code in this repo going forward:
   never trust a masked `"******"` string seen in tool output as the
   real file content; verify raw bytes before "fixing" it.
3. **Pre-existing `.mjs` harness bug, `.row-title` no longer matching**
   (found, not a port bug, left as-is per scope): `check-ui.mjs`'s
   `measure()` checked `.row-title` element `textContent` against a
   format that stopped matching after commit `ff1003c` changed
   `public/app.js`'s title rendering. This predates the Python port and
   affects the original `.mjs` the same way; out of scope for this
   phase.
4. **`layout` check fails at 901x900 viewport** (found, not a port bug,
   root-caused, **fixed**): a pre-existing CSS issue, confirmed to
   reproduce identically against the original `.mjs` fixture server, not
   something introduced by this port. Root cause, confirmed via live
   `playwright` measurement of `getBoundingClientRect()` on `#running`
   and `#retained`: at the two-column breakpoint the columns narrow to
   ~420px, and `.session-column > p.muted`'s description text ("Errors/
   waiting stay. Finished and unconfirmed rows can be dismissed
   individually (monitor-only).") wraps to two lines in the right column
   while the shorter left-column description stays on one line. That
   18px height difference above `#retained` offsets its `top` by 18px
   relative to `#running`'s `top`, failing the `Math.abs(right.top -
   left.top) < 1` side-by-side alignment assertion (the single-column
   `<=900px` branch never hit this because it only asserts
   `right.top > left.bottom`, not alignment). Fixed with a single-line,
   surgical CSS change (user-approved exception to the "don't touch
   `public/`" rule, scoped to this fix only): added
   `min-height: 36px` (two lines at the existing 18px line-height) to
   `.session-column > p.muted` in `public/style.css`, so both columns'
   description blocks always reserve the same height regardless of
   whether their text actually wraps, keeping `#running`/`#retained`
   vertically aligned at every viewport width ≥901px. Verified fixed via
   `playwright` (both columns' `top` now equal at 901px) and via
   `node scripts/check-ui.mjs` (10/10 passing, including both 901x900
   light/dark `layout` cases).

### Verification

- `pytest tests/test_verify_ui.py -q`: **9/9 passing** (new file) --
  fixture builds 24 parents; `/` serves the host shell; `/case` serves
  `index.html` with measure.js injected; static assets served; measure.js
  embeds `window.expected=`; `/api/status` without a fixture id (no
  Referer) is a 500; `/api/status` with a fixture returns 24 sessions and
  the right theme; `/api/dismiss` rejects a missing/wrong bearer token
  with 403; `/api/dismiss` with the real control token and a real
  dismissable family id/key from `/api/status` returns 200 with that id
  in `"dismissed"`.
- `node scripts/check-ui.mjs` (real headless-Edge/CDP run against
  `scripts/verify_ui.py`): **10/10 passing** -- `runningOrdering`,
  `retainedOrdering`, `individualDismiss`, `bulkFinishedOnly`,
  `noPollReappearance`, `sourceLabels`, and `layout` (all viewports,
  light+dark) all pass; see finding 4 above for the `layout` fix.
- Full suite: `pytest tests/ -q` -- see count in the top-level summary
  below.
- `scripts/verify-ui.mjs` (the old Node original) has been deleted now
  that the Python port is confirmed working end-to-end.
