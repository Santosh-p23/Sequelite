"""Command-line interface and interactive SQL shell."""
import argparse
import json
from pathlib import Path
import sys

from .engine import Database, DatabaseError


def display(result, json_output=False):
    if json_output:
        print(json.dumps({'columns': result.columns, 'rows': result.rows,
                          'affected': result.affected, 'message': result.message}))
    elif result.columns:
        rows = [['NULL' if v is None else str(v) for v in row] for row in result.rows]
        widths = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c)
                  for i, c in enumerate(result.columns)]
        def line(row):
            return ' | '.join(v.ljust(w) for v, w in zip(row, widths))
        print(line(result.columns))
        print('-+-'.join('-' * w for w in widths))
        for row in rows:
            print(line(row))
        print(f'({len(rows)} row(s))')
    else:
        print(result.message)


def complete(sql):
    """Recognize statement-ending semicolons outside strings and comments."""
    quoted = False
    i = 0
    last = ''
    while i < len(sql):
        char = sql[i]
        if char == "'":
            if quoted and i + 1 < len(sql) and sql[i + 1] == "'":
                i += 2
                continue
            quoted = not quoted
        elif not quoted and sql[i:i+2] == '--':
            end = sql.find('\n', i)
            if end < 0:
                break
            i = end
        if not char.isspace():
            last = char
        i += 1
    return not quoted and last == ';'


def main(argv=None):
    parser = argparse.ArgumentParser(description='Sequelite — a tiny, original SQL database')
    parser.add_argument('database', nargs='?', default=':memory:', help='database file (default: memory)')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('-c', '--command', help='execute SQL and exit')
    source.add_argument('-f', '--file', help='execute a SQL file and exit')
    parser.add_argument('--json', action='store_true', help='print each result as JSON')
    args = parser.parse_args(argv)
    try:
        db = Database(args.database)
        def run(sql):
            for result in db.execute(sql):
                display(result, args.json)
        if args.command is not None:
            run(args.command)
        elif args.file:
            run(Path(args.file).read_text())
        elif not sys.stdin.isatty():
            run(sys.stdin.read())
        else:
            print('Sequelite | .help for commands | end SQL with a semicolon')
            buffer = ''
            while True:
                try:
                    line = input('   ...> ' if buffer else 'sequelite> ')
                except EOFError:
                    break
                except KeyboardInterrupt:
                    print('\nStatement cancelled')
                    buffer = ''
                    continue
                if not buffer and line.startswith('.'):
                    command = line.strip()
                    if command in ('.quit', '.exit'):
                        break
                    if command == '.help':
                        print('.tables  .schema  .quit\nSQL: CREATE, DROP, INSERT, SELECT, UPDATE, DELETE, BEGIN, COMMIT, ROLLBACK')
                    elif command == '.tables':
                        print('  '.join(sorted(db.tables)))
                    elif command == '.schema':
                        for name, table in db.tables.items():
                            columns = []
                            for col in table['columns']:
                                suffix = ' PRIMARY KEY' if col['primary'] else ' NOT NULL' if col['required'] else ''
                                columns.append(f"{col['name']} {col['type']}{suffix}")
                            print(f"CREATE TABLE {name} ({', '.join(columns)});")
                    else:
                        print('Unknown command. Use .help.', file=sys.stderr)
                    continue
                buffer += line + '\n'
                if complete(buffer):
                    try:
                        run(buffer)
                    except DatabaseError as exc:
                        print(f'Error: {exc}', file=sys.stderr)
                    buffer = ''
            if buffer.strip():
                print('Incomplete SQL discarded.', file=sys.stderr)
        if db.in_transaction:
            print('Uncommitted transaction discarded.', file=sys.stderr)
        return 0
    except (DatabaseError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
