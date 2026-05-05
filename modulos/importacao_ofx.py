"""
Modulo Importacao OFX
=====================
Upload de extrato bancario OFX, preview e importacao para o banco.
"""

import hashlib
import re
from decimal import Decimal
from io import BytesIO

import pandas as pd
import streamlit as st
from ofxparse import OfxParser

from database import get_session
from models import TransacaoBancaria


def _injetar_fitid_faltante(conteudo: bytes) -> bytes:
    """
    Alguns bancos brasileiros geram OFX sem o campo <FITID>, que e obrigatorio.
    Esta funcao detecta blocos <STMTTRN> sem FITID e gera um identificador
    deterministico (hash de data + valor + descricao + posicao) para que o
    parse funcione e a deduplicacao continue confiavel em reimportacoes.
    """
    try:
        texto = conteudo.decode("utf-8", errors="ignore")
    except Exception:
        texto = conteudo.decode("latin-1", errors="ignore")

    def _campo(bloco: str, tag: str) -> str:
        m = re.search(rf"<{tag}>([^<\r\n]*)", bloco, flags=re.IGNORECASE)
        return (m.group(1).strip() if m else "")

    contador = {"n": 0}

    def _processar_bloco(match):
        bloco = match.group(0)
        if re.search(r"<FITID>", bloco, flags=re.IGNORECASE):
            return bloco

        contador["n"] += 1
        chave = "|".join([
            _campo(bloco, "DTPOSTED"),
            _campo(bloco, "TRNAMT"),
            _campo(bloco, "MEMO"),
            _campo(bloco, "NAME"),
            _campo(bloco, "CHECKNUM"),
            _campo(bloco, "REFNUM"),
            str(contador["n"]),
        ])
        fitid = "AUTO" + hashlib.md5(chave.encode("utf-8")).hexdigest()[:20].upper()

        # Injeta o FITID logo apos a abertura do <STMTTRN>
        return re.sub(
            r"(<STMTTRN>)",
            r"\1<FITID>" + fitid,
            bloco,
            count=1,
            flags=re.IGNORECASE,
        )

    texto_corrigido = re.sub(
        r"<STMTTRN>.*?</STMTTRN>",
        _processar_bloco,
        texto,
        flags=re.IGNORECASE | re.DOTALL,
    )

    return texto_corrigido.encode("utf-8")


def _parse_ofx(arquivo_bytes: bytes):
    """Parseia o conteudo do OFX e retorna lista de dicts com as transacoes."""
    conteudo_corrigido = _injetar_fitid_faltante(arquivo_bytes)
    ofx = OfxParser.parse(BytesIO(conteudo_corrigido))
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
