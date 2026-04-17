"""
Funcoes utilitarias com cache para reduzir queries ao banco.

Estrategia:
  - TTL de 30 segundos: compromisso entre frescor dos dados e performance.
  - Funcoes retornam dados "puros" (dicts, tuplas), nao objetos SQLAlchemy.
  - Invalidacao manual via invalidar_cache_cadastros() nos pontos CRUD.
"""

from decimal import Decimal

import streamlit as st

from database import get_session
from models import CategoriaDespesa, CentroCusto, Remessa, Tecnico


TTL_CACHE = 30  # segundos


@st.cache_data(ttl=TTL_CACHE)
def opcoes_categorias() -> dict:
    """Retorna dict {label: id} de categorias agrupadas por centro de custo."""
    session = get_session()
    try:
        categorias = (
            session.query(CategoriaDespesa)
            .join(CentroCusto)
            .order_by(CentroCusto.codigo, CategoriaDespesa.nome)
            .all()
        )
        return {
            f"{cat.centro_custo.codigo} | {cat.nome}": cat.id
            for cat in categorias
        }
    finally:
        session.close()


@st.cache_data(ttl=TTL_CACHE)
def cambio_medio_cached() -> Decimal:
    """Cambio medio ponderado das remessas recebidas, ou R$6,00 de projecao."""
    cambio_projecao = Decimal("6.00")
    session = get_session()
    try:
        remessas = session.query(Remessa).filter(Remessa.recebida == True).all()
        if not remessas:
            return cambio_projecao
        total_eur = sum((r.valor_eur for r in remessas), Decimal("0"))
        total_brl = sum(
            (r.valor_brl for r in remessas if r.valor_brl),
            Decimal("0"),
        )
        if not total_eur or total_eur == 0:
            return cambio_projecao
        return (total_brl / total_eur).quantize(Decimal("0.0001"))
    finally:
        session.close()


@st.cache_data(ttl=TTL_CACHE)
def tem_categorias() -> bool:
    """Verifica se existe pelo menos uma CategoriaDespesa cadastrada."""
    session = get_session()
    try:
        return session.query(CategoriaDespesa).first() is not None
    finally:
        session.close()


@st.cache_data(ttl=TTL_CACHE)
def tem_tecnicos_ativos() -> bool:
    """Verifica se existe pelo menos um Tecnico ativo."""
    session = get_session()
    try:
        return (
            session.query(Tecnico)
            .filter(Tecnico.ativo == True)
            .first()
        ) is not None
    finally:
        session.close()


def invalidar_cache_cadastros():
    """Limpa caches apos mutacoes em CC/Categoria/Remessa/Tecnico.
    Chame apos qualquer commit que altere essas entidades."""
    opcoes_categorias.clear()
    cambio_medio_cached.clear()
    tem_categorias.clear()
    tem_tecnicos_ativos.clear()
