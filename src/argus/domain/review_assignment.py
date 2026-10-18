"""Deterministic, optimal case-count balancing with executor exclusions."""
from __future__ import annotations

import collections
import heapq
import random
import re
from typing import Any

ALIAS = re.compile(r"[a-z][a-z0-9-]{1,63}")


def reviewer_aliases(value: Any) -> list[str]:
    """Read only sign-in aliases from untrusted roster input."""
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise ValueError("Enter between 1 and 50 reviewers.")
    result = []
    for row in value:
        alias = str(row.get("alias") or "").strip().lower() if isinstance(row, dict) else ""
        if not ALIAS.fullmatch(alias):
            raise ValueError("Enter a valid sign-in alias in each Name field.")
        if alias in result:
            raise ValueError("Each sign-in alias must appear only once.")
        result.append(alias)
    return sorted(result)


def validate_roster(value: Any) -> list[dict[str, str]]:
    """Validate identities supplied by the server's Jira resolver."""
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise ValueError("Enter between 1 and 50 reviewers.")
    aliases: set[str] = set()
    identities: set[str] = set()
    result = []
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("Jira could not resolve a reviewer identity.")
        name, alias, jira_key = (
            str(row.get(k) or "").strip() for k in ("name", "alias", "jira_key")
        )
        alias = alias.lower()
        if (
            not name or len(name) > 120 or not ALIAS.fullmatch(alias)
            or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", jira_key)
        ):
            raise ValueError("Jira could not resolve a reviewer identity.")
        if alias in aliases or jira_key.casefold() in identities:
            raise ValueError("Each sign-in alias and Jira identity must appear only once.")
        aliases.add(alias)
        identities.add(jira_key.casefold())
        result.append({"name": name, "alias": alias, "jira_key": jira_key})
    return sorted(result, key=lambda r: r["alias"])


def eligible(case: dict[str, Any], reviewer: dict[str, str]) -> bool:
    executor = case.get("executor")
    return bool(executor) and str(executor).casefold() != reviewer["jira_key"].casefold()


def allocate(
    cases: list[dict[str, Any]],
    roster: list[dict[str, str]],
    *,
    seed: str,
    fixed: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Minimize sum(load**2), preserving fixed work; never assign unknown executors.

    A min-cost flow groups executions by their eligible reviewer set. Marginal
    reviewer slots cost 2*load+1, so the flow optimizes the exact convex objective,
    including existing fixed loads. Add each reviewer's next slot only when the
    previous slot fills; unused higher-cost slots cannot improve a shortest path.
    """
    rng = random.Random(seed)
    reviewers = sorted(roster, key=lambda row: row["alias"])
    rng.shuffle(reviewers)
    by_alias = {r["alias"]: r for r in reviewers}
    assignments: dict[str, str] = {}
    reasons: dict[str, str] = {}
    counts = dict.fromkeys(by_alias, 0)
    groups: dict[tuple[str, ...], list[str]] = collections.defaultdict(list)
    for case in sorted(cases, key=lambda c: c["key"]):
        key = case["key"]
        owner = (fixed or {}).get(key)
        if owner is not None:
            if owner not in by_alias or not eligible(case, by_alias[owner]):
                raise ValueError("Started work needs an eligible reviewer or an explicit transfer.")
            assignments[key] = owner
            counts[owner] += 1
            continue
        allowed = tuple(r["alias"] for r in reviewers if eligible(case, r))
        if not allowed:
            reasons[key] = (
                "Executor identity is unknown." if not case.get("executor")
                else "No eligible reviewer; add someone other than the executor."
            )
        else:
            groups[allowed].append(key)

    total = sum(map(len, groups.values()))
    group_rows = list(groups.items())
    rng.shuffle(group_rows)
    # Edges are [destination, reverse-index, residual-capacity, cost].
    source, group_base = 0, 1
    reviewer_base = group_base + len(group_rows)
    sink = reviewer_base + len(reviewers)
    graph: list[list[list[int]]] = [[] for _ in range(sink + 1)]

    def edge(start: int, end: int, capacity: int, cost: int) -> list[int]:
        forward = [end, len(graph[end]), capacity, cost]
        reverse = [start, len(graph[start]), 0, -cost]
        graph[start].append(forward)
        graph[end].append(reverse)
        return forward

    reviewer_nodes = {r["alias"]: reviewer_base + i for i, r in enumerate(reviewers)}
    connections = []
    for i, (allowed, keys) in enumerate(group_rows):
        rng.shuffle(keys)
        node = group_base + i
        edge(source, node, len(keys), 0)
        connections.append([
            (alias, edge(node, reviewer_nodes[alias], len(keys), 0))
            for alias in allowed
        ])
    next_cost = {}
    for alias, node in reviewer_nodes.items():
        edge(node, sink, 1, 2 * counts[alias] + 1)
        next_cost[node] = 2 * counts[alias] + 3

    potential = [0] * len(graph)
    for sent in range(total):
        distances = [10**18] * len(graph)
        previous: list[tuple[int, int] | None] = [None] * len(graph)
        distances[source] = 0
        queue = [(0, source)]
        while queue:
            distance, node = heapq.heappop(queue)
            if distance != distances[node]:
                continue
            for index, (target, _, capacity, cost) in enumerate(graph[node]):
                candidate = distance + cost + potential[node] - potential[target]
                if capacity and candidate < distances[target]:
                    distances[target] = candidate
                    previous[target] = (node, index)
                    heapq.heappush(queue, (candidate, target))
        sink_path = previous[sink]
        if sink_path is None:
            raise RuntimeError("Eligible review allocation could not be completed.")
        assigned_node = sink_path[0]
        for node, distance in enumerate(distances):
            if distance < 10**18:
                potential[node] += distance
        node = sink
        while node != source:
            prior = previous[node]
            assert prior is not None
            start, index = prior
            item = graph[start][index]
            item[2] -= 1
            graph[node][item[1]][2] += 1
            node = start
        if sent + 1 < total:
            edge(assigned_node, sink, 1, next_cost[assigned_node])
            next_cost[assigned_node] += 2
    for (_, keys), links in zip(group_rows, connections, strict=True):
        offset = 0
        for alias, link in links:
            assigned = len(keys) - link[2]
            for key in keys[offset:offset + assigned]:
                assignments[key] = alias
            counts[alias] += assigned
            offset += assigned
    return {"assignments": assignments, "unassigned": reasons, "counts": counts}
