#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["copilot-session-monitor"]
#
# [tool.uv.sources]
# copilot-session-monitor = { path = "..", editable = true }
# ///
"""Synthetic dashboard UI fixture + browser checks: port of ``scripts/verify-ui.mjs``.

Serves 24 synthetic parent sessions (working/finished/waiting/error/unknown,
plus children and dormant/missing relatives) through the real dashboard
static assets (``public/index.html`` etc.) and family-grouping logic
(``pymonitor.families``), together with the embedded client-side JS
(``measure.js``) that asserts the dashboard renders the fixture correctly
(card counts, columns, theme colors, badge text, dismiss/clear-finished
behavior, ...). ``scripts/check-ui.mjs`` drives a headless Edge instance
against this server to run those checks for real.

The embedded ``host()``/``measure()`` JavaScript is unchanged from the
original Node implementation -- it is 100% client-side browser code, so it
is kept verbatim as a string and served as-is rather than reimplemented.

Run with::

    uv run scripts/verify_ui.py

No prior ``pip install`` step is required: ``uv run`` installs this project
(editable, per the ``[tool.uv.sources]`` block above) into an ephemeral
virtual environment on first use. The server prints its URL
(``Synthetic UI checks: http://127.0.0.1:<port>``) once it is listening and
then runs until interrupted (Ctrl+C) or killed by a parent process --
``scripts/check-ui.mjs`` spawns it, scrapes that line for the URL, and kills
it when done.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from aiohttp import web

from pymonitor.actions import DismissError, MonitorActions, read_dismiss_entries
from pymonitor.families import FamilyMonitor, group_families

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public"

TIMESTAMP = "2026-10-02T22:00:00.000Z"
_TIMESTAMP_DT = datetime.fromisoformat(TIMESTAMP.replace("Z", "+00:00"))

# Fixture-only bearer credential returned by /api/control and required on
# subsequent /api/dismiss POSTs, matching app.js's control() flow.
_CONTROL_TOKEN = "synthetic-only"

_STATES_BY_OFFSET = [
    "finished", "waiting", "error", "unknown", "finished", "error",
    "waiting", "waiting", "finished", "finished", "finished", "finished",
]
_TITLES = [
    "Notebook completion reliability",
    "Review requested parser changes",
    "LongUnbrokenSessionTitle" + "AndMoreTitle" * 20,
    "Investigate environment selection",
]
_MACHINE_TORTURE = "TEST-MACHINE-" + "LongMachineName" * 12


def _ts_minus(index: int) -> str:
    moment = _TIMESTAMP_DT - timedelta(milliseconds=index * 60_000)
    return moment.isoformat().replace("+00:00", "Z")


def _build_parents() -> list[dict[str, Any]]:
    parents = []
    for index in range(24):
        state = "working" if index < 12 else _STATES_BY_OFFSET[index - 12]
        title = _TITLES[index % len(_TITLES)]
        finished_at = TIMESTAMP if state == "finished" else None

        if index == 8:
            last_response_at = None
            last_alert = None
        else:
            last_response_at = _ts_minus(index)
            kind = "finished" if index < 12 else ("warning" if state == "unknown" else state)
            last_alert = {
                "sessionId": f"fixture-{index}",
                "key": f"alert-{index}",
                "at": TIMESTAMP,
                "kind": kind,
                "message": "Parent monitor alert only; no transcript content",
            }

        if index == 17:
            detail = "Run interrupted"
        elif index == 18:
            detail = "Permission needed"
        elif index == 19:
            detail = "Plan approval needed"
        elif state == "waiting":
            detail = "Input needed"
        elif state == "finished":
            detail = "Current run finished; not task or PR success"
        elif state == "error":
            detail = "Run error"
        elif state == "unknown":
            detail = "Owner unavailable; no completion inferred"
        else:
            detail = "Agent running"

        parents.append({
            "id": f"fixture-{index}",
            "title": title,
            "state": state,
            "machine": _MACHINE_TORTURE,
            "source": "Copilot desktop",
            "startedAt": TIMESTAMP,
            "firstObservedAt": TIMESTAMP,
            "lastResponseAt": last_response_at,
            "finishedAt": finished_at,
            "activity": "Executing tools",
            "parentId": None,
            "contextOnly": False,
            "lastAlert": last_alert,
            "detail": detail,
        })
    return parents


def _build_members(parents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    for index, parent in enumerate(parents):
        if index < 12:
            members.append({**parent, "state": "finished", "finishedAt": TIMESTAMP})
        else:
            members.append(dict(parent))
        members.append({
            **parent,
            "id": f"child-{index}",
            "parentId": parent["id"],
            "title": f"Child {index}",
            "lastAlert": {
                "sessionId": f"child-{index}",
                "key": f"child-alert-{index}",
                "kind": "error",
                "at": TIMESTAMP,
                "message": "CHILD ALERT MUST NOT REPLACE PARENT",
            },
        })
    return members


def _build_relatives(parents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relatives: list[dict[str, Any]] = []
    for index in range(len(parents)):
        relatives.append({
            "id": f"dormant-{index}",
            "parentId": f"child-{index}",
            "title": "Dormant full child name " + "LongUnbrokenName" * 16,
            "detail": "Execution not observed",
        })
        relatives.append({
            "id": f"missing-{index}",
            "parentId": f"dormant-{index}",
            "title": "Name unavailable (missing metadata)",
            "detail": "Metadata unavailable",
        })
    return relatives


PARENTS = _build_parents()
MEMBERS = _build_members(PARENTS)
RELATIVES = _build_relatives(PARENTS)
SESSIONS = group_families(MEMBERS, RELATIVES)

REPORTERS = ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]
NETWORK_MACHINE = "DUPLICATE-WINDOWS-HOST"


def _network_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        reporter_id = REPORTERS[int(str(row["id"]).rsplit("-", 1)[-1]) % 2]
        out.append({
            **row,
            "reporterId": reporter_id,
            "machine": NETWORK_MACHINE,
            "machineTag": f"{NETWORK_MACHINE} ({reporter_id[:8]})",
        })
    return out


def _title_text(row: dict[str, Any]) -> str:
    # NOTE: the original scripts/verify-ui.mjs returned a bracketed
    # "[MACHINE... /reporterId] title" string here, expecting that to be the
    # literal `.row-title` textContent. That matched app.js's rendering
    # *before* commit ff1003c ("Fix permanent 'unconfirmed' state and trim
    # machine info from compact card"), which moved the machine/reporter
    # label out of `.row-title`'s textContent into its `title` tooltip
    # attribute (and into the `.muted` metadata line) to shorten the
    # compact card. verify-ui.mjs was never updated to match, so this
    # specific assertion has been silently broken since that commit --
    # independent of, and predating, this Python port. `.row-title`'s
    # *actual* textContent is just the bare title, so EXPECTED must match
    # that for the {running,retained}Ordering checks to mean anything.
    return row["title"]


EXPECTED = {
    key: [
        _title_text(row)
        for row in _network_rows(SESSIONS)
        if (row["state"] == "working") == (key == "running")
    ]
    for key in ("running", "retained")
}


# ---------------------------------------------------------------------------
# Embedded client-side JS, preserved verbatim from scripts/verify-ui.mjs.
# This is 100% browser code (DOM/CSS/accessibility assertions); it is NOT
# reimplemented in Python, only stored as a literal string and served as-is.
# ---------------------------------------------------------------------------

MEASURE_JS = r"""function measure() {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const all = selector => [...document.querySelectorAll(selector)];
  const mode = new URL(location.href).searchParams.get('mode');
  const checks = {};
  const metrics = {};
  const check = (name, condition) => { checks[name] = Boolean(condition); };
  const within = () => document.documentElement.scrollWidth <= innerWidth
    && all('.card').every(card => card.getBoundingClientRect().right <= innerWidth);
  const run = async () => {
    for (let attempt = 0; !all('.card').length && attempt < 100; attempt++) await wait(100);
    if (all('.card').length !== 24) throw new Error('Expected 24 fixture rows');
    metrics.viewport = [innerWidth, innerHeight];
    const running = document.querySelector('#running');
    const retained = document.querySelector('#retained');
    const left = running.getBoundingClientRect();
    const right = retained.getBoundingClientRect();
    check('layout', innerWidth <= 900 ? right.top > left.bottom && Math.abs(right.left - left.left) < 1
      : right.left > left.right && Math.abs(right.top - left.top) < 1);
    check('theme', document.documentElement.dataset.theme === mode);
    check('palette', getComputedStyle(document.documentElement).backgroundColor ===
      (mode === 'dark' ? 'rgb(12, 18, 28)' : 'rgb(245, 247, 251)'));
    check('noOverflow', within());
    check('counts', document.querySelector('#running-count').textContent === '12'
      && document.querySelector('#retained-count').textContent === '12');
    // Deviates from the original verify-ui.mjs check, which tested
    // `.row-title`'s textContent for a bracketed machine/reporter prefix.
    // app.js stopped rendering that prefix into `.row-title` textContent in
    // commit ff1003c (moved to the `title` tooltip attribute and the
    // `.muted` metadata line instead); see the _title_text() comment above.
    // This checks the same underlying "duplicate machine names are
    // distinguished by reporter" behavior against where app.js actually
    // renders it today.
    // NOTE: `.muted` is also used by static page-shell elements (header
    // banners, the list-guide legend, the dismiss-result line, the
    // notifications blurb) outside of any `.card`. Scope the selector to
    // `.card .muted` so only the per-row metadata line (one per rendered
    // session card) is checked, matching the original `.row-title`-scoped
    // intent before commit ff1003c moved this text onto `.muted`.
    check('sourceLabels', all('.card .muted').every(node => /DUPLICATE-WINDOWS-HOST \((11111111|22222222)\)/.test(node.textContent))
      && document.querySelector('#source-summary').textContent.startsWith('2/2'));
    for (const id of ['running', 'retained']) {
      const rows = all(`#${id} > .card`);
      metrics[id] = {
        firstRowTop: rows[0].getBoundingClientRect().top,
        heights: [...new Set(rows.map(row => row.getBoundingClientRect().height))],
        fullyVisible: rows.filter(row => {
          const box = row.getBoundingClientRect();
          return box.top >= 0 && box.bottom <= innerHeight;
        }).length,
      };
      check(`${id}Ordering`, JSON.stringify(rows.map(row => row.querySelector('.row-title').textContent)) ===
        JSON.stringify(window.expected[id]));
      check(`${id}Height`, rows.every(row => {
        const height = row.getBoundingClientRect().height;
        return height >= 50 && height <= (innerWidth <= 900 ? 95 : 65);
      }));
      if (innerWidth === 1200 && innerHeight === 900) check(`${id}TenVisible`, metrics[id].fullyVisible >= 10);
    }
    const badges = all('.badge').map(node => node.textContent);
    check('states', ['Working 1/2', 'Run finished', 'Needs input', 'Error', 'Unconfirmed'].every(text => badges.includes(text)));
    check('grouping', all('.card').length === 24 && all('.row-title').every(node => !node.textContent.startsWith('Child ')));
    check('visibleTimes', all('.finished > summary .compact-time').every(node =>
      node.textContent.startsWith('Parent alert: Finished') && node.getBoundingClientRect().height > 0)
      && all('.compact-time').some(node => node.textContent.startsWith('No parent alert observed; first seen')));
    check('parentAlertProvenance', all('#running .compact-time').filter(node => !node.textContent.startsWith('No parent'))
      .every(node => node.textContent.startsWith('Parent alert: Finished'))
      && !all('.parent-alert').some(node => node.textContent.includes('CHILD ALERT MUST NOT REPLACE PARENT')));
    check('readableFonts', all('.row-title').every(node => parseFloat(getComputedStyle(node).fontSize) >= 14)
      && all('.compact-time').every(node => parseFloat(getComputedStyle(node).fontSize) >= 13));
    const title = all('.row-title').find(node => node.textContent.includes('LongUnbroken'));
    const card = title.closest('.card');
    const summary = card.querySelector('summary');
    const closedHeight = card.getBoundingClientRect().height;
    check('longTitleTruncated', title.scrollWidth > title.clientWidth && getComputedStyle(title).textOverflow === 'ellipsis');
    summary.focus({ preventScroll: true });
    check('focusBeforePoll', document.activeElement === summary);
    summary.click();
    check('expands', card.open && card.getBoundingClientRect().height > closedHeight
      && title.textContent.endsWith(card.querySelector('.full-title').textContent)
      && card.querySelector('.full-title').getBoundingClientRect().height > 0
      && card.querySelector('.relatives').textContent.includes('Child: Child') && within());
    check('dormantNames', card.querySelector('.relatives').textContent.includes('Dormant full child name')
      && card.querySelector('.relatives').textContent.includes('Not observed')
      && card.querySelector('.depth-3').textContent.includes('Name unavailable'));
    await wait(1900);
    check('liveDisclosureAndFocus', card.isConnected && card.open && document.activeElement === summary
      && document.querySelector('#running').contains(card));
    summary.click();
    await wait(1900);
    check('collapses', !card.open && card.getBoundingClientRect().height === closedHeight);
    const dismissedCard = document.querySelector('.card.finished');
    const button = dismissedCard.querySelector('.dismiss');
    check('dismissAccessible', button.tagName === 'BUTTON' && button.type === 'button'
      && button.getAttribute('aria-label').includes('from this monitor only')
      && button.getBoundingClientRect().height >= 32 && button.getBoundingClientRect().width >= 44);
    button.focus({ preventScroll: true });
    await wait(1900);
    check('dismissFocusStable', document.activeElement === button && !dismissedCard.open);
    button.click();
    for (let attempt = 0; dismissedCard.isConnected && attempt < 60; attempt++) await wait(100);
    check('individualDismiss', !dismissedCard.isConnected && !dismissedCard.open && all('.card').length === 23
      && document.querySelector('#dismiss-result').textContent.includes('Copilot sessions and files are unchanged'));
    const clear = document.querySelector('#clear-finished');
    clear.click();
    for (let attempt = 0; all('.card.finished').length && attempt < 60; attempt++) await wait(100);
    check('bulkFinishedOnly', !all('.card.finished').length && all('#running > .card').length === 12
      && all('#retained > .card').length === 6 && clear.disabled);
    await wait(1900);
    check('noPollReappearance', all('.card').length === 18 && !all('.card.finished').length && within());
    return { mode, ...metrics, checks, passed: Object.values(checks).every(Boolean) };
  };
  run().catch(error => ({ mode, viewport: [innerWidth, innerHeight], passed: false, error: error.message }))
    .then(result => fetch('/results', { method: 'POST', body: JSON.stringify(result) }));
}"""

HOST_JS = r"""function host() {
  document.querySelector('#viewport').textContent = `Actual browser viewport: ${innerWidth} x ${innerHeight} CSS pixels`;
  const sizes = [[1200, 900], [900, 900], [901, 900], [360, 900], [320, 700], [innerWidth, innerHeight]];
  const cases = [];
  for (const [width, height] of [...new Map(sizes.map(size => [size.join('x'), size])).values()]) {
    for (const mode of ['light', 'dark']) cases.push({ width, height, mode });
  }
  const total = cases.length;
  let active;
  const next = () => {
    document.querySelector('iframe')?.remove();
    active = cases.shift();
    if (!active) return;
    const frame = document.createElement('iframe');
    frame.width = active.width;
    frame.height = active.height;
    frame.src = `/case?mode=${active.mode}&fixture=${crypto.randomUUID()}`;
    frame.title = `${active.width}x${active.height} ${active.mode} synthetic monitor fixture`;
    document.body.append(frame);
  };
  const render = async () => {
    const data = await (await fetch('/results')).json();
    document.querySelector('#results').textContent = data.map(row =>
      `${row.viewport.join('x')} ${row.mode}: ${row.passed ? 'PASS' : 'FAIL'} ${JSON.stringify(row.running)} ${JSON.stringify(row.retained)}`
    ).join('\n');
    document.title = `Density checks: ${data.filter(row => row.passed).length}/${total} passed`;
    if (active && data.some(row => row.viewport[0] === active.width && row.viewport[1] === active.height && row.mode === active.mode)) next();
  };
  next();
  setInterval(render, 1000);
}"""

_ROOT_HTML = (
    "<!doctype html><title>Density checks</title>"
    "<h1>Synthetic compact-row checks</h1>"
    '<p id="viewport"></p><pre id="results">Measuring...</pre>'
    "<style>iframe{display:block;border:0;margin:12px 0}pre{white-space:pre-wrap}</style>"
    '<script src="/host.js"></script>'
)


# ---------------------------------------------------------------------------
# Per-fixture ``FamilyMonitor``/``MonitorActions`` contexts, keyed by the
# ``?fixture=`` query param read off the *Referer* header (mirrors the
# original's ``fixtureFor(request)``: the iframe document's own URL -- not
# the request's own URL -- carries the fixture id for same-origin fetches
# made from inside it).
# ---------------------------------------------------------------------------

_contexts: dict[str, dict[str, Any]] = {}
_results_store: dict[str, dict[str, Any]] = {}


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _healthy_true() -> bool:
    return True


def _fixture_for(request: web.Request) -> dict[str, Any]:
    referer = request.headers.get("Referer")
    if not referer:
        raise RuntimeError("Fixture ID required")
    query = parse_qs(urlparse(referer).query)
    fixture_id = query.get("fixture", [None])[0]
    if not fixture_id:
        raise RuntimeError("Fixture ID required")

    if fixture_id not in _contexts:
        monitor = FamilyMonitor("TEST", _noop)
        monitor.engine.rows = {row["id"]: dict(row) for row in MEMBERS}
        monitor.relatives = RELATIVES
        _contexts[fixture_id] = {
            "monitor": monitor,
            "mode": query.get("mode", [None])[0],
            "actions": MonitorActions(monitor, _noop, _noop, _healthy_true),
        }
    return _contexts[fixture_id]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def _handle_root(_request: web.Request) -> web.Response:
    return web.Response(text=_ROOT_HTML, content_type="text/html")


async def _handle_host_js(_request: web.Request) -> web.Response:
    return web.Response(text=f"({HOST_JS})();", content_type="text/javascript")


async def _handle_measure_js(_request: web.Request) -> web.Response:
    body = f"window.expected={json.dumps(EXPECTED)};({MEASURE_JS})();"
    return web.Response(text=body, content_type="text/javascript")


_CONTENT_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}


async def _handle_static(request: web.Request) -> web.Response:
    file = "index.html" if request.path == "/case" else request.path.lstrip("/")
    content = (PUBLIC / file).read_text(encoding="utf-8")
    if file == "index.html":
        content = content.replace("</body>", '<script src="/measure.js" defer></script></body>')
    content_type = _CONTENT_TYPES.get(Path(file).suffix, "application/octet-stream")
    return web.Response(text=content, content_type=content_type)


async def _handle_results(request: web.Request) -> web.Response:
    if request.method == "POST":
        body = await request.read()
        if len(body) > 20_000:
            raise RuntimeError("Fixture result too large")
        value = json.loads(body.decode("utf-8"))
        key = "{}/{}".format("x".join(str(part) for part in value["viewport"]), value["mode"])
        _results_store[key] = value
    return web.json_response(list(_results_store.values()))


async def _handle_api_status(request: web.Request) -> web.Response:
    context = _fixture_for(request)
    snapshot = context["monitor"].snapshot()
    payload = {
        **snapshot,
        "sessions": _network_rows(snapshot["sessions"]),
        "healthy": True,
        "issues": [],
        "coverage": "SYNTHETIC FIXTURE - paired sources",
        "sources": [
            {"id": reporter_id, "label": NETWORK_MACHINE, "healthy": True, "lastSeen": TIMESTAMP, "issues": []}
            for reporter_id in REPORTERS
        ],
        "machine": "TEST-MACHINE",
        "updatedAt": TIMESTAMP,
        "notification": {"message": "Fixture: notifications not connected"},
        "theme": {"mode": context["mode"], "source": "windows-apps"},
    }
    return web.json_response(payload)


async def _handle_api_control(_request: web.Request) -> web.Response:
    return web.json_response({"token": _CONTROL_TOKEN})


async def _handle_api_dismiss(request: web.Request) -> web.Response:
    # NOTE: a prior edit here accidentally replaced the expected
    # "Bearer <token>" comparison literal with a 6-asterisk placeholder
    # (an artifact of a display/output redaction round-trip), which made
    # every dismiss request fail with 403 regardless of a valid token --
    # the root cause of the individualDismiss/bulkFinishedOnly/
    # noPollReappearance check failures. Compare against a derived
    # constant instead of a literal to avoid reintroducing that class of
    # mistake.
    if request.headers.get("Authorization") != f"Bearer {_CONTROL_TOKEN}":
        return web.Response(status=403)
    context = _fixture_for(request)
    entries = await read_dismiss_entries(request.content.iter_any())
    result = await context["actions"].dismiss(entries)
    return web.json_response(result)


@web.middleware
async def _error_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as http_exc:
        http_exc.headers["Cache-Control"] = "no-store"
        raise
    except DismissError as error:
        response = web.json_response({"error": str(error)}, status=error.status)
    except Exception as error:  # noqa: BLE001 -- mirrors the .mjs catch-all 500
        print(f"Fixture failure: {error!r}")
        response = web.Response(status=500, text="Fixture failure; see console")
    response.headers["Cache-Control"] = "no-store"
    return response


def _build_app() -> web.Application:
    app = web.Application(middlewares=[_error_middleware])
    app.router.add_get("/", _handle_root)
    app.router.add_get("/host.js", _handle_host_js)
    app.router.add_get("/measure.js", _handle_measure_js)
    app.router.add_get("/case", _handle_static)
    app.router.add_get("/style.css", _handle_static)
    app.router.add_get("/app.js", _handle_static)
    app.router.add_get("/api/status", _handle_api_status)
    app.router.add_get("/api/control", _handle_api_control)
    app.router.add_post("/api/dismiss", _handle_api_dismiss)
    app.router.add_get("/results", _handle_results)
    app.router.add_post("/results", _handle_results)
    return app


async def _run() -> None:
    app = _build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    # scripts/check-ui.mjs regex-scrapes stdout for this exact URL pattern.
    print(f"Synthetic UI checks: http://127.0.0.1:{port}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main() -> int:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
