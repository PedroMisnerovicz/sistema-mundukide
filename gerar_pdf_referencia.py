"""
Gera o PDF de referencia 'tabelas_folha_pagamento.pdf' a partir das constantes
atuais em modulos/folha_pagamento.py. Rode quando alterar tabelas/aliquotas.
"""

from datetime import date
from decimal import Decimal
from fpdf import FPDF

from modulos.folha_pagamento import (
    TABELA_INSS_2026,
    TETO_INSS_2026,
    TABELA_IRRF_2026,
    LIMITE_ISENCAO_IRRF,
    LIMITE_REDUCAO_IRRF,
    ALIQUOTA_INSS_PATRONAL,
    ALIQUOTA_FGTS,
    ALIQUOTA_PIS_FOLHA,
    ALIQUOTA_TERCEIROS,
    FATOR_CUSTO_TOTAL,
)


def _fmt_brl(v: Decimal) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _section(pdf: FPDF, titulo: str):
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_fill_color(220, 230, 241)
    pdf.set_text_color(44, 62, 80)
    pdf.cell(0, 8, " " + titulo, border=0, fill=True, ln=1)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)


def _caption(pdf: FPDF, txt: str):
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(90, 90, 90)
    pdf.multi_cell(0, 4, txt)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(1)


def _table_header(pdf: FPDF, cols: list[str], widths: list[int]):
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(52, 152, 219)
    pdf.set_text_color(255, 255, 255)
    for c, w in zip(cols, widths):
        pdf.cell(w, 7, c, border=1, fill=True, align="C")
    pdf.ln()
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 9)


def _row(pdf: FPDF, vals: list[str], widths: list[int], aligns: list[str] | None = None):
    if aligns is None:
        aligns = ["L"] + ["R"] * (len(vals) - 1)
    for v, w, a in zip(vals, widths, aligns):
        pdf.cell(w, 6, v, border=1, align=a)
    pdf.ln()


def gerar():
    pdf = FPDF(orientation="P", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Tabelas da Folha de Pagamento - Sistema Mundukide", ln=1, align="C")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 5, f"Referencia tecnica | Gerado em {date.today().strftime('%d/%m/%Y')}", ln=1, align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(5)

    # ── Secao 1: Tabela INSS Empregado 2026 ──
    _section(pdf, "1. Tabela INSS Empregado 2026")
    _caption(pdf, f"Fonte: Portaria Interministerial MPS/MF 13/2026 | Metodo: (Salario x Aliquota) - Parcela Deduzir | Teto maximo: R$ {_fmt_brl(TETO_INSS_2026)}")
    cols = ["Faixa", "Salario ate (R$)", "Aliquota", "Parcela a Deduzir (R$)"]
    widths = [25, 50, 35, 70]
    _table_header(pdf, cols, widths)
    for i, (limite, aliq, deduz) in enumerate(TABELA_INSS_2026, 1):
        _row(pdf, [
            f"{i}a Faixa",
            _fmt_brl(limite),
            f"{aliq*100:.1f}%".replace(".", ","),
            _fmt_brl(deduz),
        ], widths, aligns=["C", "R", "C", "R"])
    pdf.ln(4)

    # ── Secao 2: Tabela IRRF 2026 ──
    _section(pdf, "2. Tabela IRRF 2026")
    _caption(pdf,
        f"Fonte: Lei 15.270/2025 (vigencia 2026) | Base: Salario bruto - INSS | "
        f"Isencao total para salario bruto <= R$ {_fmt_brl(LIMITE_ISENCAO_IRRF)} | "
        f"Reducao gradual entre R$ {_fmt_brl(LIMITE_ISENCAO_IRRF)} e R$ {_fmt_brl(LIMITE_REDUCAO_IRRF)}"
    )
    cols = ["Faixa", "Base de Calculo ate (R$)", "Aliquota", "Parcela a Deduzir (R$)"]
    widths = [25, 60, 30, 65]
    _table_header(pdf, cols, widths)
    for i, (limite, aliq, deduz) in enumerate(TABELA_IRRF_2026, 1):
        if limite >= Decimal("99999999"):
            base_str = f"Acima de {_fmt_brl(TABELA_IRRF_2026[-2][0])}"
        else:
            base_str = _fmt_brl(limite)
        aliq_str = "Isento" if aliq == 0 else f"{aliq*100:.1f}%".replace(".", ",")
        deduz_str = "-" if deduz == 0 else _fmt_brl(deduz)
        _row(pdf, [
            f"{i}a Faixa",
            base_str,
            aliq_str,
            deduz_str,
        ], widths, aligns=["C", "R", "C", "R"])
    pdf.ln(4)

    # ── Secao 3: Encargos Patronais ──
    _section(pdf, "3. Encargos Patronais - Empresa Lucro Presumido")
    fator_str = f"{float(FATOR_CUSTO_TOTAL):.4f}".replace(".", ",")
    _caption(pdf,
        f"Aliquotas fixas sobre o salario bruto. Fator total = 1 + 20% + 8% + 1% + 5,8% + 4/36 + 1/12 = aprox. {fator_str}x  "
        f"=> Custo total = Salario bruto x Fator"
    )
    cols = ["Encargo", "Aliquota", "Base de Calculo", "Observacao"]
    widths = [45, 25, 35, 75]
    _table_header(pdf, cols, widths)
    encargos = [
        ("INSS Patronal", f"{ALIQUOTA_INSS_PATRONAL*100:.1f}%".replace(".", ","), "Salario bruto", "Empresa Lucro Presumido"),
        ("FGTS", f"{ALIQUOTA_FGTS*100:.1f}%".replace(".", ","), "Salario bruto", "Fundo de Garantia por Tempo de Servico"),
        ("PIS sobre Folha", f"{ALIQUOTA_PIS_FOLHA*100:.1f}%".replace(".", ","), "Salario bruto", "Contribuicao social sobre a folha"),
        ("Terceiros (Sistema S)", f"{ALIQUOTA_TERCEIROS*100:.1f}%".replace(".", ","), "Salario bruto", "Sal-Educ + INCRA + SESI + SENAI + SEBRAE"),
        ("Provisao Ferias", "11,11% (4/36)", "Salario bruto", "Inclui adicional de 1/3 / 12 meses"),
        ("Provisao 13o Salario", "8,33% (1/12)", "Salario bruto", "Gratificacao natalina proporcional"),
    ]
    for nome, aliq, base, obs in encargos:
        _row(pdf, [nome, aliq, base, obs], widths, aligns=["L", "C", "C", "L"])
    pdf.ln(4)

    # ── Secao 4: Componentes Calculados ──
    _section(pdf, "4. Componentes Calculados por Tecnico")
    _caption(pdf, "Todos os itens abaixo sao calculados automaticamente pelo sistema a partir do salario bruto de cada tecnico.")
    cols = ["Componente", "Tipo", "Formula / Regra"]
    widths = [50, 35, 95]
    _table_header(pdf, cols, widths)
    componentes = [
        ("INSS Empregado", "Desconto", "Tabela progressiva INSS 2026 (Secao 1)"),
        ("IRRF", "Desconto", "Tabela IRRF 2026 + isencao Lei 15.270/2025 (Secao 2)"),
        ("Salario Liquido", "Resultado", "Salario Bruto - INSS - IRRF"),
        ("INSS Patronal", "Encargo patronal", "Salario Bruto x 20%"),
        ("FGTS", "Encargo patronal", "Salario Bruto x 8%"),
        ("PIS Folha", "Encargo patronal", "Salario Bruto x 1%"),
        ("Terceiros (Sistema S)", "Encargo patronal", "Salario Bruto x 5,8%"),
        ("Provisao Ferias", "Encargo patronal", "Salario Bruto x 4/36 (aprox. 11,11%)"),
        ("Provisao 13o", "Encargo patronal", "Salario Bruto / 12 (aprox. 8,33%)"),
        ("Total Descontos", "Consolidado", "INSS Empregado + IRRF"),
        ("Total Encargos", "Consolidado", "INSS Pat + FGTS + PIS + Terceiros + Prov.Ferias + Prov.13o"),
        ("Custo Total Mensal", "Consolidado", "Salario Bruto + Total Encargos"),
    ]
    for nome, tipo, formula in componentes:
        _row(pdf, [nome, tipo, formula], widths, aligns=["L", "C", "L"])
    pdf.ln(4)

    # ── Secao 5: Modelo de Dados ──
    pdf.add_page()
    _section(pdf, "5. Modelo de Dados - Tabela tecnicos (banco de dados)")
    _caption(pdf, "Tabela que armazena os dados cadastrais de cada tecnico. O salario bruto e calculado automaticamente pelo sistema a partir do custo maximo informado.")
    cols = ["Campo", "Tipo", "Descricao"]
    widths = [40, 35, 105]
    _table_header(pdf, cols, widths)
    campos = [
        ("id", "Integer (PK)", "Identificador unico automatico"),
        ("nome", "String(120)", "Nome completo do tecnico"),
        ("custo_maximo", "Numeric(14,2)", "Custo total maximo mensal (salario + todos os encargos)"),
        ("salario_bruto", "Numeric(14,2)", f"Calculado: custo_maximo / Fator (aprox. {fator_str})"),
        ("data_admissao", "Date", "Data de admissao - usada para calculo proporcional no mes de entrada"),
        ("ativo", "Boolean", "Indica se o tecnico esta ativo e deve aparecer nos calculos"),
    ]
    for campo, tipo, desc in campos:
        _row(pdf, [campo, tipo, desc], widths, aligns=["C", "C", "L"])

    pdf.output("tabelas_folha_pagamento.pdf")
    print("PDF gerado: tabelas_folha_pagamento.pdf")


if __name__ == "__main__":
    gerar()
