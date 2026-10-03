"""Idempotent VetanKosh schema migration for SQLite/PostgreSQL."""
from sqlalchemy import inspect, text
from models import Base, engine

Base.metadata.create_all(bind=engine)

ADDITIONS = {
 "employers_by_tan": {
   "cit_tds_address": "VARCHAR", "cit_tds_city": "VARCHAR", "cit_tds_pincode": "VARCHAR",
   "is_verified": "BOOLEAN DEFAULT TRUE", "created_by_user_id": "VARCHAR"
 },
 "form16_generations": {
   "storage_path": "VARCHAR", "pdf_blob": ("BYTEA" if engine.dialect.name == "postgresql" else "BLOB"), "content_sha256": "VARCHAR", "version_no": "INTEGER DEFAULT 1", "is_latest": "BOOLEAN DEFAULT TRUE"
 }
}
with engine.begin() as conn:
    insp = inspect(conn)
    for table, cols in ADDITIONS.items():
        if table not in insp.get_table_names():
            continue
        existing = {c['name'] for c in insp.get_columns(table)}
        for name, ddl in cols.items():
            if name not in existing:
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
print('VetanKosh database migration complete.')
