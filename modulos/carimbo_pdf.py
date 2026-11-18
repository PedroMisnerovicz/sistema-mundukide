"""
Modulo Carimbo de Documentos
============================
Tela Streamlit que aplica o carimbo padronizado AVCD/Mundukide em todas
as paginas de um ou mais PDFs enviados pelo usuario.

A logica do carimbo fica em modulos/carimbo_core.py (compartilhada com
o programa avulso em carimbo_exe/).
"""

import io
import zipfile
from pathlib import Path

import fitz  # PyMuPDF
import streamlit as st

from modulos.carimbo_core import _aplicar_carimbo


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
            "Posicao: centralizado na pagina, com pequenas variacoes para "
            "simular um carimbo aplicado a mao."
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
