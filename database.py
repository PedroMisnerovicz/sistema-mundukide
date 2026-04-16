import os
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session
from pathlib import Path

# ── Conexao ──────────────────────────────────────────────────
# Se existir DATABASE_URL nos secrets do Streamlit, usa PostgreSQL (nuvem).
# Caso contrario, usa SQLite local (desenvolvimento).

_db_url = ""
try:
    import streamlit as st
    _db_url = st.secrets["database"]["url"]
except Exception:
    pass

if _db_url:
    # PostgreSQL (Supabase / nuvem)
    engine = create_engine(_db_url, echo=False)
    _is_sqlite = False
else:
    # SQLite local
    DB_PATH = Path(__file__).parent / "mundukide.db"
    engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
    _is_sqlite = True

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        """Ativa foreign keys no SQLite (desabilitado por padrao)."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_session() -> Session:
    return SessionLocal()


def _apply_migrations():
    """Adiciona colunas novas em tabelas existentes (ALTER TABLE).
    Funciona tanto para SQLite quanto para PostgreSQL."""
    try:
        with engine.connect() as conn:
            if _is_sqlite:
                result = conn.execute(text("PRAGMA table_info(categorias_despesa)"))
                cols = {row[1] for row in result.fetchall()}
            else:
                result = conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'categorias_despesa'"
                ))
                cols = {row[0] for row in result.fetchall()}

            if "teto_brl" not in cols:
                conn.execute(text(
                    "ALTER TABLE categorias_despesa ADD COLUMN teto_brl NUMERIC(14,2)"
                ))
            if "tipo_teto" not in cols:
                conn.execute(text(
                    "ALTER TABLE categorias_despesa ADD COLUMN tipo_teto VARCHAR(10)"
                ))
            if "teto_eur" not in cols:
                conn.execute(text(
                    "ALTER TABLE categorias_despesa ADD COLUMN teto_eur NUMERIC(14,2)"
                ))

            if _is_sqlite:
                result = conn.execute(text("PRAGMA table_info(tecnicos)"))
                cols_tec = {row[1] for row in result.fetchall()}
            else:
                result = conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'tecnicos'"
                ))
                cols_tec = {row[0] for row in result.fetchall()}

            if "custo_maximo" not in cols_tec:
                conn.execute(text(
                    "ALTER TABLE tecnicos ADD COLUMN custo_maximo NUMERIC(14,2)"
                ))

            conn.commit()
    except Exception:
        pass


def init_db():
    """Cria todas as tabelas se nao existirem e aplica migracoes."""
    from models import Base
    Base.metadata.create_all(bind=engine)
    _apply_migrations()
