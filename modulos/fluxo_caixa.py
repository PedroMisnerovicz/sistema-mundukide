"""
Modulo Fluxo de Caixa
=====================
Visao mes a mes do fluxo de caixa do projeto, do mes atual ate dezembro/2027.
Alimentado pelos lancamentos (ItemDespesa), remessas (Remessa) e
projecoes de lancamentos recorrentes (LancamentoRecorrente).
"""

from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st
from sqlalchemy import extract, func as sqlfunc

from database import get_session
from models import (
    CategoriaDespesa,
    CentroCusto,
    ItemDespesa,
    LancamentoRecorrente,
    Remessa,
)


def _gerar_meses(inicio: date, fim: date) -> list:
    """Retorna lista de tuplas (ano, mes) do inicio ao fim (inclusive)."""
    meses = []
    ano, mes = inicio.year, inicio.month
    while (ano, mes) <= (fim.year, fim.month):
        meses.append((ano, mes))
        mes += 1
        if mes > 12:
            mes = 1
            ano += 1
    return meses


def _gastos_por_mes(session) -> dict:
    """Retorna dict {(ano, mes): total_gastos} de ItemDespesa."""
    resultados = (
        session.query(
            extract("year", ItemDespesa.data).label("ano"),
            extract("month", ItemDespesa.data).label("mes"),
            sqlfunc.sum(ItemDespesa.valor_brl).label("total"),
        )
        .group_by("ano", "mes")
        .all()
    )
    return {
        (int(r.ano), int(r.mes)): Decimal(str(r.total))
        for r in resultados
    }


def _entradas_por_mes(session) -> dict:
    """Retorna dict {(ano, mes): total_entradas} de remessas recebidas."""
    remessas = (
        session.query(Remessa)
        .filter(Remessa.recebida == True, Remessa.data_recebimento != None)
        .all()
    )
    entradas = {}
    for r in remessas:
        if r.valor_brl and r.data_recebimento:
            chave = (r.data_recebimento.year, r.data_recebimento.month)
            entradas[chave] = entradas.get(chave, Decimal("0.00")) + r.valor_brl
    return entradas


def _projecao_recorrentes(session, meses: list) -> dict:
    """
    Para cada mes na lista, calcula o total projetado de lancamentos recorrentes.
    Retorna dict {(ano, mes): total_projetado}.
    So inclui meses futuros (a partir do mes atual).
    """
    recorrentes = (
        session.query(LancamentoRecorrente)
        .filter(LancamentoRecorrente.ativo == True)
        .all()
    )

    projecao = {}
    hoje = date.today()

    for ano, mes in meses:
        # So projeta a partir do mes atual
        if (ano, mes) < (hoje.year, hoje.month):
            continue

        total_mes = Decimal("0.00")
        for lr in recorrentes:
            if _recorrente_ocorre_no_mes(lr, ano, mes):
                total_mes += lr.valor_brl

        if total_mes > 0:
            projecao[(ano, mes)] = total_mes

    return projecao


def _recorrente_ocorre_no_mes(lr: LancamentoRecorrente, ano: int, mes: int) -> bool:
    """Verifica se um lancamento recorrente ocorre no mes/ano dado."""
    # Verifica se esta dentro do periodo
    if date(ano, mes, 1) < date(lr.data_inicio.year, lr.data_inicio.month, 1):
        return False
    if date(ano, mes, 1) > date(lr.data_fim.year, lr.data_fim.month, 1):
        return False

    if lr.frequencia == "MENSAL":
        return True
    elif lr.frequencia == "TRIMESTRAL":
        # Ocorre a cada 3 meses a partir da data de inicio
        meses_desde = (ano - lr.data_inicio.year) * 12 + (mes - lr.data_inicio.month)
        return meses_desde % 3 == 0
    elif lr.frequencia == "ANUAL":
        # Ocorre uma vez por ano no mesmo mes da data de inicio
        return mes == lr.data_inicio.month
    return False


def render():
    st.title("Fluxo de Caixa")

    session = get_session()

    # Periodo: mes atual ate dezembro/2027
    hoje = date.today()
    inicio = date(hoje.year, hoje.month, 1)
    fim = date(2027, 12, 31)
    meses = _gerar_meses(inicio, fim)

    # Dados reais
    gastos_reais = _gastos_por_mes(session)
    entradas_reais = _entradas_por_mes(session)

    # Incluir meses passados que tenham dados
    todos_meses_com_dados = set(gastos_reais.keys()) | set(entradas_reais.keys())
    meses_passados = [
        m for m in sorted(todos_meses_com_dados)
        if m < (hoje.year, hoje.month)
    ]
    meses_completos = meses_passados + meses

    # Projecao de recorrentes
    projecao = _projecao_recorrentes(session, meses_completos)

    # Monta tabela
    saldo_acumulado = Decimal("0.00")
    linhas = []

    for ano, mes in meses_completos:
        chave = (ano, mes)
        entradas = entradas_reais.get(chave, Decimal("0.00"))
        saidas_reais = gastos_reais.get(chave, Decimal("0.00"))

        # Para meses futuros, soma projecao de recorrentes
        eh_futuro = chave >= (hoje.year, hoje.month)
        saidas_projetadas = projecao.get(chave, Decimal("0.00")) if eh_futuro else Decimal("0.00")

        # Total saidas: reais + projetadas (evita duplicar)
        # Para o mes atual, soma ambos
        # Para meses passados, so reais
        # Para meses futuros sem dados reais, so projecao
        if eh_futuro:
            saidas_total = saidas_reais + saidas_projetadas
        else:
            saidas_total = saidas_reais

        saldo_mes = entradas - saidas_total
        saldo_acumulado += saldo_mes

        tipo = "Realizado" if chave < (hoje.year, hoje.month) else (
            "Atual" if chave == (hoje.year, hoje.month) else "Projecao"
        )

        linhas.append({
            "Mes/Ano": f"{mes:02d}/{ano}",
            "Entradas": entradas,
            "Saidas": saidas_total,
            "Saldo Mes": saldo_mes,
            "Saldo Acumulado": saldo_acumulado,
            "Tipo": tipo,
        })

    if not linhas:
        st.info("Nenhum dado disponivel para o fluxo de caixa.")
        session.close()
        return

    # Exibe metricas
    total_entradas = sum(l["Entradas"] for l in linhas)
    total_saidas = sum(l["Saidas"] for l in linhas)

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Entradas", f"R$ {total_entradas:,.2f}")
    col2.metric("Total Saidas", f"R$ {total_saidas:,.2f}")
    col3.metric("Saldo Final Projetado", f"R$ {saldo_acumulado:,.2f}")

    st.markdown("---")

    # Tabela formatada
    dados_display = []
    for l in linhas:
        dados_display.append({
            "Mes/Ano": l["Mes/Ano"],
            "Entradas": f"R$ {l['Entradas']:,.2f}",
            "Saidas": f"R$ {l['Saidas']:,.2f}",
            "Saldo Mes": f"R$ {l['Saldo Mes']:,.2f}",
            "Saldo Acumulado": f"R$ {l['Saldo Acumulado']:,.2f}",
            "Tipo": l["Tipo"],
        })

    st.dataframe(
        pd.DataFrame(dados_display),
        use_container_width=True,
        hide_index=True,
        height=min(600, len(dados_display) * 36 + 38),
    )

    # Detalhamento do mes selecionado
    st.markdown("---")
    st.subheader("Detalhamento por Mes")

    opcoes_mes = [l["Mes/Ano"] for l in linhas]
    # Default: mes atual
    idx_atual = next(
        (i for i, l in enumerate(linhas) if l["Tipo"] == "Atual"), 0
    )
    mes_sel = st.selectbox("Selecione o mes", opcoes_mes, index=idx_atual, key="sel_mes_fc")

    # Parse do mes selecionado
    m_sel, a_sel = int(mes_sel[:2]), int(mes_sel[3:])

    # Lancamentos reais do mes
    itens_mes = (
        session.query(ItemDespesa)
        .join(CategoriaDespesa)
        .join(CentroCusto)
        .filter(
            extract("year", ItemDespesa.data) == a_sel,
            extract("month", ItemDespesa.data) == m_sel,
        )
        .order_by(ItemDespesa.data)
        .all()
    )

    if itens_mes:
        st.markdown("**Lancamentos Realizados:**")
        dados_itens = []
        for item in itens_mes:
            cat = item.categoria_despesa
            dados_itens.append({
                "Data": str(item.data),
                "Descricao": item.descricao or "—",
                "Categoria": f"{cat.centro_custo.codigo} | {cat.nome}",
                "Valor": f"R$ {item.valor_brl:,.2f}",
                "Status": "Conciliado" if item.conciliado else "Pendente",
            })
        st.dataframe(pd.DataFrame(dados_itens), use_container_width=True, hide_index=True)
    else:
        st.caption("Nenhum lancamento realizado neste mes.")

    # Lancamentos recorrentes projetados para o mes
    recorrentes_mes = (
        session.query(LancamentoRecorrente)
        .filter(LancamentoRecorrente.ativo == True)
        .all()
    )
    rec_do_mes = [
        lr for lr in recorrentes_mes
        if _recorrente_ocorre_no_mes(lr, a_sel, m_sel)
    ]

    if rec_do_mes and (a_sel, m_sel) >= (hoje.year, hoje.month):
        st.markdown("**Lancamentos Recorrentes Projetados:**")
        dados_rec = []
        for lr in rec_do_mes:
            cat = lr.categoria_despesa
            tec = lr.tecnico
            dados_rec.append({
                "Descricao": lr.descricao,
                "Valor": f"R$ {lr.valor_brl:,.2f}",
                "Categoria": f"{cat.centro_custo.codigo} | {cat.nome}",
                "Frequencia": lr.frequencia,
                "Tecnico": tec.nome if tec else "—",
            })
        st.dataframe(pd.DataFrame(dados_rec), use_container_width=True, hide_index=True)

    # Entradas do mes (remessas)
    remessas_mes = (
        session.query(Remessa)
        .filter(
            Remessa.recebida == True,
            extract("year", Remessa.data_recebimento) == a_sel,
            extract("month", Remessa.data_recebimento) == m_sel,
        )
        .all()
    )

    if remessas_mes:
        st.markdown("**Entradas (Remessas):**")
        dados_rem = []
        for r in remessas_mes:
            dados_rem.append({
                "Remessa": f"Remessa {r.numero}",
                "Valor BRL": f"R$ {r.valor_brl:,.2f}",
                "Valor EUR": f"EUR {r.valor_eur:,.2f}",
                "Cambio": f"R$ {r.cambio_efetivado:,.4f}" if r.cambio_efetivado else "—",
                "Data": str(r.data_recebimento),
            })
        st.dataframe(pd.DataFrame(dados_rem), use_container_width=True, hide_index=True)

    session.close()
