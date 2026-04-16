"""
Modulo Folha de Pagamento
=========================
Calculo da folha de pagamento dos 4 tecnicos do projeto.
Tabelas INSS e IRRF atualizadas para 2026 (Portaria MPS/MF 13/2026 e Lei 15.270/2025).
Empresa contratante: Lucro Presumido.
"""

from calendar import monthrange
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fpdf import FPDF

import pandas as pd
import streamlit as st
from sqlalchemy import func as sqlfunc

from database import get_session
from models import (
    CentroCusto,
    CategoriaDespesa,
    LancamentoRecorrente,
    Tecnico,
)

# ──────────────── Tabelas 2026 ─────────────────────────────

# INSS Empregado — Portaria Interministerial MPS/MF 13/2026
# Calculo simplificado: (salario * aliquota) - parcela_deduzir
TABELA_INSS_2026 = [
    (Decimal("1621.00"),  Decimal("0.075"), Decimal("0.00")),
    (Decimal("2902.84"),  Decimal("0.09"),  Decimal("24.32")),
    (Decimal("4354.27"),  Decimal("0.12"),  Decimal("111.40")),
    (Decimal("8475.55"),  Decimal("0.14"),  Decimal("198.49")),
]
TETO_INSS_2026 = Decimal("8475.55")

# IRRF — Tabela base vigente 2026
# Base de calculo = salario - INSS empregado
TABELA_IRRF_2026 = [
    (Decimal("2259.20"), Decimal("0.00"),   Decimal("0.00")),
    (Decimal("2826.65"), Decimal("0.075"),  Decimal("169.44")),
    (Decimal("3751.05"), Decimal("0.15"),   Decimal("381.44")),
    (Decimal("4664.68"), Decimal("0.225"),  Decimal("662.77")),
    (Decimal("99999999"), Decimal("0.275"), Decimal("896.00")),
]
# Lei 15.270/2025: isencao total para salario bruto <= R$5.000
# Reducao gradual entre R$5.000,01 e R$7.350
LIMITE_ISENCAO_IRRF = Decimal("5000.00")
LIMITE_REDUCAO_IRRF = Decimal("7350.00")

# Encargos patronais (Lucro Presumido)
ALIQUOTA_INSS_PATRONAL = Decimal("0.20")  # 20%
ALIQUOTA_FGTS = Decimal("0.08")           # 8%
ALIQUOTA_PIS_FOLHA = Decimal("0.01")      # 1% sobre folha


# Fator multiplicador: salario_bruto * FATOR = custo_total
# FATOR = 1 + INSS_PAT(0.20) + FGTS(0.08) + PIS(0.01) + FERIAS(4/36) + 13o(1/12)
FATOR_CUSTO_TOTAL = (
    Decimal("1")
    + ALIQUOTA_INSS_PATRONAL
    + ALIQUOTA_FGTS
    + ALIQUOTA_PIS_FOLHA
    + Decimal("4") / Decimal("36")   # provisao ferias: (sal + 1/3) / 12
    + Decimal("1") / Decimal("12")   # provisao 13o: sal / 12
)


# ──────────────── Traducoes (PDF bilingue) ────────────────
TRADUCOES_FOLHA = {
    "pt": {
        "titulo_pdf": "Folha de Pagamento - Projeto Mundukide",
        "gerado_em": "Gerado em",
        "cadastro_section": "Cadastro de Tecnicos",
        "calculo_section": "Calculo Mensal",
        "resumo_section": "Resumo Mensal Consolidado",
        "nome": "Nome",
        "custo_maximo": "Custo Maximo",
        "salario_bruto": "Sal. Bruto",
        "data_admissao": "Data Admissao",
        "status": "Status",
        "inss": "INSS",
        "irrf": "IRRF",
        "salario_liquido": "Sal. Liquido",
        "inss_patronal": "INSS Patronal",
        "fgts": "FGTS",
        "pis": "PIS",
        "prov_ferias": "Prov. Ferias",
        "prov_13": "Prov. 13o",
        "total_descontos": "Total Descontos",
        "total_encargos": "Total Encargos",
        "custo_total": "Custo Total Mensal",
        "total": "TOTAL",
        "tecnico": "Tecnico",
        "ativo": "Ativo",
        "inativo": "Inativo",
        "mes_referencia": "Mes de Referencia",
        "proporcional": "proporcional",
        "dias": "dias",
        "valor_integral": "Valor Integral",
        "valor_proporcional": "Valor Proporcional",
        "dias_trabalhados": "Dias Trabalhados",
        "dias_mes": "Dias no Mes",
        "fator_proporcional": "Fator Proporcional",
        "nota": "Observacoes",
    },
    "es": {
        "titulo_pdf": "Nomina - Proyecto Mundukide",
        "gerado_em": "Generado el",
        "cadastro_section": "Registro de Tecnicos",
        "calculo_section": "Calculo Mensual",
        "resumo_section": "Resumen Mensual Consolidado",
        "nome": "Nombre",
        "custo_maximo": "Costo Maximo",
        "salario_bruto": "Sal. Bruto",
        "data_admissao": "Fecha Admision",
        "status": "Estado",
        "inss": "INSS",
        "irrf": "IRRF",
        "salario_liquido": "Sal. Neto",
        "inss_patronal": "INSS Patronal",
        "fgts": "FGTS",
        "pis": "PIS",
        "prov_ferias": "Prov. Vacaciones",
        "prov_13": "Prov. 13o",
        "total_descontos": "Total Descuentos",
        "total_encargos": "Total Cargas Sociales",
        "custo_total": "Costo Total Mensual",
        "total": "TOTAL",
        "tecnico": "Tecnico",
        "ativo": "Activo",
        "inativo": "Inactivo",
        "mes_referencia": "Mes de Referencia",
        "proporcional": "proporcional",
        "dias": "dias",
        "valor_integral": "Valor Integral",
        "valor_proporcional": "Valor Proporcional",
        "dias_trabalhados": "Dias Trabajados",
        "dias_mes": "Dias del Mes",
        "fator_proporcional": "Factor Proporcional",
        "nota": "Observaciones",
    },
}


def _tf(chave: str, idioma: str = "pt") -> str:
    """Traduz label da folha de pagamento."""
    return TRADUCOES_FOLHA.get(idioma, TRADUCOES_FOLHA["pt"]).get(chave, chave)


def calcular_bruto_de_custo(custo_maximo: Decimal) -> Decimal:
    """Calculo inverso: dado o custo total maximo, retorna o salario bruto."""
    if custo_maximo <= 0:
        return Decimal("0.00")
    return _q2(custo_maximo / FATOR_CUSTO_TOTAL)


# ──────────────── Funcoes de Calculo ───────────────────────

def _q2(valor: Decimal) -> Decimal:
    """Arredonda para 2 casas decimais."""
    return valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calcular_inss(salario: Decimal) -> Decimal:
    """Calcula INSS empregado (tabela progressiva 2026 — metodo simplificado)."""
    if salario <= 0:
        return Decimal("0.00")
    base = min(salario, TETO_INSS_2026)
    for teto_faixa, aliq, deduz in TABELA_INSS_2026:
        if base <= teto_faixa:
            return _q2(base * aliq - deduz)
    # Acima do teto
    return _q2(TETO_INSS_2026 * Decimal("0.14") - Decimal("198.49"))


def calcular_irrf(salario: Decimal, inss: Decimal) -> Decimal:
    """Calcula IRRF (tabela 2026 + isencao Lei 15.270/2025)."""
    if salario <= 0:
        return Decimal("0.00")

    base = salario - inss
    if base <= 0:
        return Decimal("0.00")

    # Calculo pela tabela progressiva
    irrf_bruto = Decimal("0.00")
    for teto_faixa, aliq, deduz in TABELA_IRRF_2026:
        if base <= teto_faixa:
            irrf_bruto = _q2(base * aliq - deduz)
            break

    if irrf_bruto <= 0:
        return Decimal("0.00")

    # Isencao total para salario bruto <= R$5.000
    if salario <= LIMITE_ISENCAO_IRRF:
        return Decimal("0.00")

    # Reducao gradual entre R$5.000,01 e R$7.350
    if salario <= LIMITE_REDUCAO_IRRF:
        fator = (salario - LIMITE_ISENCAO_IRRF) / (LIMITE_REDUCAO_IRRF - LIMITE_ISENCAO_IRRF)
        return _q2(irrf_bruto * fator)

    return irrf_bruto


def calcular_folha_tecnico(salario: Decimal) -> dict:
    """Retorna dict com todos os componentes da folha para um salario."""
    inss = calcular_inss(salario)
    irrf = calcular_irrf(salario, inss)
    fgts = _q2(salario * ALIQUOTA_FGTS)
    pis = _q2(salario * ALIQUOTA_PIS_FOLHA)
    inss_patronal = _q2(salario * ALIQUOTA_INSS_PATRONAL)
    prov_ferias = _q2(salario * Decimal("4") / Decimal("36"))  # (sal + 1/3) / 12
    prov_13 = _q2(salario / Decimal("12"))
    liquido = _q2(salario - inss - irrf)

    return {
        "salario_bruto": salario,
        "inss": inss,
        "irrf": irrf,
        "fgts": fgts,
        "pis": pis,
        "inss_patronal": inss_patronal,
        "prov_ferias": prov_ferias,
        "prov_13": prov_13,
        "salario_liquido": liquido,
        "total_descontos": _q2(inss + irrf),
        "total_encargos": _q2(inss_patronal + fgts + pis + prov_ferias + prov_13),
    }


def calcular_proporcional(
    salario_bruto: Decimal,
    data_admissao: date,
    ano_ref: int,
    mes_ref: int,
) -> dict | None:
    """
    Calcula folha proporcional para o mes de admissao.

    Retorna None se a admissao nao cai no mes de referencia,
    ou se a admissao e no dia 1 (mes integral).
    """
    if data_admissao.year != ano_ref or data_admissao.month != mes_ref:
        return None

    _, dias_no_mes = monthrange(ano_ref, mes_ref)

    if data_admissao.day == 1:
        return None  # Mes integral, sem proporcional

    dias_trabalhados = dias_no_mes - data_admissao.day + 1
    fator = Decimal(str(dias_trabalhados)) / Decimal(str(dias_no_mes))
    salario_prop = _q2(salario_bruto * fator)

    return {
        "dias_no_mes": dias_no_mes,
        "dias_trabalhados": dias_trabalhados,
        "fator": fator,
        "salario_proporcional": salario_prop,
        "folha_integral": calcular_folha_tecnico(salario_bruto),
        "folha_proporcional": calcular_folha_tecnico(salario_prop),
    }


# ──────────────── Helpers ──────────────────────────────────

def _to_decimal(valor, fallback=Decimal("0.00")):
    try:
        return Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        return fallback


def _opcoes_categorias(session):
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


# ──────────────── PDF ─────────────────────────────────────

_NOMES_MES = {
    "pt": [
        "", "Janeiro", "Fevereiro", "Marco", "Abril", "Maio", "Junho",
        "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
    ],
    "es": [
        "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
        "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
    ],
}


def _pdf_section(pdf: FPDF, titulo: str):
    """Titulo de secao no PDF (padrao dashboard)."""
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(44, 62, 80)
    pdf.cell(0, 10, titulo, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)


def _pdf_table_header(pdf: FPDF, colunas: list[str], larguras: list[int]):
    """Cabecalho de tabela com fundo azul."""
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(52, 152, 219)
    pdf.set_text_color(255, 255, 255)
    for col, w in zip(colunas, larguras):
        pdf.cell(w, 7, col, border=1, fill=True, align="C")
    pdf.ln()
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 8)


def _pdf_table_row(pdf: FPDF, valores: list[str], larguras: list[int], bold=False):
    """Linha de dados na tabela."""
    if bold:
        pdf.set_font("Helvetica", "B", 8)
    for val, w in zip(valores, larguras):
        align = "L" if valores.index(val) == 0 and not bold else "R"
        if bold:
            align = "R" if valores.index(val) > 0 else "L"
        pdf.cell(w, 6, val, border=1, align=align)
    pdf.ln()
    if bold:
        pdf.set_font("Helvetica", "", 8)


def _fmt(valor) -> str:
    """Formata Decimal como R$ X.XXX,XX para PDF."""
    return f"R$ {valor:,.2f}"


def _gerar_pdf_folha(session, idioma: str = "pt", ano_ref: int = None, mes_ref: int = None, nota: str = "") -> bytes:
    """Gera PDF da folha de pagamento no idioma escolhido."""
    t = lambda k: _tf(k, idioma)

    if ano_ref is None:
        ano_ref = date.today().year
    if mes_ref is None:
        mes_ref = date.today().month

    tecnicos = (
        session.query(Tecnico)
        .filter(Tecnico.ativo == True)
        .order_by(Tecnico.nome)
        .all()
    )

    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Titulo
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, t("titulo_pdf"), new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 10)
    nome_mes = _NOMES_MES.get(idioma, _NOMES_MES["pt"])[mes_ref]
    pdf.cell(
        0, 8,
        f"{t('mes_referencia')}: {nome_mes} {ano_ref}  |  {t('gerado_em')}: {date.today().strftime('%d/%m/%Y')}",
        new_x="LMARGIN", new_y="NEXT", align="C",
    )
    pdf.ln(5)

    # ── Nota descritiva (se informada) ──
    if nota and nota.strip():
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(44, 62, 80)
        pdf.cell(0, 6, t("nota") + ":", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(0, 0, 0)
        pdf.set_fill_color(245, 245, 245)
        pdf.multi_cell(0, 6, nota.strip(), border=1, fill=True)
        pdf.ln(4)

    # ── Secao 1: Cadastro ──
    _pdf_section(pdf, t("cadastro_section"))
    cad_cols = [t("nome"), t("custo_maximo"), t("salario_bruto"), t("data_admissao")]
    cad_w = [80, 50, 50, 40]
    _pdf_table_header(pdf, cad_cols, cad_w)
    for tc in tecnicos:
        custo_str = _fmt(tc.custo_maximo) if tc.custo_maximo else "---"
        _pdf_table_row(pdf, [
            tc.nome,
            custo_str,
            _fmt(tc.salario_bruto),
            tc.data_admissao.strftime("%d/%m/%Y"),
        ], cad_w)
    pdf.ln(5)

    # ── Secao 2: Calculo Mensal ──
    _pdf_section(pdf, f"{t('calculo_section')} - {nome_mes} {ano_ref}")

    calc_cols = [
        t("tecnico"), t("salario_bruto"), t("inss"), t("irrf"),
        t("salario_liquido"), t("inss_patronal"), t("fgts"),
        t("pis"), t("prov_ferias"), t("prov_13"),
    ]
    calc_w = [50, 28, 24, 24, 27, 27, 24, 21, 26, 26]

    _pdf_table_header(pdf, calc_cols, calc_w)

    totais = {k: Decimal("0") for k in [
        "salario_bruto", "inss", "irrf", "fgts", "pis",
        "inss_patronal", "prov_ferias", "prov_13", "salario_liquido",
        "total_descontos", "total_encargos",
    ]}

    for tc in tecnicos:
        prop = calcular_proporcional(tc.salario_bruto, tc.data_admissao, ano_ref, mes_ref)
        if prop is not None:
            folha = prop["folha_proporcional"]
            label = f"{tc.nome} ({t('proporcional')}: {prop['dias_trabalhados']}/{prop['dias_no_mes']} {t('dias')})"
        else:
            folha = calcular_folha_tecnico(tc.salario_bruto)
            label = tc.nome

        _pdf_table_row(pdf, [
            label,
            _fmt(folha["salario_bruto"]),
            _fmt(folha["inss"]),
            _fmt(folha["irrf"]),
            _fmt(folha["salario_liquido"]),
            _fmt(folha["inss_patronal"]),
            _fmt(folha["fgts"]),
            _fmt(folha["pis"]),
            _fmt(folha["prov_ferias"]),
            _fmt(folha["prov_13"]),
        ], calc_w)

        for k in totais:
            totais[k] += folha[k]

    # Linha de totais
    _pdf_table_row(pdf, [
        t("total"),
        _fmt(totais["salario_bruto"]),
        _fmt(totais["inss"]),
        _fmt(totais["irrf"]),
        _fmt(totais["salario_liquido"]),
        _fmt(totais["inss_patronal"]),
        _fmt(totais["fgts"]),
        _fmt(totais["pis"]),
        _fmt(totais["prov_ferias"]),
        _fmt(totais["prov_13"]),
    ], calc_w, bold=True)

    pdf.ln(5)

    # ── Secao 3: Resumo ──
    _pdf_section(pdf, t("resumo_section"))
    custo_total = _q2(totais["salario_bruto"] + totais["total_encargos"])

    resumo_cols = [
        t("salario_bruto"), t("total_descontos"), t("salario_liquido"),
        t("total_encargos"), t("custo_total"),
    ]
    resumo_w = [55, 55, 55, 55, 55]
    _pdf_table_header(pdf, resumo_cols, resumo_w)
    _pdf_table_row(pdf, [
        _fmt(totais["salario_bruto"]),
        _fmt(totais["total_descontos"]),
        _fmt(totais["salario_liquido"]),
        _fmt(totais["total_encargos"]),
        _fmt(custo_total),
    ], resumo_w, bold=True)

    # Detalhes proporcionais (se houver)
    props = []
    for tc in tecnicos:
        p = calcular_proporcional(tc.salario_bruto, tc.data_admissao, ano_ref, mes_ref)
        if p is not None:
            props.append((tc, p))

    if props:
        pdf.ln(5)
        _pdf_section(pdf, f"{t('valor_integral')} vs {t('valor_proporcional')}")
        det_cols = [
            t("tecnico"), t("dias_trabalhados"), t("fator_proporcional"),
            f"{t('salario_bruto')} ({t('valor_integral')})",
            f"{t('salario_bruto')} ({t('valor_proporcional')})",
            f"{t('total_encargos')} ({t('valor_integral')})",
            f"{t('total_encargos')} ({t('valor_proporcional')})",
        ]
        det_w = [50, 25, 25, 42, 42, 42, 42]
        _pdf_table_header(pdf, det_cols, det_w)
        for tc, p in props:
            fi = p["folha_integral"]
            fp = p["folha_proporcional"]
            _pdf_table_row(pdf, [
                tc.nome,
                f"{p['dias_trabalhados']}/{p['dias_no_mes']}",
                f"{float(p['fator']):.4f}",
                _fmt(fi["salario_bruto"]),
                _fmt(fp["salario_bruto"]),
                _fmt(fi["total_encargos"]),
                _fmt(fp["total_encargos"]),
            ], det_w)

    return bytes(pdf.output())


# ──────────────── Render Principal ─────────────────────────

def render():
    st.title("Folha de Pagamento")

    session = get_session()

    tab_cad, tab_calc, tab_gerar = st.tabs([
        "Cadastro de Tecnicos",
        "Calculo Mensal",
        "Gerar Lancamentos Recorrentes",
    ])

    with tab_cad:
        _aba_cadastro(session)

    with tab_calc:
        _aba_calculo(session)

    with tab_gerar:
        _aba_gerar_recorrentes(session)

    session.close()


# ──────────────── Aba: Cadastro ────────────────────────────

def _aba_cadastro(session):
    st.subheader("Cadastro de Tecnicos do Projeto")
    st.caption(
        "Registre os 4 tecnicos do projeto. Os encargos serao "
        "calculados isoladamente dos demais funcionarios da empresa."
    )

    # Formulario de criacao
    with st.expander("Novo Tecnico", expanded=False):
        with st.form("form_tecnico", clear_on_submit=True):
            col1, col2 = st.columns(2)
            nome = col1.text_input(
                "Nome *", max_chars=120, placeholder="Ex: Joao Silva",
            )
            custo = col2.number_input(
                "Custo Maximo Mensal (R$) *",
                min_value=0.01, step=100.0, format="%.2f",
                help="Valor total incluindo salario + FGTS + INSS + PIS + prov. 13o + prov. ferias",
            )
            data_adm = st.date_input(
                "Data de Admissao *", value=date.today(),
            )
            enviar = st.form_submit_button("Cadastrar Tecnico")

        # Preview do calculo inverso (fora do form para atualizar em tempo real)
        if custo and custo > 0:
            bruto_prev = calcular_bruto_de_custo(_to_decimal(custo))
            folha_prev = calcular_folha_tecnico(bruto_prev)
            st.caption(f"Fator de encargos: {float(FATOR_CUSTO_TOTAL):.4f}x")
            cp1, cp2, cp3, cp4 = st.columns(4)
            cp1.metric("Salario Bruto", f"R$ {bruto_prev:,.2f}")
            cp2.metric("Salario Liquido", f"R$ {folha_prev['salario_liquido']:,.2f}")
            cp3.metric("Encargos Patronais", f"R$ {folha_prev['total_encargos']:,.2f}")
            cp4.metric("Descontos (INSS+IRRF)", f"R$ {folha_prev['total_descontos']:,.2f}")

        if enviar:
            if not nome.strip():
                st.error("Nome e obrigatorio.")
            else:
                custo_dec = _to_decimal(custo)
                bruto_calc = calcular_bruto_de_custo(custo_dec)
                tec = Tecnico(
                    nome=nome.strip(),
                    custo_maximo=custo_dec,
                    salario_bruto=bruto_calc,
                    data_admissao=data_adm,
                    ativo=True,
                )
                session.add(tec)
                session.commit()
                st.success(f"Tecnico '{tec.nome}' cadastrado! Salario bruto calculado: R$ {bruto_calc:,.2f}")
                st.rerun()

    # Listagem
    tecnicos = (
        session.query(Tecnico)
        .filter(Tecnico.ativo == True)
        .order_by(Tecnico.nome)
        .all()
    )

    if not tecnicos:
        st.info("Nenhum tecnico cadastrado.")
        return

    dados = []
    for t in tecnicos:
        custo_str = f"R$ {t.custo_maximo:,.2f}" if t.custo_maximo else "—"
        folha_t = calcular_folha_tecnico(t.salario_bruto)
        dados.append({
            "Nome": t.nome,
            "Custo Maximo": custo_str,
            "Sal. Bruto": f"R$ {t.salario_bruto:,.2f}",
            "Sal. Liquido": f"R$ {folha_t['salario_liquido']:,.2f}",
            "Encargos": f"R$ {folha_t['total_encargos']:,.2f}",
            "Data Admissao": str(t.data_admissao),
        })
    st.dataframe(pd.DataFrame(dados), use_container_width=True, hide_index=True)

    # Edicao / exclusao
    st.markdown("---")
    st.markdown("**Editar, Desativar ou Excluir**")

    opcoes = {
        f"{t.nome} (Custo Max: R$ {t.custo_maximo:,.2f})" if t.custo_maximo
        else f"{t.nome} (Bruto: R$ {t.salario_bruto:,.2f})": t.id
        for t in tecnicos
    }
    sel = st.selectbox("Selecione o tecnico", list(opcoes.keys()), key="sel_tec_edit")
    tec_id = opcoes[sel]
    tec_obj = session.get(Tecnico, tec_id)

    col_ed, col_del = st.columns([3, 1])

    with col_ed:
        custo_atual = float(tec_obj.custo_maximo) if tec_obj.custo_maximo else float(tec_obj.salario_bruto * FATOR_CUSTO_TOTAL)
        with st.form(f"form_tec_edit_{tec_id}"):
            nome_ed = st.text_input("Nome", value=tec_obj.nome, key=f"tec_nome_{tec_id}")
            custo_ed = st.number_input(
                "Custo Maximo Mensal (R$)", value=custo_atual,
                min_value=0.01, step=100.0, format="%.2f", key=f"tec_custo_{tec_id}",
                help="Valor total incluindo salario + FGTS + INSS + PIS + prov. 13o + prov. ferias",
            )
            data_ed = st.date_input(
                "Data Admissao", value=tec_obj.data_admissao, key=f"tec_data_{tec_id}",
            )
            salvar = st.form_submit_button("Atualizar")

        # Preview do calculo inverso na edicao
        if custo_ed and custo_ed > 0:
            bruto_ed_prev = calcular_bruto_de_custo(_to_decimal(custo_ed))
            folha_ed_prev = calcular_folha_tecnico(bruto_ed_prev)
            ep1, ep2, ep3, ep4 = st.columns(4)
            ep1.metric("Salario Bruto", f"R$ {bruto_ed_prev:,.2f}")
            ep2.metric("Salario Liquido", f"R$ {folha_ed_prev['salario_liquido']:,.2f}")
            ep3.metric("Encargos Patronais", f"R$ {folha_ed_prev['total_encargos']:,.2f}")
            ep4.metric("Descontos (INSS+IRRF)", f"R$ {folha_ed_prev['total_descontos']:,.2f}")

        if salvar:
            custo_dec_ed = _to_decimal(custo_ed)
            bruto_calc_ed = calcular_bruto_de_custo(custo_dec_ed)
            tec_obj.nome = nome_ed.strip()
            tec_obj.custo_maximo = custo_dec_ed
            tec_obj.salario_bruto = bruto_calc_ed
            tec_obj.data_admissao = data_ed
            session.commit()
            st.success(f"Tecnico atualizado! Salario bruto: R$ {bruto_calc_ed:,.2f}")
            st.rerun()

    with col_del:
        if st.button("Desativar", type="primary", key=f"btn_desat_{tec_id}"):
            tec_obj.ativo = False
            session.commit()
            st.success(f"Tecnico '{tec_obj.nome}' desativado.")
            st.rerun()

        st.markdown("---")
        confirm_key = f"confirm_delete_{tec_id}"

        if st.session_state.get(confirm_key, False):
            st.warning(
                f"Confirma a exclusao definitiva de '{tec_obj.nome}'? "
                "Lancamentos recorrentes vinculados terao o tecnico desvinculado."
            )
            col_yes, col_no = st.columns(2)
            with col_yes:
                if st.button("Sim", type="primary", key=f"btn_confirm_del_{tec_id}"):
                    session.delete(tec_obj)
                    session.commit()
                    st.session_state.pop(confirm_key, None)
                    st.success(f"Tecnico '{tec_obj.nome}' excluido.")
                    st.rerun()
            with col_no:
                if st.button("Nao", key=f"btn_cancel_del_{tec_id}"):
                    st.session_state.pop(confirm_key, None)
                    st.rerun()
        else:
            if st.button("Excluir", type="secondary", key=f"btn_del_{tec_id}"):
                st.session_state[confirm_key] = True
                st.rerun()


# ──────────────── Aba: Calculo Mensal ──────────────────────

def _aba_calculo(session):
    st.subheader("Calculo da Folha Mensal")

    tecnicos = (
        session.query(Tecnico)
        .filter(Tecnico.ativo == True)
        .order_by(Tecnico.nome)
        .all()
    )

    if not tecnicos:
        st.info("Cadastre pelo menos um tecnico primeiro.")
        return

    # Seletor de mes/ano de referencia (para calculo proporcional)
    _meses_pt = [
        "", "Janeiro", "Fevereiro", "Marco", "Abril", "Maio", "Junho",
        "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
    ]
    col_mes, col_ano, _ = st.columns([1, 1, 4])
    with col_mes:
        mes_ref = st.selectbox(
            "Mes de Referencia",
            list(range(1, 13)),
            index=date.today().month - 1,
            format_func=lambda m: _meses_pt[m],
            key="mes_ref_calculo",
        )
    with col_ano:
        ano_ref = st.number_input(
            "Ano", min_value=2020, max_value=2035,
            value=date.today().year, key="ano_ref_calculo",
        )

    st.caption(
        "Tabelas vigentes: INSS 2026 (Portaria MPS/MF 13/2026) | "
        "IRRF 2026 (Lei 15.270/2025 — isencao ate R$5.000) | "
        "Empresa: Lucro Presumido"
    )

    nota_pdf = st.text_area(
        "Observacoes (opcional)",
        value=st.session_state.get("folha_nota", ""),
        placeholder="Adicione observacoes que serao incluidas no PDF...",
        height=80,
        key="folha_nota_input",
    )
    st.session_state["folha_nota"] = nota_pdf

    # Calcula para cada tecnico (com proporcional se aplicavel)
    linhas = []
    totais = {
        "salario_bruto": Decimal("0"), "inss": Decimal("0"),
        "irrf": Decimal("0"), "fgts": Decimal("0"),
        "pis": Decimal("0"), "inss_patronal": Decimal("0"),
        "prov_ferias": Decimal("0"), "prov_13": Decimal("0"),
        "salario_liquido": Decimal("0"),
        "total_descontos": Decimal("0"), "total_encargos": Decimal("0"),
    }

    for t in tecnicos:
        prop = calcular_proporcional(t.salario_bruto, t.data_admissao, ano_ref, mes_ref)
        if prop is not None:
            folha = prop["folha_proporcional"]
            linhas.append({"Nome": t.nome, "proporcional": prop, **folha})
        else:
            folha = calcular_folha_tecnico(t.salario_bruto)
            linhas.append({"Nome": t.nome, "proporcional": None, **folha})
        for k in totais:
            totais[k] += folha[k]

    # Tabela detalhada
    dados_display = []
    for l in linhas:
        tecnico_label = l["Nome"]
        if l["proporcional"] is not None:
            p = l["proporcional"]
            tecnico_label += f" (proporcional: {p['dias_trabalhados']}/{p['dias_no_mes']} dias)"
        dados_display.append({
            "Tecnico": tecnico_label,
            "Sal. Bruto": f"R$ {l['salario_bruto']:,.2f}",
            "INSS": f"R$ {l['inss']:,.2f}",
            "IRRF": f"R$ {l['irrf']:,.2f}",
            "Sal. Liquido": f"R$ {l['salario_liquido']:,.2f}",
            "INSS Patronal": f"R$ {l['inss_patronal']:,.2f}",
            "FGTS": f"R$ {l['fgts']:,.2f}",
            "PIS": f"R$ {l['pis']:,.2f}",
            "Prov. Ferias": f"R$ {l['prov_ferias']:,.2f}",
            "Prov. 13o": f"R$ {l['prov_13']:,.2f}",
        })

    st.dataframe(pd.DataFrame(dados_display), use_container_width=True, hide_index=True)

    # Detalhe proporcional (comparacao integral vs proporcional)
    proporcional_info = [l for l in linhas if l.get("proporcional") is not None]
    if proporcional_info:
        st.markdown("---")
        st.markdown("**Calculo Proporcional — Mes de Admissao**")
        for l in proporcional_info:
            p = l["proporcional"]
            fi = p["folha_integral"]
            fp = p["folha_proporcional"]
            with st.expander(
                f"{l['Nome']} — {p['dias_trabalhados']}/{p['dias_no_mes']} dias "
                f"(fator: {float(p['fator']):.4f})",
                expanded=True,
            ):
                col_i, col_p = st.columns(2)
                with col_i:
                    st.markdown("**Valor Integral (mes completo)**")
                    st.metric("Salario Bruto", f"R$ {fi['salario_bruto']:,.2f}")
                    st.metric("Salario Liquido", f"R$ {fi['salario_liquido']:,.2f}")
                    st.metric("Total Encargos", f"R$ {fi['total_encargos']:,.2f}")
                    custo_int = _q2(fi['salario_bruto'] + fi['total_encargos'])
                    st.metric("Custo Total", f"R$ {custo_int:,.2f}")
                with col_p:
                    st.markdown("**Valor Proporcional (aplicado)**")
                    st.metric("Salario Bruto", f"R$ {fp['salario_bruto']:,.2f}")
                    st.metric("Salario Liquido", f"R$ {fp['salario_liquido']:,.2f}")
                    st.metric("Total Encargos", f"R$ {fp['total_encargos']:,.2f}")
                    custo_prop = _q2(fp['salario_bruto'] + fp['total_encargos'])
                    st.metric("Custo Total", f"R$ {custo_prop:,.2f}")

    # Totalizadores
    st.markdown("---")
    st.markdown("**Resumo Mensal Consolidado**")

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Salarios Brutos", f"R$ {totais['salario_bruto']:,.2f}")
    col2.metric("Total Descontos (INSS+IRRF)", f"R$ {totais['total_descontos']:,.2f}")
    col3.metric("Total Salarios Liquidos", f"R$ {totais['salario_liquido']:,.2f}")

    col4, col5, col6 = st.columns(3)
    col4.metric("Total Encargos Patronais", f"R$ {totais['total_encargos']:,.2f}")
    col5.metric("INSS Patronal", f"R$ {totais['inss_patronal']:,.2f}")
    col6.metric("FGTS Total", f"R$ {totais['fgts']:,.2f}")

    col7, col8, col9 = st.columns(3)
    col7.metric("PIS sobre Folha", f"R$ {totais['pis']:,.2f}")
    col8.metric("Provisao Ferias", f"R$ {totais['prov_ferias']:,.2f}")
    col9.metric("Provisao 13o", f"R$ {totais['prov_13']:,.2f}")

    # Custo total mensal do projeto com pessoal
    custo_total = _q2(totais['salario_bruto'] + totais['total_encargos'])
    st.markdown("---")
    st.metric(
        "Custo Total Mensal (Salarios + Encargos)",
        f"R$ {custo_total:,.2f}",
    )

    # Exportacao PDF
    st.markdown("---")
    col_lang, col_exp, col_dl, _ = st.columns([1, 1, 1, 3])
    with col_lang:
        idioma_folha = st.selectbox(
            "Idioma do PDF", ["Portugues", "Espanhol"], key="idioma_folha_pdf",
        )
        lang_code = "pt" if idioma_folha == "Portugues" else "es"
    with col_exp:
        st.markdown("")  # espacamento vertical
        if st.button("Exportar PDF", type="secondary", key="btn_export_folha"):
            pdf_bytes = _gerar_pdf_folha(session, lang_code, ano_ref, mes_ref, nota=nota_pdf)
            st.session_state["folha_pdf"] = pdf_bytes
            st.session_state["folha_pdf_lang"] = lang_code
            st.rerun()
    if "folha_pdf" in st.session_state:
        with col_dl:
            st.markdown("")  # espacamento vertical
            st.download_button(
                "Baixar PDF",
                data=st.session_state["folha_pdf"],
                file_name=f"folha_pagamento_{ano_ref}{mes_ref:02d}.pdf",
                mime="application/pdf",
                type="primary",
                key="btn_dl_folha_pdf",
            )


# ─────────── Aba: Gerar Lancamentos Recorrentes ───────────

def _aba_gerar_recorrentes(session):
    st.subheader("Gerar Lancamentos Recorrentes da Folha")
    st.caption(
        "Gera lancamentos recorrentes automaticos a partir dos dados da folha. "
        "Selecione a categoria de destino para cada componente."
    )

    tecnicos = (
        session.query(Tecnico)
        .filter(Tecnico.ativo == True)
        .order_by(Tecnico.nome)
        .all()
    )

    if not tecnicos:
        st.info("Cadastre pelo menos um tecnico primeiro.")
        return

    opcoes_cat = _opcoes_categorias(session)
    if not opcoes_cat:
        st.warning("Cadastre categorias de despesa antes de gerar lancamentos.")
        return

    cat_keys = list(opcoes_cat.keys())

    st.markdown("**Selecione as categorias de destino para cada componente:**")

    with st.form("form_gerar_recorrentes"):
        cat_salario = st.selectbox("Salario Liquido", cat_keys, key="cat_sal")
        cat_inss_emp = st.selectbox("INSS Empregado", cat_keys, key="cat_inss_emp")
        cat_irrf = st.selectbox("IRRF", cat_keys, key="cat_irrf")
        cat_inss_pat = st.selectbox("INSS Patronal", cat_keys, key="cat_inss_pat")
        cat_fgts = st.selectbox("FGTS", cat_keys, key="cat_fgts")
        cat_pis = st.selectbox("PIS sobre Folha", cat_keys, key="cat_pis")
        cat_ferias = st.selectbox("Provisao de Ferias", cat_keys, key="cat_ferias")
        cat_13 = st.selectbox("Provisao 13o Salario", cat_keys, key="cat_13")

        col_d1, col_d2 = st.columns(2)
        data_inicio = col_d1.date_input("Data de inicio", value=date.today())
        data_fim = col_d2.date_input("Data de fim", value=date(2027, 12, 31))

        gerar = st.form_submit_button("Gerar Lancamentos Recorrentes", type="primary")

    if gerar:
        # Remove recorrentes antigos vinculados a tecnicos
        antigos = (
            session.query(LancamentoRecorrente)
            .filter(LancamentoRecorrente.tecnico_id != None)
            .all()
        )
        for ant in antigos:
            session.delete(ant)

        count = 0
        for t in tecnicos:
            folha = calcular_folha_tecnico(t.salario_bruto)

            componentes = [
                (cat_salario, folha["salario_liquido"], f"Salario Liquido - {t.nome}"),
                (cat_inss_emp, folha["inss"], f"INSS Empregado - {t.nome}"),
                (cat_irrf, folha["irrf"], f"IRRF - {t.nome}"),
                (cat_inss_pat, folha["inss_patronal"], f"INSS Patronal - {t.nome}"),
                (cat_fgts, folha["fgts"], f"FGTS - {t.nome}"),
                (cat_pis, folha["pis"], f"PIS Folha - {t.nome}"),
                (cat_ferias, folha["prov_ferias"], f"Prov. Ferias - {t.nome}"),
                (cat_13, folha["prov_13"], f"Prov. 13o - {t.nome}"),
            ]

            for cat_key, valor, desc in componentes:
                if valor <= 0:
                    continue
                lr = LancamentoRecorrente(
                    categoria_despesa_id=opcoes_cat[cat_key],
                    valor_brl=valor,
                    descricao=desc,
                    frequencia="MENSAL",
                    data_inicio=data_inicio,
                    data_fim=data_fim,
                    tecnico_id=t.id,
                    ativo=True,
                )
                session.add(lr)
                count += 1

        session.commit()
        st.success(f"{count} lancamentos recorrentes gerados com sucesso!")
        st.rerun()

    # Mostra recorrentes existentes vinculados a tecnicos
    existentes = (
        session.query(LancamentoRecorrente)
        .filter(LancamentoRecorrente.tecnico_id != None, LancamentoRecorrente.ativo == True)
        .all()
    )

    if existentes:
        st.markdown("---")
        st.markdown("**Lancamentos Recorrentes da Folha (existentes):**")
        dados = []
        for lr in existentes:
            cat = lr.categoria_despesa
            tec = lr.tecnico
            dados.append({
                "Tecnico": tec.nome if tec else "—",
                "Descricao": lr.descricao,
                "Valor": f"R$ {lr.valor_brl:,.2f}",
                "Categoria": f"{cat.centro_custo.codigo} | {cat.nome}",
                "Periodo": f"{lr.data_inicio} a {lr.data_fim}",
            })
        st.dataframe(pd.DataFrame(dados), use_container_width=True, hide_index=True)
