"""
Modulo Conciliacao
==================
Interface para conciliar transacoes bancarias (OFX) atribuindo
splits a categorias de despesa ou vinculando lancamentos manuais.

Dois fluxos para cada transacao OFX:
  A) Vincular lancamentos manuais existentes (match)
  B) Criar splits novos diretamente
  Ambos podem ser combinados na mesma transacao.
"""

from decimal import Decimal, InvalidOperation

import pandas as pd
import streamlit as st

from database import get_session
from models import (
    CentroCusto,
    CategoriaDespesa,
    ItemDespesa,
    Remessa,
    TransacaoBancaria,
)


def _to_decimal(valor, fallback=Decimal("0.00")):
    try:
        return Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        return fallback


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
    st.title("🔗 Conciliacao")

    session = get_session()

    # Verifica pre-requisitos
    categorias = session.query(CategoriaDespesa).first()
    if not categorias:
        st.warning(
            "Nenhuma categoria de despesa cadastrada. "
            "Va em Cadastros > Categorias de Despesas primeiro."
        )
        session.close()
        return

    total_tx = session.query(TransacaoBancaria).count()
    if total_tx == 0:
        st.info("Nenhuma transacao importada. Va em Importacao OFX primeiro.")
        session.close()
        return

    tab_debitos, tab_creditos, tab_conciliadas = st.tabs([
        "Debitos Pendentes",
        "Creditos (Entradas)",
        "Ja Conciliadas",
    ])

    with tab_debitos:
        _aba_debitos_pendentes(session)

    with tab_creditos:
        _aba_creditos(session)

    with tab_conciliadas:
        _aba_conciliadas(session)

    session.close()


# ──────────────── aba: debitos pendentes ────────────────────

def _aba_debitos_pendentes(session):
    st.subheader("Debitos Pendentes de Conciliacao")

    pendentes = (
        session.query(TransacaoBancaria)
        .filter(
            TransacaoBancaria.tipo == "DEBIT",
            TransacaoBancaria.conciliada == False,
        )
        .order_by(TransacaoBancaria.data)
        .all()
    )

    if not pendentes:
        st.success("Nenhum debito pendente de conciliacao!")
        return

    st.caption(f"{len(pendentes)} transacao(oes) pendente(s)")

    opcoes_cat = _opcoes_categorias(session)

    if not opcoes_cat:
        st.warning("Cadastre categorias de despesa antes de conciliar.")
        return

    # Lancamentos manuais nao conciliados (disponiveis para match).
    # Exclui itens que ja fazem parte de um Reembolso.
    manuais_disponiveis = (
        session.query(ItemDespesa)
        .filter(
            ItemDespesa.conciliado == False,
            ItemDespesa.transacao_bancaria_id == None,
            ItemDespesa.reembolso_id == None,
        )
        .order_by(ItemDespesa.data_pagamento.asc().nullslast(), ItemDespesa.data)
        .all()
    )

    for tx in pendentes:
        valor_abs = abs(tx.valor)
        splits_existentes = tx.itens_despesa
        total_splits = sum(s.valor_brl for s in splits_existentes)
        saldo = valor_abs - total_splits

        icone = "✅" if saldo == 0 else "⏳"
        label = f"{icone} {tx.data} | {tx.descricao} | R$ {valor_abs:,.2f}"

        with st.expander(label, expanded=False):
            # Info da transacao
            col_info1, col_info2, col_info3 = st.columns(3)
            col_info1.metric("Valor Total", f"R$ {valor_abs:,.2f}")
            col_info2.metric("Ja Alocado", f"R$ {total_splits:,.2f}")
            col_info3.metric("Saldo Pendente", f"R$ {saldo:,.2f}")

            # Splits/lancamentos ja vinculados
            if splits_existentes:
                st.markdown("**Itens vinculados:**")
                dados_splits = []
                for s in splits_existentes:
                    cat = s.categoria_despesa
                    dados_splits.append({
                        "Fornecedor/Cliente": s.fornecedor_cliente or "—",
                        "Categoria": f"{cat.centro_custo.codigo} | {cat.nome}",
                        "Valor": f"R$ {s.valor_brl:,.2f}",
                        "Descricao": s.descricao or "—",
                        "Origem": "Manual" if s.conciliado else "Split direto",
                    })
                st.dataframe(
                    pd.DataFrame(dados_splits),
                    use_container_width=True, hide_index=True,
                )

                # Botoes de desvinculacao individual
                for s in splits_existentes:
                    cat = s.categoria_despesa
                    btn_key = f"desvincular_{s.id}_{tx.id}"
                    btn_label = f"Remover: {cat.centro_custo.codigo}|{cat.nome} R${s.valor_brl:,.2f}"
                    if st.button(btn_label, key=btn_key):
                        if s.conciliado:
                            # Era um manual vinculado — desvincular (volta a ser manual)
                            s.transacao_bancaria_id = None
                            s.conciliado = False
                            session.commit()
                            st.info("Lancamento manual desvinculado (continua existindo em Lancamentos).")
                        else:
                            # Era um split criado direto — excluir
                            session.delete(s)
                            session.commit()
                            st.success("Split removido!")
                        st.rerun()

            if saldo > 0:
                st.markdown("---")

                # ─── Fluxo A: Vincular lancamentos manuais ───
                manuais_para_tx = [
                    m for m in manuais_disponiveis
                    if m.valor_brl <= saldo
                ]

                if manuais_para_tx:
                    st.markdown("**Vincular lancamento manual existente:**")
                    opcoes_manuais = {
                        f"#{m.id} | {m.data_pagamento or m.data} | "
                        f"{(m.fornecedor_cliente or '—')[:25]} | "
                        f"{m.categoria_despesa.centro_custo.codigo}|{m.categoria_despesa.nome} | "
                        f"R$ {m.valor_brl:,.2f} | {(m.descricao or '')[:30]}": m.id
                        for m in manuais_para_tx
                    }
                    sel_manual = st.selectbox(
                        "Lancamento disponivel",
                        list(opcoes_manuais.keys()),
                        key=f"sel_manual_{tx.id}",
                    )
                    if st.button("Vincular este lancamento", key=f"btn_vincular_{tx.id}"):
                        manual_id = opcoes_manuais[sel_manual]
                        manual_obj = session.get(ItemDespesa, manual_id)
                        manual_obj.transacao_bancaria_id = tx.id
                        manual_obj.conciliado = True
                        # Atualiza data de pagamento com a data real do debito
                        manual_obj.data_pagamento = tx.data
                        session.commit()
                        st.success("Lancamento manual vinculado!")
                        st.rerun()

                    st.markdown("---")

                # ─── Fluxo B: Criar split novo ───
                st.markdown("**Ou criar split novo:**")
                with st.form(f"form_split_{tx.id}"):
                    cat_sel = st.selectbox(
                        "Categoria",
                        list(opcoes_cat.keys()),
                        key=f"cat_split_{tx.id}",
                    )
                    col_v, col_d = st.columns([1, 2])
                    valor_split = col_v.number_input(
                        "Valor (R$)",
                        min_value=0.01,
                        max_value=float(saldo),
                        value=float(saldo),
                        step=0.01,
                        format="%.2f",
                        key=f"val_split_{tx.id}",
                    )
                    desc_split = col_d.text_input(
                        "Descricao (opcional)",
                        key=f"desc_split_{tx.id}",
                    )
                    adicionar = st.form_submit_button("Adicionar Split")

                if adicionar:
                    val_dec = _to_decimal(valor_split)
                    if val_dec <= 0:
                        st.error("Valor deve ser maior que zero.")
                    elif val_dec > saldo:
                        st.error(f"Valor excede o saldo pendente (R$ {saldo:,.2f}).")
                    else:
                        novo_item = ItemDespesa(
                            transacao_bancaria_id=tx.id,
                            categoria_despesa_id=opcoes_cat[cat_sel],
                            valor_brl=val_dec,
                            descricao=desc_split.strip(),
                            data=tx.data,
                            data_pagamento=tx.data,
                            conciliado=False,
                        )
                        session.add(novo_item)
                        session.commit()
                        st.success("Split adicionado!")
                        st.rerun()

            # Finalizar conciliacao
            if saldo == 0 and splits_existentes:
                st.markdown("---")
                st.success("Todos os valores foram alocados!")
                if st.button(
                    "Finalizar Conciliacao",
                    type="primary",
                    key=f"btn_finalizar_{tx.id}",
                ):
                    tx.conciliada = True
                    session.commit()
                    st.success("Transacao conciliada com sucesso!")
                    st.rerun()


# ──────────────── aba: creditos ─────────────────────────────

def _aba_creditos(session):
    st.subheader("Creditos (Entradas)")
    st.caption(
        "Vincule cada credito do extrato a uma remessa para confirmar o recebimento "
        "e calcular o cambio efetivado automaticamente."
    )

    creditos = (
        session.query(TransacaoBancaria)
        .filter(TransacaoBancaria.tipo == "CREDIT")
        .order_by(TransacaoBancaria.data.desc())
        .all()
    )

    if not creditos:
        st.info("Nenhum credito encontrado no extrato importado.")
        return

    # Remessas disponiveis
    remessas = session.query(Remessa).order_by(Remessa.numero).all()

    # Identifica quais creditos ja estao vinculados a alguma remessa
    creditos_vinculados = {
        r.transacao_bancaria_id: r for r in remessas if r.transacao_bancaria_id
    }

    for tx in creditos:
        remessa_vinculada = creditos_vinculados.get(tx.id)

        if remessa_vinculada:
            icone = "✅"
            info = f"Remessa {remessa_vinculada.numero}"
        else:
            icone = "⏳"
            info = "Nao vinculado"

        label = f"{icone} {tx.data} | {tx.descricao} | R$ {tx.valor:,.2f} | {info}"

        with st.expander(label, expanded=not remessa_vinculada):
            col1, col2 = st.columns(2)
            col1.metric("Valor BRL (extrato)", f"R$ {tx.valor:,.2f}")
            col2.metric("Data", str(tx.data))

            if remessa_vinculada:
                # Ja vinculado — mostra detalhes e opcao de desvincular
                r = remessa_vinculada
                st.success(
                    f"Vinculado a Remessa {r.numero} — "
                    f"€ {r.valor_eur:,.2f} × R$ {r.cambio_efetivado:,.4f} = "
                    f"R$ {r.valor_brl:,.2f}"
                )
                if st.button("Desvincular desta Remessa", key=f"desvincular_cred_{tx.id}"):
                    r.transacao_bancaria_id = None
                    r.recebida = False
                    r.cambio_efetivado = None
                    r.valor_brl = None
                    r.data_recebimento = None
                    tx.conciliada = False
                    session.commit()
                    st.info("Credito desvinculado. Remessa voltou ao status pendente.")
                    st.rerun()
            else:
                # Nao vinculado — mostra opcoes de remessa
                remessas_disponiveis = [
                    r for r in remessas
                    if not r.recebida and r.valor_eur and r.valor_eur > 0
                ]

                if not remessas_disponiveis:
                    st.warning(
                        "Todas as remessas ja foram recebidas ou nao possuem valor EUR cadastrado. "
                        "Verifique em Cadastros > Remessas."
                    )
                else:
                    opcoes = {
                        f"Remessa {r.numero} — € {r.valor_eur:,.2f}": r.numero
                        for r in remessas_disponiveis
                    }

                    with st.form(f"form_vincular_cred_{tx.id}"):
                        sel = st.selectbox(
                            "Vincular a qual remessa?",
                            list(opcoes.keys()),
                            key=f"sel_rem_cred_{tx.id}",
                        )
                        numero_sel = opcoes[sel]
                        rem_sel = next(r for r in remessas if r.numero == numero_sel)

                        # Preview do cambio
                        if rem_sel.valor_eur and rem_sel.valor_eur > 0:
                            cambio_calc = (tx.valor / rem_sel.valor_eur).quantize(
                                Decimal("0.0001")
                            )
                            st.info(
                                f"Cambio calculado: R$ {tx.valor:,.2f} / € {rem_sel.valor_eur:,.2f} "
                                f"= **R$ {cambio_calc:,.4f} / €**"
                            )

                        vincular = st.form_submit_button("Confirmar Vinculo", type="primary")

                    if vincular:
                        cambio_efetivado = (tx.valor / rem_sel.valor_eur).quantize(
                            Decimal("0.0001")
                        )
                        rem_sel.recebida = True
                        rem_sel.valor_brl = tx.valor
                        rem_sel.cambio_efetivado = cambio_efetivado
                        rem_sel.data_recebimento = tx.data
                        rem_sel.transacao_bancaria_id = tx.id
                        tx.conciliada = True
                        session.commit()
                        st.success(
                            f"Remessa {rem_sel.numero} confirmada! "
                            f"Cambio: R$ {cambio_efetivado:,.4f}/€ | "
                            f"Total: R$ {tx.valor:,.2f}"
                        )
                        st.rerun()

    # Totalizador
    st.markdown("---")
    total = sum(tx.valor for tx in creditos)
    vinculados = sum(1 for tx in creditos if tx.id in creditos_vinculados)
    col_a, col_b = st.columns(2)
    col_a.metric("Total Creditos", f"R$ {total:,.2f}")
    col_b.metric("Vinculados a Remessas", f"{vinculados} de {len(creditos)}")


# ──────────────── aba: ja conciliadas ───────────────────────

def _aba_conciliadas(session):
    st.subheader("Transacoes Conciliadas")

    conciliadas = (
        session.query(TransacaoBancaria)
        .filter(TransacaoBancaria.conciliada == True)
        .order_by(TransacaoBancaria.data.desc())
        .all()
    )

    if not conciliadas:
        st.info("Nenhuma transacao conciliada ainda.")
        return

    for tx in conciliadas:
        valor_abs = abs(tx.valor)
        with st.expander(f"✅ {tx.data} | {tx.descricao} | R$ {valor_abs:,.2f}"):
            dados = []
            for s in tx.itens_despesa:
                cat = s.categoria_despesa
                dados.append({
                    "Fornecedor/Cliente": s.fornecedor_cliente or "—",
                    "Centro de Custo": cat.centro_custo.codigo,
                    "Categoria": cat.nome,
                    "Valor": f"R$ {s.valor_brl:,.2f}",
                    "Descricao": s.descricao or "—",
                    "Origem": "Manual" if s.conciliado else "Split direto",
                })
            st.dataframe(
                pd.DataFrame(dados),
                use_container_width=True, hide_index=True,
            )

            if st.button("Desfazer Conciliacao", key=f"btn_desfazer_{tx.id}"):
                tx.conciliada = False
                session.commit()
                st.info("Conciliacao desfeita. A transacao voltou para pendentes.")
                st.rerun()
