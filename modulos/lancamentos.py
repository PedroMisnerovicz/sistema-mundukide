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
from models import CentroCusto, CategoriaDespesa, ItemDespesa, LancamentoRecorrente, Remessa

CAMBIO_PROJECAO = Decimal("6.00")


def _to_decimal(valor, fallback=Decimal("0.00")):
    try:
        return Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        return fallback


def _cambio_medio(session) -> Decimal:
    """Cambio medio ponderado das remessas recebidas, ou R$6,00 de projecao."""
    remessas = session.query(Remessa).filter(Remessa.recebida == True).all()
    if not remessas:
        return CAMBIO_PROJECAO
    total_eur = sum(r.valor_eur for r in remessas)
    total_brl = sum(r.valor_brl for r in remessas)
    if not total_eur or total_eur == 0:
        return CAMBIO_PROJECAO
    return (Decimal(str(total_brl)) / Decimal(str(total_eur))).quantize(Decimal("0.0001"))


def _opcoes_categorias(session):
    """Retorna dict {label: id} de categorias agrupadas por centro de custo."""
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

    tab_novo, tab_lista, tab_recorrentes = st.tabs([
        "Novo Lancamento",
        "Lancamentos Registrados",
        "Lancamentos Recorrentes",
    ])

    with tab_novo:
        _aba_novo(session)

    with tab_lista:
        _aba_lista(session)

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
        data_desp = col1.date_input("Data *", value=date.today(), key="lanc_data")
        cat_sel = col2.selectbox("Categoria *", list(opcoes_cat.keys()), key="lanc_cat")

        col3, col4 = st.columns(2)
        valor = col3.number_input(
            "Valor (R$) *", min_value=0.01, step=10.0,
            format="%.2f", key="lanc_valor",
        )
        descricao = col4.text_input(
            "Descricao *", max_chars=255,
            placeholder="Ex: Compra de materiais escritorio",
            key="lanc_desc",
        )

        # Lancamento recorrente
        st.markdown("---")
        recorrente = st.checkbox("Lancamento recorrente?", key="lanc_recorrente")

        freq = "MENSAL"
        data_inicio_rec = data_desp
        data_fim_rec = date(2027, 12, 31)

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

        enviar = st.form_submit_button("Registrar Lancamento", type="primary")

    if enviar:
        if not descricao.strip():
            st.error("Descricao e obrigatoria.")
        else:
            cat_id = opcoes_cat[cat_sel]

            # Verifica teto da categoria
            alerta = _verificar_teto_categoria(session, cat_id, valor, data_desp)
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
                data=data_desp,
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
        .order_by(ItemDespesa.data.desc())
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
        dados.append({
            "ID": item.id,
            "Data": str(item.data),
            "Descricao": item.descricao or "—",
            "Categoria": f"{cat.centro_custo.codigo} | {cat.nome}",
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
        f"#{i.id} | {i.data} | {i.descricao[:40]} | R$ {i.valor_brl:,.2f}": i.id
        for i in itens_editaveis
    }
    sel = st.selectbox("Selecione o lancamento", list(opcoes.keys()), key="sel_lanc_edit")
    item_id = opcoes[sel]
    item_obj = session.get(ItemDespesa, item_id)

    opcoes_cat = _opcoes_categorias(session)

    col_ed, col_del = st.columns([3, 1])

    with col_ed:
        with st.form("form_lanc_edit"):
            data_ed = st.date_input("Data", value=item_obj.data, key="lanc_data_ed")

            cat_atual = f"{item_obj.categoria_despesa.centro_custo.codigo} | {item_obj.categoria_despesa.nome}"
            idx_cat = list(opcoes_cat.keys()).index(cat_atual) if cat_atual in opcoes_cat else 0
            cat_ed = st.selectbox(
                "Categoria", list(opcoes_cat.keys()),
                index=idx_cat, key="lanc_cat_ed",
            )

            col_ve, col_de = st.columns(2)
            valor_ed = col_ve.number_input(
                "Valor (R$)", value=float(item_obj.valor_brl),
                min_value=0.01, step=10.0, format="%.2f", key="lanc_valor_ed",
            )
            desc_ed = col_de.text_input(
                "Descricao", value=item_obj.descricao or "",
                key="lanc_desc_ed",
            )
            salvar = st.form_submit_button("Atualizar")

        if salvar:
            item_obj.data = data_ed
            item_obj.categoria_despesa_id = opcoes_cat[cat_ed]
            item_obj.valor_brl = _to_decimal(valor_ed)
            item_obj.descricao = desc_ed.strip()
            session.commit()
            st.success("Lancamento atualizado!")
            st.rerun()

    with col_del:
        if st.button("Excluir", type="primary", key="btn_del_lanc"):
            session.delete(item_obj)
            session.commit()
            st.success("Lancamento excluido!")
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
            col_d1, col_d2 = st.columns(2)
            inicio_ed = col_d1.date_input("Inicio", value=lr_obj.data_inicio, key=f"rec_ini_{lr_id}")
            fim_ed = col_d2.date_input("Fim", value=lr_obj.data_fim, key=f"rec_fim_{lr_id}")
            salvar = st.form_submit_button("Atualizar")

        if salvar:
            lr_obj.descricao = desc_ed.strip()
            lr_obj.valor_brl = _to_decimal(val_ed)
            lr_obj.categoria_despesa_id = opcoes_cat[cat_ed]
            lr_obj.frequencia = freq_ed
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
