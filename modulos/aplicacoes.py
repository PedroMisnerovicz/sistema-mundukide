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


def saldo_conta_corrente(session) -> Decimal:
    """Saldo real da conta corrente = soma de todas as linhas do extrato OFX."""
    resultado = (
        session.query(sqlfunc.coalesce(sqlfunc.sum(TransacaoBancaria.valor), 0))
        .scalar()
    )
    return _q2(resultado)


def consolidado(session) -> dict:
    """Fecha o caixa do projeto e devolve a checagem de divergencia.

    Identidade esperada:
        conta corrente + aplicado  ==  remessas recebidas + rendimento liquido
                                       - gastos que ja passaram no banco
    """
    cc = saldo_conta_corrente(session)
    aplicado = saldo_aplicado(session)
    disponivel = _q2(cc + aplicado)

    remessas_recebidas = Decimal(str(
        session.query(sqlfunc.coalesce(sqlfunc.sum(Remessa.valor_brl), 0))
        .filter(Remessa.recebida == True)
        .scalar()
    ))
    rend = rendimento_liquido(session)

    # Apenas despesas efetivamente pagas pelo banco entram na identidade.
    # Lancamentos manuais ainda nao conciliados sao "a pagar" — nao sairam da conta.
    gastos_no_banco = Decimal(str(
        session.query(sqlfunc.coalesce(sqlfunc.sum(ItemDespesa.valor_brl), 0))
        .filter(ItemDespesa.transacao_bancaria_id != None)
        .scalar()
    ))

    esperado = _q2(remessas_recebidas + rend - gastos_no_banco)

    return {
        "saldo_conta_corrente": cc,
        "saldo_aplicado": aplicado,
        "disponivel_total": disponivel,
        "remessas_recebidas": remessas_recebidas,
        "rendimento_liquido": rend,
        "gastos_no_banco": gastos_no_banco,
        "esperado": esperado,
        "divergencia": _q2(disponivel - esperado),
    }


def analise_divergencia(session) -> dict:
    """Separa a divergencia de caixa entre a parte esperada e a que exige acao.

    Parte esperada: movimentos de aplicacao/resgate ja registrados cuja linha
    ainda nao existe no extrato importado (tipico de movimentacao feita hoje,
    cujo OFX so sai depois). Enquanto o vinculo nao existe, o mesmo dinheiro e
    contado na conta corrente e no fundo — a divergencia e so um atraso de
    informacao, nao um erro de lancamento.
    """
    div = consolidado(session)["divergencia"]
    pendentes = movimentos_sem_extrato(session)

    # So explica a divergencia o movimento que ainda NAO tem linha no extrato.
    # Se a linha ja foi importada (mesmo sem vinculo), a conta corrente ja
    # descontou o valor e o movimento nao e mais causa de divergencia — e apenas
    # uma pendencia de vinculo. Consumo greedy para nao usar a mesma linha duas vezes.
    usados = set()
    sem_extrato = []
    for m in pendentes:
        candidatas = [
            tx for tx in transacoes_candidatas(session, m.tipo, Decimal(str(m.valor_brl)))
            if tx.id not in usados and abs(tx.valor) == m.valor_brl
        ]
        if candidatas:
            usados.add(candidatas[0].id)
        else:
            sem_extrato.append(m)

    efeito = _q2(sum(
        (m.efeito_no_saldo_aplicado for m in sem_extrato), Decimal("0.00")
    ))
    residual = _q2(div - efeito)

    return {
        "divergencia": div,
        "pendentes": sem_extrato,
        "aguardando_vinculo": pendentes,
        "efeito_pendentes": efeito,
        "residual": residual,
        "tem_explicacao": bool(sem_extrato) and abs(efeito) >= Decimal("0.01"),
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
        "Disponivel Total = conta corrente + aplicado. "
        "Rendimento Liquido = rendimentos do fundo menos IR/IOF (receita do projeto, "
        "que nao altera os tetos em EUR)."
    )

    analise = analise_divergencia(session)

    # Aviso direto no painel: a conta corrente ainda nao refletiu o movimento
    if analise["tem_explicacao"]:
        st.caption(
            f"⏳ {len(analise['pendentes'])} movimento(s) ainda sem linha no extrato. "
            "Ate importar o OFX do periodo, o valor da **Conta Corrente** acima nao "
            "desconta esse dinheiro — por isso o Disponivel Total aparece maior que o real."
        )
    elif analise["aguardando_vinculo"]:
        st.caption(
            f"🔗 {len(analise['aguardando_vinculo'])} movimento(s) com a linha ja no "
            "extrato, faltando apenas vincular. Os saldos acima ja estao corretos — "
            "vincule em *Movimentos Registrados* para fechar a conciliacao."
        )

    # Checagem de integridade
    div = c["divergencia"]
    with st.expander("Conferencia de caixa (checagem automatica)", expanded=abs(div) >= Decimal("0.01")):
        st.markdown(
            f"""
| Conta | Valor |
|---|---|
| Remessas recebidas | {_fmt(c['remessas_recebidas'])} |
| (+) Rendimento liquido da aplicacao | {_fmt(c['rendimento_liquido'])} |
| (−) Despesas ja pagas pelo banco | {_fmt(c['gastos_no_banco'])} |
| **= Caixa esperado** | **{_fmt(c['esperado'])}** |
| Caixa real (conta corrente + aplicado) | {_fmt(c['disponivel_total'])} |
| **Divergencia** | **{_fmt(div)}** |
"""
        )
        if abs(div) < Decimal("0.01"):
            st.success(
                "Caixa conferido: o dinheiro que existe bate exatamente com o que "
                "entrou menos o que saiu."
            )
        else:
            pendentes = analise["pendentes"]
            efeito = analise["efeito_pendentes"]
            residual = analise["residual"]

            if analise["tem_explicacao"]:
                detalhe = "\n".join(
                    f"- {m.data.strftime('%d/%m/%Y')} — {ROTULOS_CURTOS[m.tipo]} de "
                    f"{_fmt(m.valor_brl)}"
                    for m in pendentes
                )
                st.info(
                    f"**{_fmt(abs(efeito))} da divergencia sao esperados** e nao indicam "
                    f"erro nos seus lancamentos:\n\n{detalhe}\n\n"
                    "Esse(s) movimento(s) ja foram registrados aqui, mas a linha "
                    "correspondente ainda nao existe no extrato importado — normal "
                    "quando a movimentacao foi feita hoje, porque o OFX do dia so fica "
                    "disponivel depois. Enquanto isso, o mesmo dinheiro e contado na "
                    "conta corrente e no fundo ao mesmo tempo.\n\n"
                    "**O que fazer:** quando o OFX do periodo estiver disponivel, "
                    "importe em *Importacao OFX* e vincule em *Movimentos Registrados*. "
                    "A divergencia zera sozinha."
                )

            if abs(residual) >= Decimal("0.01"):
                titulo = (
                    f"Sobram {_fmt(residual)} sem explicacao."
                    if analise["tem_explicacao"]
                    else f"Divergencia de {_fmt(div)}."
                )
                st.warning(
                    f"{titulo} Causas comuns, em ordem de probabilidade:\n\n"
                    "1. Alguma linha do extrato ainda nao foi classificada na Conciliacao.\n"
                    "2. Uma aplicacao ou resgate do extrato ainda nao foi registrado aqui.\n"
                    "3. Um credito recebido ainda nao foi vinculado a uma remessa.\n"
                    "4. A conta tinha saldo antes do primeiro OFX importado."
                )

        st.caption(
            "Esta conferencia valida tudo que passa pela conta corrente. Ela **nao** "
            "consegue validar rendimento e IR/IOF, porque esses valores nascem dentro "
            "do fundo e nao tem linha no extrato bancario — use a conferencia do saldo "
            "aplicado abaixo para isso."
        )

    _conferencia_fundo(session, c["saldo_aplicado"])


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
    st.warning(
        f"{len(pendentes)} movimento(s) registrado(s) sem vinculo com o extrato da "
        "conta corrente. Enquanto o vinculo nao existir, a conferencia de caixa vai "
        "acusar divergencia. Se o OFX do periodo ja foi importado, vincule aqui — "
        "assim voce evita lancar o mesmo movimento duas vezes."
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

### As duas conferencias (e por que sao duas)

**Conferencia de caixa** — roda sozinha toda vez que voce abre a tela:

```
remessas recebidas + rendimento liquido - despesas pagas pelo banco
                    deve ser igual a
        saldo em conta corrente + saldo aplicado
```

Ela valida tudo que passa pela conta corrente: linha do extrato nao
classificada, resgate nao registrado, credito sem remessa vinculada.

**Conferencia do saldo aplicado** — voce digita o saldo do extrato do fundo.

Essa segunda conferencia existe porque rendimento e IR/IOF nascem dentro do
fundo e nao tem linha nenhuma no extrato bancario. Eles entram nos dois lados
da conta de caixa ao mesmo tempo, entao a primeira conferencia nunca acusaria
um rendimento esquecido. A unica fonte externa capaz de provar que o
rendimento foi lancado certo e o proprio extrato da aplicacao — por isso a
comparacao direta com o saldo que o banco informa.

Com as duas rodando, todo real do projeto tem conferencia contra um documento
do banco.
"""
    )
