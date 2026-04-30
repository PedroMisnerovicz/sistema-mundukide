"""
Modulo de Cadastros
===================
Quatro abas: Centros de Custo | Categorias de Despesas | Remessas | Visao Geral
"""

from datetime import date
from decimal import Decimal, InvalidOperation

import pandas as pd
import streamlit as st
from sqlalchemy import func as sqlfunc

from database import get_session
from models import CentroCusto, CategoriaDespesa, Remessa, ItemDespesa
from modulos.cache_utils import invalidar_cache_cadastros

CAMBIO_PROJECAO = Decimal("6.00")


# ───────────────────────── helpers ──────────────────────────

def _to_decimal(valor, fallback=Decimal("0.00")):
    try:
        return Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        return fallback


def _cambio_medio(session) -> Decimal:
    """Media ponderada dos cambios EFETIVADOS das remessas recebidas
    (ponderada pelo valor em EUR). Cai para o cambio de projecao
    (R$6,00) se nenhuma remessa tem cambio efetivado registrado."""
    remessas = (
        session.query(Remessa)
        .filter(
            Remessa.recebida == True,
            Remessa.cambio_efetivado.isnot(None),
            Remessa.valor_eur.isnot(None),
            Remessa.valor_brl.isnot(None),
        )
        .all()
    )
    if not remessas:
        return CAMBIO_PROJECAO
    total_eur = sum(r.valor_eur for r in remessas)
    total_brl = sum(r.valor_brl for r in remessas)
    if total_eur == 0:
        return CAMBIO_PROJECAO
    return (total_brl / total_eur).quantize(Decimal("0.0001"))


# ──────────────────── aba: centros de custo ─────────────────

def _aba_centros_custo():
    st.subheader("Centros de Custo")

    session = get_session()

    # ---------- formulario de criacao ----------
    with st.expander("Novo Centro de Custo", expanded=False):
        with st.form("form_cc", clear_on_submit=True):
            col1, col2 = st.columns(2)
            codigo = col1.text_input("Codigo *", max_chars=20, placeholder="Ex: ALIM")
            nome = col2.text_input("Nome *", max_chars=120, placeholder="Ex: Alimentacao")
            col3, col4 = st.columns(2)
            teto_eur = col3.number_input("Teto EUR *", min_value=0.01, step=100.0, format="%.2f")
            descricao = col4.text_input("Descricao", max_chars=255)
            enviar = st.form_submit_button("Salvar")

        if enviar:
            if not codigo or not nome:
                st.error("Codigo e Nome sao obrigatorios.")
            else:
                existente = session.query(CentroCusto).filter_by(codigo=codigo.upper()).first()
                if existente:
                    st.error(f"Codigo '{codigo.upper()}' ja existe.")
                else:
                    cc = CentroCusto(
                        codigo=codigo.upper().strip(),
                        nome=nome.strip(),
                        teto_eur=_to_decimal(teto_eur),
                        descricao=descricao.strip(),
                    )
                    session.add(cc)
                    session.commit()
                    invalidar_cache_cadastros()
                    st.success(f"Centro '{cc.codigo}' criado!")
                    st.rerun()

    # ---------- listagem ----------
    centros = session.query(CentroCusto).order_by(CentroCusto.codigo).all()

    if not centros:
        st.info("Nenhum centro de custo cadastrado.")
        session.close()
        return

    cambio = _cambio_medio(session)

    dados = []
    for cc in centros:
        teto_brl = (cc.teto_eur * cambio).quantize(Decimal("0.01"))
        dados.append({
            "ID": cc.id,
            "Codigo": cc.codigo,
            "Nome": cc.nome,
            "Teto EUR": f"€ {cc.teto_eur:,.2f}",
            "Teto BRL (proj.)": f"R$ {teto_brl:,.2f}",
            "Descricao": cc.descricao or "",
        })

    st.caption(f"Cambio utilizado na projecao: R$ {cambio:,.4f} / €")
    st.dataframe(pd.DataFrame(dados), use_container_width=True, hide_index=True)

    # ---------- edicao / exclusao ----------
    st.markdown("---")
    st.markdown("**Editar ou Excluir**")
    opcoes = {f"{cc.codigo} – {cc.nome}": cc.id for cc in centros}
    selecionado = st.selectbox("Selecione o centro de custo", list(opcoes.keys()), key="sel_cc_edit")
    cc_id = opcoes[selecionado]
    cc_obj = session.get(CentroCusto, cc_id)

    col_ed, col_del = st.columns([3, 1])

    with col_ed:
        with st.form(f"form_cc_edit_{cc_id}"):
            nome_ed = st.text_input("Nome", value=cc_obj.nome, key=f"cc_nome_ed_{cc_id}")
            teto_ed = st.number_input(
                "Teto EUR", value=float(cc_obj.teto_eur), min_value=0.01,
                step=100.0, format="%.2f", key=f"cc_teto_ed_{cc_id}"
            )
            desc_ed = st.text_input("Descricao", value=cc_obj.descricao or "", key=f"cc_desc_ed_{cc_id}")
            salvar_ed = st.form_submit_button("Atualizar")

        if salvar_ed:
            cc_obj.nome = nome_ed.strip()
            cc_obj.teto_eur = _to_decimal(teto_ed)
            cc_obj.descricao = desc_ed.strip()
            session.commit()
            invalidar_cache_cadastros()
            st.success("Atualizado!")
            st.rerun()

    with col_del:
        tem_categorias = session.query(CategoriaDespesa).filter_by(centro_custo_id=cc_id).first()
        if tem_categorias:
            st.warning("Possui categorias vinculadas. Exclua as categorias primeiro.")
        else:
            if st.button("Excluir", type="primary", key="btn_del_cc"):
                session.delete(cc_obj)
                session.commit()
                invalidar_cache_cadastros()
                st.success("Excluido!")
                st.rerun()

    session.close()


# ──────────────── aba: categorias de despesas ───────────────

def _aba_categorias():
    st.subheader("Categorias de Despesas")
    st.caption("Cada categoria pertence a um Centro de Custo. O teto e controlado no nivel do Centro.")

    session = get_session()

    centros = session.query(CentroCusto).order_by(CentroCusto.codigo).all()

    if not centros:
        st.warning("Cadastre pelo menos um Centro de Custo antes de criar categorias.")
        session.close()
        return

    cambio = _cambio_medio(session)

    # ---------- formulario de criacao ----------
    with st.expander("Nova Categoria", expanded=False):
        with st.form("form_cat", clear_on_submit=True):
            opcoes_cc = {f"{cc.codigo} – {cc.nome}": cc.id for cc in centros}
            cc_sel = st.selectbox("Centro de Custo *", list(opcoes_cc.keys()), key="cat_cc_sel")
            col1, col2 = st.columns(2)
            cat_nome = col1.text_input("Nome da Categoria *", max_chars=120, placeholder="Ex: Salarios")
            cat_desc = col2.text_input("Descricao", max_chars=255)

            st.markdown("**Teto de Gastos (opcional)**")
            col_t1, col_t2, col_t3 = st.columns(3)
            teto_val = col_t1.number_input(
                "Teto (EUR)", min_value=0.0, step=100.0,
                format="%.2f", key="cat_teto_val",
            )
            tipo_teto = col_t2.selectbox(
                "Tipo do Teto", ["Sem teto", "MENSAL", "GLOBAL"],
                key="cat_tipo_teto",
            )
            teto_brl_preview = (Decimal(str(teto_val)) * cambio).quantize(Decimal("0.01"))
            col_t3.caption(
                f"Equivalente: R$ {teto_brl_preview:,.2f}\n"
                f"(cambio R$ {cambio:,.4f}/€ — atualiza com remessas recebidas)"
            )

            enviar_cat = st.form_submit_button("Salvar")

        if enviar_cat:
            if not cat_nome:
                st.error("Nome e obrigatorio.")
            else:
                cc_id = opcoes_cc[cc_sel]
                cat = CategoriaDespesa(
                    nome=cat_nome.strip(),
                    centro_custo_id=cc_id,
                    descricao=cat_desc.strip(),
                    teto_eur=_to_decimal(teto_val) if tipo_teto != "Sem teto" and teto_val > 0 else None,
                    teto_brl=None,
                    tipo_teto=tipo_teto if tipo_teto != "Sem teto" and teto_val > 0 else None,
                )
                session.add(cat)
                session.commit()
                invalidar_cache_cadastros()
                st.success(f"Categoria '{cat.nome}' criada!")
                st.rerun()

    # ---------- listagem ----------
    categorias = (
        session.query(CategoriaDespesa)
        .join(CentroCusto)
        .order_by(CentroCusto.codigo, CategoriaDespesa.nome)
        .all()
    )

    if not categorias:
        st.info("Nenhuma categoria cadastrada.")
        session.close()
        return

    st.caption(f"Cambio utilizado na conversao: R$ {cambio:,.4f} / € (medio ponderado das remessas recebidas)")

    dados = []
    for cat in categorias:
        teto_str = "—"
        if cat.teto_eur and cat.tipo_teto:
            teto_brl_calc = (cat.teto_eur * cambio).quantize(Decimal("0.01"))
            teto_str = f"€ {cat.teto_eur:,.2f} = R$ {teto_brl_calc:,.2f} ({cat.tipo_teto.lower()})"
        elif cat.teto_brl and cat.tipo_teto:
            teto_str = f"R$ {cat.teto_brl:,.2f} ({cat.tipo_teto.lower()})"
        dados.append({
            "ID": cat.id,
            "Categoria": cat.nome,
            "Centro de Custo": f"{cat.centro_custo.codigo} – {cat.centro_custo.nome}",
            "Teto": teto_str,
            "Descricao": cat.descricao or "",
        })

    st.dataframe(pd.DataFrame(dados), use_container_width=True, hide_index=True)

    # ---------- edicao / exclusao ----------
    st.markdown("---")
    st.markdown("**Editar ou Excluir**")

    opcoes_cat = {f"{cat.centro_custo.codigo} | {cat.nome}": cat.id for cat in categorias}
    sel_cat = st.selectbox("Selecione a categoria", list(opcoes_cat.keys()), key="sel_cat_edit")
    cat_id = opcoes_cat[sel_cat]
    cat_obj = session.get(CategoriaDespesa, cat_id)

    col_ed, col_del = st.columns([3, 1])

    with col_ed:
        with st.form(f"form_cat_edit_{cat_id}"):
            opcoes_cc_ed = {f"{cc.codigo} – {cc.nome}": cc.id for cc in centros}
            cc_atual = f"{cat_obj.centro_custo.codigo} – {cat_obj.centro_custo.nome}"
            idx_cc = list(opcoes_cc_ed.keys()).index(cc_atual) if cc_atual in opcoes_cc_ed else 0
            cc_sel_ed = st.selectbox(
                "Centro de Custo", list(opcoes_cc_ed.keys()),
                index=idx_cc, key=f"cat_cc_ed_{cat_id}"
            )
            nome_cat_ed = st.text_input("Nome", value=cat_obj.nome, key=f"cat_nome_ed_{cat_id}")
            desc_cat_ed = st.text_input("Descricao", value=cat_obj.descricao or "", key=f"cat_desc_ed_{cat_id}")

            st.markdown("**Teto de Gastos**")
            col_te1, col_te2 = st.columns(2)
            teto_atual_eur = float(cat_obj.teto_eur) if cat_obj.teto_eur else 0.0
            teto_val_ed = col_te1.number_input(
                "Teto (EUR)", value=teto_atual_eur,
                min_value=0.0, step=100.0, format="%.2f",
                key=f"cat_teto_ed_{cat_id}",
            )
            tipos = ["Sem teto", "MENSAL", "GLOBAL"]
            idx_tipo = tipos.index(cat_obj.tipo_teto) if cat_obj.tipo_teto in tipos else 0
            tipo_teto_ed = col_te2.selectbox(
                "Tipo do Teto", tipos, index=idx_tipo,
                key=f"cat_tipo_ed_{cat_id}",
            )
            if teto_val_ed > 0:
                teto_brl_ed_preview = (Decimal(str(teto_val_ed)) * cambio).quantize(Decimal("0.01"))
                st.caption(f"Equivalente: R$ {teto_brl_ed_preview:,.2f} (cambio R$ {cambio:,.4f}/€)")

            salvar_cat_ed = st.form_submit_button("Atualizar")

        if salvar_cat_ed:
            cat_obj.nome = nome_cat_ed.strip()
            cat_obj.centro_custo_id = opcoes_cc_ed[cc_sel_ed]
            cat_obj.descricao = desc_cat_ed.strip()
            if tipo_teto_ed != "Sem teto" and teto_val_ed > 0:
                cat_obj.teto_eur = _to_decimal(teto_val_ed)
                cat_obj.teto_brl = None
                cat_obj.tipo_teto = tipo_teto_ed
            else:
                cat_obj.teto_eur = None
                cat_obj.teto_brl = None
                cat_obj.tipo_teto = None
            session.commit()
            invalidar_cache_cadastros()
            st.success("Categoria atualizada!")
            st.rerun()

    with col_del:
        tem_despesas = session.query(ItemDespesa).filter_by(categoria_despesa_id=cat_id).first()
        if tem_despesas:
            st.warning("Possui despesas vinculadas. Nao pode excluir.")
        else:
            if st.button("Excluir", type="primary", key="btn_del_cat"):
                session.delete(cat_obj)
                session.commit()
                invalidar_cache_cadastros()
                st.success("Categoria excluida!")
                st.rerun()

    session.close()


# ──────────────────────── aba: remessas ─────────────────────

def _aba_remessas():
    st.subheader("Remessas do Projeto")
    st.caption("O projeto recebe ate 3 remessas em Euros. Registre cada uma com o cambio efetivado.")

    session = get_session()

    # Garante que as 3 remessas existam no banco (inicializa se necessario)
    for num in (1, 2, 3):
        existe = session.query(Remessa).filter_by(numero=num).first()
        if not existe:
            session.add(Remessa(numero=num, valor_eur=Decimal("0.00"), recebida=False))
    session.commit()

    remessas = session.query(Remessa).order_by(Remessa.numero).all()

    for rem in remessas:
        status_icon = "✅" if rem.recebida else "⏳"
        with st.expander(f"{status_icon} Remessa {rem.numero}", expanded=not rem.recebida):
            with st.form(f"form_rem_{rem.numero}"):
                col1, col2 = st.columns(2)
                valor_eur = col1.number_input(
                    "Valor em EUR",
                    value=float(rem.valor_eur) if rem.valor_eur else 0.0,
                    min_value=0.0, step=1000.0, format="%.2f",
                    key=f"rem_eur_{rem.numero}",
                )
                recebida = col2.checkbox(
                    "Remessa recebida?",
                    value=rem.recebida,
                    key=f"rem_recebida_{rem.numero}",
                )

                col3, col4 = st.columns(2)
                cambio = col3.number_input(
                    "Cambio efetivado (EUR→BRL)",
                    value=float(rem.cambio_efetivado) if rem.cambio_efetivado else 6.0,
                    min_value=0.0001, step=0.01, format="%.4f",
                    key=f"rem_cambio_{rem.numero}",
                    help="Preenchido somente se a remessa estiver marcada como recebida.",
                )
                data_rec = col4.date_input(
                    "Data de recebimento",
                    value=rem.data_recebimento or date.today(),
                    key=f"rem_data_{rem.numero}",
                )

                obs = st.text_input(
                    "Observacao",
                    value=rem.observacao or "",
                    key=f"rem_obs_{rem.numero}",
                )

                # Projecao
                val_eur_dec = _to_decimal(valor_eur)
                if recebida:
                    cambio_dec = _to_decimal(cambio)
                    val_brl_calc = (val_eur_dec * cambio_dec).quantize(Decimal("0.01"))
                    st.info(f"Valor convertido: R$ {val_brl_calc:,.2f}")
                else:
                    val_brl_proj = (val_eur_dec * CAMBIO_PROJECAO).quantize(Decimal("0.01"))
                    st.caption(f"Projecao (cambio R$6,00): R$ {val_brl_proj:,.2f}")

                salvar_rem = st.form_submit_button("Salvar Remessa")

            if salvar_rem:
                rem.valor_eur = val_eur_dec
                rem.recebida = recebida
                rem.observacao = obs.strip()

                if recebida:
                    rem.cambio_efetivado = _to_decimal(cambio)
                    rem.valor_brl = (val_eur_dec * _to_decimal(cambio)).quantize(Decimal("0.01"))
                    rem.data_recebimento = data_rec
                else:
                    rem.cambio_efetivado = None
                    rem.valor_brl = None
                    rem.data_recebimento = None

                session.commit()
                invalidar_cache_cadastros()
                st.success(f"Remessa {rem.numero} salva!")
                st.rerun()

    # ---------- resumo ----------
    st.markdown("---")
    st.markdown("**Resumo das Remessas**")

    dados = []
    for rem in remessas:
        if rem.recebida:
            brl_str = f"R$ {rem.valor_brl:,.2f}" if rem.valor_brl else "—"
            cambio_str = f"R$ {rem.cambio_efetivado:,.4f}" if rem.cambio_efetivado else "—"
        else:
            proj = (rem.valor_eur * CAMBIO_PROJECAO).quantize(Decimal("0.01")) if rem.valor_eur else Decimal("0")
            brl_str = f"R$ {proj:,.2f} (proj.)"
            cambio_str = "R$ 6,0000 (proj.)"

        dados.append({
            "Remessa": rem.numero,
            "Valor EUR": f"€ {rem.valor_eur:,.2f}" if rem.valor_eur else "—",
            "Cambio": cambio_str,
            "Valor BRL": brl_str,
            "Status": "Recebida" if rem.recebida else "Pendente",
            "Data": str(rem.data_recebimento) if rem.data_recebimento else "—",
        })

    st.dataframe(pd.DataFrame(dados), use_container_width=True, hide_index=True)

    session.close()


# ──────────────────── aba: visao geral ──────────────────────

def _aba_visao_geral():
    st.subheader("Visao Geral do Orcamento")

    session = get_session()

    cambio = _cambio_medio(session)
    centros = session.query(CentroCusto).order_by(CentroCusto.codigo).all()
    remessas = session.query(Remessa).order_by(Remessa.numero).all()

    # --- totais de remessa ---
    total_eur_remessas = sum(r.valor_eur for r in remessas if r.valor_eur)
    total_brl_remessas = Decimal("0.00")
    for r in remessas:
        if r.recebida and r.valor_brl:
            total_brl_remessas += r.valor_brl
        elif r.valor_eur:
            total_brl_remessas += (r.valor_eur * CAMBIO_PROJECAO).quantize(Decimal("0.01"))

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Projeto (EUR)", f"€ {total_eur_remessas:,.2f}")
    col2.metric("Total Projeto (BRL)", f"R$ {total_brl_remessas:,.2f}")
    col3.metric("Cambio Medio", f"R$ {cambio:,.4f} / €")

    st.markdown("---")

    # --- centros de custo ---
    if not centros:
        st.info("Nenhum centro de custo cadastrado. Va na aba 'Centros de Custo' primeiro.")
        session.close()
        return

    total_teto_eur = Decimal("0.00")
    total_teto_brl = Decimal("0.00")

    dados = []
    for cc in centros:
        teto_brl = (cc.teto_eur * cambio).quantize(Decimal("0.01"))
        total_teto_eur += cc.teto_eur
        total_teto_brl += teto_brl

        # Gasto realizado (soma dos itens via categorias deste CC)
        gasto_brl = (
            session.query(sqlfunc.coalesce(sqlfunc.sum(ItemDespesa.valor_brl), 0))
            .join(CategoriaDespesa)
            .filter(CategoriaDespesa.centro_custo_id == cc.id)
            .scalar()
        )
        gasto_brl = Decimal(str(gasto_brl))
        saldo_brl = teto_brl - gasto_brl
        pct = (gasto_brl / teto_brl * 100).quantize(Decimal("0.1")) if teto_brl > 0 else Decimal("0")

        dados.append({
            "Codigo": cc.codigo,
            "Centro de Custo": cc.nome,
            "Teto EUR": f"€ {cc.teto_eur:,.2f}",
            "Teto BRL": f"R$ {teto_brl:,.2f}",
            "Gasto BRL": f"R$ {gasto_brl:,.2f}",
            "Saldo BRL": f"R$ {saldo_brl:,.2f}",
            "% Exec.": f"{pct}%",
        })

    st.dataframe(pd.DataFrame(dados), use_container_width=True, hide_index=True)

    # --- totalizador ---
    st.markdown("---")
    col_a, col_b = st.columns(2)
    col_a.metric("Total Tetos (EUR)", f"€ {total_teto_eur:,.2f}")
    col_b.metric("Total Tetos (BRL proj.)", f"R$ {total_teto_brl:,.2f}")

    # --- alerta se tetos > remessas ---
    if total_teto_eur > total_eur_remessas and total_eur_remessas > 0:
        diferenca = total_teto_eur - total_eur_remessas
        st.warning(
            f"A soma dos tetos (€ {total_teto_eur:,.2f}) excede o total das remessas "
            f"(€ {total_eur_remessas:,.2f}) em € {diferenca:,.2f}. Revise os tetos."
        )
    elif total_eur_remessas > 0 and total_teto_eur < total_eur_remessas:
        folga = total_eur_remessas - total_teto_eur
        st.success(
            f"Folga orcamentaria: € {folga:,.2f} entre remessas e tetos."
        )

    session.close()


# ──────────────────── render principal ──────────────────────

def render():
    st.title("📋 Cadastros")

    tab_cc, tab_cat, tab_rem, tab_vis = st.tabs([
        "Centros de Custo",
        "Categorias de Despesas",
        "Remessas",
        "Visao Geral",
    ])

    with tab_cc:
        _aba_centros_custo()

    with tab_cat:
        _aba_categorias()

    with tab_rem:
        _aba_remessas()

    with tab_vis:
        _aba_visao_geral()
