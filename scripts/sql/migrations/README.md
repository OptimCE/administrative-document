# Migrations

There is no migration runner here. `../schema.sql` is the single source of truth
for the LOCAL database and is mirrored by hand in `shared/models/local_models.py`.
Production applies these files through `optimce-migrator`
(`migrations/administrative-document/`), which strips their `BEGIN;`/`COMMIT;`
and their `schema_version` INSERT.

The service is deployed, so every schema change is:

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

| version | where it lives | what |
|---|---|---|
| 1 | `schema.sql` (self-inserted) | initial schema |
| 2 | `seeds/0001`–`0003` (0003 inserts the row) | CWaPE deadline rules + template catalogue |
| 3 | `0003_procedures_engine.sql` | procedures engine; July dossiers converted |

**`schema.sql` self-inserts version 1 only**, although it embodies 3. Version 2
is reference data, not schema: a fresh database built from `schema.sql` has not
received it, and self-inserting 3 would make the migrator skip 2. So 0003 is
written to be a no-op for the schema on top of a fresh install.

`tests/test_migration_0003.py` applies 0003 to the schema as it was released
(`tests/sql/schema_before_0003.sql`, never edited), so a migration is tested on
the shape it actually upgrades. Do the same for the next one.
