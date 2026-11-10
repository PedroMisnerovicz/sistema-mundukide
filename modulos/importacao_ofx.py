"""
Modulo Importacao OFX
=====================
Upload de extrato bancario OFX, preview e importacao para o banco.
"""

import hashlib
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal
from io import BytesIO

import pandas as pd
import streamlit as st
from ofxparse import OfxParser
from sqlalchemy import func

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


def _q2(valor) -> Decimal:
    return Decimal(str(valor)).quantize(Decimal("0.01"))


def _metadados_ofx(arquivo_bytes: bytes) -> dict:
    """Le do OFX o numero da conta e o saldo informado pelo banco.

    Retorna {"conta", "saldo", "data_saldo"} — qualquer um pode vir None se o
    banco nao preencher o campo.
    """
    texto = arquivo_bytes.decode("latin-1", errors="ignore")

    def _tag(tag: str, trecho: str):
        m = re.search(rf"<{tag}>([^<\r\n]*)", trecho, flags=re.IGNORECASE)
        return m.group(1).strip() if m and m.group(1).strip() else None

    conta = _tag("ACCTID", texto)
    saldo = None
    data_saldo = None
    bloco = re.search(
        r"<LEDGERBAL>(.*?)</LEDGERBAL>", texto, flags=re.IGNORECASE | re.DOTALL,
    )
    if bloco:
        bruto = _tag("BALAMT", bloco.group(1))
        if bruto:
            try:
                saldo = _q2(bruto.replace(",", "."))
            except Exception:
                saldo = None
        dt = _tag("DTASOF", bloco.group(1))
        if dt and len(dt) >= 8 and dt[:8].isdigit():
            try:
                data_saldo = datetime.strptime(dt[:8], "%Y%m%d").date()
            except ValueError:
                data_saldo = None

    return {"conta": conta, "saldo": saldo, "data_saldo": data_saldo}


def conta_de_referencia(session):
    """Conta (ACCTID) dos extratos ja importados — a mais frequente no banco.
    None enquanto nenhum extrato tiver a conta registrada."""
    linha = (
        session.query(TransacaoBancaria.conta_ofx, func.count(TransacaoBancaria.id))
        .filter(TransacaoBancaria.conta_ofx != None)
        .group_by(TransacaoBancaria.conta_ofx)
        .order_by(func.count(TransacaoBancaria.id).desc())
        .first()
    )
    return linha[0] if linha else None


def analisar_importacao(session, transacoes: list, meta: dict) -> dict:
    """Confere o arquivo contra o que ja esta no banco, ANTES de importar.

    Classifica cada transacao do arquivo em:
      - duplicadas: mesmo FITID ja importado (sempre ignoradas);
      - suspeitas:  FITID novo, mas ja existe no banco uma linha com a mesma
                    data, valor e descricao. Acontece quando o mesmo periodo e
                    exportado de novo com outro intervalo de datas — o banco nao
                    manda FITID e o identificador gerado muda;
      - novas:      o restante.

    Tambem devolve a conta de referencia e os saldos para conferencia.
    """
    fitids = [tx["fitid"] for tx in transacoes]
    ja_importados = set()
    if fitids:
        ja_importados = {
            f for (f,) in session.query(TransacaoBancaria.fitid)
            .filter(TransacaoBancaria.fitid.in_(fitids))
            .all()
        }

    duplicadas = [tx for tx in transacoes if tx["fitid"] in ja_importados]
    candidatas = [tx for tx in transacoes if tx["fitid"] not in ja_importados]

    novas, suspeitas = [], []
    if candidatas:
        data_ini = min(tx["data"] for tx in candidatas)
        data_fim = max(tx["data"] for tx in candidatas)
        no_banco = (
            session.query(TransacaoBancaria)
            .filter(
                TransacaoBancaria.data >= data_ini,
                TransacaoBancaria.data <= data_fim,
            )
            .all()
        )
        # Linhas do banco que NAO sao as mesmas do arquivo (FITID diferente)
        contagem = Counter(
            (t.data, _q2(t.valor), (t.descricao or "").strip())
            for t in no_banco
            if t.fitid not in ja_importados
        )
        for tx in candidatas:
            chave = (tx["data"], _q2(tx["valor"]), tx["descricao"].strip())
            if contagem[chave] > 0:
                contagem[chave] -= 1
                suspeitas.append(tx)
            else:
                novas.append(tx)

    saldo_banco = session.query(
        func.coalesce(func.sum(TransacaoBancaria.valor), 0)
    ).scalar()

    return {
        "novas": novas,
        "suspeitas": suspeitas,
        "duplicadas": duplicadas,
        "conta_referencia": conta_de_referencia(session),
        "conta_arquivo": meta.get("conta"),
        "saldo_sistema_atual": _q2(saldo_banco or 0),
    }


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
        conteudo = arquivo.getvalue()
        transacoes = _parse_ofx(conteudo)
        meta = _metadados_ofx(conteudo)
    except Exception as e:
        st.error(f"Erro ao ler o arquivo OFX: {e}")
        return

    if not transacoes:
        st.warning("Nenhuma transacao encontrada no arquivo.")
        return

    session = get_session()
    try:
        analise = analisar_importacao(session, transacoes, meta)
    finally:
        session.close()

    novas = analise["novas"]
    suspeitas = analise["suspeitas"]
    duplicadas = analise["duplicadas"]

    st.success(f"{len(transacoes)} transacao(oes) encontrada(s) no arquivo.")

    # Preview com a situacao de cada linha
    fit_susp = {tx["fitid"] for tx in suspeitas}
    fit_dup = {tx["fitid"] for tx in duplicadas}
    dados_preview = []
    for tx in transacoes:
        if tx["fitid"] in fit_dup:
            situacao = "Ja importada"
        elif tx["fitid"] in fit_susp:
            situacao = "⚠️ Possivel duplicata"
        else:
            situacao = "Nova"
        dados_preview.append({
            "Situacao": situacao,
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

    # ── Conferencias de seguranca ──
    st.markdown("---")
    st.markdown("**Conferencias antes de importar**")

    bloqueado = False

    # 1) Conta do arquivo x conta do projeto
    conta_arq = analise["conta_arquivo"]
    conta_ref = analise["conta_referencia"]
    if conta_ref and conta_arq and conta_arq != conta_ref:
        st.error(
            f"Este arquivo e da conta **{conta_arq}**, mas os extratos ja "
            f"importados sao da conta **{conta_ref}**. Provavelmente foi exportado "
            "o extrato de outra conta."
        )
        if not st.checkbox(
            "Tenho certeza de que este e o extrato da conta do projeto",
            key=f"ofx_forcar_conta_{conta_arq}",
        ):
            bloqueado = True
    elif conta_ref and not conta_arq:
        st.warning(
            "O arquivo nao informa o numero da conta — nao foi possivel conferir "
            f"se e a conta do projeto ({conta_ref})."
        )
    elif conta_arq and not conta_ref:
        st.info(
            f"Conta do arquivo: {conta_arq}. Ainda nao ha conta de referencia "
            "registrada; esta passara a ser a referencia."
        )
    elif conta_arq and conta_ref:
        st.success(f"Conta conferida: {conta_arq} (a mesma dos extratos anteriores).")
    else:
        st.warning("O arquivo nao informa o numero da conta — conferencia de conta ignorada.")

    # 2) Periodo ja importado
    if duplicadas:
        st.info(
            f"{len(duplicadas)} transacao(oes) deste arquivo ja foram importadas "
            "antes e serao ignoradas."
        )
    importar_suspeitas = False
    if suspeitas:
        st.warning(
            f"{len(suspeitas)} transacao(oes) parecem **ja estar no sistema** "
            "(mesma data, valor e descricao de linhas importadas de outro arquivo). "
            "Isso acontece quando o mesmo periodo e exportado de novo. "
            "Por seguranca elas NAO serao importadas."
        )
        importar_suspeitas = st.checkbox(
            "Importar tambem essas transacoes (sao lancamentos diferentes, "
            "nao duplicatas)",
            key="ofx_importar_suspeitas",
        )

    a_importar = novas + (suspeitas if importar_suspeitas else [])

    # 3) Saldo informado pelo banco x saldo do extrato no sistema
    if meta["saldo"] is not None:
        saldo_apos = analise["saldo_sistema_atual"] + sum(
            (_q2(tx["valor"]) for tx in a_importar), Decimal("0.00"),
        )
        diferenca = meta["saldo"] - saldo_apos
        data_txt = (
            meta["data_saldo"].strftime("%d/%m/%Y") if meta["data_saldo"] else "—"
        )
        ultima_data = max(tx["data"] for tx in transacoes)
        if diferenca == 0:
            st.success(
                f"Saldo conferido: o banco informa R$ {meta['saldo']:,.2f} em "
                f"{data_txt}, igual ao saldo do extrato no sistema apos a importacao."
            )
        else:
            st.warning(
                f"Saldo informado pelo banco em {data_txt}: **R$ {meta['saldo']:,.2f}**. "
                f"Saldo do extrato no sistema apos esta importacao: "
                f"**R$ {saldo_apos:,.2f}**. Diferenca: **R$ {diferenca:,.2f}**.  \n"
                f"A ultima transacao do arquivo e de {ultima_data.strftime('%d/%m/%Y')}. "
                "Se houve movimentacao depois dessa data ate a data do saldo, a "
                "diferenca e esperada e deve bater com esses movimentos. Caso "
                "contrario, pode estar faltando algum periodo no sistema."
            )

    # Botao importar
    st.markdown("---")
    if not a_importar:
        st.info("Nao ha transacoes novas neste arquivo para importar.")
        return
    if st.button(
        f"Importar {len(a_importar)} transacao(oes) para o Banco de Dados",
        type="primary",
        key="btn_importar_ofx",
        disabled=bloqueado,
    ):
        _importar_transacoes(a_importar, conta_arq)


def _importar_transacoes(transacoes: list, conta: str = None):
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
            conta_ofx=conta,
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
