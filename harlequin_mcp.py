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
(Postgres / MySQL / SQL Server) without conflict. It is strictly read-only:
every query is screened and anything that could change data or schema is refused.

Tools:
  - list_profiles()                              -> available profiles + adapter
  - get_schema(profile, name_filter, columns)   -> catalog tree (tables/columns)
  - run_query(profile, sql, limit)              -> rows for a read-only SELECT
  - export_query(profile, sql, dest_path, ...)   -> write full result to a file

It reuses Harlequin's own machinery, so no changes to Harlequin are required:
  harlequin.config.load_config / get_config_for_profile  -> read profiles
  harlequin.plugins.load_adapter_plugins                 -> resolve adapter class
"""

from __future__ import annotations

import json
import re
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

# A statement must begin with one of these to even be considered read-only.
_READ_ONLY_PREFIXES = ("select", "with", "explain", "show", "describe", "desc", "table")

# Any of these keywords appearing anywhere (outside string/comment) means the
# statement can change data or schema, so it is refused even if it starts with a
# read-only prefix (e.g. a data-modifying CTE: WITH x AS (DELETE ...) SELECT ...).
_WRITE_KEYWORDS = frozenset(
    {
        "insert", "update", "delete", "drop", "truncate", "alter", "create",
        "replace", "merge", "upsert", "grant", "revoke", "vacuum", "attach",
        "detach", "copy", "call", "do", "lock", "rename", "reindex", "refresh",
        "comment", "into", "nextval", "setval",
    }
)

# dest_path extension -> export format. Inferred when `format` is left blank.
_EXPORT_FORMATS = {
    ".csv": "csv",
    ".tsv": "csv",
    ".parquet": "parquet",
    ".pq": "parquet",
    ".json": "json",
    ".ndjson": "json",
    ".feather": "feather",
    ".arrow": "feather",
    ".orc": "orc",
}

mcp = FastMCP("harlequin")


def _strip_sql(sql: str) -> str:
    """Remove comments and string/dollar-quoted literals so keyword scanning of the
    remaining SQL can't be fooled by text inside strings or tripped by it."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)  # /* block comments */
    sql = re.sub(r"--[^\n]*", " ", sql)  # -- line comments
    sql = re.sub(r"\$\$.*?\$\$", " ", sql, flags=re.S)  # $$ dollar-quoted $$
    sql = re.sub(r"'(?:[^']|'')*'", " ", sql)  # 'single quoted'
    sql = re.sub(r'"(?:[^"]|"")*"', " ", sql)  # "quoted identifiers"
    return sql


def _read_only_violation(sql: str) -> str | None:
    """Return a human-readable reason the SQL is refused, or None if it is read-only.

    Defense in depth: requires a read-only opening keyword, rejects stacked
    statements, and rejects any data/DDL-changing keyword anywhere in the body.
    """
    cleaned = _strip_sql(sql)
    statements = [s for s in cleaned.split(";") if s.strip()]
    if len(statements) > 1:
        return "multiple statements are not allowed; send one read-only query"
    body = statements[0].strip() if statements else ""
    if not body:
        return "empty statement"
    if not body.lstrip("(").lower().startswith(_READ_ONLY_PREFIXES):
        return (
            "statement is not read-only; only SELECT / WITH / EXPLAIN / SHOW / "
            "DESCRIBE / TABLE queries are allowed"
        )
    found = sorted({w for w in re.findall(r"[a-z_]+", body.lower()) if w in _WRITE_KEYWORDS})
    if found:
        return f"statement contains write keyword(s): {', '.join(found)}"
    return None


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
def get_columns(
    profile: str | None = None,
    path: list[str] | None = None,
) -> Any:
    """
    Return the columns of a single table/view with their real SQL data types and
    nullability. Cheaper and more precise than get_schema(include_columns=True)
    when you already know which table you want.

    Args:
        profile: Profile name. Omit to use the default profile.
        path: Exact (case-insensitive) names down to the table, top-down, e.g.
            ["mydb", "public", "orders"] or ["mydb", "orders"]. On a miss,
            returns the available names at that level.

    Types come from information_schema.columns. If that view is unavailable for
    the adapter, falls back to the engine's short type labels (e.g. ## / s / ts).
    """
    path = path or []
    if not path:
        return {"error": "path is required (drill down to a table)."}

    adapter = _build_adapter(profile)
    conn: HarlequinConnection = adapter.connect()
    try:
        catalog: Catalog = conn.get_catalog()
        nodes: list[CatalogItem] = list(catalog.items)
        item: CatalogItem | None = None
        for i, segment in enumerate(path):
            matches = _match(nodes, segment)
            if not matches:
                return {
                    "error": f"no node named {segment!r} at path {path[:i]}",
                    "available": sorted(n.label for n in nodes),
                }
            if i == len(path) - 1:
                item = matches[0]
            else:
                nodes = [c for m in matches for c in _children(m)]
        assert item is not None

        table_name = item.label
        schema_name = path[-2] if len(path) >= 2 else None
        cols = _columns_from_information_schema(conn, table_name, schema_name)
        if cols is None:
            cols = _columns_from_describe(conn, item)
    finally:
        getattr(conn, "close", lambda: None)()

    return {"table": item.qualified_identifier, "columns": cols}


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _columns_from_information_schema(
    conn: HarlequinConnection, table_name: str, schema_name: str | None
) -> list[dict[str, Any]] | None:
    """Real column types via information_schema; None if it errors or finds nothing."""
    where = f"table_name = {_sql_literal(table_name)}"
    if schema_name:
        where += f" AND table_schema = {_sql_literal(schema_name)}"
    sql = (
        "SELECT column_name, data_type, is_nullable, ordinal_position "
        f"FROM information_schema.columns WHERE {where} ORDER BY ordinal_position"
    )
    try:
        cursor = conn.execute(sql)
        if cursor is None:
            return None
        names = [n for n, _ in cursor.columns()]
        rows = _rows(cursor.fetchall(), names)
    except Exception:
        return None
    if not rows:
        return None
    return [
        {
            "name": r[0],
            "type": r[1],
            "nullable": str(r[2]).upper() not in {"NO", "0", "FALSE"},
            "position": r[3],
        }
        for r in rows
    ]


def _columns_from_describe(
    conn: HarlequinConnection, item: CatalogItem
) -> list[dict[str, Any]]:
    """Fallback: names + the engine's short type labels via a 0-row select."""
    cursor = conn.execute(f"SELECT * FROM {item.qualified_identifier}")
    if cursor is None:
        return []
    cursor = cursor.set_limit(0)
    return [
        {"name": name, "type": type_label, "position": i + 1}
        for i, (name, type_label) in enumerate(cursor.columns())
    ]


@mcp.tool()
def run_query(
    profile: str | None = None,
    sql: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """
    Execute a read-only query against a profile and return rows.

    Only SELECT / WITH / EXPLAIN / SHOW / DESCRIBE / TABLE queries are allowed.
    Any statement that can modify data or schema (INSERT, UPDATE, DELETE, DROP,
    TRUNCATE, ALTER, CREATE, ...) is refused — this server is read-only.

    Args:
        profile: Profile name. Omit to use the default profile.
        sql: A single SQL statement.
        limit: Max rows returned.
    """
    violation = _read_only_violation(sql)
    if violation:
        return {"error": f"Refused (read-only server): {violation}."}

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


@mcp.tool()
def export_query(
    profile: str | None = None,
    sql: str = "",
    dest_path: str = "",
    format: str = "",
    limit: int = 0,
) -> dict[str, Any]:
    """
    Run a read-only query and write its FULL result set to a file, bypassing the
    row/size limits of run_query. Use this for large extracts ("dump these rows
    to parquet/csv") instead of returning data into the conversation.

    Only read-only queries are allowed; any data/schema-changing statement is
    refused (see run_query).

    Args:
        profile: Profile name. Omit to use the default profile.
        sql: A single read-only SQL statement.
        dest_path: Destination file path. The parent directory must exist; an
            existing file is overwritten. Tip: write to the session scratchpad
            for throwaway extracts.
        format: One of csv, parquet, json, feather, orc. Left blank, it is
            inferred from the dest_path extension (.csv/.parquet/.json/...).
        limit: Max rows to write. 0 (default) means no limit — write everything.

    Returns the destination path, format, and the number of rows/columns written.
    """
    if not dest_path:
        return {"error": "dest_path is required."}
    violation = _read_only_violation(sql)
    if violation:
        return {"error": f"Refused (read-only server): {violation}."}

    fmt = format.lower() or _EXPORT_FORMATS.get(_suffix(dest_path), "")
    if fmt not in {"csv", "parquet", "json", "feather", "orc"}:
        return {
            "error": f"Unknown export format {fmt or '(none)'!r}; pass `format` or "
            "use a known extension (.csv/.parquet/.json/.feather/.orc).",
        }

    import os

    parent = os.path.dirname(os.path.abspath(os.path.expanduser(dest_path)))
    if not os.path.isdir(parent):
        return {"error": f"Parent directory does not exist: {parent}"}

    adapter = _build_adapter(profile)
    conn = adapter.connect()
    try:
        cursor = conn.execute(sql)
        if cursor is None:
            return {"error": "Statement returned no result set to export."}
        if limit > 0:
            cursor = cursor.set_limit(limit)
        columns = [name for name, _type in cursor.columns()]
        data = cursor.fetchall()
    finally:
        getattr(conn, "close", lambda: None)()

    table = _arrow_table(data, columns)
    out = os.path.abspath(os.path.expanduser(dest_path))
    _write_table(table, out, fmt)

    return {
        "path": out,
        "format": fmt,
        "rows": table.num_rows,
        "columns": list(table.column_names),
        "bytes": os.path.getsize(out),
    }


def _suffix(path: str) -> str:
    import os

    return os.path.splitext(path)[1].lower()


def _arrow_table(data: Any, columns: list[str]) -> Any:
    """Coerce an adapter result into a pyarrow.Table, preserving column order."""
    import pyarrow as pa

    if data is None:
        return pa.table({c: [] for c in columns})
    if isinstance(data, pa.Table):
        return data.select(columns) if columns else data
    if isinstance(data, pa.RecordBatch):
        return pa.Table.from_batches([data])
    if hasattr(data, "to_pydict"):  # other arrow-like batches
        cols = data.to_pydict()
        return pa.table({c: cols[c] for c in columns}) if columns else pa.table(cols)
    if isinstance(data, dict):  # Mapping[str, Sequence]
        return pa.table({c: data[c] for c in columns}) if columns else pa.table(data)
    # Sequence of row iterables -> list of dicts, let Arrow infer types.
    records = [dict(zip(columns, row)) for row in data]
    return pa.Table.from_pylist(records)


def _write_table(table: Any, dest_path: str, fmt: str) -> None:
    """Write a pyarrow.Table to dest_path in the requested format."""
    if fmt == "csv":
        import pyarrow.csv as pc

        pc.write_csv(table, dest_path)
    elif fmt == "parquet":
        import pyarrow.parquet as pq

        pq.write_table(table, dest_path)
    elif fmt == "feather":
        import pyarrow.feather as pf

        pf.write_feather(table, dest_path)
    elif fmt == "orc":
        import pyarrow.orc as po

        po.write_table(table, dest_path)
    elif fmt == "json":
        # JSON array of row objects, written incrementally so we never build one
        # giant string for large extracts.
        with open(dest_path, "w", encoding="utf-8") as fh:
            fh.write("[")
            for i, record in enumerate(table.to_pylist()):
                fh.write(("," if i else "") + json.dumps(record, default=str))
            fh.write("]")
    else:  # pragma: no cover - guarded by caller
        raise ValueError(f"unsupported format {fmt!r}")


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
