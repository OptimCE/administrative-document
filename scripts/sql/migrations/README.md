# Migrations

There is no migration runner. `../schema.sql` is the single source of truth for
the LOCAL database and is mirrored by hand in `shared/models/local_models.py`.

**While the service is unreleased**, `schema.sql` is edited in place and there
are no migration files — a baseline copy here would only be a second thing to
keep in sync. `schema.sql` is written to be re-runnable (`CREATE TABLE IF NOT
EXISTS`, `CREATE OR REPLACE FUNCTION`, `DROP TRIGGER IF EXISTS` before each
`CREATE TRIGGER`), so applying it to an existing database is safe.

**Once the service is deployed**, every schema change becomes:

1. an edit to `schema.sql` (so a fresh install is correct), **and**
2. a matching model change in `shared/models/local_models.py`, **and**
3. a forward-only file here, named `000N_short_description.sql`, wrapped in
   `BEGIN; … COMMIT;` and written to be idempotent, ending with:

```sql
INSERT INTO schema_version (version, description)
VALUES (N, 'short description')
ON CONFLICT DO NOTHING;
```

Migrations are applied manually; they are never run automatically at boot.
