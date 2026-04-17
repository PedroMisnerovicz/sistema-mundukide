import os
from sqlalchemy import create_engine, event, inspect, text
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
    # Pool configurado para Supabase Session Pooler:
    # - pool_pre_ping: testa conexao antes de usar (evita erros com conexoes mortas)
    # - pool_recycle: recicla conexoes a cada 5 min (antes do timeout do pooler)
    # - pool_size + max_overflow: reusa conexoes, reduzindo latencia
    engine = create_engine(
        _db_url,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=5,
    )
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


def _colunas(conn, tabela: str) -> set:
    """Retorna o conjunto de colunas de uma tabela (compativel SQLite/PostgreSQL)."""
    if _is_sqlite:
        result = conn.execute(text(f"PRAGMA table_info({tabela})"))
        return {row[1] for row in result.fetchall()}
    result = conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = :t"
    ), {"t": tabela})
    return {row[0] for row in result.fetchall()}


def _tabela_existe(conn, tabela: str) -> bool:
    """Verifica se uma tabela existe (compativel SQLite/PostgreSQL)."""
    if _is_sqlite:
        result = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"
        ), {"t": tabela})
    else:
        result = conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_name=:t"
        ), {"t": tabela})
    return result.fetchone() is not None


def _apply_migrations():
    """Adiciona colunas novas em tabelas existentes (ALTER TABLE) e faz backfills.
    Idempotente — pode rodar toda vez sem quebrar.
    Funciona tanto para SQLite quanto para PostgreSQL."""
    try:
        with engine.connect() as conn:
            # ── categorias_despesa ───────────────────────────
            cols = _colunas(conn, "categorias_despesa")
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

            # ── tecnicos ─────────────────────────────────────
            cols_tec = _colunas(conn, "tecnicos")
            if "custo_maximo" not in cols_tec:
                conn.execute(text(
                    "ALTER TABLE tecnicos ADD COLUMN custo_maximo NUMERIC(14,2)"
                ))

            # ── lancamentos_recorrentes (Fase 3 do refactor) ─
            if _tabela_existe(conn, "lancamentos_recorrentes"):
                cols_lr = _colunas(conn, "lancamentos_recorrentes")
                if "dia_pagamento_previsto" not in cols_lr:
                    conn.execute(text(
                        "ALTER TABLE lancamentos_recorrentes "
                        "ADD COLUMN dia_pagamento_previsto INTEGER"
                    ))

            # ── itens_despesa (Fase 1 do refactor) ───────────
            if _tabela_existe(conn, "itens_despesa"):
                cols_item = _colunas(conn, "itens_despesa")
                if "fornecedor_cliente" not in cols_item:
                    conn.execute(text(
                        "ALTER TABLE itens_despesa ADD COLUMN fornecedor_cliente VARCHAR(200)"
                    ))
                if "data_emissao" not in cols_item:
                    conn.execute(text(
                        "ALTER TABLE itens_despesa ADD COLUMN data_emissao DATE"
                    ))
                if "data_pagamento" not in cols_item:
                    conn.execute(text(
                        "ALTER TABLE itens_despesa ADD COLUMN data_pagamento DATE"
                    ))
                if "reembolso_id" not in cols_item:
                    conn.execute(text(
                        "ALTER TABLE itens_despesa ADD COLUMN reembolso_id INTEGER"
                    ))
                if "lancamento_recorrente_id" not in cols_item:
                    conn.execute(text(
                        "ALTER TABLE itens_despesa ADD COLUMN lancamento_recorrente_id INTEGER"
                    ))

                # Backfill: data_pagamento = data para registros antigos
                conn.execute(text(
                    "UPDATE itens_despesa SET data_pagamento = data "
                    "WHERE data_pagamento IS NULL AND data IS NOT NULL"
                ))

            conn.commit()
    except Exception:
        pass


def init_db():
    """Cria todas as tabelas se nao existirem e aplica migracoes."""
    from models import Base
    Base.metadata.create_all(bind=engine)
    _apply_migrations()
