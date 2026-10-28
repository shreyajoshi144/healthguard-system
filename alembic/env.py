
from logging.config import fileConfig
from alembic import context
from sqlalchemy import engine_from_config,pool
from db.models import Base
import os
config=context.config
if config.config_file_name: fileConfig(config.config_file_name)
url=os.getenv("DATABASE_URL",config.get_main_option("sqlalchemy.url","sqlite:///./healthguard.db"))
config.set_main_option("sqlalchemy.url",url.replace("%","%%"))
target_metadata=Base.metadata
def _widen_version_table(connection):
    """Revision ids here exceed Alembic's default VARCHAR(32) version column
    (e.g. 0009_sprint4_scope_write_permissions). SQLite ignores the length, so
    this only fails on PostgreSQL. Create/widen the table deterministically
    before any migration runs; idempotent and safe on already-migrated DBs."""
    if connection.dialect.name != "postgresql":
        return
    from sqlalchemy import text
    connection.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL, CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"))
    connection.execute(text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)"))
    connection.commit()

def run_migrations_offline():
    context.configure(url=url,target_metadata=target_metadata,literal_binds=True,compare_type=True)
    with context.begin_transaction(): context.run_migrations()
def run_migrations_online():
    connectable=engine_from_config(config.get_section(config.config_ini_section),prefix="sqlalchemy.",poolclass=pool.NullPool)
    with connectable.connect() as connection:
        _widen_version_table(connection)
        context.configure(connection=connection,target_metadata=target_metadata,compare_type=True)
        with context.begin_transaction(): context.run_migrations()
if context.is_offline_mode(): run_migrations_offline()
else: run_migrations_online()
