import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sequelite import Database, DatabaseError
from sequelite.locking import writer_lock


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'data.db'
        self.db = Database(self.path)
        self.db.execute('CREATE TABLE t (id INTEGER); INSERT INTO t VALUES (1);')

    def rows(self):
        return Database(self.path).execute('SELECT * FROM t ORDER BY id')[0].rows

    def test_stale_writer_cannot_overwrite_newer_data(self):
        stale = Database(self.path)
        self.db.execute('INSERT INTO t VALUES (2)')
        with self.assertRaisesRegex(DatabaseError, 'Database changed'):
            stale.execute('INSERT INTO t VALUES (3)')
        self.assertEqual(stale.execute('SELECT * FROM t')[0].rows, [[1]])
        self.assertEqual(self.rows(), [[1], [2]])
        Database(self.path).execute('INSERT INTO t VALUES (3)')
        self.assertEqual(self.rows(), [[1], [2], [3]])

    def test_transaction_conflict_preserves_rollback(self):
        stale = Database(self.path)
        stale.execute('BEGIN; INSERT INTO t VALUES (3)')
        self.db.execute('INSERT INTO t VALUES (2)')
        with self.assertRaisesRegex(DatabaseError, 'Database changed'):
            stale.execute('COMMIT')
        self.assertTrue(stale.in_transaction)
        stale.execute('ROLLBACK')
        self.assertEqual(self.rows(), [[1], [2]])

    def test_competing_creation_does_not_replace_database(self):
        path = self.path.parent / 'new.db'
        first, second = Database(path), Database(path)
        first.execute('CREATE TABLE first (id INTEGER)')
        with self.assertRaisesRegex(DatabaseError, 'Database changed'):
            second.execute('CREATE TABLE second (id INTEGER)')
        self.assertEqual(set(Database(path).tables), {'first'})

    def test_busy_error_across_processes_and_retry(self):
        with writer_lock(self.path):
            result = subprocess.run(
                [sys.executable, '-m', 'sequelite', str(self.path), '-c', 'INSERT INTO t VALUES (2)'],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn('Database is busy', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(self.rows(), [[1]])
        self.db.execute('INSERT INTO t VALUES (2)')
        self.assertEqual(self.rows(), [[1], [2]])

    def test_failed_save_releases_lock(self):
        with patch('sequelite.engine.os.replace', side_effect=OSError('disk error')):
            with self.assertRaisesRegex(DatabaseError, 'disk error'):
                self.db.execute('INSERT INTO t VALUES (2)')
        self.db.execute('INSERT INTO t VALUES (3)')
        self.assertEqual(self.rows(), [[1], [3]])

    def test_process_exit_releases_lock(self):
        script = '''
import os, sys
from sequelite.locking import writer_lock
with writer_lock(sys.argv[1]):
    os._exit(0)
'''
        subprocess.run([sys.executable, '-c', script, str(self.path)], check=True, timeout=10)
        self.db.execute('INSERT INTO t VALUES (2)')
        self.assertEqual(self.rows(), [[1], [2]])

    def test_two_processes_with_same_snapshot_only_one_succeeds(self):
        script = '''
import sys
from sequelite import Database, DatabaseError
db = Database(sys.argv[1])
print('ready', flush=True)
sys.stdin.readline()
try:
    db.execute('INSERT INTO t VALUES (' + sys.argv[2] + ')')
    print('saved')
except DatabaseError as exc:
    print(str(exc))
'''
        workers = [subprocess.Popen(
            [sys.executable, '-c', script, str(self.path), str(value)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) for value in (2, 3)]
        try:
            for worker in workers:
                self.assertEqual(worker.stdout.readline().strip(), 'ready')
            for worker in workers:
                worker.stdin.write('\n')
                worker.stdin.flush()
            results = [worker.communicate(timeout=10) for worker in workers]
            outputs = [output.strip() for output, _ in results]
            self.assertEqual(outputs.count('saved'), 1, outputs)
            self.assertTrue(any('Database changed' in output or 'Database is busy' in output for output in outputs))
            self.assertTrue(all(not error for _, error in results), results)
            self.assertIn(self.rows(), ([[1], [2]], [[1], [3]]))
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate()
