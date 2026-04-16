"""
Modulo Carimbo de Documentos
============================
Aplica carimbo padronizado AVCD/Mundukide em todas as paginas
de um ou mais PDFs enviados pelo usuario.

Carimbo:
  Linha 1 — Financiado pela AGENCIA VASCA DE COOPERACIÓN AL DESARROLLO (AVCD)
  Linha 2 — PRO-2025K2/0002
  Linha 3 — BRASIL

Tamanho: ~8,8 cm x 2,6 cm | Posicao: canto inferior direito, margem 1 cm
Biblioteca: PyMuPDF (fitz)
"""

import io
import random
import zipfile
from pathlib import Path

import fitz  # PyMuPDF
import streamlit as st

# ─── Constantes do carimbo ─────────────────────────────────────────────────

_CM       = 28.3465          # pontos por centimetro (1 inch = 72 pt; 1 cm = 72/2.54)
_LARG     = 8.8 * _CM        # largura  ~249.4 pt
_ALT      = 2.6 * _CM        # altura   ~73.7 pt
_MARG     = 1.0 * _CM        # margem   ~28.3 pt

_LINHA1   = "Financiado pela AGENCIA VASCA DE COOPERACIÓN AL DESARROLLO (AVCD)"
_LINHA2   = "PRO-2025K2/0002"
_LINHA3   = "BRASIL"

_FONTE    = "Helvetica-Bold"  # nome correto no PyMuPDF >= 1.24
_FS1      = 7.0               # fontsize linha 1 (texto longo — quebra em 2 linhas)
_FS2      = 9.0               # fontsize linhas 2 e 3

# Layout interno do carimbo (em pontos)
_PAD_H    = 6.0               # margem horizontal interna
_H_L1     = 24.0              # altura alocada para linha 1 (2 linhas × ~12pt)
_H_L2     = 16.0              # altura alocada para linha 2
_H_L3     = 16.0              # altura alocada para linha 3
_GAP      = 3.0               # espacamento entre blocos de linha

# Aparencia de carimbo manual (simula carimbo de borracha aplicado a mao)
_COR_TINTA  = (0.0, 0.18, 0.55)   # azul escuro — cor tipica de tinta de carimbo
_OPACIDADE  = 0.82                  # leve transparencia (simula absorcao da tinta no papel)
_BORDA_W    = 1.5                   # borda ligeiramente mais encorpada
_JITTER     = 3.0                   # variacao de posicao em pontos (~1 mm por eixo)
_ROT_MAX    = 2.0                   # rotacao maxima em graus (±)


# ─── Logica de carimbagem ──────────────────────────────────────────────────

def _carimbar_pagina(page: fitz.Page) -> None:
    """Aplica o carimbo com aparencia manual (in-place).

    Cada pagina recebe uma leve rotacao e variacao de posicao
    aleatorias para simular um carimbo de borracha aplicado a mao.
    """
    pw = page.rect.width
    ph = page.rect.height

    # Pequena variacao de posicao por pagina (~1 mm por eixo)
    jx = random.uniform(-_JITTER, _JITTER)
    jy = random.uniform(-_JITTER, _JITTER)

    x0 = pw - _MARG - _LARG + jx
    y0 = ph - _MARG - _ALT + jy
    x1 = pw - _MARG + jx
    y1 = ph - _MARG + jy

    caixa = fitz.Rect(x0, y0, x1, y1)

    # Rotacao sutil aleatoria em torno do centro do carimbo
    angulo = random.uniform(-_ROT_MAX, _ROT_MAX)
    centro = fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)
    morph = (centro, fitz.Matrix(1, 1).prerotate(angulo))

    # Retangulo: sem fundo (transparente) + borda azul (tinta de carimbo)
    page.draw_rect(caixa, color=_COR_TINTA, fill=None, width=_BORDA_W,
                   morph=morph, stroke_opacity=_OPACIDADE)

    # Centraliza verticalmente o grupo das 3 linhas dentro da caixa
    conteudo_h = _H_L1 + _GAP + _H_L2 + _GAP + _H_L3
    pad_v = (_ALT - conteudo_h) / 2

    tx0 = x0 + _PAD_H
    tx1 = x1 - _PAD_H
    sy  = y0 + pad_v

    r1 = fitz.Rect(tx0, sy, tx1, sy + _H_L1);  sy += _H_L1 + _GAP
    r2 = fitz.Rect(tx0, sy, tx1, sy + _H_L2);  sy += _H_L2 + _GAP
    r3 = fitz.Rect(tx0, sy, tx1, sy + _H_L3)

    page.insert_textbox(r1, _LINHA1, fontname=_FONTE, fontsize=_FS1,
                        color=_COR_TINTA, align=1, morph=morph,
                        fill_opacity=_OPACIDADE)
    page.insert_textbox(r2, _LINHA2, fontname=_FONTE, fontsize=_FS2,
                        color=_COR_TINTA, align=1, morph=morph,
                        fill_opacity=_OPACIDADE)
    page.insert_textbox(r3, _LINHA3, fontname=_FONTE, fontsize=_FS2,
                        color=_COR_TINTA, align=1, morph=morph,
                        fill_opacity=_OPACIDADE)


def _aplicar_carimbo(pdf_bytes: bytes) -> bytes:
    """Recebe bytes de um PDF, carimba todas as paginas e retorna os bytes resultantes."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page in doc:
        _carimbar_pagina(page)
    buf = io.BytesIO()
    doc.save(buf, garbage=4, deflate=True)
    doc.close()
    return buf.getvalue()


# ─── Interface Streamlit ───────────────────────────────────────────────────

def render() -> None:
    st.title("🔖 Carimbo de Documentos")
    st.caption(
        "Aplica automaticamente o carimbo padronizado do projeto "
        "**AVCD / Mundukide** em todas as paginas dos PDFs enviados. "
        "O arquivo original nunca e alterado."
    )

    uploaded = st.file_uploader(
        "Selecione um ou mais arquivos PDF",
        type=["pdf"],
        accept_multiple_files=True,
        key="carimbo_upload",
    )

    if not uploaded:
        st.info("Faca upload de um ou mais arquivos PDF para comecar.")
        return

    # ── Leitura e validacao dos arquivos ────────────────────────────────────
    dados: list[dict] = []
    for uf in uploaded:
        raw = uf.read()
        try:
            doc   = fitz.open(stream=raw, filetype="pdf")
            npag  = len(doc)
            doc.close()
            dados.append({"nome": uf.name, "paginas": npag, "bytes": raw, "erro": None})
        except Exception as exc:
            dados.append({"nome": uf.name, "paginas": 0, "bytes": raw, "erro": str(exc)})

    # ── Preview dos arquivos ────────────────────────────────────────────────
    st.subheader("Arquivos recebidos")

    col_n, col_p, col_s = st.columns([4, 1, 1])
    col_n.markdown("**Arquivo**")
    col_p.markdown("**Paginas**")
    col_s.markdown("**Status**")

    total_pag = 0
    for d in dados:
        c1, c2, c3 = st.columns([4, 1, 1])
        c1.text(d["nome"])
        if d["erro"]:
            c2.text("—")
            c3.markdown("❌ Invalido")
        else:
            c2.text(str(d["paginas"]))
            c3.markdown("✅ OK")
            total_pag += d["paginas"]

    validos = [d for d in dados if d["erro"] is None]
    com_erro = [d for d in dados if d["erro"] is not None]

    for d in com_erro:
        st.error(f"**{d['nome']}**: nao foi possivel ler o arquivo — {d['erro']}")

    if not validos:
        st.warning("Nenhum arquivo valido para processar.")
        return

    st.caption(
        f"**{len(validos)}** arquivo(s) valido(s) | "
        f"**{total_pag}** pagina(s) a carimbar"
    )

    st.divider()

    # ── Modelo do carimbo (preview textual) ─────────────────────────────────
    with st.expander("Ver modelo do carimbo"):
        st.markdown(
            """
            <div style="
                border: 2px solid rgba(0, 46, 140, 0.82);
                padding: 10px 16px;
                display: inline-block;
                background: transparent;
                font-family: Arial, sans-serif;
                text-align: center;
                min-width: 380px;
                color: rgba(0, 46, 140, 0.82);
                transform: rotate(-1.5deg);
            ">
                <div style="font-weight:bold; font-size:11px;">
                    Financiado pela AGENCIA VASCA DE COOPERACIÓN AL<br>DESARROLLO (AVCD)
                </div>
                <div style="font-weight:bold; font-size:14px; margin-top:4px;">
                    PRO-2025K2/0002
                </div>
                <div style="font-weight:bold; font-size:14px; margin-top:2px;">
                    BRASIL
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.caption(
            "Aparencia de carimbo manual: tinta azul, leve rotacao e "
            "variacao de posicao em cada pagina. "
            "Posicao: canto inferior direito, margem de ~1 cm das bordas."
        )

    # ── Botao de processamento ───────────────────────────────────────────────
    if st.button("🔖 Aplicar Carimbo", type="primary", key="btn_aplicar_carimbo"):
        resultados: list[tuple[str, bytes, int]] = []   # (nome_saida, bytes, n_pag)
        erros_proc: list[tuple[str, str]] = []

        barra = st.progress(0, text="Iniciando...")

        for i, d in enumerate(validos):
            barra.progress(i / len(validos), text=f"Carimbando: {d['nome']}")
            try:
                saida      = _aplicar_carimbo(d["bytes"])
                stem       = Path(d["nome"]).stem
                nome_saida = f"{stem}_carimbado.pdf"
                resultados.append((nome_saida, saida, d["paginas"]))
            except Exception as exc:
                erros_proc.append((d["nome"], str(exc)))

        barra.progress(1.0, text="Concluido!")
        barra.empty()

        # Erros de processamento
        for nome, msg in erros_proc:
            st.error(f"Erro ao carimbar **{nome}**: {msg}")

        if not resultados:
            st.error("Nenhum arquivo foi carimbado com sucesso.")
            return

        total_carimbadas = sum(r[2] for r in resultados)
        st.success(
            f"Carimbo aplicado com sucesso em **{total_carimbadas}** pagina(s) "
            f"de **{len(resultados)}** arquivo(s)."
        )

        # ── Download ────────────────────────────────────────────────────────
        if len(resultados) == 1:
            nome_saida, conteudo, _ = resultados[0]
            st.download_button(
                label=f"⬇️ Baixar {nome_saida}",
                data=conteudo,
                file_name=nome_saida,
                mime="application/pdf",
                key="dl_unico",
            )
        else:
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for nome_saida, conteudo, _ in resultados:
                    zf.writestr(nome_saida, conteudo)
            zip_buf.seek(0)
            st.download_button(
                label=f"⬇️ Baixar ZIP com {len(resultados)} arquivos carimbados",
                data=zip_buf.getvalue(),
                file_name="documentos_carimbados.zip",
                mime="application/zip",
                key="dl_zip",
            )
