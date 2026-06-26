#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.2.0",
#   "harlequin-postgres",
#   "harlequin-mysql",
#   "harlequin-clickhouse",
#   "harlequin-odbc",  # SQL Server (mssql); needs system unixODBC + a driver
# ]
# ///
"""
Harlequin MCP server.

Exposes the databases configured in your Harlequin profiles (~/.harlequin.toml,
./pyproject.toml, etc.) to an MCP client such as Claude Code. It opens its OWN
read-only-by-default connection using the same profile config Harlequin uses, so
it can run alongside a live Harlequin TUI session against server databases
(Postgres / MySQL / SQL Server) without conflict.

Tools:
  - list_profiles()                              -> available profiles + adapter
  - get_schema(profile, name_filter, columns)   -> catalog tree (tables/columns)
  - run_query(profile, sql, limit, allow_writes) -> rows for a SELECT

It reuses Harlequin's own machinery, so no changes to Harlequin are required:
  harlequin.config.load_config / get_config_for_profile  -> read profiles
  harlequin.plugins.load_adapter_plugins                 -> resolve adapter class
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from harlequin.adapter import HarlequinAdapter, HarlequinConnection
from harlequin.catalog import Catalog, CatalogItem, InteractiveCatalogItem
from harlequin.config import get_config_for_profile, load_config
from harlequin.plugins import load_adapter_plugins

# Profile keys that are consumed by Harlequin itself, not by the adapter.
# Mirrors src/harlequin/cli.py inner_cli().
_NON_ADAPTER_KEYS = {
    "conn_str",
    "adapter",
    "limit",
    "theme",
    "keymap_name",
    "show_files",
    "show_s3",
    "locale",
    "no_download_tzdata",
}

_READ_ONLY_PREFIXES = ("select", "with", "explain", "show", "describe", "desc", "table")

mcp = FastMCP("harlequin")


def _build_adapter(profile_name: str | None) -> HarlequinAdapter:
    """Instantiate the adapter for a profile, exactly like the Harlequin CLI does."""
    profile, _ = get_config_for_profile(config_path=None, profile_name=profile_name)
    config: dict[str, Any] = dict(profile)

    conn_str = config.pop("conn_str", ())
    if isinstance(conn_str, str):
        conn_str = (conn_str,)

    adapter_name = config.pop("adapter", "duckdb")
    adapters = load_adapter_plugins()
    if adapter_name not in adapters:
        raise ValueError(
            f"Adapter {adapter_name!r} not installed. Installed: {sorted(adapters)}"
        )

    adapter_options = {k: v for k, v in config.items() if k not in _NON_ADAPTER_KEYS}
    return adapters[adapter_name](conn_str=tuple(conn_str), **adapter_options)


def _children(item: CatalogItem) -> list[CatalogItem]:
    """Return an item's children, lazy-loading them if the adapter defers them."""
    kids = list(item.children or [])
    if not kids and isinstance(item, InteractiveCatalogItem) and not item.loaded:
        try:
            kids = list(item.fetch_children())
        except Exception:
            kids = []
    return kids


# Cap the serialized get_schema payload so we never blow past the MCP result
# limit. On large databases the client otherwise dumps the result to a file.
_MAX_RESULT_CHARS = 60_000


def _emit(item: CatalogItem, depth: int) -> dict[str, Any]:
    """Serialize a catalog node, descending at most `depth` levels.

    Crucially, children (and for lazy adapters, the round-trips that load them)
    are only fetched while depth > 0, so listing tables never pulls every
    table's columns.
    """
    node: dict[str, Any] = {
        "name": item.label,
        "type": item.type_label,
        "id": item.qualified_identifier,
    }
    if depth > 0:
        kids = _children(item)
        if kids:
            node["children"] = [_emit(c, depth - 1) for c in kids]
    return node


def _match(items: list[CatalogItem], segment: str) -> list[CatalogItem]:
    seg = segment.lower()
    return [it for it in items if it.label.lower() == seg]


def _prune(node: dict[str, Any], needle: str) -> dict[str, Any] | None:
    """Keep nodes whose name matches `needle`, plus ancestors/descendants."""
    if needle in node["name"].lower():
        return node
    kept = [p for c in node.get("children", []) if (p := _prune(c, needle))]
    if kept:
        return {**node, "children": kept}
    return None


@mcp.tool()
def list_profiles() -> list[dict[str, Any]]:
    """List the Harlequin profiles available from the discovered config files."""
    config = load_config(config_path=None)
    default = config.get("default_profile")
    return [
        {
            "name": name,
            "adapter": prof.get("adapter", "duckdb"),
            "is_default": name == default,
        }
        for name, prof in config.get("profiles", {}).items()
    ]


@mcp.tool()
def get_schema(
    profile: str | None = None,
    path: list[str] | None = None,
    max_depth: int = 1,
    include_columns: bool = False,
    name_filter: str = "",
) -> Any:
    """
    Browse the catalog (databases -> schemas -> tables -> columns) one level at a
    time. Drill down with `path` instead of dumping the whole tree, so big
    databases stay within the MCP result limit.

    Args:
        profile: Profile name. Omit to use the default profile.
        path: Exact (case-insensitive) names to drill into, top-down. Examples:
            []                      -> list databases (+ their schemas)
            ["dev_db"]              -> list schemas in dev_db
            ["dev_db", "dbo"]       -> list tables/views in dev_db.dbo
            ["dev_db", "dbo", "orders"] -> that table (set include_columns)
            On a miss, returns the available names at that level.
        max_depth: Levels below the resolved path to include (default 1).
        include_columns: Descend one extra level so tables show their columns.
        name_filter: Optional case-insensitive substring to prune the result
            (e.g. find a table by partial name within the resolved path).

    Returns the matched subtree, or, if it would still be too large, an error
    with the counts so you can narrow `path` further.
    """
    path = path or []
    depth = max_depth + (1 if include_columns else 0)

    adapter = _build_adapter(profile)
    conn: HarlequinConnection = adapter.connect()
    try:
        catalog: Catalog = conn.get_catalog()
        nodes: list[CatalogItem] = list(catalog.items)
        roots: list[CatalogItem] = nodes
        for i, segment in enumerate(path):
            matches = _match(nodes, segment)
            if not matches:
                return {
                    "error": f"no node named {segment!r} at path {path[:i]}",
                    "available": sorted(n.label for n in nodes),
                }
            if i == len(path) - 1:
                roots = matches
            else:
                nodes = [c for m in matches for c in _children(m)]
        tree = [_emit(r, depth) for r in roots]
    finally:
        getattr(conn, "close", lambda: None)()

    if name_filter:
        needle = name_filter.lower()
        tree = [p for n in tree if (p := _prune(n, needle))]

    text = json.dumps(tree, default=str)
    if len(text) > _MAX_RESULT_CHARS:
        return {
            "error": "result too large; narrow `path`, lower `max_depth`, "
            "or set include_columns=false",
            "size_chars": len(text),
            "nodes": [
                {"name": n["name"], "children": len(n.get("children", []))}
                for n in tree
            ],
        }
    return tree


@mcp.tool()
def run_query(
    profile: str | None = None,
    sql: str = "",
    limit: int = 50,
    allow_writes: bool = False,
) -> dict[str, Any]:
    """
    Execute a query against a profile and return rows. Read-only by default.

    Args:
        profile: Profile name. Omit to use the default profile.
        sql: A single SQL statement.
        limit: Max rows returned.
        allow_writes: Must be True to run a non-SELECT statement.
    """
    stripped = sql.strip().lstrip("(").lower()
    if not allow_writes and not stripped.startswith(_READ_ONLY_PREFIXES):
        return {
            "error": "Refused: statement is not read-only. "
            "Pass allow_writes=true to override."
        }

    adapter = _build_adapter(profile)
    conn = adapter.connect()
    try:
        cursor = conn.execute(sql)
        if cursor is None:
            return {"columns": [], "rows": [], "message": "statement returned no rows"}
        cursor = cursor.set_limit(limit)
        columns = [name for name, _type in cursor.columns()]
        data = cursor.fetchall()
    finally:
        getattr(conn, "close", lambda: None)()

    return {"columns": columns, "rows": _rows(data, columns)}


def _rows(data: Any, columns: list[str]) -> list[list[Any]]:
    """Normalize the adapter's result (pyarrow Table/Batch, seq, or mapping) to rows."""
    if data is None:
        return []
    if hasattr(data, "to_pylist"):  # pyarrow Table / RecordBatch
        return [[d.get(c) for c in columns] for d in data.to_pylist()]
    if hasattr(data, "to_pydict"):
        cols = data.to_pydict()
        n = len(next(iter(cols.values()))) if cols else 0
        return [[cols[c][i] for c in columns] for i in range(n)]
    if isinstance(data, dict):  # Mapping[str, Sequence]
        n = len(next(iter(data.values()))) if data else 0
        return [[data[c][i] for c in columns] for i in range(n)]
    return [list(row) for row in data]  # Sequence[Iterable]


if __name__ == "__main__":
    mcp.run()
