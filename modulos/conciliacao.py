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

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import pandas as pd
import streamlit as st

from database import get_session
from models import (
    CentroCusto,
    CategoriaDespesa,
    ItemDespesa,
    LancamentoRecorrente,
    Reembolso,
    Remessa,
    TransacaoBancaria,
)
from modulos.cache_utils import opcoes_categorias as opcoes_categorias_cached

# Janela (em dias) para sugerir match entre debito bancario e projecao de folha.
JANELA_MATCH_FOLHA_DIAS = 15

# Tolerancia em BRL para considerar valor "igual" (arredondamentos).
TOLERANCIA_MATCH_VALOR = Decimal("0.01")


def _to_decimal(valor, fallback=Decimal("0.00")):
    try:
        return Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        return fallback


def _opcoes_categorias(session=None):
    """Retorna dict {label: id} de categorias (cacheadas por 30s)."""
    return opcoes_categorias_cached()


def _lr_ocorre_no_mes(lr: LancamentoRecorrente, ano: int, mes: int) -> bool:
    """Verifica se um lancamento recorrente tem ocorrencia no mes/ano dado."""
    if date(ano, mes, 1) < date(lr.data_inicio.year, lr.data_inicio.month, 1):
        return False
    if date(ano, mes, 1) > date(lr.data_fim.year, lr.data_fim.month, 1):
        return False
    if lr.frequencia == "MENSAL":
        return True
    if lr.frequencia == "TRIMESTRAL":
        meses_desde = (ano - lr.data_inicio.year) * 12 + (mes - lr.data_inicio.month)
        return meses_desde % 3 == 0
    if lr.frequencia == "ANUAL":
        return mes == lr.data_inicio.month
    return False


def _meses_proximos(data_ref: date, raio: int = 1):
    """Gera (ano, mes) para os meses ao redor da data de referencia (para busca de match)."""
    for delta in range(-raio, raio + 1):
        mes = data_ref.month + delta
        ano = data_ref.year
        while mes < 1:
            mes += 12
            ano -= 1
        while mes > 12:
            mes -= 12
            ano += 1
        yield (ano, mes)


def _candidatos_par_estorno(session, tx: TransacaoBancaria) -> list:
    """Retorna transacoes candidatas a parear com tx em um estorno.

    Criterios:
      - tipo oposto (DEBIT <-> CREDIT)
      - mesmo valor (com sinal oposto: -79,80 <-> +79,80)
      - ou esta pendente (conciliada == False) OU ja e um estorno solo
        (eh_estorno == True AND estorno_par_id IS NULL).
    """
    tipo_oposto = "CREDIT" if tx.tipo == "DEBIT" else "DEBIT"
    valor_oposto = -tx.valor
    return (
        session.query(TransacaoBancaria)
        .filter(
            TransacaoBancaria.id != tx.id,
            TransacaoBancaria.tipo == tipo_oposto,
            TransacaoBancaria.valor == valor_oposto,
            (
                (TransacaoBancaria.conciliada == False)
                | (
                    (TransacaoBancaria.eh_estorno == True)
                    & (TransacaoBancaria.estorno_par_id == None)
                )
            ),
        )
        .order_by(TransacaoBancaria.data.desc())
        .all()
    )


def _marcar_como_estorno(session, tx: TransacaoBancaria, par_id=None):
    """Marca uma transacao como estorno (conciliada, sem impacto orcamentario).
    Se par_id for passado, faz o pareamento bidirecional.

    IMPORTANTE: remove qualquer ItemDespesa ja vinculado (estorno nao gera despesa).
    Despesas que fazem parte de Reembolso NAO sao removidas — caller deve barrar.
    """
    for it in list(tx.itens_despesa):
        if it.reembolso_id is None:
            session.delete(it)
    tx.eh_estorno = True
    tx.conciliada = True

    if par_id:
        par = session.get(TransacaoBancaria, par_id)
        if par:
            for it in list(par.itens_despesa):
                if it.reembolso_id is None:
                    session.delete(it)
            tx.estorno_par_id = par.id
            par.estorno_par_id = tx.id
            par.eh_estorno = True
            par.conciliada = True


def _desfazer_estorno(session, tx: TransacaoBancaria):
    """Desfaz marcacao de estorno. Se houver par, desfaz no par tambem."""
    par = tx.estorno_par
    tx.eh_estorno = False
    tx.conciliada = False
    tx.estorno_par_id = None
    if par is not None:
        par.estorno_par_id = None
        par.eh_estorno = False
        par.conciliada = False


def _bloco_marcar_estorno(session, tx: TransacaoBancaria, contexto: str):
    """Renderiza o bloco 'Marcar como estorno' para uma transacao.
    contexto = 'debito' ou 'credito' (so muda o texto auxiliar).
    Retorna True se o usuario acionou a marcacao (caller deve st.rerun)."""
    candidatos = _candidatos_par_estorno(session, tx)

    legenda = (
        "Use quando este debito foi estornado depois (ex: tarifa cobrada e devolvida)."
        if contexto == "debito"
        else "Use quando esta entrada e um estorno (ex: devolucao de tarifa, TED errado revertido)."
    )
    st.markdown("**🔄 Marcar como estorno (sem impacto orcamentario):**")
    st.caption(legenda)

    opcoes_par = {"(sem par - marcar sozinho)": None}
    for cand in candidatos:
        sinal = "+" if cand.tipo == "CREDIT" else "-"
        marca_estorno = " [estorno solo]" if cand.eh_estorno else ""
        label = (
            f"{cand.data.strftime('%d/%m/%Y')} | {cand.descricao[:50]} | "
            f"{sinal}R$ {abs(cand.valor):,.2f}{marca_estorno}"
        )
        opcoes_par[label] = cand.id

    sel_par = st.selectbox(
        "Parear com transacao oposta (mesmo valor)",
        list(opcoes_par.keys()),
        key=f"sel_par_estorno_{tx.id}",
    )
    st.caption(
        "Se a transacao oposta ja esta conciliada com despesa lancada, "
        "va em 'Ja Conciliadas' primeiro e desfaca a conciliacao dela "
        "para que apareca aqui."
    )
    if st.button(
        "Confirmar marcacao de estorno",
        key=f"btn_marcar_estorno_{tx.id}",
    ):
        _marcar_como_estorno(session, tx, opcoes_par[sel_par])
        session.commit()
        if opcoes_par[sel_par] is not None:
            st.success("Estorno marcado e pareado com a transacao oposta!")
        else:
            st.success(
                "Estorno marcado (sem par). Tu pode parear depois marcando "
                "a transacao oposta."
            )
        return True
    return False


def _candidatos_folha(session, tx: TransacaoBancaria) -> list:
    """Retorna lista de tuplas (lr, ano, mes, data_projetada, dias_diff) ordenadas
    por proximidade de data para um debito bancario tx.
    Criterios:
      - lr ativo
      - valor igual ao debito (tolerancia R$ 0,01)
      - ocorrencia projetada dentro de ±JANELA_MATCH_FOLHA_DIAS da data do debito
      - ocorrencia ainda nao realizada no mes
    """
    valor_abs = abs(tx.valor)

    recorrentes = (
        session.query(LancamentoRecorrente)
        .filter(
            LancamentoRecorrente.ativo == True,
            LancamentoRecorrente.valor_brl >= valor_abs - TOLERANCIA_MATCH_VALOR,
            LancamentoRecorrente.valor_brl <= valor_abs + TOLERANCIA_MATCH_VALOR,
        )
        .all()
    )

    candidatos = []
    for lr in recorrentes:
        melhor = None  # (dias_diff, ano, mes, data_proj)
        for ano, mes in _meses_proximos(tx.data, raio=1):
            if not _lr_ocorre_no_mes(lr, ano, mes):
                continue
            data_proj = lr.data_projetada_no_mes(ano, mes)
            if data_proj < lr.data_inicio or data_proj > lr.data_fim:
                continue
            dias_diff = abs((data_proj - tx.data).days)
            if dias_diff > JANELA_MATCH_FOLHA_DIAS:
                continue
            if lr.realizado_no_mes(ano, mes):
                continue
            if melhor is None or dias_diff < melhor[0]:
                melhor = (dias_diff, ano, mes, data_proj)
        if melhor is not None:
            dias_diff, ano, mes, data_proj = melhor
            candidatos.append((lr, ano, mes, data_proj, dias_diff))

    candidatos.sort(key=lambda c: c[4])  # proximidade de data
    return candidatos


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

    tab_pendentes, tab_conciliadas = st.tabs([
        "Pendentes",
        "Ja Conciliadas",
    ])

    with tab_pendentes:
        _aba_pendentes(session)

    with tab_conciliadas:
        _aba_conciliadas(session)

    session.close()


# ──────────────── aba: pendentes (creditos + debitos) ───────

def _aba_pendentes(session):
    """Aba unica com creditos e debitos ainda nao conciliados."""
    n_creditos = (
        session.query(TransacaoBancaria)
        .filter(
            TransacaoBancaria.tipo == "CREDIT",
            TransacaoBancaria.conciliada == False,
        )
        .count()
    )
    n_debitos = (
        session.query(TransacaoBancaria)
        .filter(
            TransacaoBancaria.tipo == "DEBIT",
            TransacaoBancaria.conciliada == False,
        )
        .count()
    )

    if n_creditos == 0 and n_debitos == 0:
        st.success("Nenhuma transacao pendente de conciliacao!")
        return

    col_a, col_b = st.columns(2)
    col_a.metric("Creditos pendentes", n_creditos)
    col_b.metric("Debitos pendentes", n_debitos)

    st.markdown("---")

    if n_creditos > 0:
        st.markdown("### 💰 Creditos (Entradas / Remessas)")
        _aba_creditos(session)
        st.markdown("---")

    if n_debitos > 0:
        st.markdown("### 💸 Debitos")
        _aba_debitos_pendentes(session)


# ──────────────── aba: debitos pendentes ────────────────────

def _aba_debitos_pendentes(session):

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

                # ─── Fluxo E: Marcar como estorno ───
                # Apenas possivel quando nenhum split foi criado (saldo == valor total)
                if not splits_existentes:
                    if _bloco_marcar_estorno(session, tx, contexto="debito"):
                        st.rerun()
                    st.markdown("---")

                # ─── Fluxo C: Vincular a Reembolso (valor exato) ───
                # Apenas possivel quando nenhum split foi criado ainda (saldo == valor total)
                if not splits_existentes:
                    reembolsos_compat = (
                        session.query(Reembolso)
                        .filter(
                            Reembolso.conciliado == False,
                            Reembolso.transacao_bancaria_id == None,
                            Reembolso.valor_total_brl == valor_abs,
                        )
                        .order_by(Reembolso.data_pagamento.desc())
                        .all()
                    )

                    if reembolsos_compat:
                        st.markdown("**Vincular a Reembolso (valor exato):**")
                        opcoes_reemb = {
                            f"#{r.id} | {r.data_pagamento} | {r.beneficiario} | "
                            f"R$ {r.valor_total_brl:,.2f} | {len(r.itens_despesa)} item(ns)": r.id
                            for r in reembolsos_compat
                        }
                        sel_reemb = st.selectbox(
                            "Reembolso disponivel",
                            list(opcoes_reemb.keys()),
                            key=f"sel_reemb_{tx.id}",
                        )
                        if st.button("Vincular este Reembolso", key=f"btn_vinc_reemb_{tx.id}"):
                            reemb = session.get(Reembolso, opcoes_reemb[sel_reemb])
                            # Cascateia conciliacao para o reembolso e todos os filhos
                            reemb.transacao_bancaria_id = tx.id
                            reemb.conciliado = True
                            for it in reemb.itens_despesa:
                                it.transacao_bancaria_id = tx.id
                                it.conciliado = True
                                it.data_pagamento = tx.data
                                it.data = tx.data
                            tx.conciliada = True
                            session.commit()
                            st.success(
                                f"Reembolso de {reemb.beneficiario} vinculado! "
                                f"{len(reemb.itens_despesa)} despesa(s) conciliada(s) em cascata."
                            )
                            st.rerun()

                        st.markdown("---")

                # ─── Fluxo D: Vincular a Folha (LancamentoRecorrente) ───
                # Apenas possivel quando nenhum split foi criado ainda (saldo == valor total)
                if not splits_existentes:
                    candidatos_folha = _candidatos_folha(session, tx)

                    if candidatos_folha:
                        st.markdown("**Vincular a Folha (Lancamento Recorrente):**")
                        st.caption(
                            f"Sugestoes dentro de ±{JANELA_MATCH_FOLHA_DIAS} dias e "
                            f"com valor igual (±R$ {TOLERANCIA_MATCH_VALOR:,.2f})."
                        )
                        opcoes_folha = {}
                        for lr, ano_oc, mes_oc, data_proj, dias_diff in candidatos_folha:
                            beneficiario = lr.tecnico.nome if lr.tecnico else "—"
                            label = (
                                f"[{data_proj.strftime('%d/%m/%Y')}] "
                                f"{lr.descricao} — {beneficiario} — "
                                f"R$ {lr.valor_brl:,.2f} "
                                f"({dias_diff} dia(s) de diferenca)"
                            )
                            opcoes_folha[label] = (lr.id, ano_oc, mes_oc, data_proj)

                        sel_folha = st.selectbox(
                            "Projecao compativel",
                            list(opcoes_folha.keys()),
                            key=f"sel_folha_{tx.id}",
                        )
                        if st.button("Vincular a Folha", key=f"btn_vinc_folha_{tx.id}"):
                            lr_id, ano_oc, mes_oc, data_proj = opcoes_folha[sel_folha]
                            lr = session.get(LancamentoRecorrente, lr_id)
                            beneficiario = lr.tecnico.nome if lr.tecnico else lr.descricao

                            # Cria ItemDespesa "realizado" vinculado ao recorrente
                            novo = ItemDespesa(
                                transacao_bancaria_id=tx.id,
                                categoria_despesa_id=lr.categoria_despesa_id,
                                lancamento_recorrente_id=lr.id,
                                valor_brl=valor_abs,
                                descricao=lr.descricao,
                                fornecedor_cliente=beneficiario,
                                data=tx.data,
                                data_emissao=data_proj,
                                data_pagamento=tx.data,
                                conciliado=True,
                            )
                            session.add(novo)
                            tx.conciliada = True
                            session.commit()
                            st.success(
                                f"Folha conciliada! '{lr.descricao}' de "
                                f"{mes_oc:02d}/{ano_oc} foi realizada."
                            )
                            st.rerun()

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
                        # Auto-finaliza se zerou o saldo
                        if manual_obj.valor_brl == saldo:
                            tx.conciliada = True
                        session.commit()
                        if tx.conciliada:
                            st.success("Lancamento vinculado e transacao conciliada!")
                        else:
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
                        # Auto-finaliza se este split zerou o saldo
                        if val_dec == saldo:
                            tx.conciliada = True
                        session.commit()
                        if tx.conciliada:
                            st.success("Split adicionado e transacao conciliada!")
                        else:
                            st.success("Split adicionado!")
                        st.rerun()


# ──────────────── aba: creditos ─────────────────────────────

def _aba_creditos(session):
    st.caption(
        "Vincule cada credito do extrato a uma remessa para confirmar o recebimento "
        "e calcular o cambio efetivado automaticamente."
    )

    creditos = (
        session.query(TransacaoBancaria)
        .filter(
            TransacaoBancaria.tipo == "CREDIT",
            TransacaoBancaria.conciliada == False,
        )
        .order_by(TransacaoBancaria.data.desc())
        .all()
    )

    if not creditos:
        st.info("Nenhum credito pendente.")
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
                # Nao vinculado — mostra TODAS as remessas com valor EUR cadastrado.
                # Permite reatribuir uma remessa ja recebida (corrige vinculos errados).
                remessas_disponiveis = [
                    r for r in remessas
                    if r.valor_eur and r.valor_eur > 0
                ]

                if not remessas_disponiveis:
                    st.warning(
                        "Nenhuma remessa cadastrada com valor EUR. "
                        "Verifique em Cadastros > Remessas."
                    )
                    # Mesmo sem remessas, permite marcar estorno
                    st.markdown("---")
                    if _bloco_marcar_estorno(session, tx, contexto="credito"):
                        st.rerun()
                else:
                    opcoes = {}
                    for r in remessas_disponiveis:
                        sufixo = " — JA RECEBIDA (sera reatribuida)" if r.recebida else ""
                        opcoes[f"Remessa {r.numero} — € {r.valor_eur:,.2f}{sufixo}"] = r.numero

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

                        # Aviso quando vai sobrescrever vinculo existente
                        if rem_sel.recebida and rem_sel.transacao_bancaria_id:
                            tx_anterior = session.get(
                                TransacaoBancaria, rem_sel.transacao_bancaria_id
                            )
                            if tx_anterior:
                                st.warning(
                                    f"Atencao: a Remessa {rem_sel.numero} ja esta "
                                    f"vinculada ao credito de {tx_anterior.data} "
                                    f"(R$ {tx_anterior.valor:,.2f}). "
                                    f"Ao confirmar, o vinculo anterior sera desfeito "
                                    f"e este credito passara a ser a Remessa {rem_sel.numero}."
                                )

                        vincular = st.form_submit_button("Confirmar Vinculo", type="primary")

                    if vincular:
                        # Se a remessa ja estava vinculada a outro credito, libera o anterior
                        if (
                            rem_sel.transacao_bancaria_id
                            and rem_sel.transacao_bancaria_id != tx.id
                        ):
                            tx_anterior = session.get(
                                TransacaoBancaria, rem_sel.transacao_bancaria_id
                            )
                            if tx_anterior:
                                tx_anterior.conciliada = False

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

                    # Alternativa: marcar como estorno (nao e remessa)
                    st.markdown("---")
                    if _bloco_marcar_estorno(session, tx, contexto="credito"):
                        st.rerun()

# ──────────────── aba: ja conciliadas ───────────────────────

def _aba_conciliadas(session):
    st.subheader("Transacoes Conciliadas")

    # Limites de data com base nas conciliadas existentes
    datas_existentes = [
        d for (d,) in session.query(TransacaoBancaria.data)
        .filter(TransacaoBancaria.conciliada == True)
        .all()
    ]

    if not datas_existentes:
        st.info("Nenhuma transacao conciliada ainda.")
        return

    data_min = min(datas_existentes)
    data_max = max(datas_existentes)

    # Filtro de periodo
    col_ini, col_fim, col_tipo = st.columns([2, 2, 2])
    data_ini = col_ini.date_input(
        "De",
        value=data_min,
        min_value=data_min,
        max_value=data_max,
        key="conc_data_ini",
        format="DD/MM/YYYY",
    )
    data_fim = col_fim.date_input(
        "Ate",
        value=data_max,
        min_value=data_min,
        max_value=data_max,
        key="conc_data_fim",
        format="DD/MM/YYYY",
    )
    filtro_tipo = col_tipo.selectbox(
        "Tipo",
        ["Todos", "Apenas Debitos", "Apenas Creditos"],
        key="conc_filtro_tipo",
    )

    if data_ini > data_fim:
        st.error("A data inicial nao pode ser maior que a data final.")
        return

    query = (
        session.query(TransacaoBancaria)
        .filter(
            TransacaoBancaria.conciliada == True,
            TransacaoBancaria.data >= data_ini,
            TransacaoBancaria.data <= data_fim,
        )
    )
    if filtro_tipo == "Apenas Debitos":
        query = query.filter(TransacaoBancaria.tipo == "DEBIT")
    elif filtro_tipo == "Apenas Creditos":
        query = query.filter(TransacaoBancaria.tipo == "CREDIT")

    conciliadas = query.order_by(TransacaoBancaria.data.desc()).all()

    if not conciliadas:
        st.info("Nenhuma transacao conciliada no periodo selecionado.")
        return

    st.caption(f"{len(conciliadas)} transacao(oes) no periodo")

    # Mapa de remessas para identificar creditos vinculados
    remessas_por_tx = {
        r.transacao_bancaria_id: r
        for r in session.query(Remessa).filter(Remessa.transacao_bancaria_id != None).all()
    }

    for tx in conciliadas:
        valor_abs = abs(tx.valor)

        # Caso 0: transacao e um ESTORNO (tem prioridade — nao impacta tetos)
        if tx.eh_estorno:
            sinal = "+" if tx.tipo == "CREDIT" else "-"
            par = tx.estorno_par
            if par:
                sinal_par = "+" if par.tipo == "CREDIT" else "-"
                info_par = (
                    f" | Par: {par.data.strftime('%d/%m/%Y')} "
                    f"{sinal_par}R$ {abs(par.valor):,.2f}"
                )
            else:
                info_par = " | (sem par)"
            titulo = (
                f"🔄 ESTORNO | {tx.data} | {tx.descricao} | "
                f"{sinal}R$ {valor_abs:,.2f}{info_par}"
            )

            with st.expander(titulo):
                st.info(
                    "Esta transacao foi marcada como **ESTORNO** e NAO impacta "
                    "nenhum teto orcamentario nem e tratada como remessa."
                )
                if par:
                    sinal_par = "+" if par.tipo == "CREDIT" else "-"
                    st.caption(
                        f"Pareada com: {par.data.strftime('%d/%m/%Y')} | "
                        f"{par.descricao} | {sinal_par}R$ {abs(par.valor):,.2f}"
                    )
                else:
                    st.warning(
                        "Sem par registrado. Se a transacao oposta ja foi importada, "
                        "marque-a tambem como estorno (em Pendentes) e selecione esta "
                        "como par."
                    )

                if st.button(
                    "Desfazer marcacao de estorno",
                    key=f"btn_desfazer_estorno_{tx.id}",
                ):
                    tinha_par = par is not None
                    _desfazer_estorno(session, tx)
                    session.commit()
                    if tinha_par:
                        st.info(
                            "Estorno desfeito. As duas transacoes (par) "
                            "voltaram para pendentes."
                        )
                    else:
                        st.info("Estorno desfeito. A transacao voltou para pendentes.")
                    st.rerun()
            continue

        # Caso 1: transacao e um CREDITO (Remessa recebida)
        if tx.tipo == "CREDIT":
            remessa = remessas_por_tx.get(tx.id)
            if remessa:
                titulo = (
                    f"✅ {tx.data} | {tx.descricao} | R$ {valor_abs:,.2f} | "
                    f"Remessa {remessa.numero}"
                )
            else:
                titulo = f"✅ {tx.data} | {tx.descricao} | R$ {valor_abs:,.2f} | Credito"

            with st.expander(titulo):
                if remessa:
                    st.success(
                        f"Remessa {remessa.numero} — "
                        f"€ {remessa.valor_eur:,.2f} × R$ {remessa.cambio_efetivado:,.4f} = "
                        f"R$ {remessa.valor_brl:,.2f}"
                    )
                else:
                    st.info("Credito conciliado sem vinculo a remessa.")

                if st.button("Desfazer Conciliacao", key=f"btn_desfazer_cred_{tx.id}"):
                    if remessa:
                        remessa.transacao_bancaria_id = None
                        remessa.recebida = False
                        remessa.cambio_efetivado = None
                        remessa.valor_brl = None
                        remessa.data_recebimento = None
                    tx.conciliada = False
                    session.commit()
                    st.info("Conciliacao desfeita. O credito voltou para pendentes.")
                    st.rerun()
            continue

        # Caso 2: transacao e um DEBITO
        # Identifica se esta transacao corresponde a um Reembolso conciliado
        reembolso_vinc = (
            session.query(Reembolso)
            .filter(Reembolso.transacao_bancaria_id == tx.id)
            .first()
        )

        # Identifica se ha ItemDespesa gerado por vinculo a Folha
        itens_folha = [
            s for s in tx.itens_despesa
            if s.lancamento_recorrente_id is not None and s.reembolso_id is None
        ]

        titulo = f"✅ {tx.data} | {tx.descricao} | R$ {valor_abs:,.2f}"
        if reembolso_vinc:
            titulo += f" | Reembolso: {reembolso_vinc.beneficiario}"
        elif itens_folha:
            titulo += " | Folha"

        with st.expander(titulo):
            if reembolso_vinc:
                st.info(
                    f"Esta transacao esta vinculada ao Reembolso "
                    f"de **{reembolso_vinc.beneficiario}** "
                    f"({len(reembolso_vinc.itens_despesa)} despesa(s))."
                )
            elif itens_folha:
                lr = itens_folha[0].lancamento_recorrente
                st.info(
                    f"Esta transacao realizou a projecao recorrente "
                    f"**{lr.descricao}**."
                )

            dados = []
            for s in tx.itens_despesa:
                cat = s.categoria_despesa
                if s.reembolso_id:
                    origem = "Reembolso"
                elif s.lancamento_recorrente_id:
                    origem = "Folha"
                elif s.conciliado:
                    origem = "Manual"
                else:
                    origem = "Split direto"
                dados.append({
                    "Fornecedor/Cliente": s.fornecedor_cliente or "—",
                    "Centro de Custo": cat.centro_custo.codigo,
                    "Categoria": cat.nome,
                    "Valor": f"R$ {s.valor_brl:,.2f}",
                    "Descricao": s.descricao or "—",
                    "Origem": origem,
                })
            st.dataframe(
                pd.DataFrame(dados),
                use_container_width=True, hide_index=True,
            )

            if st.button("Desfazer Conciliacao", key=f"btn_desfazer_{tx.id}"):
                if reembolso_vinc:
                    # Reverte cascata do reembolso
                    reembolso_vinc.transacao_bancaria_id = None
                    reembolso_vinc.conciliado = False
                    for it in reembolso_vinc.itens_despesa:
                        it.transacao_bancaria_id = None
                        it.conciliado = False
                # Remove itens gerados por vinculo a folha (voltam a ser projecao)
                for it_folha in itens_folha:
                    session.delete(it_folha)
                tx.conciliada = False
                session.commit()
                st.info("Conciliacao desfeita. A transacao voltou para pendentes.")
                st.rerun()
