"""
Modulo Aplicacao Financeira
===========================
Controle do saldo parado em conta corrente que foi aplicado em fundo/conta
separada (sem OFX proprio).

Modelo mental:
    CONTA CORRENTE  ──(aplicacao)──▶  FUNDO  ──(resgate)──▶  CONTA CORRENTE
                                        │
                                        └── rendimento (+) e IR/IOF (-)

  - Aplicacao e resgate aparecem no extrato OFX da conta corrente e NAO sao
    despesa nem remessa: o dinheiro so mudou de lugar.
  - Rendimento e IR/IOF acontecem dentro do fundo (sem linha no OFX) e sao
    registrados manualmente a partir do extrato da aplicacao.
  - Rendimento e receita nova do projeto, mas NAO altera os tetos em EUR.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

import pandas as pd
import streamlit as st
from sqlalchemy import func as sqlfunc

from database import get_session
from models import (
    TIPOS_MOVIMENTO_COM_EXTRATO,
    ItemDespesa,
    MovimentoAplicacao,
    Remessa,
    TransacaoBancaria,
)

# Rotulos amigaveis dos tipos de movimento
ROTULOS_TIPO = {
    "APLICACAO": "Aplicacao (conta corrente ➜ fundo)",
    "RESGATE": "Resgate (fundo ➜ conta corrente)",
    "RENDIMENTO": "Rendimento do fundo",
    "IR_IOF": "IR / IOF retido sobre o rendimento",
}

ROTULOS_CURTOS = {
    "APLICACAO": "Aplicacao",
    "RESGATE": "Resgate",
    "RENDIMENTO": "Rendimento",
    "IR_IOF": "IR/IOF",
}

# Tipos que possuem linha correspondente no extrato da conta corrente
TIPOS_COM_EXTRATO = TIPOS_MOVIMENTO_COM_EXTRATO

# Sinal de cada tipo no extrato da conta corrente
TIPO_BANCARIO_ESPERADO = {"APLICACAO": "DEBIT", "RESGATE": "CREDIT"}


# ──────────────── Calculos consolidados ────────────────────

def _q2(v: Decimal) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"))


def _to_decimal(valor, fallback=Decimal("0.00")):
    try:
        return Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        return fallback


def _soma_por_tipo(session, tipo: str) -> Decimal:
    resultado = (
        session.query(sqlfunc.coalesce(sqlfunc.sum(MovimentoAplicacao.valor_brl), 0))
        .filter(MovimentoAplicacao.tipo == tipo)
        .scalar()
    )
    return Decimal(str(resultado))


def saldo_aplicado(session) -> Decimal:
    """Quanto esta no fundo agora: aplicacoes + rendimentos - resgates - IR/IOF."""
    return _q2(
        _soma_por_tipo(session, "APLICACAO")
        + _soma_por_tipo(session, "RENDIMENTO")
        - _soma_por_tipo(session, "RESGATE")
        - _soma_por_tipo(session, "IR_IOF")
    )


def rendimento_liquido(session) -> Decimal:
    """Receita liquida gerada pela aplicacao: rendimentos - IR/IOF."""
    return _q2(_soma_por_tipo(session, "RENDIMENTO") - _soma_por_tipo(session, "IR_IOF"))


def saldo_extrato(session) -> Decimal:
    """Saldo da conta corrente segundo o extrato OFX ja importado.

    NAO e a fonte do saldo do projeto — serve para conferir os lancamentos.
    Fica defasado entre uma importacao e outra.
    """
    resultado = (
        session.query(sqlfunc.coalesce(sqlfunc.sum(TransacaoBancaria.valor), 0))
        .scalar()
    )
    return _q2(resultado)


def _despesa_esta_paga(item: ItemDespesa, hoje: date) -> bool:
    """True quando o dinheiro ja saiu (ou deveria ter saido) da conta.

    Se esta vinculada ao extrato, o banco ja debitou — nao ha duvida.
    Caso contrario vale a data de pagamento informada no lancamento.
    """
    if item.transacao_bancaria_id is not None:
        return True
    data_efetiva = item.data_pagamento or item.data
    return data_efetiva is not None and data_efetiva <= hoje


def despesas_lancadas(session, hoje: date = None) -> dict:
    """Separa as despesas lancadas entre ja pagas e ainda a pagar.

    Conta TODOS os lancamentos, conciliados ou nao: o saldo do projeto vem do
    que foi lancado, nao do que o extrato ja mostrou.
    """
    hoje = hoje or date.today()
    itens = session.query(ItemDespesa).all()

    pagas = Decimal("0.00")
    a_pagar = Decimal("0.00")
    pagas_sem_extrato = Decimal("0.00")

    for item in itens:
        valor = Decimal(str(item.valor_brl))
        if _despesa_esta_paga(item, hoje):
            pagas += valor
            if item.transacao_bancaria_id is None:
                pagas_sem_extrato += valor
        else:
            a_pagar += valor

    return {
        "pagas": _q2(pagas),
        "a_pagar": _q2(a_pagar),
        "pagas_sem_extrato": _q2(pagas_sem_extrato),
        "total": _q2(pagas + a_pagar),
    }


def consolidado(session, hoje: date = None) -> dict:
    """Saldo do projeto a partir dos LANCAMENTOS (nao do extrato).

    O extrato OFX chega uma vez por mes e serve para validar — se o saldo
    dependesse dele, ficaria parado no tempo entre uma importacao e outra.

        conta corrente = remessas recebidas - despesas pagas
                         - aplicacoes + resgates
        saldo aplicado = aplicacoes + rendimentos - resgates - IR/IOF
        disponivel     = conta corrente + saldo aplicado

    O rendimento nao aparece na conta corrente porque nasce e fica dentro do
    fundo — so entra na conta se for resgatado.
    """
    hoje = hoje or date.today()

    remessas_recebidas = Decimal(str(
        session.query(sqlfunc.coalesce(sqlfunc.sum(Remessa.valor_brl), 0))
        .filter(Remessa.recebida == True)
        .scalar()
    ))

    desp = despesas_lancadas(session, hoje)
    aplicacoes = _soma_por_tipo(session, "APLICACAO")
    resgates = _soma_por_tipo(session, "RESGATE")

    cc = _q2(remessas_recebidas - desp["pagas"] - aplicacoes + resgates)
    aplicado = saldo_aplicado(session)
    disponivel = _q2(cc + aplicado)
    rend = rendimento_liquido(session)

    return {
        "saldo_conta_corrente": cc,
        "saldo_aplicado": aplicado,
        "disponivel_total": disponivel,
        "remessas_recebidas": remessas_recebidas,
        "rendimento_liquido": rend,
        "despesas_pagas": desp["pagas"],
        "despesas_a_pagar": desp["a_pagar"],
        "despesas_pagas_sem_extrato": desp["pagas_sem_extrato"],
        "aplicacoes": _q2(aplicacoes),
        "resgates": _q2(resgates),
        "disponivel_apos_compromissos": _q2(disponivel - desp["a_pagar"]),
    }


def validacao_extrato(session, hoje: date = None) -> dict:
    """Confere os lancamentos contra o extrato OFX ja importado.

    O extrato so conhece o que ja foi importado. Enquanto voce lanca durante o
    mes e importa o OFX uma vez, e NORMAL que os dois numeros sejam diferentes:
    a diferenca deve ser exatamente o que ainda nao passou pelo extrato.

        diferenca esperada = remessas sem credito importado
                             - despesas pagas sem linha no extrato
                             - aplicacoes sem linha + resgates sem linha

    O que sobra disso (residual) e o que realmente merece atencao: linha do
    extrato importada e nunca classificada, ou valor lancado diferente do que
    o banco debitou.
    """
    hoje = hoje or date.today()
    c = consolidado(session, hoje)
    extrato = saldo_extrato(session)

    diferenca = _q2(c["saldo_conta_corrente"] - extrato)

    # Remessas recebidas que ainda nao foram casadas com um credito do extrato
    remessas_sem_extrato = Decimal(str(
        session.query(sqlfunc.coalesce(sqlfunc.sum(Remessa.valor_brl), 0))
        .filter(Remessa.recebida == True, Remessa.transacao_bancaria_id == None)
        .scalar()
    ))

    # Movimentos de aplicacao/resgate ainda sem linha no extrato
    mov_sem_extrato = movimentos_sem_extrato(session)
    aplic_sem_extrato = _q2(sum(
        (Decimal(str(m.valor_brl)) for m in mov_sem_extrato if m.tipo == "APLICACAO"),
        Decimal("0.00"),
    ))
    resg_sem_extrato = _q2(sum(
        (Decimal(str(m.valor_brl)) for m in mov_sem_extrato if m.tipo == "RESGATE"),
        Decimal("0.00"),
    ))

    esperada = _q2(
        remessas_sem_extrato
        - c["despesas_pagas_sem_extrato"]
        - aplic_sem_extrato
        + resg_sem_extrato
    )
    residual = _q2(diferenca - esperada)

    return {
        "saldo_extrato": extrato,
        "saldo_lancamentos": c["saldo_conta_corrente"],
        "diferenca": diferenca,
        "esperada": esperada,
        "residual": residual,
        "remessas_sem_extrato": remessas_sem_extrato,
        "despesas_sem_extrato": c["despesas_pagas_sem_extrato"],
        "aplicacoes_sem_extrato": aplic_sem_extrato,
        "resgates_sem_extrato": resg_sem_extrato,
        "movimentos_sem_extrato": mov_sem_extrato,
        "tem_extrato": session.query(TransacaoBancaria).first() is not None,
        "exige_acao": abs(residual) >= Decimal("0.01"),
    }


# ──────────────── Vinculo com o extrato ────────────────────

def marcar_transacao_como_aplicacao(session, tx: TransacaoBancaria):
    """Marca a linha do extrato como movimentacao de aplicacao.

    Nao gera despesa nem remessa — remove qualquer split solto que exista.
    """
    for item in list(tx.itens_despesa):
        if item.reembolso_id is None:
            session.delete(item)
    tx.eh_aplicacao = True
    tx.conciliada = True


def desmarcar_transacao_aplicacao(session, tx: TransacaoBancaria):
    """Devolve a linha do extrato para a fila de conciliacao."""
    tx.eh_aplicacao = False
    tx.conciliada = False


def transacoes_candidatas(session, tipo_mov: str, valor: Decimal = None) -> list:
    """Linhas do extrato compativeis com um movimento de aplicacao/resgate."""
    tipo_bancario = TIPO_BANCARIO_ESPERADO.get(tipo_mov)
    if tipo_bancario is None:
        return []

    ja_vinculadas = {
        m.transacao_bancaria_id
        for m in session.query(MovimentoAplicacao)
        .filter(MovimentoAplicacao.transacao_bancaria_id != None)
        .all()
    }

    query = session.query(TransacaoBancaria).filter(
        TransacaoBancaria.tipo == tipo_bancario,
    )
    candidatas = [
        tx for tx in query.order_by(TransacaoBancaria.data.desc()).all()
        if tx.id not in ja_vinculadas
        and (not tx.conciliada or tx.eh_aplicacao)
        and not tx.eh_estorno
    ]

    if valor is not None and valor > 0:
        exatas = [tx for tx in candidatas if abs(tx.valor) == valor]
        if exatas:
            return exatas
    return candidatas


# ──────────────── Render principal ─────────────────────────

def render():
    st.title("Aplicacao Financeira")
    st.caption(
        "Controle do saldo aplicado, dos rendimentos e do caixa consolidado do projeto."
    )

    session = get_session()

    _painel_consolidado(session)

    st.markdown("---")

    tab_novo, tab_mov, tab_ajuda = st.tabs([
        "Registrar Movimento",
        "Movimentos Registrados",
        "Como funciona",
    ])

    with tab_novo:
        _aba_novo_movimento(session)

    with tab_mov:
        _aba_movimentos(session)

    with tab_ajuda:
        _aba_ajuda()

    session.close()


# ──────────────── Painel consolidado ───────────────────────

def _fmt(v) -> str:
    s = f"{float(v):,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def _painel_consolidado(session):
    c = consolidado(session)

    st.subheader("Saldo Consolidado do Projeto")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Saldo em Conta Corrente", _fmt(c["saldo_conta_corrente"]))
    col2.metric("Saldo Aplicado", _fmt(c["saldo_aplicado"]))
    col3.metric("Disponivel Total", _fmt(c["disponivel_total"]))
    col4.metric("Rendimento Liquido", _fmt(c["rendimento_liquido"]))

    st.caption(
        "Estes saldos vem dos seus **lancamentos** e atualizam na hora, sem depender "
        "da importacao do OFX. Disponivel Total = conta corrente + aplicado. "
        "Rendimento Liquido = rendimentos do fundo menos IR/IOF (receita do projeto, "
        "que nao altera os tetos em EUR)."
    )

    if c["despesas_a_pagar"] > 0:
        st.caption(
            f"Ha {_fmt(c['despesas_a_pagar'])} em despesas lancadas com pagamento "
            f"futuro. Descontando esses compromissos, sobram "
            f"{_fmt(c['disponivel_apos_compromissos'])}."
        )

    with st.expander("Como este saldo e calculado", expanded=False):
        st.markdown(
            f"""
| Conta | Valor |
|---|---|
| Remessas recebidas | {_fmt(c['remessas_recebidas'])} |
| (−) Despesas lancadas e ja pagas | {_fmt(c['despesas_pagas'])} |
| (−) Aplicado no fundo | {_fmt(c['aplicacoes'])} |
| (+) Resgatado do fundo | {_fmt(c['resgates'])} |
| **= Saldo em conta corrente** | **{_fmt(c['saldo_conta_corrente'])}** |
| (+) Saldo aplicado (com rendimentos) | {_fmt(c['saldo_aplicado'])} |
| **= Disponivel Total** | **{_fmt(c['disponivel_total'])}** |
"""
        )
        st.caption(
            "O rendimento nao entra na conta corrente porque nasce e permanece dentro "
            "do fundo — so chega na conta se voce resgatar."
        )

    _validacao_extrato(session)


def _validacao_extrato(session):
    """Confronta os lancamentos com o extrato OFX ja importado."""
    v = validacao_extrato(session)

    if not v["tem_extrato"]:
        return

    titulo = "Validacao com o extrato bancario (OFX)"
    with st.expander(titulo, expanded=v["exige_acao"]):
        st.markdown(
            f"""
| Conta | Valor |
|---|---|
| Saldo pelos seus lancamentos | {_fmt(v['saldo_lancamentos'])} |
| Saldo pelo extrato importado | {_fmt(v['saldo_extrato'])} |
| **Diferenca** | **{_fmt(v['diferenca'])}** |
| Diferenca explicada (ainda nao passou pelo extrato) | {_fmt(v['esperada'])} |
| **Sobra sem explicacao** | **{_fmt(v['residual'])}** |
"""
        )

        detalhes = []
        if v["remessas_sem_extrato"] > 0:
            detalhes.append(
                f"- Remessas recebidas sem credito importado: {_fmt(v['remessas_sem_extrato'])}"
            )
        if v["despesas_sem_extrato"] > 0:
            detalhes.append(
                f"- Despesas lancadas que o extrato ainda nao mostrou: "
                f"{_fmt(v['despesas_sem_extrato'])}"
            )
        if v["aplicacoes_sem_extrato"] > 0:
            detalhes.append(
                f"- Aplicacoes sem linha no extrato: {_fmt(v['aplicacoes_sem_extrato'])}"
            )
        if v["resgates_sem_extrato"] > 0:
            detalhes.append(
                f"- Resgates sem linha no extrato: {_fmt(v['resgates_sem_extrato'])}"
            )

        if detalhes:
            st.info(
                "A diferenca abaixo e **esperada** — sao lancamentos seus que o extrato "
                "importado ainda nao cobre. Some quando voce importar o OFX do periodo:\n\n"
                + "\n".join(detalhes)
            )

        if not v["exige_acao"]:
            st.success(
                "Seus lancamentos batem com o extrato. Toda diferenca esta explicada "
                "por movimentacao que o OFX importado ainda nao alcanca."
            )
        else:
            st.warning(
                f"Sobram {_fmt(v['residual'])} sem explicacao. Causas comuns:\n\n"
                "1. Alguma linha do extrato foi importada e nunca classificada na Conciliacao.\n"
                "2. Um valor lancado esta diferente do que o banco debitou.\n"
                "3. Uma despesa aconteceu no banco e nunca foi lancada no sistema.\n"
                "4. A conta tinha saldo antes do primeiro OFX importado."
            )

        st.caption(
            "Esta validacao cobre tudo que passa pela conta corrente. Ela **nao** valida "
            "rendimento e IR/IOF, que nascem dentro do fundo e nao tem linha no extrato "
            "bancario — para isso use a conferencia do saldo aplicado abaixo."
        )

    _conferencia_fundo(session, consolidado(session)["saldo_aplicado"])


def _conferencia_fundo(session, saldo_calculado: Decimal):
    """Compara o saldo aplicado calculado com o informado no extrato do fundo.

    E a unica forma de validar rendimento e IR/IOF, que nao passam pela conta
    corrente e portanto nao aparecem na conferencia de caixa.
    """
    with st.expander("Conferir com o extrato da aplicacao", expanded=False):
        st.caption(
            "Pegue o saldo que o banco informa no extrato do fundo e digite abaixo. "
            "Se houver diferenca, faltou lancar algum rendimento ou IR/IOF aqui."
        )
        col1, col2 = st.columns(2)
        informado = col1.number_input(
            "Saldo informado no extrato da aplicacao (R$)",
            min_value=0.0, step=100.0, format="%.2f",
            key="apl_saldo_informado",
        )
        col2.metric("Saldo aplicado no sistema", _fmt(saldo_calculado))

        inf_dec = _to_decimal(informado)
        if inf_dec <= 0:
            return

        dif = _q2(inf_dec - saldo_calculado)
        if abs(dif) < Decimal("0.01"):
            st.success("Saldo aplicado confere com o extrato do fundo.")
        elif dif > 0:
            st.warning(
                f"O fundo tem {_fmt(dif)} a mais do que o sistema registra. "
                "Provavelmente falta lancar um **Rendimento** desse valor."
            )
        else:
            st.warning(
                f"O fundo tem {_fmt(abs(dif))} a menos do que o sistema registra. "
                "Provavelmente falta lancar um **IR/IOF** desse valor, ou um resgate "
                "que ainda nao foi registrado."
            )


# ──────────────── Aba: novo movimento ──────────────────────

def _aba_novo_movimento(session):
    st.subheader("Registrar Movimento da Aplicacao")

    tipo_mov = st.selectbox(
        "Tipo de movimento *",
        list(ROTULOS_TIPO.keys()),
        format_func=lambda t: ROTULOS_TIPO[t],
        key="apl_tipo",
    )

    if tipo_mov in TIPOS_COM_EXTRATO:
        st.info(
            "Este movimento passa pela conta corrente. Vincule a linha do extrato "
            "para que ela saia da fila de conciliacao sem virar despesa ou remessa."
        )
    else:
        st.info(
            "Este movimento acontece dentro do fundo e nao aparece no extrato da "
            "conta corrente. Registre a partir do extrato da aplicacao."
        )

    col1, col2 = st.columns(2)
    data_mov = col1.date_input("Data *", value=date.today(), key="apl_data")
    valor_mov = col2.number_input(
        "Valor (R$) *", min_value=0.01, step=100.0, format="%.2f", key="apl_valor",
    )

    valor_dec = _to_decimal(valor_mov)

    tx_id = None
    if tipo_mov in TIPOS_COM_EXTRATO:
        candidatas = transacoes_candidatas(session, tipo_mov, valor_dec)
        if candidatas:
            opcoes = {"(nao vincular agora)": None}
            for tx in candidatas:
                marca = " [ja marcada como aplicacao]" if tx.eh_aplicacao else ""
                opcoes[
                    f"{tx.data.strftime('%d/%m/%Y')} | {tx.descricao[:50]} | "
                    f"R$ {abs(tx.valor):,.2f}{marca}"
                ] = tx.id
            sel = st.selectbox(
                "Linha do extrato correspondente",
                list(opcoes.keys()),
                key="apl_tx",
            )
            tx_id = opcoes[sel]
        else:
            st.caption(
                "Nenhuma linha compativel encontrada no extrato. "
                "Importe o OFX do periodo ou registre sem vinculo e vincule depois."
            )

    desc_mov = st.text_input(
        "Descricao", max_chars=255,
        placeholder="Ex: CDB Banco X - aplicacao do saldo ocioso",
        key="apl_desc",
    )

    if st.button("Registrar Movimento", type="primary", key="apl_btn_salvar"):
        if valor_dec <= 0:
            st.error("Informe um valor maior que zero.")
        else:
            mov = MovimentoAplicacao(
                data=data_mov,
                tipo=tipo_mov,
                valor_brl=valor_dec,
                descricao=desc_mov.strip(),
                transacao_bancaria_id=tx_id,
            )
            session.add(mov)

            if tx_id is not None:
                tx = session.get(TransacaoBancaria, tx_id)
                marcar_transacao_como_aplicacao(session, tx)

            session.commit()
            msg = f"{ROTULOS_CURTOS[tipo_mov]} de {_fmt(valor_dec)} registrada."
            if tx_id is not None:
                msg += " Linha do extrato marcada como movimentacao de aplicacao."
            st.success(msg)
            st.rerun()


# ──────────────── Aba: movimentos ──────────────────────────

def _aba_movimentos(session):
    st.subheader("Movimentos Registrados")

    movimentos = (
        session.query(MovimentoAplicacao)
        .order_by(MovimentoAplicacao.data.desc(), MovimentoAplicacao.id.desc())
        .all()
    )

    if not movimentos:
        st.info("Nenhum movimento de aplicacao registrado ate o momento.")
        return

    # Totais por tipo
    cols = st.columns(4)
    for col, tipo in zip(cols, ROTULOS_TIPO.keys()):
        col.metric(ROTULOS_CURTOS[tipo], _fmt(_soma_por_tipo(session, tipo)))

    # Tabela com saldo aplicado acumulado (ordem cronologica)
    acumulado = Decimal("0.00")
    linhas_cron = []
    for m in sorted(movimentos, key=lambda x: (x.data, x.id)):
        acumulado += m.efeito_no_saldo_aplicado
        linhas_cron.append({
            "id": m.id,
            "Data": m.data.strftime("%d/%m/%Y"),
            "Tipo": ROTULOS_CURTOS[m.tipo],
            "Valor": _fmt(m.valor_brl),
            "Saldo Aplicado": _fmt(acumulado),
            "Extrato": (
                m.transacao_bancaria.data.strftime("%d/%m/%Y")
                if m.transacao_bancaria else "—"
            ),
            "Descricao": m.descricao or "—",
        })

    df = pd.DataFrame(list(reversed(linhas_cron))).drop(columns=["id"])
    st.dataframe(df, use_container_width=True, hide_index=True)

    _bloco_vincular_pendentes(session, movimentos)

    # Exclusao
    st.markdown("---")
    st.markdown("**Excluir movimento**")
    opcoes = {
        f"{m.data.strftime('%d/%m/%Y')} | {ROTULOS_CURTOS[m.tipo]} | {_fmt(m.valor_brl)}": m.id
        for m in movimentos
    }
    col_sel, col_btn = st.columns([3, 1], vertical_alignment="bottom")
    sel = col_sel.selectbox("Movimento", list(opcoes.keys()), key="apl_sel_del")
    if col_btn.button("Excluir", key="apl_btn_del"):
        mov = session.get(MovimentoAplicacao, opcoes[sel])
        tx = mov.transacao_bancaria
        if tx is not None:
            desmarcar_transacao_aplicacao(session, tx)
        session.delete(mov)
        session.commit()
        st.success(
            "Movimento excluido."
            + (" A linha do extrato voltou para a fila de conciliacao." if tx else "")
        )
        st.rerun()


def movimentos_sem_extrato(session) -> list:
    """Aplicacoes/resgates registrados que ainda nao foram ligados ao extrato.

    Acontece quando o movimento e lancado antes de importar o OFX do periodo.
    Enquanto nao forem ligados, a conferencia de caixa acusa divergencia.
    """
    return (
        session.query(MovimentoAplicacao)
        .filter(
            MovimentoAplicacao.tipo.in_(list(TIPOS_COM_EXTRATO)),
            MovimentoAplicacao.transacao_bancaria_id == None,
        )
        .order_by(MovimentoAplicacao.data)
        .all()
    )


def _bloco_vincular_pendentes(session, movimentos: list):
    """Permite ligar a linha do extrato a movimentos registrados antes do OFX."""
    pendentes = [
        m for m in movimentos
        if m.tipo in TIPOS_COM_EXTRATO and m.transacao_bancaria_id is None
    ]
    if not pendentes:
        return

    st.markdown("---")
    st.markdown("**Movimentos aguardando a linha do extrato**")
    st.info(
        f"{len(pendentes)} movimento(s) registrado(s) sem vinculo com o extrato da "
        "conta corrente. **Os saldos ja estao corretos** — o vinculo serve para a "
        "validacao contra o OFX. Depois de importar o extrato do periodo, vincule "
        "aqui em vez de classificar a linha pela Conciliacao, senao o valor conta "
        "em dobro."
    )

    opcoes_mov = {
        f"{m.data.strftime('%d/%m/%Y')} | {ROTULOS_CURTOS[m.tipo]} | {_fmt(m.valor_brl)}": m.id
        for m in pendentes
    }
    sel_mov = st.selectbox(
        "Movimento", list(opcoes_mov.keys()), key="apl_vinc_mov",
    )
    mov = session.get(MovimentoAplicacao, opcoes_mov[sel_mov])

    candidatas = transacoes_candidatas(session, mov.tipo, Decimal(str(mov.valor_brl)))
    if not candidatas:
        st.caption(
            "Nenhuma linha compativel no extrato ainda. Importe o OFX do periodo "
            "em 'Importacao OFX' e volte aqui."
        )
        return

    opcoes_tx = {
        f"{tx.data.strftime('%d/%m/%Y')} | {tx.descricao[:50]} | R$ {abs(tx.valor):,.2f}": tx.id
        for tx in candidatas
    }
    sel_tx = st.selectbox(
        "Linha do extrato", list(opcoes_tx.keys()), key="apl_vinc_tx",
    )

    if st.button("Vincular ao extrato", key="apl_btn_vincular"):
        tx = session.get(TransacaoBancaria, opcoes_tx[sel_tx])
        mov.transacao_bancaria_id = tx.id
        marcar_transacao_como_aplicacao(session, tx)
        session.commit()
        st.success("Movimento vinculado. A linha saiu da fila de conciliacao.")
        st.rerun()


# ──────────────── Aba: ajuda ───────────────────────────────

def _aba_ajuda():
    st.markdown(
        """
### Como registrar cada situacao

**1. Voce aplicou o dinheiro parado**
Registre um movimento do tipo **Aplicacao** e vincule a linha de saida do
extrato da conta corrente. O dinheiro sai do saldo em conta e entra no saldo
aplicado — o *Disponivel Total* nao muda, porque nada foi gasto.

**2. O fundo rendeu no mes**
Pegue o extrato da aplicacao e registre um movimento do tipo **Rendimento**.
Nao existe linha no OFX da conta corrente para isso. O rendimento aumenta o
saldo aplicado e aparece como receita do projeto.

**3. O banco reteve IR ou IOF**
Registre um movimento do tipo **IR / IOF**. Ele reduz o saldo aplicado e o
rendimento liquido. Nao consome teto de centro de custo — e custo da receita
financeira, nao do projeto.

**4. Voce resgatou para pagar contas**
Registre um movimento do tipo **Resgate** e vincule a linha de entrada do
extrato. O dinheiro volta para a conta corrente. Atencao: esta entrada **nao**
e uma remessa — se voce vincular a uma remessa por engano, o cambio efetivado
do projeto fica errado.

---

### Por que o rendimento nao aumenta os tetos

Os tetos de cada centro de custo sao definidos em **euros** e sao
inegociaveis. O rendimento entra como caixa adicional disponivel e fica
reportado em linha separada, sem alterar o orcamento aprovado. Se os
diretores decidirem incorporar o rendimento ao orcamento, isso passa a exigir
uma regra de rateio entre os centros de custo — avise que o sistema e ajustado.

---

### De onde vem o saldo

O saldo vem dos seus **lancamentos**, nao do extrato:

```
conta corrente = remessas recebidas - despesas pagas
                 - aplicado no fundo + resgatado do fundo
saldo aplicado = aplicacoes + rendimentos - resgates - IR/IOF
disponivel     = conta corrente + saldo aplicado
```

Ou seja: assim que voce lanca uma despesa ou registra uma aplicacao, o saldo
muda na hora. Voce nao precisa importar o OFX para o numero ficar correto —
o OFX serve para **conferir**, nao para calcular.

Uma despesa conta como paga quando esta vinculada ao extrato ou quando a data
de pagamento informada ja chegou. Despesas com pagamento futuro aparecem
separadas, como compromissos a pagar.

---

### As duas conferencias (e por que sao duas)

**Validacao com o extrato bancario** — aparece quando ha OFX importado.
Compara o saldo dos seus lancamentos com o saldo do extrato. Os dois quase
nunca serao iguais no meio do mes, e isso e normal: a diferenca deve ser
exatamente o que voce lancou e o extrato ainda nao alcancou. O sistema calcula
essa parte esperada e mostra separado o que **sobra** — que e o que realmente
merece atencao: linha importada e nunca classificada, valor lancado diferente
do que o banco debitou, ou despesa que passou no banco e nunca foi lancada.

**Conferencia do saldo aplicado** — voce digita o saldo do extrato do fundo.

Essa segunda existe porque rendimento e IR/IOF nascem dentro do fundo e nao
tem linha nenhuma no extrato bancario. Eles nunca apareceriam na primeira
validacao, entao um rendimento esquecido passaria despercebido. A unica fonte
externa capaz de provar que o rendimento foi lancado certo e o proprio extrato
da aplicacao.

Com as duas rodando, todo real do projeto tem conferencia contra um documento
do banco.
"""
    )
