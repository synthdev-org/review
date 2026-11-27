"""Pure diff batch packer: splits a PR's changed files into batches of at most
``max_bytes`` rendered bytes, keeping linked files (and each test with the
source it tests) together. No infra, no ``indexing`` import; the link weights
come in as data (see ``review.diff_links`` for how they are built).
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

IMPORT_WEIGHT: float = 3.0
CALL_WEIGHT: float = 1.0
TEST_PAIR_WEIGHT: float = 100.0  # cut last, hub-exempt
HUB_MIN_DEGREE: int = 8  # hub = degree >= max(8, 25% of files)
HUB_DEGREE_FRACTION: float = 0.25
SEPARATOR_BYTES: int = 2  # "\n\n" between files in render_diff

FileLinks = Mapping[str, Mapping[str, float]]  # path -> {other path -> weight}


class BatchPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    batches: tuple[tuple[str, ...], ...]
    dropped: tuple[str, ...]


def _candidate_sources(test_path: str) -> list[str]:
    """Basenames a test file's source could have, most likely first."""
    base = posixpath.basename(test_path)
    stem, ext = posixpath.splitext(base)
    if ext == ".go":
        return [stem[: -len("_test")] + ext] if stem.endswith("_test") else []
    if ext == ".py":
        if stem.startswith("test_"):
            return [stem[len("test_") :] + ext]
        return [stem[: -len("_test")] + ext] if stem.endswith("_test") else []
    inner, inner_ext = posixpath.splitext(stem)
    if inner_ext in (".test", ".spec") and inner:
        alt = {".ts": [".tsx"], ".tsx": [".ts"]}.get(ext, [])
        return [inner + e for e in [ext, *alt]]
    return []


def paired_source(test_path: str, paths: Sequence[str]) -> str | None:
    """The source file ``test_path`` tests, or None. Same directory wins, else
    a unique basename match across ``paths``; ambiguous or none -> None.
    """
    candidates = _candidate_sources(test_path)
    if not candidates:
        return None
    directory = posixpath.dirname(test_path)
    for name in candidates:
        same_dir = posixpath.join(directory, name)
        if same_dir in paths and same_dir != test_path:
            return same_dir
    found = {p for p in paths if posixpath.basename(p) in candidates and p != test_path}
    return next(iter(found)) if len(found) == 1 else None


class _UnionFind:
    def __init__(self, size: int, weights: list[int]) -> None:
        self.parent = list(range(size))
        self.weight = list(weights)  # root -> bytes of the group

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int, limit: int | None = None) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if limit is not None and self.weight[ra] + self.weight[rb] > limit:
            return
        ra, rb = min(ra, rb), max(ra, rb)  # lowest diff index stays the root
        self.parent[rb] = ra
        self.weight[ra] += self.weight[rb]


def _fold_edges(
    paths: Sequence[str], links: FileLinks
) -> tuple[dict[tuple[int, int], float], set[tuple[int, int]]]:
    """Undirected link weights (i < j) and the test-pair edge set."""
    index = {p: i for i, p in enumerate(paths)}
    edges: dict[tuple[int, int], float] = {}
    for src, targets in links.items():
        for dst, weight in targets.items():
            if src in index and dst in index and src != dst:
                key = (min(index[src], index[dst]), max(index[src], index[dst]))
                edges[key] = edges.get(key, 0.0) + weight
    pairs: set[tuple[int, int]] = set()
    for path in paths:
        source = paired_source(path, paths)
        if source is not None:
            key = (min(index[path], index[source]), max(index[path], index[source]))
            pairs.add(key)
            edges[key] = edges.get(key, 0.0) + TEST_PAIR_WEIGHT
    return edges, pairs


def _drop_hub_edges(
    edges: dict[tuple[int, int], float], pairs: set[tuple[int, int]], count: int
) -> dict[tuple[int, int], float]:
    degree = [0] * count
    for i, j in edges:
        degree[i] += 1
        degree[j] += 1
    threshold = max(HUB_MIN_DEGREE, HUB_DEGREE_FRACTION * count)
    hubs = {i for i in range(count) if degree[i] >= threshold}
    return {k: w for k, w in edges.items() if k in pairs or not (k[0] in hubs or k[1] in hubs)}


def _risk_key(
    paths: Sequence[str], members: Sequence[int], risk: Mapping[str, float]
) -> tuple[float, float]:
    """Riskiest member first, then the sum: many cheap files must not outrank one valuable one."""
    scores = [risk.get(paths[i], 0.0) for i in members]
    return (-max(scores), -sum(scores))


def _group_files(
    paths: Sequence[str], cost: list[int], cap: int, edges: Mapping[tuple[int, int], float]
) -> list[list[int]]:
    """Link-connected file groups; oversize components are split on their weakest edges."""
    components = _UnionFind(len(paths), cost)
    for i, j in edges:
        components.union(i, j)
    # Oversize components are rebuilt by merging strongest edges first, only
    # while the merged group still fits, so the split falls on the weakest.
    pieces = _UnionFind(len(paths), cost)
    oversize = {
        r for r in range(len(paths)) if components.find(r) == r and components.weight[r] > cap
    }
    ranked = sorted(
        edges.items(),
        key=lambda kv: (
            -kv[1],
            posixpath.dirname(paths[kv[0][0]]) != posixpath.dirname(paths[kv[0][1]]),
            kv[0],
        ),
    )
    for (i, j), _ in ranked:
        limit = cap if components.find(i) in oversize else None
        pieces.union(i, j, limit)
    groups: dict[int, list[int]] = {}
    for i in range(len(paths)):
        groups.setdefault(pieces.find(i), []).append(i)
    return list(groups.values())


def _pack_bins(ordered: Sequence[list[int]], cost: Sequence[int], cap: int) -> list[list[int]]:
    """First-fit: each group goes into the first bin it fits, else opens a new one."""
    bins: list[list[int]] = []
    bin_cost: list[int] = []
    for group in ordered:
        group_cost = sum(cost[i] for i in group)
        for b, used in enumerate(bin_cost):
            if used + group_cost <= cap:
                bins[b].extend(group)
                bin_cost[b] += group_cost
                break
        else:
            bins.append(list(group))
            bin_cost.append(group_cost)
    return bins


def _order_key(
    paths: Sequence[str],
    risk: Mapping[str, float] | None,
    plain: Callable[[list[int]], str],
) -> Callable[[list[int]], tuple[Any, ...]]:
    """Sort key: riskiest first when ``risk`` is given, else ``plain`` then first index."""
    if risk is None:
        return lambda g: (plain(g), g[0])
    return lambda g: (*_risk_key(paths, g, risk), g[0])


def plan_batches(
    paths: Sequence[str],
    sizes: Mapping[str, int],
    links: FileLinks,
    *,
    max_bytes: int,
    max_chunks: int,
    risk: Mapping[str, float] | None = None,
) -> BatchPlan:
    """Pack ``paths`` (diff order) into batches of at most ``max_bytes`` bytes.

    With ``risk`` (path -> score; missing paths count 0) groups and batches are
    ordered riskiest first instead of by directory / diff order, so the
    ``max_chunks`` cut drops the least valuable files. ``None`` keeps the old plan.
    """
    if not paths:
        return BatchPlan(batches=(), dropped=())
    # Each file costs its size plus one separator; the last separator is not real.
    cost = [sizes[p] + SEPARATOR_BYTES for p in paths]
    cap = max_bytes + SEPARATOR_BYTES
    if sum(cost) <= cap:
        return BatchPlan(batches=(tuple(paths),), dropped=())

    edges, pairs = _fold_edges(paths, links)
    edges = _drop_hub_edges(edges, pairs, len(paths))

    groups = _group_files(paths, cost, cap, edges)
    ordered = sorted(groups, key=_order_key(paths, risk, lambda g: posixpath.dirname(paths[g[0]])))
    bins = _pack_bins(ordered, cost, cap)
    batches = sorted((sorted(b) for b in bins), key=_order_key(paths, risk, lambda b: ""))
    kept, dropped = batches[:max_chunks], batches[max_chunks:]
    return BatchPlan(
        batches=tuple(tuple(paths[i] for i in b) for b in kept),
        dropped=tuple(paths[i] for i in sorted(i for b in dropped for i in b)),
    )


__all__ = [
    "CALL_WEIGHT",
    "HUB_DEGREE_FRACTION",
    "HUB_MIN_DEGREE",
    "IMPORT_WEIGHT",
    "SEPARATOR_BYTES",
    "TEST_PAIR_WEIGHT",
    "BatchPlan",
    "FileLinks",
    "paired_source",
    "plan_batches",
]
