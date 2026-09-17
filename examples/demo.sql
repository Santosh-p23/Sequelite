-- Run with: python3 -m sequelite :memory: -f examples/demo.sql
CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT, priority INTEGER);
INSERT INTO tasks VALUES (1, 'Build a SQL parser', 'done', 3), (2, 'Add persistence', 'done', 2), (3, 'Write documentation', 'todo', 1);
SELECT title, priority FROM tasks WHERE status = 'done' ORDER BY priority DESC;
BEGIN;
UPDATE tasks SET status = 'done' WHERE id = 3;
SELECT COUNT(*) FROM tasks WHERE status = 'done';
ROLLBACK;
SELECT * FROM tasks;
DELETE FROM tasks WHERE id = 1;
SELECT COUNT(*) FROM tasks;
