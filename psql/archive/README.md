# psql/archive — PRE-BASELINE history (NOT executed)

These files are the schema's history from before 2026-07-13. **Nothing runs them.**
They are superseded by `psql/migrations/000_baseline_schema_dumped_from_live_database_2026_07_13.sql`.

## Why they were retired

They did not work. Verified on a virgin Postgres 16 (2026-07-13):

- **`init-db.sql`** died after **3 tables** — `ERROR: relation "devices" does not exist`
  (it referenced `devices` before creating it), and it referenced the `smarthome_anon`
  role **24 times** without ever creating it.
- Consequently **`004`, `008`, `009` all failed** on a fresh database.

The live database only existed because it **grew organically for months** via
`app.py`'s boot-DDL (schema-as-code) plus manual `psql` runs. **The schema had never
once been built from the repo's own files.** A fresh clone produced a database that
died after three tables — which is very plausibly why no MOBIUS installer ever worked.

## What replaced them

`migrations/000_baseline...sql` — **dumped from the live database with `pg_dump`**, i.e.
baselined from *reality*, per canonical SQL.1 and server-intercom's warning:
*"baseline ONLY what the live DB actually has — I baselined constraints the DB did not
have and the files silently diverged."*

Verified: baseline → 010 → 011 builds a fresh database that matches live exactly
(72 tables+views on both).

Kept for forensic reference only. Do not resurrect; write a new numbered migration.
