"""
Modulo Importacao OFX
=====================
Upload de extrato bancario OFX, preview e importacao para o banco.
"""

from decimal import Decimal
from io import BytesIO

import pandas as pd
import streamlit as st
from ofxparse import OfxParser

from database import get_session
from models import TransacaoBancaria


def _parse_ofx(arquivo_bytes: bytes):
    """Parseia o conteudo do OFX e retorna lista de dicts com as transacoes."""
    ofx = OfxParser.parse(BytesIO(arquivo_bytes))
    transacoes = []
    for conta in ofx.accounts if hasattr(ofx, 'accounts') else [ofx.account]:
        for tx in conta.statement.transactions:
            tipo = "CREDIT" if tx.amount >= 0 else "DEBIT"
            descricao = tx.memo or tx.payee or tx.name or "Sem descricao"
            transacoes.append({
                "fitid": tx.id,
                "data": tx.date.date() if hasattr(tx.date, 'date') else tx.date,
                "descricao": str(descricao).strip(),
                "valor": Decimal(str(tx.amount)),
                "tipo": tipo,
            })
    return transacoes


def render():
    st.title("📂 Importacao OFX")

    tab_upload, tab_extrato = st.tabs(["Upload OFX", "Extrato Importado"])

    with tab_upload:
        _aba_upload()

    with tab_extrato:
        _aba_extrato()


# ──────────────────── aba: upload ───────────────────────────

def _aba_upload():
    st.subheader("Importar Extrato Bancario")
    st.caption("Faca upload do arquivo OFX da conta exclusiva do projeto.")

    arquivo = st.file_uploader(
        "Selecione o arquivo OFX",
        type=["ofx"],
        key="upload_ofx",
    )

    if arquivo is None:
        return

    # Parse do OFX
    try:
        conteudo = arquivo.read()
        transacoes = _parse_ofx(conteudo)
    except Exception as e:
        st.error(f"Erro ao ler o arquivo OFX: {e}")
        return

    if not transacoes:
        st.warning("Nenhuma transacao encontrada no arquivo.")
        return

    st.success(f"{len(transacoes)} transacao(oes) encontrada(s) no arquivo.")

    # Preview
    dados_preview = []
    for tx in transacoes:
        dados_preview.append({
            "Data": str(tx["data"]),
            "Descricao": tx["descricao"],
            "Valor": f"R$ {tx['valor']:,.2f}",
            "Tipo": tx["tipo"],
            "FITID": tx["fitid"],
        })

    st.dataframe(pd.DataFrame(dados_preview), use_container_width=True, hide_index=True)

    # Resumo rapido
    debitos = [tx for tx in transacoes if tx["tipo"] == "DEBIT"]
    creditos = [tx for tx in transacoes if tx["tipo"] == "CREDIT"]
    total_deb = sum(tx["valor"] for tx in debitos)
    total_cred = sum(tx["valor"] for tx in creditos)

    col1, col2, col3 = st.columns(3)
    col1.metric("Debitos", f"{len(debitos)}", f"R$ {total_deb:,.2f}")
    col2.metric("Creditos", f"{len(creditos)}", f"R$ {total_cred:,.2f}")
    col3.metric("Saldo Periodo", f"R$ {total_cred + total_deb:,.2f}")

    # Botao importar
    st.markdown("---")
    if st.button("Importar para o Banco de Dados", type="primary", key="btn_importar_ofx"):
        _importar_transacoes(transacoes)


def _importar_transacoes(transacoes: list):
    """Grava transacoes no banco, pulando duplicatas pelo fitid."""
    session = get_session()

    importadas = 0
    duplicadas = 0

    for tx in transacoes:
        existe = session.query(TransacaoBancaria).filter_by(fitid=tx["fitid"]).first()
        if existe:
            duplicadas += 1
            continue

        nova = TransacaoBancaria(
            fitid=tx["fitid"],
            data=tx["data"],
            descricao=tx["descricao"],
            valor=tx["valor"],
            tipo=tx["tipo"],
            conciliada=False,
        )
        session.add(nova)
        importadas += 1

    session.commit()
    session.close()

    if importadas > 0:
        st.success(f"{importadas} transacao(oes) importada(s) com sucesso!")
    if duplicadas > 0:
        st.info(f"{duplicadas} transacao(oes) ja existiam no banco (ignoradas).")
    if importadas == 0 and duplicadas > 0:
        st.warning("Todas as transacoes deste arquivo ja foram importadas anteriormente.")


# ──────────────────── aba: extrato ──────────────────────────

def _aba_extrato():
    st.subheader("Extrato Importado")

    session = get_session()

    # Filtro
    filtro = st.radio(
        "Filtrar por status",
        ["Todas", "Pendentes", "Conciliadas"],
        horizontal=True,
        key="filtro_extrato",
    )

    query = session.query(TransacaoBancaria).order_by(TransacaoBancaria.data.desc())

    if filtro == "Pendentes":
        query = query.filter(TransacaoBancaria.conciliada == False)
    elif filtro == "Conciliadas":
        query = query.filter(TransacaoBancaria.conciliada == True)

    transacoes = query.all()

    if not transacoes:
        st.info("Nenhuma transacao encontrada.")
        session.close()
        return

    dados = []
    for tx in transacoes:
        dados.append({
            "Data": str(tx.data),
            "Descricao": tx.descricao,
            "Valor": f"R$ {tx.valor:,.2f}",
            "Tipo": tx.tipo,
            "Status": "Conciliada" if tx.conciliada else "Pendente",
            "FITID": tx.fitid,
        })

    st.dataframe(pd.DataFrame(dados), use_container_width=True, hide_index=True)

    # Totalizadores
    total = sum(tx.valor for tx in transacoes)
    pendentes = sum(1 for tx in transacoes if not tx.conciliada)
    conciliadas = sum(1 for tx in transacoes if tx.conciliada)

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Transacoes", len(transacoes))
    col2.metric("Pendentes", pendentes)
    col3.metric("Conciliadas", conciliadas)

    session.close()
