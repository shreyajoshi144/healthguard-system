from __future__ import annotations
import os
from sqlalchemy import create_engine, event
from domain.observability import inc, observe
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

DATABASE_URL=os.getenv("DATABASE_URL","sqlite:///./healthguard.db")
IS_SQLITE=DATABASE_URL.startswith("sqlite")
IS_POSTGRES=DATABASE_URL.startswith(("postgresql://","postgresql+psycopg://","postgresql+psycopg2://"))
connect_args={"check_same_thread":False} if IS_SQLITE else {}
if IS_POSTGRES:
    connect_args={"connect_timeout": int(os.getenv("HEALTHGUARD_DB_CONNECT_TIMEOUT_SECONDS","10"))}
engine_kwargs={"future":True,"pool_pre_ping":True,"connect_args":connect_args}
if IS_POSTGRES:
    engine_kwargs.update({
        "pool_size": int(os.getenv("HEALTHGUARD_DB_POOL_SIZE","10")),
        "max_overflow": int(os.getenv("HEALTHGUARD_DB_MAX_OVERFLOW","10")),
        "pool_timeout": int(os.getenv("HEALTHGUARD_DB_POOL_TIMEOUT_SECONDS","30")),
        "pool_recycle": int(os.getenv("HEALTHGUARD_DB_POOL_RECYCLE_SECONDS","1800")),
    })
else:
    engine_kwargs["poolclass"]=StaticPool if ":memory:" in DATABASE_URL else None
engine=create_engine(DATABASE_URL, **{k:v for k,v in engine_kwargs.items() if v is not None})
SessionLocal=sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

def get_db():
    db=SessionLocal()
    try:
        yield db
    finally:
        db.close()

if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, connection_record):
        cursor=dbapi_connection.cursor(); cursor.execute("PRAGMA foreign_keys=ON"); cursor.close()


@event.listens_for(engine, "before_cursor_execute")
def _observe_db_start(conn, cursor, statement, parameters, context, executemany):
    context._healthguard_db_started = __import__('time').monotonic()

@event.listens_for(engine, "after_cursor_execute")
def _observe_db_success(conn, cursor, statement, parameters, context, executemany):
    started=getattr(context, "_healthguard_db_started", None)
    if started is not None:
        observe("healthguard_db_query_duration", (__import__('time').monotonic()-started)*1000, outcome="success")
    inc("healthguard_db_operations_total", outcome="success")

@event.listens_for(engine, "handle_error")
def _observe_db_error(exception_context):
    exc=exception_context.original_exception
    inc("healthguard_db_operations_total", outcome="failure", error_type=type(exc).__name__)
    return None
