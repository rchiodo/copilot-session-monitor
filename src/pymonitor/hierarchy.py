"""Port of src/hierarchy.mjs.

Pure data-shape logic for the desktop-app session tree (parent/child
linkage sourced from ~/.copilot/data.db). The SDK has no equivalent of
this join structure, so this module stays a faithful, unmodified port.
"""
from __future__ import annotations

from typing import Any, Iterable


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def hierarchy_index(
    *,
    sessions: Iterable[dict[str, Any]],
    workspaces: Iterable[dict[str, Any]],
    links: Iterable[dict[str, Any]],
    aliases: Iterable[dict[str, Any]],
    workspace_chats: Iterable[dict[str, Any]],
    session_chats: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    workspaces = list(workspaces)
    links = list(links)
    aliases = list(aliases)
    workspace_chats = list(workspace_chats)
    session_chats = list(session_chats)

    nodes: dict[str, dict[str, Any]] = {
        row["id"]: {**row, "parentId": None, "hierarchyIssue": None} for row in sessions
    }

    def ensure(id_: str | None) -> None:
        if id_ and id_ not in nodes:
            nodes[id_] = {
                "id": id_,
                "title": f"Unavailable session {id_[:8]}",
                "parentId": None,
                "hierarchyIssue": "Session metadata unavailable",
                "execution_location": None,
                "is_running": 0,
            }

    for row in workspaces:
        ensure(row.get("session_id"))
    for row in [*workspace_chats, *session_chats]:
        ensure(row.get("session_id"))

    workspace_ids = {row["id"]: row for row in workspaces}
    references: dict[str, set[str]] = {}

    def bind(reference: str | None, id_: str | None) -> None:
        if not reference or not id_:
            return
        references.setdefault(reference, set()).add(id_)

    for row in workspaces:
        bind(row.get("id"), row.get("session_id"))
        bind(row.get("session_id"), row.get("session_id"))
    for row in aliases:
        ws = workspace_ids.get(row.get("workspace_id"))
        bind(row.get("session_id"), ws.get("session_id") if ws else None)

    def resolve(reference: str) -> dict[str, Any]:
        targets = references.get(reference)
        if targets and len(targets) > 1:
            return {"id": reference, "issue": "Ambiguous app session identity"}
        id_ = next(iter(targets), reference) if targets else reference
        return {"id": id_, "issue": None if id_ in nodes else "Recorded parent is missing"}

    def parent(child_id: str, reference: str | None, issue: str | None = None) -> None:
        child = nodes.get(child_id)
        if not child or not reference:
            return
        resolved = resolve(reference)
        if child.get("parentId") and child["parentId"] != resolved["id"]:
            child["hierarchyIssue"] = "Conflicting recorded parents"
            return
        child["parentId"] = resolved["id"]
        child["hierarchyIssue"] = _coalesce(issue, resolved["issue"], child.get("hierarchyIssue"))
        if resolved["id"] not in nodes:
            nodes[resolved["id"]] = {
                "id": resolved["id"],
                "title": f"Unavailable parent {resolved['id'][:8]}",
                "parentId": None,
                "hierarchyIssue": "Recorded parent is missing",
                "execution_location": None,
                "is_running": 0,
            }

    for row in aliases:
        ws = workspace_ids.get(row.get("workspace_id"))
        target = ws.get("session_id") if ws else None
        if target and row.get("session_id") != target:
            parent(row["session_id"], row.get("workspace_id"))
            node = nodes.get(row["session_id"])
            if node:
                node["aliasOf"] = target

    for row in workspaces:
        node = nodes.get(row.get("session_id"))
        if not node:
            continue
        node["host_id"] = row.get("host_id")
        if row.get("archived_at"):
            node["archived_at"] = row["archived_at"]
        recorded = [link for link in links if link.get("child_workspace_id") == row.get("id")]
        if recorded:
            for link in recorded:
                parent(row["session_id"], link.get("parent_workspace_id"))
        elif row.get("creator_session_id"):
            creator = resolve(row["creator_session_id"])
            creator_node = nodes.get(creator["id"])
            type_ = creator_node.get("session_type") if creator_node else None
            parent(
                row["session_id"],
                row["creator_session_id"],
                "Workspace parent link is missing" if type_ == "project" else creator["issue"],
            )

    for row in workspace_chats:
        parent(row["session_id"], row.get("workspace_id"))
    for row in session_chats:
        parent(row["session_id"], row.get("parent_session_id"))

    return nodes


def root_of(id_: str, nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    path: list[str] = []
    current = id_
    while nodes.get(current, {}).get("parentId"):
        if current in path:
            cycle = sorted(path[path.index(current):])
            return {"id": cycle[0], "issue": "Cycle in recorded app hierarchy"}
        path.append(current)
        current = nodes[current]["parentId"]
    return {"id": current, "issue": None if current in nodes else "Recorded parent is missing"}


def selected_hierarchy(nodes: dict[str, dict[str, Any]], tracked: Iterable[str]) -> list[dict[str, Any]]:
    def local(row: dict[str, Any]) -> bool:
        return row.get("execution_location") == "local" and (not row.get("host_id") or row.get("host_id") == "local")

    selected: set[str] = {id_ for id_ in tracked if id_ in nodes}
    for row in nodes.values():
        if not row.get("archived_at") and row.get("is_running") and local(row):
            selected.add(row["id"])

    roots = {root_of(id_, nodes)["id"] for id_ in selected}
    # A known running relative outside this machine blocks family completion, not local coverage.
    for row in nodes.values():
        if row.get("is_running") and root_of(row["id"], nodes)["id"] in roots:
            selected.add(row["id"])

    for id_ in list(selected):
        current = id_
        visited: set[str] = set()
        while current in nodes and current not in visited:
            selected.add(current)
            visited.add(current)
            current = nodes[current]["parentId"]

    result = []
    for id_ in selected:
        row = nodes[id_]
        result.append({
            **row,
            "hierarchyIssue": _coalesce(row.get("hierarchyIssue"), root_of(id_, nodes)["issue"]),
            "local": local(row),
        })
    return result


def related_metadata(nodes: dict[str, dict[str, Any]], selected: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = list(selected)
    roots = {root_of(row["id"], nodes)["id"] for row in selected}
    observed = {row["id"] for row in selected}
    out = []
    for row in nodes.values():
        if (row.get("aliasOf") and row["id"] not in observed):
            continue
        if root_of(row["id"], nodes)["id"] not in roots:
            continue
        if row.get("archived_at"):
            detail = "Archived; execution not observed"
        elif row.get("execution_location") != "local" or (row.get("host_id") and row.get("host_id") != "local"):
            detail = "Unavailable locally; execution not observed"
        else:
            detail = "Execution not observed"
        out.append({
            "id": row["id"],
            "parentId": row.get("parentId"),
            "aliasOf": _coalesce(row.get("aliasOf"), None),
            "title": row.get("title") or f"Name unavailable ({row['id'][:8]})",
            "hierarchyIssue": _coalesce(row.get("hierarchyIssue"), root_of(row["id"], nodes)["issue"]),
            "detail": detail,
        })
    return out
