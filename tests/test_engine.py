import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sequelite import Database, DatabaseError
from sequelite.cli import complete


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, price REAL, stock INTEGER);')

    def seed(self):
        self.db.execute("INSERT INTO items VALUES (1, 'Tea', 2.5, 8), (2, 'Coffee', 4, 0), (3, 'Water', NULL, 4);")

    def rows(self, sql='SELECT * FROM items'):
        return self.db.execute(sql)[0].rows

    def test_crud(self):
        self.seed()
        self.assertEqual(self.rows('SELECT name FROM items WHERE stock > 0 ORDER BY id DESC LIMIT 1'), [['Water']])
        self.assertEqual(self.db.execute("UPDATE items SET stock = 12 WHERE id = 1")[0].affected, 1)
        self.assertEqual(self.db.execute('DELETE FROM items WHERE stock = 0')[0].affected, 1)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM items'), [[2]])
        self.db.execute('DROP TABLE items')
        with self.assertRaises(DatabaseError):
            self.rows()

    def test_constraint_failure_is_atomic(self):
        self.seed()
        before = self.rows()
        for sql in ["INSERT INTO items VALUES (4, 'Good', 1, 1), (1, 'Duplicate', 2, 2)",
                    'UPDATE items SET id = 1', "INSERT INTO items (id) VALUES (4)",
                    "UPDATE items SET stock = 'bad'", 'INSERT INTO items (id, id) VALUES (4, 5)']:
            with self.subTest(sql=sql), self.assertRaises(DatabaseError):
                self.db.execute(sql)
            self.assertEqual(self.rows(), before)

    def test_expressions_and_null(self):
        self.seed()
        self.assertEqual(self.rows('SELECT id FROM items WHERE id = 1 OR id = 2 AND stock = 0'), [[1], [2]])
        self.assertEqual(self.rows('SELECT id FROM items WHERE (id = 1 OR id = 2) AND stock = 0'), [[2]])
        self.assertEqual(self.rows('SELECT id FROM items WHERE price IS NULL'), [[3]])
        self.assertEqual(self.rows('SELECT id FROM items WHERE price = NULL'), [])
        self.assertEqual(self.rows('SELECT id FROM items ORDER BY price'), [[3], [1], [2]])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM items LIMIT 0'), [])

    def test_select_column_named_count(self):
        self.db.execute('CREATE TABLE counters (id INTEGER, count INTEGER); '
                        'INSERT INTO counters VALUES (1, 7), (2, 3), (3, NULL);')
        for spelling in ('count', 'COUNT', 'CoUnT'):
            with self.subTest(spelling=spelling):
                result = self.db.execute(f'SELECT {spelling} FROM counters ORDER BY id')[0]
                self.assertEqual(result.columns, ['count'])
                self.assertEqual(result.rows, [[7], [3], [None]])
        self.assertEqual(
            self.rows('SELECT count, id FROM counters WHERE count >= 3 ORDER BY count LIMIT 1'),
            [[3, 2]],
        )
        self.assertEqual(self.rows('SELECT id, count FROM counters WHERE count IS NULL'), [[3, None]])
        self.assertEqual(self.rows('SELECT count FROM counters WHERE id = 99'), [])
        with self.assertRaisesRegex(DatabaseError, 'No such column: count'):
            self.db.execute('SELECT count FROM items')

    def test_count_aggregate_with_count_column(self):
        self.db.execute('CREATE TABLE counters (count INTEGER); '
                        'INSERT INTO counters VALUES (7), (3), (NULL);')
        for function in ('COUNT(*)', 'count (*)', 'CoUnT -- comment\n (*)'):
            with self.subTest(function=function):
                result = self.db.execute(f'SELECT {function} FROM counters')[0]
                self.assertEqual(result.columns, ['count(*)'])
                self.assertEqual(result.rows, [[3]])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM counters WHERE count >= 3'), [[2]])
        for function in ('COUNT()', 'COUNT(count)', 'COUNT('):
            with self.subTest(function=function), self.assertRaises(DatabaseError):
                self.db.execute(f'SELECT {function} FROM counters')

    def test_escaping_comments_and_multiple_statements(self):
        self.db.execute("-- comment\nINSERT INTO items (id, name) VALUES (1, 'It''s; tea'); SELECT * FROM items;")
        self.assertEqual(self.rows()[0], [1, "It's; tea", None, None])

    def test_invalid_sql(self):
        for sql in ['SELECT missing FROM items', 'SELECT * FROM items WHERE missing = 1',
                    'DELETE FROM items WHERE missing = 1', 'SELECT * FROM items LIMIT -1',
                    'SELECT * FROM items junk', 'CREATE TABLE bad (x INTEGER, x TEXT)',
                    'CREATE TABLE bad (x INTEGER PRIMARY KEY, y INTEGER PRIMARY KEY)',
                    'INSERT INTO items VALUES (1)', 'SELECT @ FROM items', "INSERT INTO items VALUES (1, +'bad', 1, 1)"]:
            with self.subTest(sql=sql), self.assertRaises(DatabaseError):
                self.db.execute(sql)

    def test_transactions_include_schema(self):
        self.seed()
        self.db.execute("BEGIN; DELETE FROM items; CREATE TABLE other (id INTEGER); ROLLBACK;")
        self.assertEqual(self.rows('SELECT COUNT(*) FROM items'), [[3]])
        self.assertNotIn('other', self.db.tables)
        self.db.execute('BEGIN; DELETE FROM items; COMMIT;')
        self.assertEqual(self.rows(), [])
        with self.assertRaises(DatabaseError):
            self.db.execute('COMMIT')
        self.db.execute('BEGIN')
        with self.assertRaises(DatabaseError):
            self.db.execute('BEGIN')
        self.db.execute('ROLLBACK')

    def test_persistence_commit_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.db'
            db = Database(path)
            db.execute('CREATE TABLE t (id INTEGER PRIMARY KEY); INSERT INTO t VALUES (1);')
            db.execute('BEGIN; INSERT INTO t VALUES (2);')
            self.assertEqual(Database(path).execute('SELECT * FROM t')[0].rows, [[1]])
            db.execute('COMMIT')
            self.assertEqual(Database(path).execute('SELECT * FROM t')[0].rows, [[1], [2]])
            db.execute('BEGIN; DROP TABLE t; ROLLBACK')
            self.assertIn('t', Database(path).tables)

    def test_relative_database_path_survives_directory_changes(self):
        original_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / 'first'
            second = Path(tmp) / 'second'
            first.mkdir()
            second.mkdir()
            other_path = second / 'data.db'
            other = Database(other_path)
            other.execute('CREATE TABLE t (id INTEGER); INSERT INTO t VALUES (99);')
            other_contents = other_path.read_bytes()
            try:
                os.chdir(first)
                db = Database('data.db')
                db.execute('CREATE TABLE t (id INTEGER); INSERT INTO t VALUES (1);')
                os.chdir(second)
                db.execute('INSERT INTO t VALUES (2);')
                self.assertEqual(
                    Database(first / 'data.db').execute('SELECT * FROM t')[0].rows,
                    [[1], [2]],
                )
                db.execute('BEGIN; INSERT INTO t VALUES (3); COMMIT;')
                self.assertEqual(
                    Database(first / 'data.db').execute('SELECT * FROM t')[0].rows,
                    [[1], [2], [3]],
                )
                self.assertEqual(other_path.read_bytes(), other_contents)
            finally:
                os.chdir(original_directory)

    def test_storage_failure_restores_memory(self):
        self.seed()
        with patch.object(self.db, '_save', side_effect=DatabaseError('disk full')):
            with self.assertRaises(DatabaseError):
                self.db.execute('DELETE FROM items')
        self.assertEqual(self.rows('SELECT COUNT(*) FROM items'), [[3]])
        self.db.execute('BEGIN; DELETE FROM items')
        with patch.object(self.db, '_save', side_effect=DatabaseError('disk full')):
            with self.assertRaises(DatabaseError):
                self.db.execute('COMMIT')
        self.assertTrue(self.db.in_transaction)
        self.db.execute('ROLLBACK')
        self.assertEqual(self.rows('SELECT COUNT(*) FROM items'), [[3]])

    def test_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.db'
            for content in ['not a database', '[]', '{"format":"sequelite-1","tables":{"t":{"columns":[null],"rows":[]}}}']:
                path.write_text(content)
                with self.assertRaises(DatabaseError):
                    Database(path)

    def test_shell_completion(self):
        self.assertFalse(complete("SELECT 'unfinished;"))
        self.assertTrue(complete("SELECT 'it''s; fine'; -- comment"))
        self.assertFalse(complete('-- comment;'))

    def test_cli_demo_and_error_status(self):
        process = subprocess.run([sys.executable, '-m', 'sequelite', '--json', '-f', 'examples/demo.sql'], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout.splitlines()[-1])['rows'], [[2]])
        process = subprocess.run([sys.executable, '-m', 'sequelite', '-c', 'SELECT * FROM absent'], capture_output=True, text=True)
        self.assertEqual(process.returncode, 1)
        self.assertIn('No such table', process.stderr)


if __name__ == '__main__':
    unittest.main()
