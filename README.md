# Sequelite

Sequelite is a lightweight SQL database engine implemented from scratch in Python. It provides an interactive command-line shell, an embedded Python API, persistent storage, and transactions with no external runtime dependencies.

The engine implements its own tokenizer, parser, query executor, schema validation, transaction handling, and storage format. It does not depend on Python's `sqlite3` module or another database engine.

## Features

| Feature | Support |
| --- | --- |
| Schema management | `CREATE TABLE`, `DROP TABLE`, INTEGER/REAL/TEXT columns, primary keys, NOT NULL |
| SQL data operations | `INSERT`, `SELECT`, `UPDATE`, `DELETE`, multi-row inserts |
| Query processing | Projection, comparisons, AND/OR, parentheses, NULL checks, ORDER BY, LIMIT, COUNT(*) |
| Persistent storage | Own versioned JSON database format, temporary-file writes and atomic replacement |
| Transactions | BEGIN, COMMIT, ROLLBACK; data and schema rollback; statement failure recovery |
| Embedded API and shell | Python API, interactive multiline shell, SQL scripts, piped input, JSON results |

## Quick start

Requires Python 3.10 or newer. From the project root, run the included demonstration or open a persistent database:

```sh
python3 -m sequelite :memory: -f examples/demo.sql
python3 -m sequelite notes.db
```

Inside the shell:

```sql
CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT NOT NULL, priority INTEGER);
INSERT INTO notes VALUES (1, 'Learn database internals', 3), (2, 'Write tests', 2);
SELECT title FROM notes WHERE priority >= 2 ORDER BY priority DESC;
BEGIN;
UPDATE notes SET priority = 1 WHERE id = 1;
ROLLBACK;
SELECT * FROM notes;
```

Use `.tables`, `.schema`, `.help`, and `.quit` in the interactive shell. End interactive SQL with a semicolon. Exit and reopen `notes.db` to see persisted records. An uncommitted transaction is discarded on exit.

Execute a one-off query or get machine-readable results:

```sh
python3 -m sequelite notes.db -c 'SELECT * FROM notes;'
python3 -m sequelite notes.db --json -c 'SELECT COUNT(*) FROM notes;'
```

## Build and install

### Prerequisites

- Python 3.10 or newer, with `pip` and `venv` available.
- Git if you are cloning the repository; downloading and extracting the source also works.
- Access to a Python package index for build dependencies, unless they are already cached.

Clone or download the repository, then open a terminal in the project root—the directory containing `pyproject.toml`. All commands below run from that directory.

### 1. Create and activate a virtual environment

Use a virtual environment to keep this installation separate from other Python projects. Create it once per checkout and activate it whenever you open a new terminal to work on the project.

**macOS / Linux:**

```sh
python3 -m venv .venv
source .venv/bin/activate
```

**Windows PowerShell:**

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

After activation, `python` and installed commands use the virtual environment. A virtual environment is recommended for installation; running the source directly does not require one.

If activation is unavailable, use the environment's interpreter directly: `.venv/bin/python` on macOS/Linux or `.\.venv\Scripts\python.exe` on Windows in place of `python`. Use that interpreter with `-m sequelite` in place of the `sequelite` command.

### 2. Build and install

With the virtual environment activated:

```sh
python -m pip install .
```

The `.` selects the current project directory. Pip reads `pyproject.toml`, prepares an isolated build environment with setuptools, builds a wheel, and installs it into the active virtual environment. Installation also creates the `sequelite` command. This is a pure Python package; no C compiler or SQLite installation is required. There are no external runtime dependencies.

### 3. Verify the installation

Run the included demonstration and test suite:

```sh
sequelite :memory: -f examples/demo.sql
python -m unittest discover -s tests -v
```

The demonstration prints query results and transaction messages. The test suite should finish with `OK`. Tests are run separately; installation does not run them automatically.

Start an interactive session with persistent storage:

```sh
sequelite notes.db
```

Use `.quit` to leave the shell. To leave the virtual environment afterward:

```sh
deactivate
```

### Build a distributable wheel

To save an installable package in `dist/`, run this with the virtual environment activated:

```sh
python -m pip wheel . --no-deps -w dist
```

For version 0.1.0, this produces `dist/sequelite-0.1.0-py3-none-any.whl`. The command builds the package without installing Sequelite; `--no-deps` skips runtime dependencies, but build tools are still required.

Install the wheel into an activated environment with:

```sh
python -m pip install dist/sequelite-0.1.0-py3-none-any.whl
```

The wheel contains the application package. The demo SQL and tests remain in the source checkout, so their commands above should be run from the project root.

### Development installation

To work on the source code, use an editable installation:

```sh
python -m pip install -e .
```

Python source changes are then available without reinstalling. Reinstall after changing package metadata or command entry points in `pyproject.toml`.

## Embedded API

```python
from sequelite import Database

db = Database(':memory:')
db.execute('CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT);')
db.execute("INSERT INTO books VALUES (1, 'Database Design');")
result = db.execute('SELECT * FROM books;')[0]
print(result.columns)  # ['id', 'title']
print(result.rows)     # [[1, 'Database Design']]
```

`execute` returns one `Result` per statement and raises `DatabaseError` for invalid SQL, constraint violations, or storage errors. A failed statement restores its prior state. Earlier successful statements in a batch remain applied; use an explicit transaction when you need to roll back a batch. If a statement fails inside an active transaction, the transaction remains active for correction or explicit rollback.

## Architecture

1. The tokenizer recognizes identifiers, literals, punctuation, operators, and line comments.
2. A recursive-descent parser produces statements and predicate trees, with AND binding more tightly than OR.
3. The executor evaluates predicates over rows, applies projections and ordering, and validates mutations.
4. Each mutation operates with a rollback snapshot. Explicit transactions retain a snapshot until commit or rollback.
5. File-backed commits serialize to a temporary file, flush it, and replace the database file. Opening the file reconstructs the tables.

Source: `sequelite/engine.py` (engine), `sequelite/cli.py` (shell), `tests/test_engine.py` (tests), `examples/demo.sql` (demonstration).

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests cover CRUD, query precedence, NULL, escaping, constraints and atomic failures, invalid SQL, transaction/schema rollback, reopening persisted files, failed saves, corrupt files, and CLI execution. GitHub Actions builds and tests Python 3.10, 3.12, and 3.14.

## Limitations

- Each `WHERE` condition supports up to 256 comparisons or NULL checks and 64 nested levels of parentheses. Larger conditions raise `DatabaseError` before the statement executes. The interactive shell remains usable, and any active transaction remains available for correction or rollback.
- Database files are Sequelite JSON files, not compatible with SQLite's binary file format.
- One owner per file: no concurrency control, file locking, or simultaneous writers.
- Tables live in memory. Queries scan rows; commits rewrite the file. There are no B-trees, indexes, pages, query optimizer, WAL, or crash-recovery journal.
- Atomic file replacement reduces partial writes but is not a claim of SQLite-level crash durability.
- No joins, foreign keys, ALTER TABLE, subqueries, expressions in assignments, parameters, or aggregates other than COUNT(*).
- Types are strict, identifiers are unquoted and case-insensitive, and strings use single quotes with doubled quote escaping. NULL comparisons do not match; use IS NULL or IS NOT NULL.
- No automatic primary-key generation. Provide primary-key values explicitly.

## Reference project

Sequelite is inspired by [SQLite 3](https://sqlite.org/), whose [official GitHub mirror](https://github.com/sqlite/sqlite) provides the reference project. It independently implements a subset of SQLite-style SQL operations and embedded database workflows. No SQLite source code was copied or translated.

## Development

Developed with **OpenAI Codex**, which assisted with the implementation, tests, command-line interface, packaging, CI configuration, and documentation.
