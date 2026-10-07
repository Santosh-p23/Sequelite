"""An original, small SQL interpreter with JSON-backed storage."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from dataclasses import dataclass, field

from .locking import writer_lock


class DatabaseError(Exception):
    """Invalid SQL, constraint violation, or storage failure."""


@dataclass
class Result:
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    affected: int = 0
    message: str = "OK"


TOKEN = re.compile(r"\s+|--[^\n]*|'(?:[^']|'')*'|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?|[A-Za-z_][A-Za-z_0-9]*|<=|>=|<>|!=|[(),;*=<>+-]")


def tokenize(sql):
    tokens, pos = [], 0
    while pos < len(sql):
        match = TOKEN.match(sql, pos)
        if not match:
            raise DatabaseError(f"Unexpected character at position {pos}: {sql[pos:pos+16]!r}")
        text = match.group()
        if not text.isspace() and not text.startswith('--'):
            tokens.append(text)
        pos = match.end()
    return tokens


class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.i = 0

    def peek(self, offset=0):
        index = self.i + offset
        return self.tokens[index].upper() if index < len(self.tokens) else ''

    def take(self):
        if self.i >= len(self.tokens):
            raise DatabaseError('Unexpected end of statement')
        token = self.tokens[self.i]
        self.i += 1
        return token

    def accept(self, token):
        if self.peek() == token:
            self.i += 1
            return True
        return False

    def expect(self, token):
        if not self.accept(token):
            raise DatabaseError(f"Expected {token}, got {self.peek() or 'end of statement'}")

    def name(self):
        token = self.take()
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', token):
            raise DatabaseError(f'Expected identifier, got {token}')
        return token.lower()

    def literal(self):
        signed = self.peek() in ('-', '+')
        sign = -1 if self.accept('-') else 1
        if sign == 1:
            self.accept('+')
        token = self.take()
        if token.startswith("'") and not signed:
            return token[1:-1].replace("''", "'")
        if token.upper() == 'NULL' and not signed:
            return None
        try:
            value = (float(token) if any(c in token.lower() for c in '.e') else int(token)) * sign
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError()
            return value
        except ValueError:
            raise DatabaseError(f'Expected a string, finite number, or NULL; got {token}') from None

    def expression(self):
        def atom():
            if self.accept('('):
                expr = self.expression()
                self.expect(')')
                return expr
            column = self.name()
            if self.accept('IS'):
                negate = self.accept('NOT')
                self.expect('NULL')
                return ('null', column, negate)
            op = self.take()
            if op not in ('=', '!=', '<>', '<', '<=', '>', '>='):
                raise DatabaseError(f'Unsupported comparison: {op}')
            return ('compare', column, op, self.literal())
        def conjunction():
            expr = atom()
            while self.accept('AND'):
                expr = ('and', expr, atom())
            return expr
        expr = conjunction()
        while self.accept('OR'):
            expr = ('or', expr, conjunction())
        return expr

    def where(self):
        return self.expression() if self.accept('WHERE') else None

    def statement(self):
        op = self.take().upper()
        if op in ('BEGIN', 'COMMIT', 'ROLLBACK'):
            self.accept('TRANSACTION')
            return (op,)
        if op == 'CREATE':
            self.expect('TABLE')
            name = self.name()
            self.expect('(')
            columns = []
            while True:
                col, kind = self.name(), self.take().upper()
                if kind not in ('INTEGER', 'REAL', 'TEXT'):
                    raise DatabaseError('Column type must be INTEGER, REAL, or TEXT')
                spec = dict(name=col, type=kind, primary=False, required=False)
                while self.peek() in ('PRIMARY', 'NOT'):
                    if self.accept('PRIMARY'):
                        self.expect('KEY')
                        spec['primary'] = spec['required'] = True
                    else:
                        self.expect('NOT')
                        self.expect('NULL')
                        spec['required'] = True
                columns.append(spec)
                if not self.accept(','):
                    break
            self.expect(')')
            return (op, name, columns)
        if op == 'DROP':
            self.expect('TABLE')
            return (op, self.name())
        if op == 'INSERT':
            self.expect('INTO')
            name, columns = self.name(), None
            if self.accept('('):
                columns = [self.name()]
                while self.accept(','):
                    columns.append(self.name())
                self.expect(')')
            self.expect('VALUES')
            rows = []
            while True:
                self.expect('(')
                row = [self.literal()]
                while self.accept(','):
                    row.append(self.literal())
                self.expect(')')
                rows.append(row)
                if not self.accept(','):
                    break
            return (op, name, columns, rows)
        if op == 'SELECT':
            count = self.peek() == 'COUNT' and self.peek(1) == '('
            if count:
                self.expect('COUNT')
                self.expect('(')
                self.expect('*')
                self.expect(')')
                columns = ['count(*)']
            elif self.accept('*'):
                columns = None
            else:
                columns = [self.name()]
                while self.accept(','):
                    columns.append(self.name())
            self.expect('FROM')
            name, where = self.name(), self.where()
            order, reverse, limit = None, False, None
            if self.accept('ORDER'):
                self.expect('BY')
                order = self.name()
                reverse = self.accept('DESC')
                if not reverse:
                    self.accept('ASC')
            if self.accept('LIMIT'):
                limit = self.literal()
                if type(limit) is not int or limit < 0:
                    raise DatabaseError('LIMIT must be a nonnegative integer')
            return (op, name, columns, where, order, reverse, limit, count)
        if op == 'UPDATE':
            name = self.name()
            self.expect('SET')
            values = {}
            while True:
                col = self.name()
                self.expect('=')
                if col in values:
                    raise DatabaseError(f'Duplicate assignment: {col}')
                values[col] = self.literal()
                if not self.accept(','):
                    break
            return (op, name, values, self.where())
        if op == 'DELETE':
            self.expect('FROM')
            return (op, self.name(), self.where())
        raise DatabaseError(f'Unsupported statement: {op}')


def matches(expr, row):
    if expr is None:
        return True
    kind = expr[0]
    if kind == 'and':
        return matches(expr[1], row) and matches(expr[2], row)
    if kind == 'or':
        return matches(expr[1], row) or matches(expr[2], row)
    value = row[expr[1]]
    if kind == 'null':
        return (value is not None) if expr[2] else (value is None)
    other, op = expr[3], expr[2]
    if value is None or other is None:
        return False
    if isinstance(value, str) != isinstance(other, str):
        raise DatabaseError('Cannot compare text and numeric values')
    if op == '=':
        return value == other
    if op in ('!=', '<>'):
        return value != other
    if op == '<':
        return value < other
    if op == '<=':
        return value <= other
    if op == '>':
        return value > other
    return value >= other


class Database:
    """Snapshot-based database. Each statement is atomic; batches are sequential."""
    def __init__(self, path=':memory:'):
        # Keep saves tied to the opened file if the caller changes directories.
        self.path = None if str(path) == ':memory:' else Path(path).resolve()
        self.tables = {}
        self._snapshot = None
        self._disk_version = None
        if self.path and self.path.exists():
            try:
                contents = self.path.read_bytes()
                data = json.loads(contents)
                if not isinstance(data, dict):
                    raise ValueError('Database must be an object')
                if data['format'] != 'sequelite-1' or not isinstance(data['tables'], dict):
                    raise ValueError('Unrecognized database format')
                self.tables = data['tables']
                for table in self.tables.values():
                    self._validate(table)
                self._disk_version = hashlib.sha256(contents).digest()
            except (OSError, ValueError, KeyError, TypeError, DatabaseError) as exc:
                raise DatabaseError(f'Cannot open database: {exc}') from exc

    @property
    def in_transaction(self):
        return self._snapshot is not None

    def _save(self):
        if self.path is None:
            return
        try:
            with writer_lock(self.path):
                try:
                    version = hashlib.sha256(self.path.read_bytes()).digest()
                except FileNotFoundError:
                    version = None
                if version != self._disk_version:
                    raise DatabaseError('Database changed since it was opened; reopen it before writing')
                self._write()
        except BlockingIOError as exc:
            raise DatabaseError(str(exc)) from exc
        except OSError as exc:
            raise DatabaseError(f'Cannot save database: {exc}') from exc

    def _write(self):
        """Replace the database while holding the writer lock."""
        temporary = None
        try:
            contents = json.dumps({'format': 'sequelite-1', 'tables': self.tables}, allow_nan=False).encode('utf-8')
            version = hashlib.sha256(contents).digest()
            with tempfile.NamedTemporaryFile(mode='wb', dir=self.path.parent, delete=False) as file:
                temporary = file.name
                file.write(contents)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
            self._disk_version = version
        except (OSError, ValueError) as exc:
            raise DatabaseError(f'Cannot save database: {exc}') from exc
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def execute(self, sql):
        parser = Parser(tokenize(sql))
        results = []
        while parser.peek():
            if parser.accept(';'):
                continue
            stmt = parser.statement()
            if parser.peek() and not parser.accept(';'):
                raise DatabaseError(f'Unexpected token: {parser.peek()}')
            before, snapshot = copy.deepcopy(self.tables), copy.deepcopy(self._snapshot)
            try:
                results.append(self._run(stmt))
            except Exception:
                self.tables, self._snapshot = before, snapshot
                raise
        return results

    def _table(self, name):
        if name not in self.tables:
            raise DatabaseError(f'No such table: {name}')
        return self.tables[name]

    @staticmethod
    def _columns(table, names):
        valid = {col['name'] for col in table['columns']}
        for name in names:
            if name not in valid:
                raise DatabaseError(f'No such column: {name}')

    def _expression_columns(self, table, expr):
        if expr is None:
            return
        if expr[0] in ('and', 'or'):
            self._expression_columns(table, expr[1])
            self._expression_columns(table, expr[2])
        else:
            self._columns(table, [expr[1]])

    @staticmethod
    def _validate(table):
        if not isinstance(table, dict) or not isinstance(table.get('columns'), list) or not isinstance(table.get('rows'), list):
            raise DatabaseError('Invalid table structure')
        columns = table['columns']
        for col in columns:
            if (not isinstance(col, dict) or not isinstance(col.get('name'), str)
                    or not re.fullmatch(r'[a-z_][a-z_0-9]*', col['name'])
                    or col.get('type') not in ('INTEGER', 'REAL', 'TEXT')
                    or type(col.get('primary')) is not bool
                    or type(col.get('required')) is not bool
                    or (col['primary'] and not col['required'])):
                raise DatabaseError('Invalid column schema')
        names = [c['name'] for c in columns]
        if not columns or len(names) != len(set(names)):
            raise DatabaseError('Column names must be unique and nonempty')
        if sum(c['primary'] for c in columns) > 1:
            raise DatabaseError('Only one PRIMARY KEY column is supported')
        for col in columns:
            seen = set()
            for row in table['rows']:
                if not isinstance(row, dict) or set(row) != set(names):
                    raise DatabaseError('Row does not match schema')
                value = row[col['name']]
                if value is None:
                    if col['required']:
                        raise DatabaseError(f"{col['name']} cannot be NULL")
                    continue
                valid = {'TEXT': type(value) is str, 'INTEGER': type(value) is int,
                         'REAL': type(value) in (int, float)}
                if not valid.get(col['type'], False):
                    raise DatabaseError(f"{col['name']} requires {col['type']}")
                if type(value) is float and not math.isfinite(value):
                    raise DatabaseError('Nonfinite number')
                if col['primary']:
                    if value in seen:
                        raise DatabaseError(f"Duplicate primary key: {value}")
                    seen.add(value)

    def _run(self, stmt):
        op = stmt[0]
        if op == 'BEGIN':
            if self.in_transaction:
                raise DatabaseError('A transaction is already active')
            self._snapshot = copy.deepcopy(self.tables)
            return Result(message='Transaction started')
        if op in ('COMMIT', 'ROLLBACK'):
            if not self.in_transaction:
                raise DatabaseError('No active transaction')
            if op == 'ROLLBACK':
                self.tables = self._snapshot
            else:
                self._save()
            self._snapshot = None
            return Result(message=f'Transaction {op.lower()} complete')
        name = stmt[1]
        result = Result()
        if op == 'CREATE':
            if name in self.tables:
                raise DatabaseError(f'Table already exists: {name}')
            table = {'columns': stmt[2], 'rows': []}
            self._validate(table)
            self.tables[name] = table
        elif op == 'DROP':
            self._table(name)
            del self.tables[name]
        else:
            table = self._table(name)
            names = [c['name'] for c in table['columns']]
            if op == 'INSERT':
                columns = stmt[2] if stmt[2] is not None else names
                self._columns(table, columns)
                if len(columns) != len(set(columns)):
                    raise DatabaseError('Duplicate insert column')
                for values in stmt[3]:
                    if len(values) != len(columns):
                        raise DatabaseError('Value count does not match column count')
                    row = dict.fromkeys(names)
                    row.update(zip(columns, values))
                    table['rows'].append(row)
                result.affected = len(stmt[3])
            elif op == 'SELECT':
                _, _, columns, where, order, reverse, limit, count = stmt
                self._expression_columns(table, where)
                if not count:
                    self._columns(table, columns or names)
                if order:
                    self._columns(table, [order])
                rows = [r for r in table['rows'] if matches(where, r)]
                if order:
                    rows.sort(key=lambda r: (r[order] is not None, r[order]), reverse=reverse)
                if count:
                    values = [[len(rows)]]
                else:
                    values = [[r[c] for c in (columns or names)] for r in rows]
                return Result(columns=columns or names, rows=values[:limit] if limit is not None else values)
            elif op == 'UPDATE':
                self._columns(table, stmt[2])
                self._expression_columns(table, stmt[3])
                for row in table['rows']:
                    if matches(stmt[3], row):
                        row.update(stmt[2])
                        result.affected += 1
            elif op == 'DELETE':
                self._expression_columns(table, stmt[2])
                kept = [r for r in table['rows'] if not matches(stmt[2], r)]
                result.affected = len(table['rows']) - len(kept)
                table['rows'] = kept
            self._validate(table)
        if not self.in_transaction:
            self._save()
        result.message = f'{op}: {result.affected} row(s) affected' if op in ('INSERT', 'UPDATE', 'DELETE') else f'{op} complete'
        return result
