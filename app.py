"""
Sistema Mundukide — Controle Financeiro
Ponto de entrada Streamlit.
"""

import streamlit as st
from database import init_db

init_db()

st.set_page_config(
    page_title="Mundukide — Controle Financeiro",
    page_icon="💶",
    layout="wide",
)


# --- Tela de autenticacao ---
def check_password():
    """Retorna True se o usuario digitou a senha correta."""
    if st.session_state.get("authenticated"):
        return True

    st.title("Mundukide - Controle Financeiro")
    st.subheader("Acesso restrito")
    password = st.text_input("Digite a senha de acesso:", type="password")
    if st.button("Entrar"):
        if password == st.secrets["passwords"]["app_password"]:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Senha incorreta. Tente novamente.")
    return False


if not check_password():
    st.stop()
# --- Fim da autenticacao ---

MODULOS = {
    "Cadastros": "cadastros",
    "Lancamentos": "lancamentos",
    "Importacao OFX": "importacao_ofx",
    "Conciliacao": "conciliacao",
    "Folha de Pagamento": "folha_pagamento",
    "Fluxo de Caixa": "fluxo_caixa",
    "Dashboard": "dashboard",
    "Carimbo de Documentos": "carimbo_pdf",
}

st.sidebar.title("Mundukide")
st.sidebar.caption("Controle Financeiro")
st.sidebar.markdown("---")

escolha = st.sidebar.radio("Navegacao", list(MODULOS.keys()))

modulo_nome = MODULOS[escolha]

if modulo_nome == "cadastros":
    from modulos.cadastros import render
elif modulo_nome == "lancamentos":
    from modulos.lancamentos import render
elif modulo_nome == "importacao_ofx":
    from modulos.importacao_ofx import render
elif modulo_nome == "conciliacao":
    from modulos.conciliacao import render
elif modulo_nome == "folha_pagamento":
    from modulos.folha_pagamento import render
elif modulo_nome == "fluxo_caixa":
    from modulos.fluxo_caixa import render
elif modulo_nome == "dashboard":
    from modulos.dashboard import render
elif modulo_nome == "carimbo_pdf":
    from modulos.carimbo_pdf import render

render()
