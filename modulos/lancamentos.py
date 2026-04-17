"""
Modulo Lancamentos
==================
CRUD de despesas manuais — lancamentos que existem independentemente do OFX.
Depois serao "casados" com transacoes bancarias na conciliacao mensal.
Inclui funcionalidade de lancamentos recorrentes.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

import pandas as pd
import streamlit as st
from sqlalchemy import func as sqlfunc

from database import get_session
from models import (
    CentroCusto,
    CategoriaDespesa,
    ItemDespesa,
    LancamentoRecorrente,
    Reembolso,
    Remessa,
)
from modulos.cache_utils import (
    cambio_medio_cached,
    opcoes_categorias as opcoes_categorias_cached,
)


def _to_decimal(valor, fallback=Decimal("0.00")):
    try:
        return Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        return fallback


def _cambio_medio(session=None) -> Decimal:
    """Cambio medio ponderado (cacheado por 30s)."""
    return cambio_medio_cached()


def _opcoes_categorias(session=None):
    """Retorna dict {label: id} de categorias (cacheadas por 30s)."""
    return opcoes_categorias_cached()


def render():
    st.title("✏️ Lancamentos")

    session = get_session()

    categorias = session.query(CategoriaDespesa).first()
    if not categorias:
        st.warning(
            "Nenhuma categoria de despesa cadastrada. "
            "Va em Cadastros > Categorias de Despesas primeiro."
        )
        session.close()
        return

    tab_novo, tab_lista, tab_reemb, tab_recorrentes = st.tabs([
        "Novo Lancamento",
        "Lancamentos Registrados",
        "Reembolsos",
        "Lancamentos Recorrentes",
    ])

    with tab_novo:
        _aba_novo(session)

    with tab_lista:
        _aba_lista(session)

    with tab_reemb:
        _aba_reembolsos(session)

    with tab_recorrentes:
        _aba_recorrentes(session)

    session.close()


# ──────────────── aba: novo lancamento ──────────────────────

def _verificar_teto_categoria(session, cat_id, valor_novo, data_ref=None):
    """Verifica se o lancamento ultrapassa o teto da categoria. Retorna alerta ou None."""
    cat = session.get(CategoriaDespesa, cat_id)
    if not cat or not cat.tipo_teto:
        return None

    # Teto BRL: usa teto_eur * cambio_medio se disponivel, caso contrario teto_brl legado
    if cat.teto_eur:
        cambio = _cambio_medio(session)
        teto_ref = (cat.teto_eur * cambio).quantize(Decimal("0.01"))
        teto_info = f"€ {cat.teto_eur:,.2f} × R$ {cambio:,.4f} = R$ {teto_ref:,.2f}"
    elif cat.teto_brl:
        teto_ref = cat.teto_brl
        teto_info = f"R$ {teto_ref:,.2f}"
    else:
        return None

    if cat.tipo_teto == "GLOBAL":
        gasto_atual = Decimal(str(
            session.query(sqlfunc.coalesce(sqlfunc.sum(ItemDespesa.valor_brl), 0))
            .filter(ItemDespesa.categoria_despesa_id == cat_id)
            .scalar()
        ))
    else:  # MENSAL
        if data_ref is None:
            data_ref = date.today()
        from sqlalchemy import extract
        gasto_atual = Decimal(str(
            session.query(sqlfunc.coalesce(sqlfunc.sum(ItemDespesa.valor_brl), 0))
            .filter(
                ItemDespesa.categoria_despesa_id == cat_id,
                extract("year", ItemDespesa.data) == data_ref.year,
                extract("month", ItemDespesa.data) == data_ref.month,
            )
            .scalar()
        ))

    total_com_novo = gasto_atual + _to_decimal(valor_novo)
    pct = (total_com_novo / teto_ref * 100) if teto_ref > 0 else Decimal("0")
    tipo_label = "mensal" if cat.tipo_teto == "MENSAL" else "global"

    if pct > 100:
        return ("error", f"ALERTA: Teto {tipo_label} da categoria '{cat.nome}' ULTRAPASSADO! "
                f"Teto: {teto_info} | Gasto: R$ {total_com_novo:,.2f} ({pct:.1f}%)")
    elif pct >= 80:
        return ("warning", f"Atencao: Teto {tipo_label} da categoria '{cat.nome}' proximo! "
                f"Teto: {teto_info} | Gasto: R$ {total_com_novo:,.2f} ({pct:.1f}%)")
    return None


def _aba_novo(session):
    st.subheader("Registrar Despesa")
    st.caption("Lance a despesa agora. Depois, na conciliacao mensal, vincule ao extrato OFX.")

    opcoes_cat = _opcoes_categorias(session)

    with st.form("form_lancamento", clear_on_submit=True):
        col1, col2 = st.columns(2)
        fornecedor = col1.text_input(
            "Fornecedor/Cliente *", max_chars=200,
            placeholder="Ex: Papelaria Central",
            key="lanc_fornecedor",
        )
        cat_sel = col2.selectbox("Categoria *", list(opcoes_cat.keys()), key="lanc_cat")

        col3, col4 = st.columns(2)
        data_emissao = col3.date_input(
            "Data de Emissao *", value=date.today(), key="lanc_data_emissao",
            help="Data da nota/documento",
        )
        data_pagamento = col4.date_input(
            "Data de Pagamento *", value=date.today(), key="lanc_data_pagamento",
            help="Data em que saiu (ou vai sair) do banco",
        )

        col5, col6 = st.columns(2)
        valor = col5.number_input(
            "Valor (R$) *", min_value=0.01, step=10.0,
            format="%.2f", key="lanc_valor",
        )
        descricao = col6.text_input(
            "Descricao *", max_chars=255,
            placeholder="Ex: Compra de materiais escritorio",
            key="lanc_desc",
        )

        # Lancamento recorrente
        st.markdown("---")
        recorrente = st.checkbox("Lancamento recorrente?", key="lanc_recorrente")

        freq = "MENSAL"
        data_inicio_rec = data_pagamento
        data_fim_rec = date(2027, 12, 31)
        dia_prev_rec = data_pagamento.day

        if recorrente:
            col_r1, col_r2, col_r3 = st.columns(3)
            freq = col_r1.selectbox(
                "Frequencia", ["MENSAL", "TRIMESTRAL", "ANUAL"],
                key="lanc_freq",
            )
            data_inicio_rec = col_r2.date_input(
                "Data de inicio", value=date.today(), key="lanc_rec_inicio",
            )
            data_fim_rec = col_r3.date_input(
                "Data de fim", value=date(2027, 12, 31), key="lanc_rec_fim",
            )
            dia_prev_rec = st.number_input(
                "Dia previsto de pagamento (1-31)",
                min_value=1, max_value=31,
                value=data_pagamento.day, step=1,
                key="lanc_rec_dia",
                help="Usado para sugerir match com o extrato bancario.",
            )

        enviar = st.form_submit_button("Registrar Lancamento", type="primary")

    if enviar:
        if not descricao.strip():
            st.error("Descricao e obrigatoria.")
        elif not fornecedor.strip():
            st.error("Fornecedor/Cliente e obrigatorio.")
        else:
            cat_id = opcoes_cat[cat_sel]

            # Verifica teto da categoria (usa data de pagamento como referencia)
            alerta = _verificar_teto_categoria(session, cat_id, valor, data_pagamento)
            if alerta:
                tipo_alerta, msg = alerta
                if tipo_alerta == "error":
                    st.error(msg)
                else:
                    st.warning(msg)

            # Registra o lancamento pontual
            item = ItemDespesa(
                transacao_bancaria_id=None,
                categoria_despesa_id=cat_id,
                valor_brl=_to_decimal(valor),
                descricao=descricao.strip(),
                fornecedor_cliente=fornecedor.strip(),
                data=data_pagamento,
                data_emissao=data_emissao,
                data_pagamento=data_pagamento,
                conciliado=False,
            )
            session.add(item)

            # Se recorrente, cria tambem o LancamentoRecorrente
            if recorrente:
                lr = LancamentoRecorrente(
                    categoria_despesa_id=cat_id,
                    valor_brl=_to_decimal(valor),
                    descricao=descricao.strip(),
                    frequencia=freq,
                    data_inicio=data_inicio_rec,
                    data_fim=data_fim_rec,
                    dia_pagamento_previsto=int(dia_prev_rec),
                    tecnico_id=None,
                    ativo=True,
                )
                session.add(lr)

            session.commit()

            cat = session.get(CategoriaDespesa, cat_id)
            msg_ok = f"Lancamento registrado: R$ {valor:,.2f} em {cat.centro_custo.codigo} | {cat.nome}"
            if recorrente:
                msg_ok += f" (recorrente {freq.lower()})"
            st.success(msg_ok)
            st.rerun()


# ──────────────── aba: listagem ─────────────────────────────

def _aba_lista(session):
    st.subheader("Lancamentos Registrados")

    filtro = st.radio(
        "Filtrar",
        ["Todos", "Nao conciliados", "Conciliados"],
        horizontal=True,
        key="filtro_lanc",
    )

    query = (
        session.query(ItemDespesa)
        .join(CategoriaDespesa)
        .join(CentroCusto)
        .filter(ItemDespesa.reembolso_id == None)
        .order_by(ItemDespesa.data_pagamento.desc().nullslast(), ItemDespesa.data.desc())
    )

    if filtro == "Nao conciliados":
        query = query.filter(ItemDespesa.conciliado == False)
    elif filtro == "Conciliados":
        query = query.filter(ItemDespesa.conciliado == True)

    itens = query.all()

    if not itens:
        st.info("Nenhum lancamento encontrado.")
        return

    # Tabela
    dados = []
    for item in itens:
        cat = item.categoria_despesa
        data_pag = item.data_pagamento or item.data
        data_emi = item.data_emissao
        dados.append({
            "ID": item.id,
            "Fornecedor/Cliente": item.fornecedor_cliente or "—",
            "Data Emissao": str(data_emi) if data_emi else "—",
            "Data Pagamento": str(data_pag) if data_pag else "—",
            "Descricao": item.descricao or "—",
            "Centro de Custo": cat.centro_custo.codigo,
            "Categoria": cat.nome,
            "Valor": f"R$ {item.valor_brl:,.2f}",
            "Status": "Conciliado" if item.conciliado else "Pendente",
        })

    st.dataframe(pd.DataFrame(dados), use_container_width=True, hide_index=True)

    # Totalizadores
    total = sum(item.valor_brl for item in itens)
    pendentes = sum(1 for item in itens if not item.conciliado)
    conciliados = sum(1 for item in itens if item.conciliado)

    col1, col2, col3 = st.columns(3)
    col1.metric("Total", f"R$ {total:,.2f}")
    col2.metric("Pendentes", pendentes)
    col3.metric("Conciliados", conciliados)

    # Edicao / exclusao (somente nao conciliados)
    itens_editaveis = [i for i in itens if not i.conciliado]

    if not itens_editaveis:
        return

    st.markdown("---")
    st.markdown("**Editar ou Excluir** (somente lancamentos nao conciliados)")

    opcoes = {
        f"#{i.id} | {i.data_pagamento or i.data} | {(i.fornecedor_cliente or '—')[:30]} | "
        f"{(i.descricao or '')[:30]} | R$ {i.valor_brl:,.2f}": i.id
        for i in itens_editaveis
    }
    sel = st.selectbox("Selecione o lancamento", list(opcoes.keys()), key="sel_lanc_edit")
    item_id = opcoes[sel]
    item_obj = session.get(ItemDespesa, item_id)

    opcoes_cat = _opcoes_categorias(session)

    col_ed, col_del = st.columns([3, 1])

    with col_ed:
        with st.form(f"form_lanc_edit_{item_id}"):
            fornecedor_ed = st.text_input(
                "Fornecedor/Cliente",
                value=item_obj.fornecedor_cliente or "",
                max_chars=200,
                key=f"lanc_fornecedor_ed_{item_id}",
            )

            col_de1, col_de2 = st.columns(2)
            data_emissao_ed = col_de1.date_input(
                "Data de Emissao",
                value=item_obj.data_emissao or item_obj.data_pagamento or item_obj.data,
                key=f"lanc_data_emissao_ed_{item_id}",
            )
            data_pag_ed = col_de2.date_input(
                "Data de Pagamento",
                value=item_obj.data_pagamento or item_obj.data,
                key=f"lanc_data_pag_ed_{item_id}",
            )

            cat_atual = f"{item_obj.categoria_despesa.centro_custo.codigo} | {item_obj.categoria_despesa.nome}"
            idx_cat = list(opcoes_cat.keys()).index(cat_atual) if cat_atual in opcoes_cat else 0
            cat_ed = st.selectbox(
                "Categoria", list(opcoes_cat.keys()),
                index=idx_cat, key=f"lanc_cat_ed_{item_id}",
            )

            col_ve, col_de = st.columns(2)
            valor_ed = col_ve.number_input(
                "Valor (R$)", value=float(item_obj.valor_brl),
                min_value=0.01, step=10.0, format="%.2f",
                key=f"lanc_valor_ed_{item_id}",
            )
            desc_ed = col_de.text_input(
                "Descricao", value=item_obj.descricao or "",
                key=f"lanc_desc_ed_{item_id}",
            )
            salvar = st.form_submit_button("Atualizar")

        if salvar:
            if not fornecedor_ed.strip():
                st.error("Fornecedor/Cliente e obrigatorio.")
            else:
                item_obj.fornecedor_cliente = fornecedor_ed.strip()
                item_obj.data_emissao = data_emissao_ed
                item_obj.data_pagamento = data_pag_ed
                item_obj.data = data_pag_ed
                item_obj.categoria_despesa_id = opcoes_cat[cat_ed]
                item_obj.valor_brl = _to_decimal(valor_ed)
                item_obj.descricao = desc_ed.strip()
                session.commit()
                st.success("Lancamento atualizado!")
                st.rerun()

    with col_del:
        if st.button("Excluir", type="primary", key=f"btn_del_lanc_{item_id}"):
            session.delete(item_obj)
            session.commit()
            st.success("Lancamento excluido!")
            st.rerun()


# ──────────────── aba: reembolsos ──────────────────────────

_REEMB_ITENS_KEY = "reemb_novo_itens"  # lista de dicts em session_state


def _aba_reembolsos(session):
    st.subheader("Reembolsos")
    st.caption(
        "Um reembolso agrupa varias despesas pagas a uma mesma pessoa em um unico debito bancario. "
        "As despesas internas continuam somando nos centros de custo e categorias corretos."
    )

    sub_novo, sub_lista = st.tabs(["Novo Reembolso", "Reembolsos Registrados"])

    with sub_novo:
        _reemb_novo(session)

    with sub_lista:
        _reemb_lista(session)


def _reemb_novo(session):
    """Formulario para criar um reembolso com N despesas internas."""
    opcoes_cat = _opcoes_categorias(session)
    if not opcoes_cat:
        st.warning("Cadastre categorias de despesa antes de criar reembolsos.")
        return

    # Cabecalho do reembolso
    col1, col2 = st.columns(2)
    beneficiario = col1.text_input(
        "Beneficiario *", max_chars=200,
        placeholder="Ex: Joao Silva",
        key="reemb_beneficiario",
    )
    data_pag = col2.date_input(
        "Data de Pagamento *", value=date.today(),
        key="reemb_data_pag",
        help="Data em que o debito saiu (ou vai sair) do banco",
    )
    observacao = st.text_area(
        "Observacao (opcional)", max_chars=500,
        key="reemb_obs", height=70,
    )

    st.markdown("---")
    st.markdown("**Despesas do Reembolso**")

    # Inicializa acumulador de itens
    if _REEMB_ITENS_KEY not in st.session_state:
        st.session_state[_REEMB_ITENS_KEY] = []

    itens = st.session_state[_REEMB_ITENS_KEY]

    # Formulario para adicionar uma despesa
    with st.form("form_reemb_add_item", clear_on_submit=True):
        col_a, col_b = st.columns(2)
        forn_item = col_a.text_input(
            "Fornecedor/Cliente",
            value=beneficiario,
            max_chars=200,
            placeholder="Pre-preenchido com o beneficiario — edite se necessario",
            key="reemb_item_forn",
        )
        cat_item = col_b.selectbox(
            "Categoria *", list(opcoes_cat.keys()), key="reemb_item_cat",
        )

        col_c, col_d = st.columns(2)
        valor_item = col_c.number_input(
            "Valor (R$) *", min_value=0.01, step=10.0,
            format="%.2f", key="reemb_item_valor",
        )
        data_emi_item = col_d.date_input(
            "Data de Emissao *", value=date.today(),
            key="reemb_item_data_emi",
        )

        desc_item = st.text_input(
            "Descricao", max_chars=255,
            placeholder="Ex: Almoco com cliente",
            key="reemb_item_desc",
        )

        adicionar = st.form_submit_button("Adicionar Despesa")

    if adicionar:
        if not cat_item:
            st.error("Selecione uma categoria.")
        elif valor_item <= 0:
            st.error("Valor deve ser maior que zero.")
        else:
            itens.append({
                "fornecedor": (forn_item or beneficiario or "").strip(),
                "cat_label": cat_item,
                "cat_id": opcoes_cat[cat_item],
                "valor": _to_decimal(valor_item),
                "data_emissao": data_emi_item,
                "descricao": (desc_item or "").strip(),
            })
            st.session_state[_REEMB_ITENS_KEY] = itens
            st.rerun()

    # Tabela dos itens acumulados
    if itens:
        st.markdown("**Itens adicionados:**")
        for idx, it in enumerate(itens):
            col_n, col_v, col_x = st.columns([5, 2, 1])
            col_n.write(
                f"**{it['fornecedor'] or '—'}** — {it['cat_label']} | "
                f"emissao: {it['data_emissao']}"
                + (f" | {it['descricao']}" if it['descricao'] else "")
            )
            col_v.write(f"R$ {it['valor']:,.2f}")
            if col_x.button("Remover", key=f"reemb_rm_{idx}"):
                itens.pop(idx)
                st.session_state[_REEMB_ITENS_KEY] = itens
                st.rerun()

        total = sum((it["valor"] for it in itens), Decimal("0.00"))
        st.markdown("---")
        st.metric("Total do Reembolso", f"R$ {total:,.2f}")
    else:
        st.info("Adicione pelo menos uma despesa para salvar o reembolso.")

    # Botao final
    col_salvar, col_limpar, _ = st.columns([1, 1, 3])
    with col_salvar:
        salvar = st.button(
            "Salvar Reembolso", type="primary",
            disabled=not itens or not beneficiario.strip(),
            key="btn_reemb_salvar",
        )
    with col_limpar:
        if st.button("Limpar lista", key="btn_reemb_limpar", disabled=not itens):
            st.session_state[_REEMB_ITENS_KEY] = []
            st.rerun()

    if salvar:
        if not beneficiario.strip():
            st.error("Beneficiario e obrigatorio.")
            return
        if not itens:
            st.error("Adicione pelo menos uma despesa.")
            return

        total = sum((it["valor"] for it in itens), Decimal("0.00"))

        # Transacao atomica: cria Reembolso + ItemDespesa filhos
        try:
            reembolso = Reembolso(
                beneficiario=beneficiario.strip(),
                data_pagamento=data_pag,
                valor_total_brl=total,
                conciliado=False,
                observacao=(observacao or "").strip(),
            )
            session.add(reembolso)
            session.flush()  # garante reembolso.id

            for it in itens:
                item = ItemDespesa(
                    categoria_despesa_id=it["cat_id"],
                    reembolso_id=reembolso.id,
                    valor_brl=it["valor"],
                    descricao=it["descricao"],
                    fornecedor_cliente=it["fornecedor"],
                    data=data_pag,
                    data_emissao=it["data_emissao"],
                    data_pagamento=data_pag,
                    conciliado=False,
                )
                session.add(item)

            session.commit()
            st.session_state[_REEMB_ITENS_KEY] = []
            st.success(
                f"Reembolso de R$ {total:,.2f} para '{beneficiario.strip()}' "
                f"registrado com {len(itens)} despesa(s)."
            )
            st.rerun()
        except Exception as e:
            session.rollback()
            st.error(f"Erro ao salvar reembolso: {e}")


def _reemb_lista(session):
    """Listagem de reembolsos com expansao de itens e acoes."""
    filtro = st.radio(
        "Filtrar",
        ["Todos", "Pendentes", "Conciliados"],
        horizontal=True,
        key="filtro_reemb",
    )

    query = session.query(Reembolso).order_by(Reembolso.data_pagamento.desc())
    if filtro == "Pendentes":
        query = query.filter(Reembolso.conciliado == False)
    elif filtro == "Conciliados":
        query = query.filter(Reembolso.conciliado == True)

    reembolsos = query.all()

    if not reembolsos:
        st.info("Nenhum reembolso encontrado.")
        return

    # Metricas
    total_valor = sum((r.valor_total_brl for r in reembolsos), Decimal("0.00"))
    n_pend = sum(1 for r in reembolsos if not r.conciliado)
    n_conc = sum(1 for r in reembolsos if r.conciliado)
    col_m1, col_m2, col_m3 = st.columns(3)
    col_m1.metric("Total", f"R$ {total_valor:,.2f}")
    col_m2.metric("Pendentes", n_pend)
    col_m3.metric("Conciliados", n_conc)

    st.markdown("---")

    opcoes_cat = _opcoes_categorias(session)

    for r in reembolsos:
        icone = "✅" if r.conciliado else "⏳"
        label = (
            f"{icone} {r.data_pagamento} | {r.beneficiario} | "
            f"R$ {r.valor_total_brl:,.2f} | {len(r.itens_despesa)} item(ns)"
        )

        with st.expander(label, expanded=False):
            if r.observacao:
                st.caption(r.observacao)

            # Tabela de itens filhos
            dados = []
            for it in r.itens_despesa:
                cat = it.categoria_despesa
                dados.append({
                    "Fornecedor/Cliente": it.fornecedor_cliente or "—",
                    "Centro de Custo": cat.centro_custo.codigo,
                    "Categoria": cat.nome,
                    "Descricao": it.descricao or "—",
                    "Data Emissao": str(it.data_emissao) if it.data_emissao else "—",
                    "Valor": f"R$ {it.valor_brl:,.2f}",
                })
            st.dataframe(
                pd.DataFrame(dados),
                use_container_width=True, hide_index=True,
            )

            if r.conciliado:
                st.success(
                    f"Reembolso conciliado ao debito bancario "
                    f"#{r.transacao_bancaria_id} — para desfazer, use a tela de Conciliacao."
                )
                continue

            # Edicao (cabecalho) + gestao dos itens — apenas para nao conciliados
            st.markdown("---")
            st.markdown("**Editar cabecalho:**")
            with st.form(f"form_reemb_edit_{r.id}"):
                col_e1, col_e2 = st.columns(2)
                benef_ed = col_e1.text_input(
                    "Beneficiario", value=r.beneficiario,
                    max_chars=200, key=f"reemb_benef_ed_{r.id}",
                )
                data_ed = col_e2.date_input(
                    "Data de Pagamento", value=r.data_pagamento,
                    key=f"reemb_data_ed_{r.id}",
                )
                obs_ed = st.text_area(
                    "Observacao", value=r.observacao or "",
                    max_chars=500, height=60,
                    key=f"reemb_obs_ed_{r.id}",
                )
                atualizar = st.form_submit_button("Atualizar cabecalho")

            if atualizar:
                if not benef_ed.strip():
                    st.error("Beneficiario e obrigatorio.")
                else:
                    r.beneficiario = benef_ed.strip()
                    r.data_pagamento = data_ed
                    r.observacao = (obs_ed or "").strip()
                    # Propaga data_pagamento para todos os filhos (mantem consistencia)
                    for it in r.itens_despesa:
                        it.data = data_ed
                        it.data_pagamento = data_ed
                    session.commit()
                    st.success("Reembolso atualizado!")
                    st.rerun()

            # Adicionar nova despesa ao reembolso existente
            st.markdown("---")
            st.markdown("**Adicionar despesa a este reembolso:**")
            with st.form(f"form_reemb_add_item_ex_{r.id}"):
                col_ai1, col_ai2 = st.columns(2)
                forn_ai = col_ai1.text_input(
                    "Fornecedor/Cliente", value=r.beneficiario,
                    max_chars=200, key=f"reemb_ai_forn_{r.id}",
                )
                cat_ai = col_ai2.selectbox(
                    "Categoria", list(opcoes_cat.keys()),
                    key=f"reemb_ai_cat_{r.id}",
                )
                col_ai3, col_ai4 = st.columns(2)
                valor_ai = col_ai3.number_input(
                    "Valor (R$)", min_value=0.01, step=10.0,
                    format="%.2f", key=f"reemb_ai_valor_{r.id}",
                )
                data_emi_ai = col_ai4.date_input(
                    "Data de Emissao", value=r.data_pagamento,
                    key=f"reemb_ai_data_emi_{r.id}",
                )
                desc_ai = st.text_input(
                    "Descricao", max_chars=255,
                    key=f"reemb_ai_desc_{r.id}",
                )
                add_ai = st.form_submit_button("Adicionar despesa")

            if add_ai:
                novo = ItemDespesa(
                    categoria_despesa_id=opcoes_cat[cat_ai],
                    reembolso_id=r.id,
                    valor_brl=_to_decimal(valor_ai),
                    descricao=(desc_ai or "").strip(),
                    fornecedor_cliente=(forn_ai or r.beneficiario).strip(),
                    data=r.data_pagamento,
                    data_emissao=data_emi_ai,
                    data_pagamento=r.data_pagamento,
                    conciliado=False,
                )
                session.add(novo)
                # Recalcula total
                r.valor_total_brl = sum(
                    (i.valor_brl for i in r.itens_despesa),
                    Decimal("0.00"),
                ) + _to_decimal(valor_ai)
                session.commit()
                st.success("Despesa adicionada ao reembolso!")
                st.rerun()

            # Remover despesa individual
            if len(r.itens_despesa) > 1:
                st.markdown("---")
                st.markdown("**Remover despesa:**")
                opcoes_rm = {
                    f"{(it.fornecedor_cliente or '—')[:25]} | "
                    f"{it.categoria_despesa.centro_custo.codigo}|{it.categoria_despesa.nome} | "
                    f"R$ {it.valor_brl:,.2f}": it.id
                    for it in r.itens_despesa
                }
                sel_rm = st.selectbox(
                    "Despesa a remover",
                    list(opcoes_rm.keys()),
                    key=f"reemb_rm_sel_{r.id}",
                )
                if st.button("Remover despesa", key=f"reemb_rm_btn_{r.id}"):
                    item_rm = session.get(ItemDespesa, opcoes_rm[sel_rm])
                    valor_rm = item_rm.valor_brl
                    session.delete(item_rm)
                    r.valor_total_brl = r.valor_total_brl - valor_rm
                    session.commit()
                    st.success("Despesa removida.")
                    st.rerun()

            # Excluir reembolso inteiro
            st.markdown("---")
            confirm_key = f"confirm_del_reemb_{r.id}"
            if st.session_state.get(confirm_key, False):
                st.warning(
                    f"Confirma exclusao do reembolso e TODAS as {len(r.itens_despesa)} despesa(s)?"
                )
                col_yes, col_no = st.columns(2)
                with col_yes:
                    if st.button("Sim, excluir", type="primary",
                                  key=f"btn_del_reemb_yes_{r.id}"):
                        session.delete(r)
                        session.commit()
                        st.session_state.pop(confirm_key, None)
                        st.success("Reembolso excluido.")
                        st.rerun()
                with col_no:
                    if st.button("Cancelar", key=f"btn_del_reemb_no_{r.id}"):
                        st.session_state.pop(confirm_key, None)
                        st.rerun()
            else:
                if st.button("Excluir Reembolso", type="secondary",
                              key=f"btn_del_reemb_{r.id}"):
                    st.session_state[confirm_key] = True
                    st.rerun()


# ──────────────── aba: recorrentes ─────────────────────────

def _aba_recorrentes(session):
    st.subheader("Lancamentos Recorrentes")
    st.caption(
        "Lancamentos que se repetem periodicamente (salarios, alugueis, etc). "
        "Estes lancamentos alimentam a projecao do Fluxo de Caixa."
    )

    recorrentes = (
        session.query(LancamentoRecorrente)
        .join(CategoriaDespesa)
        .join(CentroCusto)
        .order_by(LancamentoRecorrente.descricao)
        .all()
    )

    if not recorrentes:
        st.info(
            "Nenhum lancamento recorrente cadastrado. "
            "Crie um na aba 'Novo Lancamento' marcando a opcao 'Lancamento recorrente', "
            "ou gere automaticamente pela Folha de Pagamento."
        )
        return

    # Filtro
    filtro = st.radio(
        "Filtrar",
        ["Todos", "Ativos", "Inativos"],
        horizontal=True,
        key="filtro_recorrentes",
    )

    if filtro == "Ativos":
        recorrentes = [r for r in recorrentes if r.ativo]
    elif filtro == "Inativos":
        recorrentes = [r for r in recorrentes if not r.ativo]

    # Tabela
    dados = []
    total_mensal = Decimal("0.00")
    for lr in recorrentes:
        cat = lr.categoria_despesa
        tec_nome = lr.tecnico.nome if lr.tecnico else "—"
        badge = "[REC]" if lr.ativo else "[INATIVO]"
        dados.append({
            "Status": badge,
            "Descricao": lr.descricao,
            "Valor": f"R$ {lr.valor_brl:,.2f}",
            "Frequencia": lr.frequencia,
            "Dia Previsto": lr.dia_pagamento_previsto or "—",
            "Categoria": f"{cat.centro_custo.codigo} | {cat.nome}",
            "Tecnico": tec_nome,
            "Periodo": f"{lr.data_inicio} a {lr.data_fim}",
        })
        if lr.ativo and lr.frequencia == "MENSAL":
            total_mensal += lr.valor_brl
        elif lr.ativo and lr.frequencia == "TRIMESTRAL":
            total_mensal += lr.valor_brl / 3
        elif lr.ativo and lr.frequencia == "ANUAL":
            total_mensal += lr.valor_brl / 12

    st.dataframe(pd.DataFrame(dados), use_container_width=True, hide_index=True)

    col_m1, col_m2 = st.columns(2)
    col_m1.metric("Total Recorrentes Ativos", sum(1 for r in recorrentes if r.ativo))
    col_m2.metric("Impacto Mensal Estimado", f"R$ {total_mensal:,.2f}")

    # Edicao / ativacao / exclusao
    ativos = [r for r in recorrentes if r.ativo]
    if not ativos and filtro != "Inativos":
        return

    items_edit = recorrentes if filtro != "Ativos" else ativos

    if not items_edit:
        return

    st.markdown("---")
    st.markdown("**Gerenciar Lancamento Recorrente**")

    opcoes = {
        f"{'[A]' if lr.ativo else '[I]'} {lr.descricao} | R$ {lr.valor_brl:,.2f}": lr.id
        for lr in items_edit
    }
    sel = st.selectbox("Selecione", list(opcoes.keys()), key="sel_rec_edit")
    lr_id = opcoes[sel]
    lr_obj = session.get(LancamentoRecorrente, lr_id)

    opcoes_cat = _opcoes_categorias(session)

    col_ed, col_act = st.columns([3, 1])

    with col_ed:
        with st.form(f"form_rec_edit_{lr_id}"):
            desc_ed = st.text_input("Descricao", value=lr_obj.descricao, key=f"rec_desc_{lr_id}")
            val_ed = st.number_input(
                "Valor (R$)", value=float(lr_obj.valor_brl),
                min_value=0.01, step=10.0, format="%.2f", key=f"rec_val_{lr_id}",
            )
            cat_atual = f"{lr_obj.categoria_despesa.centro_custo.codigo} | {lr_obj.categoria_despesa.nome}"
            idx_cat = list(opcoes_cat.keys()).index(cat_atual) if cat_atual in opcoes_cat else 0
            cat_ed = st.selectbox(
                "Categoria", list(opcoes_cat.keys()),
                index=idx_cat, key=f"rec_cat_{lr_id}",
            )
            freq_ed = st.selectbox(
                "Frequencia", ["MENSAL", "TRIMESTRAL", "ANUAL"],
                index=["MENSAL", "TRIMESTRAL", "ANUAL"].index(lr_obj.frequencia),
                key=f"rec_freq_{lr_id}",
            )
            dia_ed = st.number_input(
                "Dia previsto de pagamento (1-31, opcional)",
                min_value=0, max_value=31,
                value=lr_obj.dia_pagamento_previsto or 0,
                step=1, key=f"rec_dia_{lr_id}",
                help="Usado para sugerir match com o extrato bancario. 0 = ultimo dia do mes.",
            )
            col_d1, col_d2 = st.columns(2)
            inicio_ed = col_d1.date_input("Inicio", value=lr_obj.data_inicio, key=f"rec_ini_{lr_id}")
            fim_ed = col_d2.date_input("Fim", value=lr_obj.data_fim, key=f"rec_fim_{lr_id}")
            salvar = st.form_submit_button("Atualizar")

        if salvar:
            lr_obj.descricao = desc_ed.strip()
            lr_obj.valor_brl = _to_decimal(val_ed)
            lr_obj.categoria_despesa_id = opcoes_cat[cat_ed]
            lr_obj.frequencia = freq_ed
            lr_obj.dia_pagamento_previsto = int(dia_ed) if dia_ed > 0 else None
            lr_obj.data_inicio = inicio_ed
            lr_obj.data_fim = fim_ed
            session.commit()
            st.success("Lancamento recorrente atualizado!")
            st.rerun()

    with col_act:
        if lr_obj.ativo:
            if st.button("Desativar", key=f"btn_desat_rec_{lr_id}"):
                lr_obj.ativo = False
                session.commit()
                st.info("Lancamento recorrente desativado.")
                st.rerun()
        else:
            if st.button("Reativar", key=f"btn_reat_rec_{lr_id}"):
                lr_obj.ativo = True
                session.commit()
                st.success("Lancamento recorrente reativado!")
                st.rerun()

        if st.button("Excluir", type="primary", key=f"btn_del_rec_{lr_id}"):
            session.delete(lr_obj)
            session.commit()
            st.success("Lancamento recorrente excluido!")
            st.rerun()
