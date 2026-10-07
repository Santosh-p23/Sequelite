import io
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from sequelite import Database, DatabaseError
from sequelite.cli import main


class ConditionLimitTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute('CREATE TABLE t (id INTEGER); INSERT INTO t VALUES (1), (2);')

    def test_supported_condition_boundaries(self):
        for operator in ('AND', 'OR'):
            condition = f' {operator} '.join(['id = 1'] * 256)
            with self.subTest(operator=operator):
                self.assertEqual(self.db.execute(f'SELECT * FROM t WHERE {condition}')[0].rows, [[1]])
        condition = '(' * 64 + ' OR '.join(['id = 1'] * 256) + ')' * 64
        self.assertEqual(self.db.execute(f'SELECT * FROM t WHERE {condition}')[0].rows, [[1]])

    def test_excessive_predicates_and_nesting_raise_database_error(self):
        for condition in (
            ' OR '.join(['id = 1'] * 257),
            ' AND '.join(['id IS NOT NULL'] * 1100),
            '(' * 65 + 'id = 1' + ')' * 65,
            '(' * 1100 + 'id = 1' + ')' * 1100,
            ' OR '.join(['(id = 1 AND id = 2)'] * 129),
        ):
            with self.subTest(length=len(condition)), self.assertRaisesRegex(DatabaseError, 'condition limit'):
                self.db.execute(f'SELECT * FROM t WHERE {condition}')

    def test_limit_resets_between_statements(self):
        condition = ' OR '.join(['id = 1'] * 256)
        query = f'SELECT * FROM t WHERE {condition};'
        results = self.db.execute(query + query)
        self.assertEqual([r.rows for r in results], [[[1]], [[1]]])

    def test_rejected_mutations_preserve_active_transaction(self):
        self.db.execute('BEGIN; INSERT INTO t VALUES (3)')
        condition = ' OR '.join(['id = 1'] * 1100)
        for prefix in ('UPDATE t SET id = 99', 'DELETE FROM t'):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(DatabaseError, 'condition limit'):
                self.db.execute(f'{prefix} WHERE {condition}')
            self.assertTrue(self.db.in_transaction)
            self.assertEqual(self.db.execute('SELECT * FROM t')[0].rows, [[1], [2], [3]])
        self.db.execute('ROLLBACK')
        self.assertEqual(self.db.execute('SELECT * FROM t')[0].rows, [[1], [2]])

    def test_cli_reports_error_without_traceback(self):
        condition = ' OR '.join(['id = 1'] * 1100)
        result = subprocess.run(
            [sys.executable, '-m', 'sequelite', '-c',
             f'CREATE TABLE t (id INTEGER); SELECT * FROM t WHERE {condition}'],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('condition limit', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_interactive_shell_continues_after_excessive_condition(self):
        condition = '(' * 1100 + 'id = 1' + ')' * 1100
        commands = ['CREATE TABLE t (id INTEGER);', 'BEGIN;', 'INSERT INTO t VALUES (1);',
                    f'DELETE FROM t WHERE {condition};', 'SELECT * FROM t;', 'COMMIT;', '.quit', EOFError()]
        output, errors = io.StringIO(), io.StringIO()
        with patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=commands), \
                patch('sys.stdout', output), patch('sys.stderr', errors):
            self.assertEqual(main(['--json']), 0)
        results = [json.loads(line) for line in output.getvalue().splitlines()[1:]]
        self.assertEqual(results[-2]['rows'], [[1]])
        self.assertEqual(results[-1]['message'], 'Transaction commit complete')
        self.assertIn('condition limit', errors.getvalue())
