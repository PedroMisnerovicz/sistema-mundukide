import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from pathlib import Path

DB_PATH = Path(__file__).parent / "mundukide.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, echo=False)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """Ativa foreign keys no SQLite (desabilitado por padrão)."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_session() -> Session:
    return SessionLocal()


def _apply_migrations():
    """Adiciona colunas novas em tabelas existentes (ALTER TABLE)."""
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(categorias_despesa)")
    cols = {row[1] for row in cursor.fetchall()}

    if "teto_brl" not in cols:
        cursor.execute(
            "ALTER TABLE categorias_despesa ADD COLUMN teto_brl NUMERIC(14,2)"
        )
    if "tipo_teto" not in cols:
        cursor.execute(
            "ALTER TABLE categorias_despesa ADD COLUMN tipo_teto VARCHAR(10)"
        )
    if "teto_eur" not in cols:
        cursor.execute(
            "ALTER TABLE categorias_despesa ADD COLUMN teto_eur NUMERIC(14,2)"
        )

    # ── Tabela tecnicos ──
    cursor.execute("PRAGMA table_info(tecnicos)")
    cols_tec = {row[1] for row in cursor.fetchall()}

    if "custo_maximo" not in cols_tec:
        cursor.execute(
            "ALTER TABLE tecnicos ADD COLUMN custo_maximo NUMERIC(14,2)"
        )

    conn.commit()
    conn.close()


def init_db():
    """Cria todas as tabelas se não existirem e aplica migrações."""
    from models import Base
    Base.metadata.create_all(bind=engine)
    _apply_migrations()
