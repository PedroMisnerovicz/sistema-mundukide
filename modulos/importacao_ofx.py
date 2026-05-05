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
    st.subheader("Movimentacao da Conta Corrente")

    session = get_session()

    # Pega todas as transacoes para descobrir o intervalo de datas
    todas = session.query(TransacaoBancaria).order_by(TransacaoBancaria.data).all()

    if not todas:
        st.info("Nenhuma transacao importada. Faca upload do OFX primeiro.")
        session.close()
        return

    data_min = todas[0].data
    data_max = todas[-1].data

    # Filtros
    col_de, col_ate, col_status = st.columns([2, 2, 2])
    data_ini = col_de.date_input(
        "Data Inicial",
        value=data_min,
        min_value=data_min,
        max_value=data_max,
        key="extrato_data_ini",
        format="DD/MM/YYYY",
    )
    data_fim = col_ate.date_input(
        "Data Final",
        value=data_max,
        min_value=data_min,
        max_value=data_max,
        key="extrato_data_fim",
        format="DD/MM/YYYY",
    )
    filtro_status = col_status.selectbox(
        "Situacao",
        ["Todas", "Pendentes", "Conciliadas"],
        key="extrato_filtro_status",
    )

    if data_ini > data_fim:
        st.error("A data inicial nao pode ser maior que a data final.")
        session.close()
        return

    # Saldo acumulado ATE o dia anterior a data_ini (saldo de abertura do periodo)
    from datetime import timedelta as _td
    saldo_abertura = (
        session.query(TransacaoBancaria)
        .filter(TransacaoBancaria.data < data_ini)
        .all()
    )
    saldo_corrente = sum(tx.valor for tx in saldo_abertura)

    # Transacoes do periodo (ordem ascendente para acumular saldo corretamente)
    no_periodo = [
        tx for tx in todas
        if data_ini <= tx.data <= data_fim
    ]

    # Aplica filtro de status (mas o saldo continua acumulando todas)
    def _passa_filtro(tx):
        if filtro_status == "Pendentes":
            return not tx.conciliada
        if filtro_status == "Conciliadas":
            return tx.conciliada
        return True

    # Constroi linhas com saldo acumulado por linha + linhas de SALDO por dia
    linhas = []
    linhas.append({
        "Situacao": "",
        "Data": data_ini.strftime("%d/%m/%y"),
        "Lancamento": "SALDO ANTERIOR",
        "Categoria": "",
        "Valor (R$)": None,
        "Saldo (R$)": float(saldo_corrente),
    })

    dia_atual = None
    saldo_dia = saldo_corrente
    for tx in no_periodo:
        # Quando muda o dia, fecha o saldo do dia anterior (se houve movimento)
        if dia_atual is not None and tx.data != dia_atual:
            linhas.append({
                "Situacao": "",
                "Data": dia_atual.strftime("%d/%m/%y"),
                "Lancamento": "SALDO",
                "Categoria": "",
                "Valor (R$)": None,
                "Saldo (R$)": float(saldo_dia),
            })

        saldo_corrente += tx.valor
        saldo_dia = saldo_corrente
        dia_atual = tx.data

        if not _passa_filtro(tx):
            continue

        situacao = "✅ Conciliado" if tx.conciliada else "⏳ Pendente"

        # Tenta descobrir categoria (se houver split) ou tipo
        categoria_str = ""
        if tx.itens_despesa:
            cats = list({s.categoria_despesa.nome for s in tx.itens_despesa})
            categoria_str = " / ".join(cats[:2])
            if len(cats) > 2:
                categoria_str += f" (+{len(cats) - 2})"
        elif tx.tipo == "CREDIT":
            categoria_str = "Entrada"
        else:
            categoria_str = "—"

        linhas.append({
            "Situacao": situacao,
            "Data": tx.data.strftime("%d/%m/%y"),
            "Lancamento": tx.descricao,
            "Categoria": categoria_str,
            "Valor (R$)": float(tx.valor),
            "Saldo (R$)": float(saldo_corrente),
        })

    # Fecha o saldo do ultimo dia
    if dia_atual is not None:
        linhas.append({
            "Situacao": "",
            "Data": dia_atual.strftime("%d/%m/%y"),
            "Lancamento": "SALDO",
            "Categoria": "",
            "Valor (R$)": None,
            "Saldo (R$)": float(saldo_dia),
        })

    df = pd.DataFrame(linhas)

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Valor (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
            "Saldo (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
        },
    )

    # Resumo
    transacoes_filtradas = [tx for tx in no_periodo if _passa_filtro(tx)]
    total_periodo = sum(tx.valor for tx in no_periodo)
    pendentes = sum(1 for tx in no_periodo if not tx.conciliada)
    conciliadas = sum(1 for tx in no_periodo if tx.conciliada)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Saldo Abertura", f"R$ {sum(t.valor for t in saldo_abertura):,.2f}")
    col2.metric("Movimentacao", f"R$ {total_periodo:,.2f}")
    col3.metric("Saldo Final", f"R$ {saldo_corrente:,.2f}")
    col4.metric("Pendentes / Conciliadas", f"{pendentes} / {conciliadas}")

    session.close()
