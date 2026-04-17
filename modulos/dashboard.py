"""
Modulo Dashboard
================
Visao gerencial do projeto com:
  - Metricas consolidadas (EUR/BRL)
  - Termometro de Liberacao (regra dos 80% — FIFO)
  - Execucao por Centro de Custo (graficos)
  - Teto por Categoria (novo)
  - Detalhamento por Categoria
  - Exportacao PDF bilíngue (PT/ES)
"""

import io
from datetime import date
from decimal import Decimal

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from fpdf import FPDF
from sqlalchemy import extract, func as sqlfunc

from database import get_session
from models import (
    CentroCusto,
    CategoriaDespesa,
    ItemDespesa,
    Remessa,
)

CAMBIO_PROJECAO = Decimal("6.00")


# ──────────────── Dicionario de Traducoes ──────────────────

TRADUCOES = {
    "pt": {
        "titulo": "Dashboard - Controle Financeiro Mundukide",
        "gerado_em": "Gerado em",
        "visao_geral": "Visao Geral do Projeto",
        "projeto_eur": "Projeto (EUR)",
        "projeto_brl": "Projeto (BRL)",
        "cambio_medio": "Cambio Medio",
        "total_gasto": "Total Gasto",
        "saldo_conta": "Saldo em Conta",
        "termometro": "Termometro de Liberacao - Regra dos 80%",
        "remessa": "Remessa",
        "recebida": "Recebida",
        "projecao": "Projecao",
        "executado": "executado",
        "elegivel": "ELEGIVEL",
        "falta": "Falta",
        "para_liberar": "para liberar",
        "aguardando": "Aguardando execucao de 80% da",
        "execucao_cc": "Execucao por Centro de Custo",
        "teto_brl_label": "Teto BRL",
        "gasto_brl_label": "Gasto BRL",
        "estourou": "ESTOUROU teto em",
        "proximo_teto": "Proximo do teto!",
        "detalhamento": "Detalhamento por Categoria",
        "categoria": "Categoria",
        "pct_cc": "% do CC",
        "teto_label": "Teto",
        "gasto_label": "Gasto",
        "saldo_label": "Saldo",
        "nenhuma_cat": "Nenhuma categoria cadastrada.",
        "nenhuma_remessa": "Nenhuma remessa cadastrada.",
        "teto_categoria": "Teto por Categoria",
        "realizado_label": "Realizado",
        "disponivel_label": "Disponivel",
        "tipo_mensal": "mensal",
        "tipo_global": "global",
        "nenhuma_remessa_cad": "Nenhuma remessa cadastrada.",
        "moeda": "R$",
    },
    "es": {
        "titulo": "Dashboard - Control Financiero Mundukide",
        "gerado_em": "Generado el",
        "visao_geral": "Vision General del Proyecto",
        "projeto_eur": "Proyecto (EUR)",
        "projeto_brl": "Proyecto (BRL)",
        "cambio_medio": "Tipo de Cambio Promedio",
        "total_gasto": "Total Gastado",
        "saldo_conta": "Saldo en Cuenta",
        "termometro": "Termometro de Liberacion - Regla del 80%",
        "remessa": "Remesa",
        "recebida": "Recibida",
        "projecao": "Proyeccion",
        "executado": "ejecutado",
        "elegivel": "ELEGIBLE",
        "falta": "Falta",
        "para_liberar": "para liberar",
        "aguardando": "Esperando ejecucion del 80% de la",
        "execucao_cc": "Ejecucion por Centro de Costo",
        "teto_brl_label": "Tope BRL",
        "gasto_brl_label": "Gasto BRL",
        "estourou": "SUPERO el tope en",
        "proximo_teto": "Proximo al tope!",
        "detalhamento": "Detalle por Categoria",
        "categoria": "Categoria",
        "pct_cc": "% del CC",
        "teto_label": "Tope",
        "gasto_label": "Gasto",
        "saldo_label": "Saldo",
        "nenhuma_cat": "Ninguna categoria registrada.",
        "nenhuma_remessa": "Ninguna remesa registrada.",
        "teto_categoria": "Tope por Categoria",
        "realizado_label": "Realizado",
        "disponivel_label": "Disponible",
        "tipo_mensal": "mensual",
        "tipo_global": "global",
        "nenhuma_remessa_cad": "Ninguna remesa registrada.",
        "moeda": "R$",
    },
}


def _t(chave, idioma="pt"):
    return TRADUCOES.get(idioma, TRADUCOES["pt"]).get(chave, chave)


# ───────────────────────── helpers ──────────────────────────

def _cambio_medio(session) -> Decimal:
    remessas = (
        session.query(Remessa)
        .filter(Remessa.recebida == True)
        .all()
    )
    if not remessas:
        return CAMBIO_PROJECAO
    total_eur = sum(r.valor_eur for r in remessas)
    total_brl = sum(r.valor_brl for r in remessas)
    if total_eur == 0:
        return CAMBIO_PROJECAO
    return (total_brl / total_eur).quantize(Decimal("0.0001"))


def _total_gasto_brl(session) -> Decimal:
    resultado = (
        session.query(sqlfunc.coalesce(sqlfunc.sum(ItemDespesa.valor_brl), 0))
        .scalar()
    )
    return Decimal(str(resultado))


def _gasto_por_cc(session, cc_id: int) -> Decimal:
    resultado = (
        session.query(sqlfunc.coalesce(sqlfunc.sum(ItemDespesa.valor_brl), 0))
        .join(CategoriaDespesa)
        .filter(CategoriaDespesa.centro_custo_id == cc_id)
        .scalar()
    )
    return Decimal(str(resultado))


def _gasto_por_categoria(session, cat_id: int) -> Decimal:
    resultado = (
        session.query(sqlfunc.coalesce(sqlfunc.sum(ItemDespesa.valor_brl), 0))
        .filter(ItemDespesa.categoria_despesa_id == cat_id)
        .scalar()
    )
    return Decimal(str(resultado))


def _gasto_cat_mes_atual(session, cat_id: int) -> Decimal:
    hoje = date.today()
    resultado = (
        session.query(sqlfunc.coalesce(sqlfunc.sum(ItemDespesa.valor_brl), 0))
        .filter(
            ItemDespesa.categoria_despesa_id == cat_id,
            extract("year", ItemDespesa.data) == hoje.year,
            extract("month", ItemDespesa.data) == hoje.month,
        )
        .scalar()
    )
    return Decimal(str(resultado))


def _categorias_com_teto(session):
    """Retorna categorias que possuem teto configurado (EUR ou BRL legado)."""
    return (
        session.query(CategoriaDespesa)
        .join(CentroCusto)
        .filter(
            (CategoriaDespesa.teto_eur != None) | (CategoriaDespesa.teto_brl != None),
            CategoriaDespesa.tipo_teto != None,
        )
        .order_by(CentroCusto.codigo, CategoriaDespesa.nome)
        .all()
    )


# ──────────────────── geracao de PDF ───────────────────────

def _pdf_section_title(pdf, titulo):
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(44, 62, 80)
    pdf.cell(0, 10, titulo, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)


def _pdf_alert(pdf, texto, tipo):
    if tipo == "success":
        pdf.set_fill_color(234, 250, 241)
        pdf.set_text_color(39, 174, 96)
    elif tipo == "warning":
        pdf.set_fill_color(254, 249, 231)
        pdf.set_text_color(211, 136, 0)
    else:
        pdf.set_fill_color(253, 236, 234)
        pdf.set_text_color(231, 76, 60)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 7, f"  {texto}", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(1)


def _pdf_metricas(pdf, remessas, cambio, total_gasto, lang):
    _pdf_section_title(pdf, _t("visao_geral", lang))

    total_eur = sum(r.valor_eur for r in remessas if r.valor_eur)
    total_brl = Decimal("0.00")
    total_recebido_brl = Decimal("0.00")
    for r in remessas:
        if r.recebida and r.valor_brl:
            total_brl += r.valor_brl
            total_recebido_brl += r.valor_brl
        elif r.valor_eur:
            total_brl += (r.valor_eur * CAMBIO_PROJECAO).quantize(Decimal("0.01"))
    saldo = total_recebido_brl - total_gasto

    col_w = (pdf.w - 20) / 5
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(52, 152, 219)
    pdf.set_text_color(255, 255, 255)
    for h in [_t("projeto_eur", lang), _t("projeto_brl", lang),
              _t("cambio_medio", lang), _t("total_gasto", lang),
              _t("saldo_conta", lang)]:
        pdf.cell(col_w, 8, h, border=1, align="C", fill=True)
    pdf.ln()

    m = _t("moeda", lang)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 10)
    for v in [f"EUR {total_eur:,.2f}", f"{m} {total_brl:,.2f}",
              f"{m} {cambio:,.4f}", f"{m} {total_gasto:,.2f}",
              f"{m} {saldo:,.2f}"]:
        pdf.cell(col_w, 8, v, border=1, align="C")
    pdf.ln(12)


def _pdf_termometro(pdf, remessas, total_gasto, lang):
    _pdf_section_title(pdf, _t("termometro", lang))

    if not remessas or all(r.valor_eur == 0 for r in remessas):
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 8, _t("nenhuma_remessa_cad", lang),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.ln(5)
        return

    valores_brl = []
    for r in remessas:
        if r.recebida and r.valor_brl:
            valores_brl.append(r.valor_brl)
        elif r.valor_eur and r.valor_eur > 0:
            valores_brl.append(
                (r.valor_eur * CAMBIO_PROJECAO).quantize(Decimal("0.01"))
            )
        else:
            valores_brl.append(Decimal("0.00"))

    gasto_restante = total_gasto
    exec_por_remessa = []
    for val_brl in valores_brl:
        if val_brl <= 0:
            exec_por_remessa.append(Decimal("0"))
            continue
        gasto_desta = min(gasto_restante, val_brl)
        gasto_restante = max(Decimal("0"), gasto_restante - val_brl)
        pct = (gasto_desta / val_brl * 100).quantize(Decimal("0.1"))
        exec_por_remessa.append(pct)

    num_rem = len(remessas)
    chart_w = min(90, (pdf.w - 20 - (num_rem - 1) * 3) / num_rem)
    chart_h = chart_w * 280 / 400

    if pdf.get_y() + chart_h > pdf.h - 15:
        pdf.add_page()
    chart_y = pdf.get_y()

    for i, rem in enumerate(remessas):
        pct = float(exec_por_remessa[i])
        cor = "#2ecc71" if pct >= 80 else "#f39c12" if pct >= 50 else "#e74c3c"

        fig = go.Figure(go.Indicator(
            mode="gauge+number",
            value=pct,
            number={"suffix": "%", "font": {"size": 32}},
            title={"text": f"{_t('remessa', lang)} {rem.numero}", "font": {"size": 16}},
            gauge={
                "axis": {"range": [0, 100], "ticksuffix": "%"},
                "bar": {"color": cor},
                "steps": [
                    {"range": [0, 50], "color": "#fdecea"},
                    {"range": [50, 80], "color": "#fef9e7"},
                    {"range": [80, 100], "color": "#eafaf1"},
                ],
                "threshold": {
                    "line": {"color": "#2c3e50", "width": 3},
                    "thickness": 0.8,
                    "value": 80,
                },
            },
        ))
        fig.update_layout(
            height=280, width=400,
            margin=dict(t=60, b=20, l=30, r=30),
            paper_bgcolor="white",
        )

        img_bytes = fig.to_image(format="png", scale=2)
        x_pos = 10 + i * (chart_w + 3)
        pdf.image(io.BytesIO(img_bytes), x=x_pos, y=chart_y, w=chart_w)

    pdf.set_y(chart_y + chart_h + 5)

    m = _t("moeda", lang)
    pdf.set_font("Helvetica", "", 9)
    for i, rem in enumerate(remessas):
        val_brl = valores_brl[i]
        status = _t("recebida", lang) if rem.recebida else _t("projecao", lang)
        pdf.cell(
            0, 6,
            f"  {_t('remessa', lang)} {rem.numero}: {m} {val_brl:,.2f} ({status}) - "
            f"{exec_por_remessa[i]}% {_t('executado', lang)}",
            new_x="LMARGIN", new_y="NEXT",
        )
    pdf.ln(3)

    for i in range(len(remessas)):
        pct = exec_por_remessa[i]
        if i == 0:
            if pct >= 80:
                _pdf_alert(pdf,
                    f"{_t('remessa', lang)} 1: {pct}% - {_t('remessa', lang)} 2 {_t('elegivel', lang)}",
                    "success")
            else:
                falta = (valores_brl[0] * Decimal("0.80")) - (
                    valores_brl[0] * pct / 100)
                falta = max(Decimal("0"), falta).quantize(Decimal("0.01"))
                _pdf_alert(pdf,
                    f"{_t('remessa', lang)} 1: {pct}% - {_t('falta', lang)} {m} {falta:,.2f} "
                    f"{_t('para_liberar', lang)} {_t('remessa', lang)} 2",
                    "warning")
        elif i == 1:
            if pct >= 80:
                _pdf_alert(pdf,
                    f"{_t('remessa', lang)} 2: {pct}% - {_t('remessa', lang)} 3 {_t('elegivel', lang)}",
                    "success")
            elif pct > 0:
                falta = (valores_brl[1] * Decimal("0.80")) - (
                    valores_brl[1] * pct / 100)
                falta = max(Decimal("0"), falta).quantize(Decimal("0.01"))
                _pdf_alert(pdf,
                    f"{_t('remessa', lang)} 2: {pct}% - {_t('falta', lang)} {m} {falta:,.2f} "
                    f"{_t('para_liberar', lang)} {_t('remessa', lang)} 3",
                    "warning")
    pdf.ln(8)


def _pdf_execucao_cc(pdf, session, centros, cambio, lang):
    if not centros:
        return

    _pdf_section_title(pdf, _t("execucao_cc", lang))

    nomes = []
    tetos = []
    gastos = []
    alertas = []

    for cc in centros:
        teto_brl = float((cc.teto_eur * cambio).quantize(Decimal("0.01")))
        gasto_brl = float(_gasto_por_cc(session, cc.id))
        nomes.append(cc.nome)
        tetos.append(teto_brl)
        gastos.append(gasto_brl)
        pct = (gasto_brl / teto_brl * 100) if teto_brl > 0 else 0
        if pct > 100:
            alertas.append((cc.codigo, cc.nome, pct, gasto_brl - teto_brl))
        elif pct >= 90:
            alertas.append((cc.codigo, cc.nome, pct, 0))

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=nomes, x=tetos, name=_t("teto_brl_label", lang),
        orientation="h", marker_color="#3498db", opacity=0.4,
    ))
    fig.add_trace(go.Bar(
        y=nomes, x=gastos, name=_t("gasto_brl_label", lang),
        orientation="h", marker_color="#e74c3c",
    ))
    fig_h = max(300, len(nomes) * 60)
    fig.update_layout(
        barmode="overlay",
        height=fig_h, width=900,
        xaxis_title="R$",
        yaxis=dict(autorange="reversed"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1),
        margin=dict(l=200, r=20, t=30, b=40),
        paper_bgcolor="white", plot_bgcolor="white",
    )

    img_bytes = fig.to_image(format="png", scale=2)
    chart_w = pdf.w - 20
    chart_h = chart_w * fig_h / 900

    if pdf.get_y() + chart_h > pdf.h - 15:
        pdf.add_page()

    pdf.image(io.BytesIO(img_bytes), x=10, w=chart_w)
    pdf.ln(5)

    m = _t("moeda", lang)
    for codigo, nome, pct, excesso in alertas:
        if excesso > 0:
            _pdf_alert(pdf, f"{codigo} - {nome}: {pct:.1f}% - {_t('estourou', lang)} "
                       f"{m} {excesso:,.2f}", "error")
        else:
            _pdf_alert(pdf, f"{codigo} - {nome}: {pct:.1f}% - "
                       f"{_t('proximo_teto', lang)}", "warning")
    pdf.ln(8)


def _pdf_teto_categoria(pdf, session, cambio, lang):
    """Secao do PDF com tetos por categoria (EUR + BRL dinamico)."""
    cats = _categorias_com_teto(session)
    if not cats:
        return

    _pdf_section_title(pdf, _t("teto_categoria", lang))
    m = _t("moeda", lang)
    usable_w = pdf.w - 20

    col_w = [usable_w * 0.27, usable_w * 0.10, usable_w * 0.12,
             usable_w * 0.12, usable_w * 0.13, usable_w * 0.13, usable_w * 0.13]

    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(52, 73, 94)
    pdf.set_text_color(255, 255, 255)
    headers = [
        _t("categoria", lang), "Tipo",
        "Teto EUR", f"{_t('teto_label', lang)} BRL",
        _t("realizado_label", lang),
        _t("disponivel_label", lang), "% Exec.",
    ]
    for i, h in enumerate(headers):
        pdf.cell(col_w[i], 7, h, border=1, align="C", fill=True)
    pdf.ln()

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 9)

    for cat in cats:
        if cat.tipo_teto == "GLOBAL":
            gasto = _gasto_por_categoria(session, cat.id)
        else:
            gasto = _gasto_cat_mes_atual(session, cat.id)

        # Teto BRL dinamico baseado no cambio medio das remessas
        if cat.teto_eur:
            teto = (cat.teto_eur * cambio).quantize(Decimal("0.01"))
            eur_str = f"EUR {cat.teto_eur:,.2f}"
        else:
            teto = cat.teto_brl
            eur_str = "—"

        disponivel = teto - gasto
        pct = (gasto / teto * 100).quantize(Decimal("0.1")) if teto > 0 else Decimal("0")
        tipo_label = _t("tipo_mensal", lang) if cat.tipo_teto == "MENSAL" else _t("tipo_global", lang)

        if pdf.get_y() > pdf.h - 20:
            pdf.add_page()

        # Cor de fundo baseada no percentual
        if pct > 100:
            pdf.set_fill_color(253, 236, 234)
        elif pct >= 80:
            pdf.set_fill_color(254, 249, 231)
        else:
            pdf.set_fill_color(255, 255, 255)

        fill = pct >= 80
        pdf.cell(col_w[0], 6, f"  {cat.centro_custo.codigo} | {cat.nome}", border=1, fill=fill)
        pdf.cell(col_w[1], 6, tipo_label, border=1, align="C", fill=fill)
        pdf.cell(col_w[2], 6, eur_str, border=1, align="R", fill=fill)
        pdf.cell(col_w[3], 6, f"{m} {teto:,.2f}", border=1, align="R", fill=fill)
        pdf.cell(col_w[4], 6, f"{m} {gasto:,.2f}", border=1, align="R", fill=fill)
        pdf.cell(col_w[5], 6, f"{m} {disponivel:,.2f}", border=1, align="R", fill=fill)
        pdf.cell(col_w[6], 6, f"{pct}%", border=1, align="C", fill=fill)
        pdf.ln()

    pdf.ln(8)


def _pdf_detalhamento(pdf, session, centros, cambio, lang):
    if not centros:
        return

    _pdf_section_title(pdf, _t("detalhamento", lang))
    usable_w = pdf.w - 20
    m = _t("moeda", lang)

    for cc in centros:
        teto_brl = (cc.teto_eur * cambio).quantize(Decimal("0.01"))
        gasto_total = _gasto_por_cc(session, cc.id)
        saldo = teto_brl - gasto_total
        pct = (
            (gasto_total / teto_brl * 100).quantize(Decimal("0.1"))
            if teto_brl > 0 else Decimal("0")
        )

        if pdf.get_y() > pdf.h - 40:
            pdf.add_page()

        pdf.set_font("Helvetica", "B", 10)
        pdf.set_fill_color(236, 240, 241)
        header = (
            f"{cc.codigo} - {cc.nome}  |  "
            f"{_t('teto_label', lang)}: {m} {teto_brl:,.2f}  |  "
            f"{_t('gasto_label', lang)}: {m} {gasto_total:,.2f}  |  "
            f"{_t('saldo_label', lang)}: {m} {saldo:,.2f}  |  {pct}%"
        )
        pdf.cell(0, 8, header, new_x="LMARGIN", new_y="NEXT", fill=True)
        pdf.ln(1)

        categorias = (
            session.query(CategoriaDespesa)
            .filter(CategoriaDespesa.centro_custo_id == cc.id)
            .order_by(CategoriaDespesa.nome)
            .all()
        )

        if not categorias:
            pdf.set_font("Helvetica", "I", 9)
            pdf.cell(0, 6, f"  {_t('nenhuma_cat', lang)}",
                     new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)
            continue

        col_w = [usable_w * 0.55, usable_w * 0.25, usable_w * 0.20]
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_fill_color(52, 73, 94)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(col_w[0], 7, _t("categoria", lang), border=1, align="C", fill=True)
        pdf.cell(col_w[1], 7, f"{_t('gasto_label', lang)} BRL", border=1, align="C", fill=True)
        pdf.cell(col_w[2], 7, _t("pct_cc", lang), border=1, align="C", fill=True)
        pdf.ln()

        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 9)

        for cat in categorias:
            gasto_cat = _gasto_por_categoria(session, cat.id)
            pct_cat = (
                f"{(gasto_cat / gasto_total * 100).quantize(Decimal('0.1'))}%"
                if gasto_total > 0 else "0%"
            )
            pdf.cell(col_w[0], 6, f"  {cat.nome}", border=1)
            pdf.cell(col_w[1], 6, f"{m} {gasto_cat:,.2f}", border=1, align="R")
            pdf.cell(col_w[2], 6, pct_cat, border=1, align="C")
            pdf.ln()

        pdf.ln(5)


def _gerar_xlsx_financiador(session, cambio):
    """Exporta todas as despesas no formato exigido pelo financiador Mundukide.

    Formato: 21 colunas fixas (C_CREATED_DATE ... C_COF_EURO).
    Valores fixos: C_CONTRACT=M349, C_ACTIVITY=Programa Brasil,
    C_USER=EXT BRAS FINAPOP, C_ORGANIZATION=Brasil Sul, C_COUNTRY=Brasil,
    C_FINA/C_COFINA/C_EXPENSE_STATE="NO EXISTE".
    C_RECEIPT/C_RECEIPT_PICTURE ficam em branco (nao armazenamos anexos).
    Valor BRL sai com sinal negativo (despesa). EUR = BRL / cambio medio.
    """
    COLUNAS = [
        "C_CREATED_DATE", "C_DESCRIPTION", "C_CONTRACT", "C_ACCOUNT",
        "C_TAGS", "C_TAGS_IDS", "C_AMOUNT", "C_CURRENCY", "C_ACTIVITY",
        "C_RECEIPT", "C_RECEIPT_PICTURE", "C_USER", "C_FINA", "C_COFINA",
        "C_EXPENSE_STATE", "C_ID", "C_ORGANIZATION", "C_COUNTRY",
        "C_EUR_EQUI", "C_FIN_EURO", "C_COF_EURO",
    ]

    itens = (
        session.query(ItemDespesa)
        .join(CategoriaDespesa)
        .join(CentroCusto)
        .order_by(
            ItemDespesa.data_pagamento, ItemDespesa.data, ItemDespesa.id,
        )
        .all()
    )

    cambio_f = float(cambio) if cambio else 0.0
    linhas = []
    for item in itens:
        cat = item.categoria_despesa
        cc = cat.centro_custo
        data_ref = item.data_pagamento or item.data
        valor_brl = -float(item.valor_brl)
        valor_eur = (valor_brl / cambio_f) if cambio_f else 0.0

        linhas.append({
            "C_CREATED_DATE": data_ref,
            "C_DESCRIPTION": item.descricao or "",
            "C_CONTRACT": "M349",
            "C_ACCOUNT": f"{cc.codigo} {cat.nome}",
            "C_TAGS": "",
            "C_TAGS_IDS": "",
            "C_AMOUNT": round(valor_brl, 2),
            "C_CURRENCY": "BRL",
            "C_ACTIVITY": "Programa Brasil",
            "C_RECEIPT": "",
            "C_RECEIPT_PICTURE": "",
            "C_USER": "EXT BRAS, FINAPOP @finapop",
            "C_FINA": "NO EXISTE",
            "C_COFINA": "NO EXISTE",
            "C_EXPENSE_STATE": "NO EXISTE",
            "C_ID": item.id,
            "C_ORGANIZATION": "Brasil Sul",
            "C_COUNTRY": "Brasil",
            "C_EUR_EQUI": round(valor_eur, 6),
            "C_FIN_EURO": round(valor_eur, 6),
            "C_COF_EURO": round(valor_eur, 6),
        })

    df = pd.DataFrame(linhas, columns=COLUNAS)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Worksheet")
    return buffer.getvalue()


def _gerar_pdf(session, remessas, centros, cambio, total_gasto, idioma="pt"):
    """Gera relatorio PDF completo do dashboard."""
    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, _t("titulo", idioma),
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 8, f"{_t('gerado_em', idioma)}: {date.today().strftime('%d/%m/%Y')}",
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(8)

    _pdf_metricas(pdf, remessas, cambio, total_gasto, idioma)
    _pdf_termometro(pdf, remessas, total_gasto, idioma)
    _pdf_execucao_cc(pdf, session, centros, cambio, idioma)
    _pdf_teto_categoria(pdf, session, cambio, idioma)
    _pdf_detalhamento(pdf, session, centros, cambio, idioma)

    return bytes(pdf.output())


# ──────────────────── render principal ──────────────────────

def render():
    st.title("Dashboard")

    session = get_session()

    remessas = session.query(Remessa).order_by(Remessa.numero).all()
    centros = session.query(CentroCusto).order_by(CentroCusto.codigo).all()
    cambio = _cambio_medio(session)
    total_gasto = _total_gasto_brl(session)

    # Botoes de exportacao (PDF + Excel Financiador)
    col_lang, col_pdf, col_pdf_dl, col_xlsx, col_xlsx_dl, _ = st.columns(
        [1, 1, 1, 1.2, 1, 1]
    )
    with col_lang:
        idioma_pdf = st.selectbox(
            "Idioma do PDF",
            ["Portugues", "Espanhol"],
            key="idioma_pdf",
        )
        lang_code = "pt" if idioma_pdf == "Portugues" else "es"

    with col_pdf:
        if st.button("Exportar PDF", type="secondary"):
            with st.spinner("Gerando PDF com graficos..."):
                pdf_bytes = _gerar_pdf(
                    session, remessas, centros, cambio, total_gasto, lang_code,
                )
                st.session_state["dashboard_pdf"] = pdf_bytes
            st.rerun()

    if "dashboard_pdf" in st.session_state:
        with col_pdf_dl:
            st.download_button(
                "Baixar PDF",
                data=st.session_state["dashboard_pdf"],
                file_name=f"dashboard_mundukide_{date.today().strftime('%Y%m%d')}.pdf",
                mime="application/pdf",
                type="primary",
            )

    with col_xlsx:
        if st.button("Exportar Excel Financiador", type="secondary"):
            with st.spinner("Gerando planilha no formato do financiador..."):
                xlsx_bytes = _gerar_xlsx_financiador(session, cambio)
                st.session_state["dashboard_xlsx"] = xlsx_bytes
            st.rerun()

    if "dashboard_xlsx" in st.session_state:
        with col_xlsx_dl:
            st.download_button(
                "Baixar Excel",
                data=st.session_state["dashboard_xlsx"],
                file_name=f"expenses_{date.today().strftime('%Y%m%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
            )

    _secao_metricas(session, remessas, cambio, total_gasto)
    st.markdown("---")
    _secao_termometro(remessas, cambio, total_gasto)
    st.markdown("---")
    _secao_execucao_cc(session, centros, cambio)
    st.markdown("---")
    _secao_teto_categoria(session, cambio)
    st.markdown("---")
    _secao_detalhamento(session, centros, cambio)

    session.close()


# ────────────── secao 1: metricas gerais ────────────────────

def _secao_metricas(session, remessas, cambio, total_gasto):
    st.subheader("Visao Geral do Projeto")

    total_eur = sum(r.valor_eur for r in remessas if r.valor_eur)
    total_brl = Decimal("0.00")
    total_recebido_brl = Decimal("0.00")
    for r in remessas:
        if r.recebida and r.valor_brl:
            total_brl += r.valor_brl
            total_recebido_brl += r.valor_brl
        elif r.valor_eur:
            total_brl += (r.valor_eur * CAMBIO_PROJECAO).quantize(Decimal("0.01"))

    saldo = total_recebido_brl - total_gasto

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Projeto (EUR)", f"EUR {total_eur:,.2f}")
    col2.metric("Projeto (BRL)", f"R$ {total_brl:,.2f}")
    col3.metric("Cambio Medio", f"R$ {cambio:,.4f}")
    col4.metric("Total Gasto", f"R$ {total_gasto:,.2f}")
    col5.metric("Saldo em Conta", f"R$ {saldo:,.2f}",
                delta=f"{'Positivo' if saldo >= 0 else 'Negativo'}")


# ──────── secao 2: termometro de liberacao (80%) ────────────

def _secao_termometro(remessas, cambio, total_gasto):
    st.subheader("Termometro de Liberacao — Regra dos 80%")
    st.caption(
        "Cada remessa so e liberada quando 80% da anterior for gasto. "
        "Calculo FIFO: o gasto abate primeiro a remessa mais antiga."
    )

    if not remessas or all(r.valor_eur == 0 for r in remessas):
        st.info("Cadastre os valores das remessas primeiro.")
        return

    valores_brl = []
    for r in remessas:
        if r.recebida and r.valor_brl:
            valores_brl.append(r.valor_brl)
        elif r.valor_eur and r.valor_eur > 0:
            valores_brl.append((r.valor_eur * CAMBIO_PROJECAO).quantize(Decimal("0.01")))
        else:
            valores_brl.append(Decimal("0.00"))

    gasto_restante = total_gasto
    exec_por_remessa = []

    for i, val_brl in enumerate(valores_brl):
        if val_brl <= 0:
            exec_por_remessa.append(Decimal("0"))
            continue

        gasto_desta = min(gasto_restante, val_brl)
        gasto_restante = max(Decimal("0"), gasto_restante - val_brl)
        pct = (gasto_desta / val_brl * 100).quantize(Decimal("0.1"))
        exec_por_remessa.append(pct)

    cols = st.columns(3)

    for i, rem in enumerate(remessas):
        pct = float(exec_por_remessa[i])
        val_brl = valores_brl[i]

        if pct >= 80:
            cor = "#2ecc71"
        elif pct >= 50:
            cor = "#f39c12"
        else:
            cor = "#e74c3c"

        fig = go.Figure(go.Indicator(
            mode="gauge+number",
            value=pct,
            number={"suffix": "%", "font": {"size": 32}},
            title={"text": f"Remessa {rem.numero}", "font": {"size": 16}},
            gauge={
                "axis": {"range": [0, 100], "ticksuffix": "%"},
                "bar": {"color": cor},
                "steps": [
                    {"range": [0, 50], "color": "#fdecea"},
                    {"range": [50, 80], "color": "#fef9e7"},
                    {"range": [80, 100], "color": "#eafaf1"},
                ],
                "threshold": {
                    "line": {"color": "#2c3e50", "width": 3},
                    "thickness": 0.8,
                    "value": 80,
                },
            },
        ))
        fig.update_layout(height=250, margin=dict(t=60, b=10, l=30, r=30))

        with cols[i]:
            st.plotly_chart(fig, use_container_width=True)
            brl_label = f"R$ {val_brl:,.2f}"
            if rem.recebida:
                st.caption(f"Recebida — {brl_label}")
            else:
                st.caption(f"Projecao — {brl_label}")

    st.markdown("**Status de Liberacao:**")

    for i in range(len(remessas)):
        rem = remessas[i]
        pct = exec_por_remessa[i]

        if i == 0:
            if pct >= 80:
                st.success(f"Remessa 1: {pct}% executado — Remessa 2 ELEGIVEL para liberacao.")
            else:
                falta_brl = (valores_brl[0] * Decimal("0.80")) - (valores_brl[0] * pct / 100)
                falta_brl = max(Decimal("0"), falta_brl).quantize(Decimal("0.01"))
                st.warning(
                    f"Remessa 1: {pct}% executado — Falta gastar R$ {falta_brl:,.2f} "
                    f"para liberar a Remessa 2."
                )
        elif i == 1:
            if pct >= 80:
                st.success(f"Remessa 2: {pct}% executado — Remessa 3 ELEGIVEL para liberacao.")
            elif pct > 0:
                falta_brl = (valores_brl[1] * Decimal("0.80")) - (valores_brl[1] * pct / 100)
                falta_brl = max(Decimal("0"), falta_brl).quantize(Decimal("0.01"))
                st.warning(
                    f"Remessa 2: {pct}% executado — Falta gastar R$ {falta_brl:,.2f} "
                    f"para liberar a Remessa 3."
                )
            elif exec_por_remessa[0] < 80:
                st.info("Remessa 2: Aguardando execucao de 80% da Remessa 1.")
        elif i == 2:
            if pct > 0:
                st.info(f"Remessa 3: {pct}% executado.")
            elif exec_por_remessa[1] < 80:
                st.info("Remessa 3: Aguardando execucao de 80% da Remessa 2.")


# ────────── secao 3: execucao por centro de custo ───────────

def _secao_execucao_cc(session, centros, cambio):
    st.subheader("Execucao por Centro de Custo")

    if not centros:
        st.info("Nenhum centro de custo cadastrado.")
        return

    nomes = []
    tetos = []
    gastos = []
    alertas = []

    for cc in centros:
        teto_brl = float((cc.teto_eur * cambio).quantize(Decimal("0.01")))
        gasto_brl = float(_gasto_por_cc(session, cc.id))

        nomes.append(f"{cc.codigo} — {cc.nome}")
        tetos.append(teto_brl)
        gastos.append(gasto_brl)

        pct = (gasto_brl / teto_brl * 100) if teto_brl > 0 else 0
        if pct > 100:
            alertas.append((cc.codigo, cc.nome, pct, gasto_brl - teto_brl))
        elif pct >= 90:
            alertas.append((cc.codigo, cc.nome, pct, 0))

    fig = go.Figure()

    fig.add_trace(go.Bar(
        y=nomes, x=tetos, name="Teto BRL",
        orientation="h", marker_color="#3498db", opacity=0.4,
    ))

    fig.add_trace(go.Bar(
        y=nomes, x=gastos, name="Gasto BRL",
        orientation="h", marker_color="#e74c3c",
    ))

    fig.update_layout(
        barmode="overlay",
        height=max(250, len(nomes) * 60),
        xaxis_title="R$",
        yaxis=dict(autorange="reversed"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=10, r=10, t=10, b=40),
    )

    st.plotly_chart(fig, use_container_width=True)

    for codigo, nome, pct, excesso in alertas:
        if excesso > 0:
            st.error(
                f"{codigo} — {nome}: {pct:.1f}% executado. "
                f"ESTOUROU o teto em R$ {excesso:,.2f}!"
            )
        else:
            st.warning(f"{codigo} — {nome}: {pct:.1f}% executado. Proximo do teto!")


# ──────── secao 3.5: teto por categoria (NOVA) ─────────────

def _secao_teto_categoria(session, cambio):
    st.subheader("Teto por Categoria")

    cats = _categorias_com_teto(session)
    if not cats:
        st.info("Nenhuma categoria com teto configurado. Configure em Cadastros > Categorias.")
        return

    st.caption(f"Cambio utilizado: R$ {cambio:,.4f}/€ (medio ponderado das remessas recebidas)")

    nomes = []
    tetos_vals = []
    gastos_vals = []
    alertas = []
    dados_tabela = []

    for cat in cats:
        if cat.tipo_teto == "GLOBAL":
            gasto = float(_gasto_por_categoria(session, cat.id))
        else:
            gasto = float(_gasto_cat_mes_atual(session, cat.id))

        # Teto BRL dinamico: teto_eur * cambio se disponivel
        if cat.teto_eur:
            teto = float((cat.teto_eur * cambio).quantize(Decimal("0.01")))
            eur_label = f"€ {cat.teto_eur:,.2f}"
        else:
            teto = float(cat.teto_brl)
            eur_label = "—"

        tipo_label = "mensal" if cat.tipo_teto == "MENSAL" else "global"
        label = f"{cat.centro_custo.codigo} | {cat.nome} ({tipo_label})"

        nomes.append(label)
        tetos_vals.append(teto)
        gastos_vals.append(gasto)

        pct = (gasto / teto * 100) if teto > 0 else 0
        if pct > 100:
            alertas.append((cat.nome, pct, gasto - teto, tipo_label))
        elif pct >= 80:
            alertas.append((cat.nome, pct, 0, tipo_label))

        dados_tabela.append({
            "Categoria": f"{cat.centro_custo.codigo} | {cat.nome}",
            "Tipo": tipo_label,
            "Teto EUR": eur_label,
            "Teto BRL": f"R$ {teto:,.2f}",
            "Realizado": f"R$ {gasto:,.2f}",
            "Disponivel": f"R$ {teto - gasto:,.2f}",
            "% Exec.": f"{pct:.1f}%",
        })

    # Grafico de barras
    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=nomes, x=tetos_vals, name="Teto BRL",
        orientation="h", marker_color="#3498db", opacity=0.4,
    ))
    fig.add_trace(go.Bar(
        y=nomes, x=gastos_vals, name="Realizado",
        orientation="h", marker_color="#e67e22",
    ))

    fig.update_layout(
        barmode="overlay",
        height=max(250, len(nomes) * 55),
        xaxis_title="R$",
        yaxis=dict(autorange="reversed"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=10, r=10, t=10, b=40),
    )

    st.plotly_chart(fig, use_container_width=True)

    st.dataframe(pd.DataFrame(dados_tabela), use_container_width=True, hide_index=True)

    for nome, pct, excesso, tipo in alertas:
        if excesso > 0:
            st.error(
                f"{nome}: {pct:.1f}% executado (teto {tipo}). "
                f"ESTOUROU em R$ {excesso:,.2f}!"
            )
        else:
            st.warning(f"{nome}: {pct:.1f}% executado (teto {tipo}). Proximo do teto!")


# ──────── secao 4: detalhamento por categoria ───────────────

def _secao_detalhamento(session, centros, cambio):
    st.subheader("Detalhamento por Categoria")

    if not centros:
        return

    for cc in centros:
        teto_brl = (cc.teto_eur * cambio).quantize(Decimal("0.01"))
        gasto_total = _gasto_por_cc(session, cc.id)
        saldo = teto_brl - gasto_total
        pct = (gasto_total / teto_brl * 100).quantize(Decimal("0.1")) if teto_brl > 0 else Decimal("0")

        header = (
            f"{cc.codigo} — {cc.nome} | "
            f"Teto: R$ {teto_brl:,.2f} | "
            f"Gasto: R$ {gasto_total:,.2f} | "
            f"Saldo: R$ {saldo:,.2f} | "
            f"{pct}%"
        )

        with st.expander(header):
            categorias = (
                session.query(CategoriaDespesa)
                .filter(CategoriaDespesa.centro_custo_id == cc.id)
                .order_by(CategoriaDespesa.nome)
                .all()
            )

            if not categorias:
                st.caption("Nenhuma categoria cadastrada neste centro.")
                continue

            dados = []
            for cat in categorias:
                gasto_cat = _gasto_por_categoria(session, cat.id)
                teto_info = ""
                if cat.teto_eur and cat.tipo_teto:
                    teto_brl_cat = (cat.teto_eur * cambio).quantize(Decimal("0.01"))
                    teto_info = (
                        f" [Teto: € {cat.teto_eur:,.2f} = R$ {teto_brl_cat:,.2f} {cat.tipo_teto.lower()}]"
                    )
                elif cat.teto_brl and cat.tipo_teto:
                    teto_info = f" [Teto: R$ {cat.teto_brl:,.2f} {cat.tipo_teto.lower()}]"
                dados.append({
                    "Categoria": cat.nome + teto_info,
                    "Gasto BRL": f"R$ {gasto_cat:,.2f}",
                    "% do CC": f"{(gasto_cat / gasto_total * 100).quantize(Decimal('0.1'))}%"
                    if gasto_total > 0
                    else "0%",
                })

            st.dataframe(
                pd.DataFrame(dados),
                use_container_width=True,
                hide_index=True,
            )
