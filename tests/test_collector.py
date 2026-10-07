"""Port of test/collector.test.mjs -- the HTTPS collector's remote-watcher
ingestion path: namespacing, lease fencing, baseline/skew trust, the
zombie-row rescue, and recovery-vote corroboration. These are the user's
named regression concerns (unconfirmed-status / zombie-row /
family-infection bug fixes) and must stay behaviorally identical to the
original JS test oracle.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from pymonitor.collector import Collector
from pymonitor.engine import Ledger
from pymonitor.families import group_families
from pymonitor.protocol import HEARTBEAT_MS, digest, metadata, observed_metadata, validate_report

AT = "2026-01-01T12:00:00.000Z"
NOW = datetime.fromisoformat(AT.replace("Z", "+00:00")).timestamp() * 1000


def member(state: str = "working", id_: str = "parent", parent_id: str | None = None) -> dict[str, Any]:
    return {
        "id": id_, "parentId": parent_id, "title": f"Synthetic {id_}", "machine": "SYNTHETIC",
        "source": "Copilot desktop", "state": state, "detail": "Synthetic state",
        "activity": "Executing tools", "runId": f"run-{id_}", "firstObservedAt": AT,
        "startedAt": AT, "lastResponseAt": AT, "lastEventAt": AT,
        "finishedAt": AT if state == "finished" else None,
        "hierarchyIssue": None, "contextOnly": False, "lastAlert": None,
    }


def finish_notice(id_: str = "parent") -> dict[str, Any]:
    return {"key": digest(f"finished-{id_}"), "familyId": id_, "kind": "finished"}


class Fixture:
    def __init__(
        self,
        tmp_path: Path,
        legacy: list[dict[str, Any]] | None = None,
        dismissed: dict[str, str] | None = None,
        process_started_at_ms: float | None = None,
    ) -> None:
        self.dir = tmp_path
        self.config = {
            "reporters": [
                {"id": str(uuid.uuid4()), "label": "Duplicate hostname", "tokenHash": "a" * 64, "legacy": index == 0}
                for index in range(2)
            ]
        }
        self.alerts: list[dict[str, Any]] = []
        self.file = str(self.dir / "collector-state.json")
        self.ledger = Ledger(str(self.dir / "notifications.json"))
        self.legacy = legacy or []
        self.dismissed = dismissed or {}
        self.process_started_at_ms = process_started_at_ms

    async def _notify(self, key: str, alert: dict[str, Any]) -> None:
        if await self.ledger.claim_digest(key):
            self.alerts.append(alert)

    async def setup(self) -> None:
        await self.ledger.load()
        self.notify = self._notify
        self.collector = Collector(self.file, self.notify, process_started_at_ms=self.process_started_at_ms)
        await self.collector.load(self.config, self.legacy, self.dismissed)
        self.identities = [
            {"version": 1, "reporterId": pair["id"], "installationId": str(uuid.uuid4()),
             "bootId": str(uuid.uuid4()), "generation": 1}
            for pair in self.config["reporters"]
        ]

    async def report(self, index: int, rows: list[dict[str, Any]], **options: Any) -> dict[str, Any]:
        source = self.collector.sources[self.identities[index]["reporterId"]]
        now = options.pop("now", NOW)
        value: dict[str, Any] = {
            "version": 1, "reporterId": source["id"], "lease": source["lease"], "seq": source["seq"] + 1,
            "sentAt": options.pop("sentAt", datetime.fromtimestamp(now / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")),
            "healthy": options.pop("healthy", True), "issues": options.pop("issues", []),
            "notices": options.pop("notices", []),
            **metadata({"members": rows}),
        }
        value.update(options)
        await self.collector.accept(value, now)
        return value

    async def connect(self, index: int = 0, now: float = NOW) -> dict[str, Any]:
        return await self.collector.connect(self.identities[index], now)


@pytest.fixture
async def fixture(tmp_path: Path):
    f = Fixture(tmp_path)
    await f.setup()
    return f


async def make_fixture(tmp_path: Path, legacy: list[dict[str, Any]] | None = None, dismissed: dict[str, str] | None = None) -> Fixture:
    tmp_path.mkdir(parents=True, exist_ok=True)
    f = Fixture(tmp_path, legacy, dismissed)
    await f.setup()
    return f


@pytest.mark.asyncio
async def test_namespace_and_aggregate_descendants(fixture: Fixture) -> None:
    f = fixture
    await f.connect(0)
    await f.connect(1)
    await f.report(0, [member("finished"), member("working", "child", "parent"), member("working", "nested", "child")])
    await f.report(1, [member("finished")])
    view = f.collector.snapshot(NOW)
    assert len(view["sessions"]) == 2
    assert view["sessions"][0]["id"] != view["sessions"][1]["id"]
    assert len(view["active"]) == 1
    assert view["active"][0]["runningCount"] == 2
    assert len(view["active"][0]["relatives"]) == 3
    assert len(f.alerts) == 0, "Baseline finished rows never alert"


@pytest.mark.asyncio
async def test_completion_dedupe_restart_dismissal(fixture: Fixture) -> None:
    f = fixture
    await f.connect()
    await f.report(0, [member()])
    report = await f.report(0, [member("finished")], notices=[finish_notice()])
    assert len(f.alerts) == 1
    result = await f.collector.accept({**report, "seq": report["seq"]}, NOW)
    assert result["duplicate"] is True
    assert len(f.alerts) == 1
    row = f.collector.snapshot(NOW)["sessions"][0]
    assert len(f.collector.dismiss([{"id": row["id"], "key": row["dismissKey"]}], NOW)["dismissed"]) == 1
    await f.collector.save()
    assert len(f.collector.snapshot(NOW)["sessions"]) == 0

    restart = Collector(f.file, f.notify)
    await restart.load(f.config)
    assert restart.snapshot(NOW)["sessions"][0]["state"] == "unknown", "Disconnected finish is never authoritative"
    lease = await restart.connect(f.identities[0], NOW)
    await restart.accept({**report, "lease": lease["lease"], "seq": 1}, NOW)
    assert len(restart.snapshot(NOW)["sessions"]) == 0
    assert len(f.alerts) == 1
    assert len(json.loads(Path(f.file).read_text())["dismissed"]) == 1


@pytest.mark.asyncio
async def test_omissions_unknown_clock_skew_heartbeat_loss(fixture: Fixture) -> None:
    for mode in ("omission", "unknown", "reader", "clock", "heartbeat"):
        f = Fixture(fixture.dir / mode, )
        f.dir.mkdir(parents=True, exist_ok=True)
        await f.setup()
        await f.connect()
        await f.report(0, [member(), member("working", "child", "parent")])
        if mode == "heartbeat":
            view = f.collector.snapshot(NOW + HEARTBEAT_MS)
            assert view["sessions"][0]["state"] == "unknown"
            with pytest.raises(Exception, match="Lease expired"):
                await f.report(0, [member("finished")], now=NOW + HEARTBEAT_MS)
            await f.connect(0, NOW + HEARTBEAT_MS)
        rows = [member("finished")]
        if mode != "omission":
            rows.append(member("unknown" if mode == "unknown" else "finished", "child", "parent"))
        kwargs: dict[str, Any] = {
            "healthy": mode != "reader",
            "sentAt": datetime.fromtimestamp((NOW - 31000) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z") if mode == "clock" else AT,
            "notices": [finish_notice()],
        }
        if mode == "heartbeat":
            kwargs["now"] = NOW + HEARTBEAT_MS
        await f.report(0, rows, **kwargs)
        assert len(f.alerts) == 0, mode
        if mode != "heartbeat":
            assert f.collector.snapshot(NOW)["sessions"][0]["state"] == "unknown", mode


@pytest.mark.asyncio
async def test_retained_finish_trusted_but_baseline_distrusts(fixture: Fixture) -> None:
    f = fixture
    await f.connect()
    await f.report(0, [member("working", "done")])
    await f.report(0, [member("finished", "done")], notices=[finish_notice("done")])
    assert len(f.alerts) == 1, "continuously observed completion is confirmed and notified"

    await f.report(0, [])
    source = f.collector.sources[f.identities[0]["reporterId"]]
    assert next(row for row in source["members"] if row["id"] == "done")["state"] == "finished", \
        "a healthy, already-baselined watcher narrowing its window keeps a retained finished row trusted"

    await f.connect(0, NOW + 1)
    await f.report(0, [], now=NOW + 1)
    source = f.collector.sources[f.identities[0]["reporterId"]]
    assert next(row for row in source["members"] if row["id"] == "done")["state"] == "unknown", \
        "a fresh baseline (reconnect) cannot vouch for a retained row it did not just observe"


@pytest.mark.asyncio
async def test_zombie_row_rescue_vs_still_open(fixture: Fixture) -> None:
    f = fixture

    def with_alert(row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "lastAlert": {"sessionId": row["id"], "key": f"alert-{row['id']}", "kind": "finished",
                                      "message": "Current run finished; this is not task or PR success", "at": AT}}

    await f.connect()
    await f.report(0, [member("working", "done"), member("working", "open")])
    await f.report(0, [with_alert(member("finished", "done")), member("working", "open")], notices=[finish_notice("done")])

    await f.connect(0, NOW + 1)
    await f.report(0, [], now=NOW + 1)

    def by_source_id(id_: str) -> dict[str, Any]:
        source = f.collector.sources[f.identities[0]["reporterId"]]
        return next(row for row in source["members"] if row["id"] == id_)

    assert by_source_id("done")["state"] == "unknown"
    assert by_source_id("open")["state"] == "unknown"

    await f.report(0, [], now=NOW + 2)
    assert by_source_id("done")["state"] == "finished", "a prior confirmed finish is a durable fact even across a reconnect gap"
    assert by_source_id("done")["finishedAt"] == AT
    assert by_source_id("done")["completionTracked"] is False
    assert by_source_id("open")["state"] == "unknown", "a row that vanished mid-run with no confirmed finish must stay unconfirmed"

    view = f.collector.snapshot(NOW + 2)
    assert next(row for row in view["sessions"] if row["id"].endswith("~done"))["state"] == "finished", \
        "the rescued row no longer poisons its family with an unconfirmed state"


@pytest.mark.asyncio
async def test_never_corroborated_finish_cannot_later_zombie_rescue(fixture: Fixture) -> None:
    """A session reported as already 'finished' on the very first report the
    collector ever sees from a source (no saved track record at all) is
    correctly distrusted down to 'unknown'. But its `lastAlert` breadcrumb
    was inherited verbatim from the watcher's own payload and must be
    cleared on this demotion too -- otherwise a *later* report that simply
    omits the row (as if the watcher's own retained window aged it out)
    would wrongly zombie-rescue it back to 'finished' using a timestamp the
    collector itself never confirmed.
    """
    f = fixture

    def with_alert(row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "lastAlert": {"sessionId": row["id"], "key": f"alert-{row['id']}", "kind": "finished",
                                      "message": "Current run finished; this is not task or PR success", "at": AT}}

    await f.connect()
    await f.report(0, [with_alert(member("finished", "done"))])

    def by_source_id(id_: str) -> dict[str, Any]:
        source = f.collector.sources[f.identities[0]["reporterId"]]
        return next(row for row in source["members"] if row["id"] == id_)

    assert by_source_id("done")["state"] == "unknown", \
        "never-before-seen 'finished' has no track record and cannot be trusted on sight"

    await f.report(0, [])
    assert by_source_id("done")["state"] == "unknown", \
        "a never-corroborated completion must not resurrect once the watcher simply omits the row"


@pytest.mark.asyncio
async def test_stale_finished_row_from_before_collector_started_never_zombie_rescues(tmp_path: Path) -> None:
    """Reproduces the perpetual stale-"finished"-after-restart bug: a row
    confirmed 'finished' (with its lastAlert breadcrumb) by one collector
    process, which is then restarted. The NEW collector process is given a
    `process_started_at_ms` after that finish. Once the restarted collector
    baselines and the watcher's report genuinely stops including the row
    (its own FamilyMonitor-level restart-staleness fix hides it), the row
    must stay demoted forever -- not resurrect via either the retained
    'finished' carry-forward or the lastAlert zombie-rescue -- across
    arbitrarily many subsequent omitted reports.
    """
    def with_alert(row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "lastAlert": {"sessionId": row["id"], "key": f"alert-{row['id']}", "kind": "finished",
                                      "message": "Current run finished; this is not task or PR success", "at": AT}}

    f = Fixture(tmp_path, process_started_at_ms=NOW + 10_000)
    await f.setup()
    await f.connect()
    await f.report(0, [member("working", "done")])
    await f.report(0, [with_alert(member("finished", "done"))], notices=[finish_notice("done")])

    def by_source_id(id_: str) -> dict[str, Any]:
        source = f.collector.sources[f.identities[0]["reporterId"]]
        return next(row for row in source["members"] if row["id"] == id_)

    assert by_source_id("done")["state"] == "finished", "confirmed within this process's own lifetime stays trusted"

    # Restart: fresh collector process, same on-disk state, now started
    # after the finish above (simulates the real restart scenario).
    for _ in range(5):
        await f.connect(0, NOW + 20_000)
        await f.report(0, [], now=NOW + 20_000)
        assert by_source_id("done")["state"] == "unknown", \
            "a finish that predates this collector run must never resurrect, no matter how many ticks pass"


@pytest.mark.asyncio
async def test_finish_after_collector_started_still_zombie_rescues(tmp_path: Path) -> None:
    """Counterpart/no-regression proof: a completion that happens AFTER the
    current collector process started must still be eligible for the
    legitimate zombie-row rescue (continuously-healthy watcher narrowing its
    reporting window), exactly as test_zombie_row_rescue_vs_still_open
    already proves with no process_started_at_ms set at all.
    """
    def with_alert(row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "lastAlert": {"sessionId": row["id"], "key": f"alert-{row['id']}", "kind": "finished",
                                      "message": "Current run finished; this is not task or PR success", "at": AT}}

    f = Fixture(tmp_path, process_started_at_ms=NOW - 10_000)
    await f.setup()
    await f.connect()
    await f.report(0, [member("working", "done")])
    await f.report(0, [with_alert(member("finished", "done"))], notices=[finish_notice("done")])

    await f.connect(0, NOW + 1)
    await f.report(0, [], now=NOW + 1)

    def by_source_id(id_: str) -> dict[str, Any]:
        source = f.collector.sources[f.identities[0]["reporterId"]]
        return next(row for row in source["members"] if row["id"] == id_)

    assert by_source_id("done")["state"] == "unknown", "a fresh baseline still cannot vouch for a row it did not just observe"

    await f.report(0, [], now=NOW + 2)
    assert by_source_id("done")["state"] == "finished", \
        "a genuinely recent (post-process-start) confirmed finish still rescues across a reconnect gap"
    assert by_source_id("done")["finishedAt"] == AT


@pytest.mark.asyncio
async def test_parent_descendant_restores_dismissed_and_stale_revision_skips(tmp_path: Path) -> None:
    for child in (False, True):
        f = await make_fixture(tmp_path / f"pd-{child}")
        await f.connect()
        await f.report(0, [member()])
        await f.report(0, [member("finished")], notices=[finish_notice()])
        finished = f.collector.snapshot(NOW)["sessions"][0]
        entry = {"id": finished["id"], "key": finished["dismissKey"]}
        f.collector.dismiss([entry], NOW)
        if child:
            await f.report(0, [member("finished"), member("working", "new-child", "parent")])
        else:
            await f.report(0, [{**member(), "runId": "new-run"}])
        active = f.collector.snapshot(NOW)["active"][0]
        assert active is not None
        assert active["dismissKey"] is None
        assert len(f.collector.dismiss([entry], NOW)["skipped"]) == 1


@pytest.mark.asyncio
async def test_leases_fence_installations_boots_replay_reorder(fixture: Fixture) -> None:
    f = fixture
    await f.connect()
    report = await f.report(0, [member()])
    with pytest.raises(Exception, match="Out-of-order"):
        await f.collector.accept({**report, "seq": 4}, NOW)
    with pytest.raises(Exception, match="conflicting"):
        await f.collector.accept({**report, "members": []}, NOW)
    with pytest.raises(Exception, match="identity conflict"):
        await f.collector.connect({**f.identities[0], "installationId": str(uuid.uuid4())}, NOW)
    with pytest.raises(Exception, match="holds this identity"):
        await f.collector.connect({**f.identities[0], "bootId": str(uuid.uuid4()), "generation": 2}, NOW)
    await f.connect()
    with pytest.raises(Exception, match="Lease expired"):
        await f.collector.accept(report, NOW)
    replacement = {**f.identities[0], "bootId": str(uuid.uuid4()), "generation": 2}
    await f.collector.connect(replacement, NOW + HEARTBEAT_MS)
    with pytest.raises(Exception, match="Old watcher"):
        await f.collector.connect(f.identities[0], NOW + HEARTBEAT_MS)


@pytest.mark.asyncio
async def test_legacy_migration_preserves_first_seen_and_dismissal(tmp_path: Path) -> None:
    row = member("finished")
    family = group_families([row])[0]
    f = await make_fixture(tmp_path, [row], {row["id"]: family["dismissKey"]})
    await f.ledger.claim("original-notification")
    before = set(f.ledger.keys)
    await f.connect()
    await f.report(0, [{**row, "firstObservedAt": "2025-12-31T12:00:00.000Z"}], notices=[finish_notice()])
    assert len(f.collector.snapshot(NOW)["sessions"]) == 0
    assert f.collector.snapshot(NOW)["members"][0]["firstObservedAt"] == AT
    assert f.ledger.keys == before
    assert len(f.alerts) == 0


def test_protocol_rejects_bad_reports() -> None:
    good = {
        "version": 1, "reporterId": str(uuid.uuid4()), "lease": "a" * 64, "seq": 1,
        "sentAt": AT, "healthy": True, "issues": [], "notices": [],
        **metadata({"members": [member()]}),
    }
    assert validate_report(good) == good
    bad_variants = [
        {**good, "version": 2},
        {**good, "prompt": "Not metadata"},
        {**good, "members": good["members"] + good["members"]},
        {**good, "members": [{**good["members"][0], "title": "x" * 513}]},
        {**good, "members": [{**good["members"][0], "lastAlert": {
            "sessionId": "child", "key": "x", "kind": "finished", "message": "x", "at": AT}}]},
    ]
    for bad in bad_variants:
        with pytest.raises(Exception):
            validate_report(bad)


@pytest.mark.asyncio
async def test_reconnect_baseline_cannot_upgrade_but_votes_recover(fixture: Fixture) -> None:
    f = fixture
    await f.connect()
    await f.report(0, [member()])
    await f.connect()
    for _ in range(2):
        await f.report(0, [member("finished")], notices=[finish_notice()])
        assert f.collector.snapshot(NOW)["sessions"][0]["state"] == "unknown", \
            "the baseline report and a single corroboration are not enough to re-trust a demoted row"
    await f.report(0, [member("finished")], notices=[finish_notice()])
    assert f.collector.snapshot(NOW)["sessions"][0]["state"] == "finished", \
        "a run of consistent, healthy, non-skewed reports of the same run recovers it"
    assert len(f.alerts) == 0
    await f.report(0, [{**member(), "runId": "new-run"}])
    await f.report(0, [{**member("finished"), "runId": "new-run"}], notices=[finish_notice()])
    assert len(f.alerts) == 1


@pytest.mark.asyncio
async def test_corroboration_votes_reset_on_run_change_or_unhealthy(fixture: Fixture) -> None:
    f = fixture
    await f.connect()
    await f.report(0, [member()])
    await f.connect()
    await f.report(0, [member("finished")], notices=[finish_notice()])
    assert f.collector.snapshot(NOW)["sessions"][0]["state"] == "unknown"
    await f.report(0, [member("finished")], healthy=False)
    await f.report(0, [member("finished")], notices=[finish_notice()])
    assert f.collector.snapshot(NOW)["sessions"][0]["state"] == "unknown", \
        "an intervening unhealthy report resets the corroboration count"
    await f.report(0, [{**member("finished"), "runId": "different-run"}], notices=[finish_notice()])
    await f.report(0, [member("finished")], notices=[finish_notice()])
    assert f.collector.snapshot(NOW)["sessions"][0]["state"] == "unknown", \
        "votes only stitch together across reports of the exact same run"


@pytest.mark.asyncio
async def test_continuously_tracked_late_flush_can_finish(tmp_path: Path) -> None:
    for completion_tracked in (True, False):
        f = await make_fixture(tmp_path / f"ltf-{completion_tracked}")
        await f.connect()
        await f.report(0, [member()])
        await f.report(0, [{**member("unknown"), "completionTracked": completion_tracked}])
        await f.report(0, [member("finished")], notices=[finish_notice()])
        assert len(f.alerts) == (1 if completion_tracked else 0)


def test_observed_metadata_trusts_finish_through_exit_but_demotes_on_loss() -> None:
    snapshot = {"members": [member("finished")]}
    for sample in (None, {"alive": False}):
        report = observed_metadata(snapshot, [{"id": "parent", **sample}] if sample else [])
        assert report["members"][0]["state"] == "finished"
    for sample in (
        {"alive": True, "readError": "Unavailable"},
        {"alive": True, "completionUnconfirmed": "Owner changed", "events": {"terminal": {}}},
        {"alive": True, "events": {"terminal": {}, "replaced": True}},
    ):
        report = observed_metadata(snapshot, [{"id": "parent", **sample}])
        assert report["members"][0]["state"] == "unknown"
        assert report["members"][0]["finishedAt"] is None
    assert observed_metadata(snapshot, [{"id": "parent", "alive": True, "events": {"terminal": {}}}])["members"][0]["state"] == "finished"


@pytest.mark.asyncio
async def test_central_parent_alert_remains_parent_only(fixture: Fixture) -> None:
    f = fixture
    await f.connect()
    alert = {"sessionId": "parent", "key": "parent-alert", "kind": "waiting", "message": "Input needed", "at": AT}
    await f.report(0, [
        {**member(), "lastAlert": alert},
        {**member("working", "child", "parent"), "lastAlert": {**alert, "sessionId": "child", "key": "child-error", "kind": "error"}},
    ])
    await f.report(0, [member(), member("working", "child", "parent")])
    row = f.collector.snapshot(NOW)["sessions"][0]
    assert row["parentAlert"]["kind"] == "waiting"
    assert row["parentAlert"]["sessionId"] == row["id"]


@pytest.mark.asyncio
async def test_failed_persistence_rolls_back_and_allows_retry(fixture: Fixture) -> None:
    f = fixture
    await f.connect()
    await f.report(0, [member()])
    source = f.collector.sources[f.identities[0]["reporterId"]]
    report = {
        "version": 1, "reporterId": source["id"], "lease": source["lease"], "seq": source["seq"] + 1,
        "sentAt": AT, "healthy": True, "issues": [],
        **metadata({"members": [member("finished")]}), "notices": [finish_notice()],
    }
    f.collector.file = str(Path(f.file) / "not-a-directory")
    with pytest.raises(Exception):
        await f.collector.accept(report, NOW)
    assert f.collector.snapshot(NOW)["sessions"][0]["state"] == "working"
    assert len(f.alerts) == 0
    f.collector.file = f.file
    await f.collector.accept(report, NOW)
    assert len(f.alerts) == 1
