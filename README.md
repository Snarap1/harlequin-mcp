# Harlequin MCP

MCP server that exposes your [Harlequin](https://harlequin.sh) database profiles to Claude Code (or any MCP client). No Harlequin modifications needed — it reuses your existing `~/.harlequin.toml` profiles.

## Tools

| Tool | Description |
|------|-------------|
| `list_profiles` | List all configured Harlequin profiles |
| `get_schema` | Browse catalog (databases → schemas → tables → columns) |
| `get_columns` | List one table's columns with real SQL types + nullability |
| `run_query` | Execute SQL (read-only by default) |
| `export_query` | Run a query and write the full result to a file (csv/parquet/json/feather/orc) |

## Requirements

- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — the only manual install required; it handles Python and all package dependencies automatically
- At least one profile configured in `~/.harlequin.toml`

All Python packages (`mcp`, `harlequin-postgres`, `harlequin-mysql`, `harlequin-clickhouse`, `harlequin-odbc`) are installed automatically by `uv` on first run. Python 3.10+ is also downloaded by `uv` if not present.

**For SQL Server (ODBC adapter only):** additionally requires a system ODBC driver manager + Microsoft ODBC Driver for SQL Server — see [SQL Server setup](#sql-server-odbc) below.

## Installation

### 1. Install uv

**Linux / macOS:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell):**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. Get the script

```bash
git clone https://github.com/Snarap1/harlequin-mcp.git
# or just download harlequin_mcp.py directly
```

**Linux / macOS only** — make it executable:
```bash
chmod +x /path/to/harlequin_mcp.py
```

### 3. Configure Harlequin profiles

If you haven't already, add profiles to `~/.harlequin.toml`:

```toml
default_profile = "my_pg"

[profiles.my_pg]
adapter = "postgres"
conn_str = "postgresql://user:pass@localhost:5432/mydb"

[profiles.my_mysql]
adapter = "mysql"
conn_str = "mysql://user:pass@localhost:3306/mydb"

[profiles.local_duck]
# adapter defaults to "duckdb"
conn_str = "~/data/myfile.duckdb"
```

### 4. Register with Claude Code

**Linux / macOS:**
```bash
claude mcp add harlequin -- uv run --script /path/to/harlequin_mcp.py
```

**Windows:**
```powershell
claude mcp add harlequin -- uv run --script C:\path\to\harlequin_mcp.py
```

Or add manually to `.claude/settings.json`:

**Linux / macOS:**
```json
{
  "mcpServers": {
    "harlequin": {
      "command": "uv",
      "args": ["run", "--script", "/path/to/harlequin_mcp.py"]
    }
  }
}
```

**Windows:**
```json
{
  "mcpServers": {
    "harlequin": {
      "command": "uv",
      "args": ["run", "--script", "C:\\path\\to\\harlequin_mcp.py"]
    }
  }
}
```

### 5. Verify

```bash
claude mcp list
# harlequin  uv run --script /path/to/harlequin_mcp.py
```

Start a Claude Code session and try:

```
list harlequin profiles
```

## Usage examples

```
# List profiles
list_profiles()

# Browse all tables in a schema
get_schema(profile="my_pg", path=["mydb", "public"])

# Get columns for a specific table, with real SQL types + nullability
get_columns(profile="my_pg", path=["mydb", "public", "orders"])

# Find tables by partial name
get_schema(profile="my_pg", name_filter="user")

# Run a query
run_query(profile="my_pg", sql="SELECT * FROM orders WHERE created_at > now() - interval '1 day'", limit=100)

# Export a large result set to a file (no row limit; format inferred from extension)
export_query(profile="my_pg", sql="SELECT * FROM orders", dest_path="/path/to/orders.parquet")

# Force a format regardless of extension
export_query(profile="my_pg", sql="SELECT * FROM orders", dest_path="/path/to/out", format="csv")
```

## Supported adapters

| Adapter | Package | Extra system deps |
|---------|---------|-------------------|
| DuckDB | built-in | — |
| PostgreSQL | `harlequin-postgres` | — |
| MySQL | `harlequin-mysql` | — |
| ClickHouse | `harlequin-clickhouse` | — |
| SQL Server | `harlequin-odbc` | See below |

All adapter packages are installed automatically by `uv` on first run.

### SQL Server (ODBC)

**Linux (Ubuntu/Debian):**
```bash
sudo apt install unixodbc unixodbc-dev
# then install Microsoft ODBC Driver for SQL Server:
# https://learn.microsoft.com/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server
```

**macOS:**
```bash
brew install unixodbc
# then install Microsoft ODBC Driver for SQL Server:
# https://learn.microsoft.com/sql/connect/odbc/linux-mac/install-microsoft-odbc-driver-sql-server-macos
```

**Windows:** ODBC driver manager is built into Windows. Just install the Microsoft ODBC Driver for SQL Server:
[https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)

## Notes

- This server is **strictly read-only** — there is no write override. `run_query` and `export_query` accept only `SELECT` / `WITH` / `EXPLAIN` / `SHOW` / `DESCRIBE` / `TABLE` statements, and additionally refuse any query that contains a data- or schema-changing keyword (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `TRUNCATE`, `ALTER`, `CREATE`, `GRANT`, `COPY`, …), data-modifying CTEs (`WITH … AS (DELETE …)`), `SELECT … INTO`, and multiple stacked statements (`;`)
- The server opens its own connection per request, so it works alongside a live Harlequin TUI session against the same database
- `get_schema` caps output at 60 000 chars to stay within MCP limits; use `path` to drill down if you hit the cap
- `get_columns` reports real SQL types from `information_schema.columns`, falling back to the engine's short type labels (`##`, `s`, `ts`, …) if that view isn't available for the adapter
- `export_query` bypasses `run_query`'s row/size limits — use it for large extracts. The format is inferred from the `dest_path` extension (`.csv`/`.parquet`/`.json`/`.feather`/`.orc`) or set explicitly via `format`; the parent directory must already exist
