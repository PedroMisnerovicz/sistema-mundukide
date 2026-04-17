"""
Modelos de dados — Sistema Mundukide
=====================================

Diagrama de Relacionamentos:

  CentroCusto (1) ── (N) CategoriaDespesa (1) ── (N) ItemDespesa (N) ── (1) TransacaoBancaria
     ↑ teto EUR              ↑ organização                ↑ split        (importado do OFX)
                              ↑ teto BRL (opcional)
                              ↑ (1) ── (N) LancamentoRecorrente

  Remessa (independente, controla recebimentos)
  Tecnico (folha de pagamento, vinculável a LancamentoRecorrente)

Regras-chave modeladas:
  - CentroCusto 1:N CategoriaDespesa → hierarquia de classificação.
  - Teto de gastos controlado no nível do Centro de Custo E opcionalmente por Categoria.
  - CategoriaDespesa serve para detalhar/organizar os lançamentos.
  - TransacaoBancaria 1:N ItemDespesa → permite SPLIT de uma transação.
  - ItemDespesa aponta para CategoriaDespesa (que herda o Centro de Custo).
  - Remessa rastreia câmbio efetivado e valor em EUR/BRL.
  - Tecnico registra os 4 técnicos do projeto para cálculo de folha.
  - LancamentoRecorrente projeta despesas futuras no fluxo de caixa.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Boolean,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


# ─────────────────────────── Base ───────────────────────────

class Base(DeclarativeBase):
    pass


# ─────────────────────── Centro de Custo ────────────────────

class CentroCusto(Base):
    """
    Categoria orçamentária com teto fixo em EUR.
    Ex: Alimentação, Transporte, Hospedagem, etc.
    """
    __tablename__ = "centros_custo"

    id = Column(Integer, primary_key=True, autoincrement=True)
    codigo = Column(String(20), unique=True, nullable=False)
    nome = Column(String(120), nullable=False)
    teto_eur = Column(
        Numeric(14, 2),
        nullable=False,
        comment="Teto orçamentário em Euros — inegociável",
    )
    descricao = Column(Text, default="")

    # Relacionamentos
    categorias = relationship(
        "CategoriaDespesa", back_populates="centro_custo",
        cascade="all, delete-orphan", lazy="select",
    )

    def __repr__(self):
        return f"<CentroCusto {self.codigo} – {self.nome}>"


# ─────────────────── Categoria de Despesa ───────────────────

class CategoriaDespesa(Base):
    """
    Subcategoria vinculada a um Centro de Custo.
    Serve para organizar o detalhamento dos lançamentos e splits.
    O teto de gastos é controlado no nível do Centro de Custo, não aqui.
    Ex: Centro "Pessoal" → Categorias: Salários, Encargos, Benefícios.
    """
    __tablename__ = "categorias_despesa"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nome = Column(String(120), nullable=False)
    centro_custo_id = Column(
        Integer,
        ForeignKey("centros_custo.id", ondelete="CASCADE"),
        nullable=False,
    )
    descricao = Column(Text, default="")
    teto_eur = Column(
        Numeric(14, 2),
        nullable=True,
        comment="Teto de gastos em EUR — valor canonico; BRL calculado dinamicamente",
    )
    teto_brl = Column(
        Numeric(14, 2),
        nullable=True,
        comment="Legado — nao usar diretamente; calcular teto_eur * cambio_medio",
    )
    tipo_teto = Column(
        String(10),
        nullable=True,
        comment="MENSAL ou GLOBAL — tipo do teto de gastos",
    )

    # Relacionamentos
    centro_custo = relationship("CentroCusto", back_populates="categorias")
    itens_despesa = relationship(
        "ItemDespesa", back_populates="categoria_despesa", lazy="select",
    )
    lancamentos_recorrentes = relationship(
        "LancamentoRecorrente", back_populates="categoria_despesa", lazy="select",
    )

    def __repr__(self):
        return f"<CategoriaDespesa {self.nome} (CC:{self.centro_custo_id})>"


# ──────────────────────────── Remessa ───────────────────────

class Remessa(Base):
    """
    Cada uma das 3 parcelas recebidas do financiador (em EUR).
    Ao registrar, informa-se o câmbio efetivado na data da conversão.

    Campos calculados (na aplicação):
      valor_brl = valor_eur * cambio_efetivado
      percentual_executado = total gasto atribuído a esta remessa / valor_brl
    """
    __tablename__ = "remessas"

    id = Column(Integer, primary_key=True, autoincrement=True)
    numero = Column(
        Integer,
        unique=True,
        nullable=False,
        comment="1, 2 ou 3",
    )
    valor_eur = Column(
        Numeric(14, 2),
        nullable=False,
        comment="Valor recebido em Euros",
    )
    cambio_efetivado = Column(
        Numeric(10, 4),
        nullable=True,
        comment="Taxa EUR→BRL no momento da conversão",
    )
    valor_brl = Column(
        Numeric(14, 2),
        nullable=True,
        comment="valor_eur * cambio_efetivado (preenchido ao registrar câmbio)",
    )
    data_recebimento = Column(Date, nullable=True)
    recebida = Column(
        Boolean,
        default=False,
        comment="True quando o dinheiro efetivamente entrou na conta",
    )
    transacao_bancaria_id = Column(
        Integer,
        ForeignKey("transacoes_bancarias.id", ondelete="SET NULL"),
        nullable=True,
        comment="Credito do OFX vinculado a esta remessa",
    )
    observacao = Column(Text, default="")

    # Relacionamento
    transacao_bancaria = relationship("TransacaoBancaria", foreign_keys=[transacao_bancaria_id])

    __table_args__ = (
        CheckConstraint("numero BETWEEN 1 AND 3", name="ck_remessa_numero"),
    )

    def __repr__(self):
        status = "recebida" if self.recebida else "pendente"
        return f"<Remessa {self.numero} – €{self.valor_eur} ({status})>"


# ─────────────────── Transação Bancária (OFX) ──────────────

class TransacaoBancaria(Base):
    """
    Linha importada do extrato OFX.
    Cada transação pode ser dividida (split) em N itens de despesa.

    Regra de integridade (aplicação):
      SUM(itens_despesa.valor_brl) == ABS(self.valor)
      para transações do tipo DEBIT que estejam conciliadas.
    """
    __tablename__ = "transacoes_bancarias"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fitid = Column(
        String(64),
        unique=True,
        nullable=False,
        comment="ID único da transação no OFX (evita duplicidade)",
    )
    data = Column(Date, nullable=False)
    descricao = Column(String(255), nullable=False)
    valor = Column(
        Numeric(14, 2),
        nullable=False,
        comment="Negativo = saída (débito); Positivo = entrada (crédito)",
    )
    tipo = Column(
        String(10),
        nullable=False,
        comment="DEBIT ou CREDIT",
    )
    conciliada = Column(
        Boolean,
        default=False,
        comment="True quando todos os splits foram atribuídos",
    )
    data_importacao = Column(
        DateTime,
        default=func.now(),
        comment="Timestamp da importação do OFX",
    )

    # Relacionamento 1:N  →  permite SPLIT
    itens_despesa = relationship(
        "ItemDespesa",
        back_populates="transacao_bancaria",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def __repr__(self):
        return f"<Transacao {self.fitid} | {self.data} | R${self.valor}>"

    @property
    def valor_splits(self) -> Decimal:
        """Soma dos splits já atribuídos."""
        return sum(
            (item.valor_brl for item in self.itens_despesa), Decimal("0.00")
        )

    @property
    def saldo_pendente(self) -> Decimal:
        """Quanto ainda falta ser alocado em splits."""
        return abs(self.valor) - self.valor_splits


# ──────────────────── Item de Despesa (Split) ───────────────

class ItemDespesa(Base):
    """
    Linha de alocação de um gasto a uma Categoria de Despesa.
    Pode existir de duas formas:

    1. Lançamento manual: transacao_bancaria_id = NULL, conciliado = False
       → Criado pelo usuário no dia a dia, antes de importar o OFX.
    2. Vinculado ao OFX: transacao_bancaria_id preenchido
       → Criado via split na conciliação, ou manual que foi "casado" com OFX.

    Várias linhas podem pertencer à mesma TransacaoBancaria (split).
    """
    __tablename__ = "itens_despesa"

    id = Column(Integer, primary_key=True, autoincrement=True)
    transacao_bancaria_id = Column(
        Integer,
        ForeignKey("transacoes_bancarias.id", ondelete="SET NULL"),
        nullable=True,
        comment="NULL = lancamento manual ainda nao vinculado ao OFX",
    )
    categoria_despesa_id = Column(
        Integer,
        ForeignKey("categorias_despesa.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reembolso_id = Column(
        Integer,
        ForeignKey("reembolsos.id", ondelete="CASCADE"),
        nullable=True,
        comment="Preenchido quando a despesa faz parte de um reembolso",
    )
    lancamento_recorrente_id = Column(
        Integer,
        ForeignKey("lancamentos_recorrentes.id", ondelete="SET NULL"),
        nullable=True,
        comment="Preenchido quando a despesa realiza uma projecao recorrente (ex: folha)",
    )
    valor_brl = Column(
        Numeric(14, 2),
        nullable=False,
        comment="Parcela em BRL alocada a esta categoria/centro de custo",
    )
    descricao = Column(String(255), default="")
    fornecedor_cliente = Column(
        String(200),
        nullable=True,
        comment="Nome do fornecedor ou cliente envolvido",
    )
    data = Column(
        Date,
        nullable=False,
        comment="Data da despesa (legado — manter sincronizada com data_pagamento)",
    )
    data_emissao = Column(
        Date,
        nullable=True,
        comment="Data da nota/documento de origem",
    )
    data_pagamento = Column(
        Date,
        nullable=True,
        comment="Data efetiva de saida do banco — usada na conciliacao",
    )
    conciliado = Column(
        Boolean,
        default=False,
        comment="True quando vinculado a uma transacao bancaria do OFX",
    )

    # Relacionamentos
    transacao_bancaria = relationship(
        "TransacaoBancaria", back_populates="itens_despesa"
    )
    categoria_despesa = relationship(
        "CategoriaDespesa", back_populates="itens_despesa"
    )
    reembolso = relationship("Reembolso", back_populates="itens_despesa")
    lancamento_recorrente = relationship(
        "LancamentoRecorrente", back_populates="itens_despesa_realizados",
    )

    @property
    def centro_custo(self):
        """Acesso direto ao Centro de Custo via categoria."""
        return self.categoria_despesa.centro_custo if self.categoria_despesa else None

    def __repr__(self):
        return (
            f"<ItemDespesa R${self.valor_brl} → "
            f"Cat:{self.categoria_despesa_id} | Tx:{self.transacao_bancaria_id}>"
        )


# ──────────────────────── Técnico (Folha) ──────────────────

class Tecnico(Base):
    """
    Técnico do projeto para cálculo de folha de pagamento.
    O projeto possui 4 técnicos cujos encargos devem ser isolados.
    """
    __tablename__ = "tecnicos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nome = Column(String(120), nullable=False)
    custo_maximo = Column(
        Numeric(14, 2),
        nullable=True,
        comment="Custo total maximo mensal (salario + encargos + provisoes)",
    )
    salario_bruto = Column(
        Numeric(14, 2),
        nullable=False,
        comment="Salário bruto mensal em BRL — calculado a partir de custo_maximo",
    )
    data_admissao = Column(Date, nullable=False)
    ativo = Column(Boolean, default=True)

    # Relacionamentos
    lancamentos_recorrentes = relationship(
        "LancamentoRecorrente", back_populates="tecnico", lazy="select",
    )

    def __repr__(self):
        return f"<Tecnico {self.nome} – R${self.salario_bruto}>"


# ─────────────── Lançamento Recorrente ─────────────────────

class LancamentoRecorrente(Base):
    """
    Template de lançamento recorrente para projeção de despesas futuras.
    Pode ser vinculado a um técnico (folha de pagamento) ou independente.
    Frequências: MENSAL, TRIMESTRAL, ANUAL.
    """
    __tablename__ = "lancamentos_recorrentes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    categoria_despesa_id = Column(
        Integer,
        ForeignKey("categorias_despesa.id", ondelete="RESTRICT"),
        nullable=False,
    )
    valor_brl = Column(
        Numeric(14, 2),
        nullable=False,
        comment="Valor em BRL de cada ocorrência",
    )
    descricao = Column(String(255), default="")
    frequencia = Column(
        String(20),
        nullable=False,
        comment="MENSAL, TRIMESTRAL ou ANUAL",
    )
    data_inicio = Column(Date, nullable=False)
    data_fim = Column(Date, nullable=False)
    dia_pagamento_previsto = Column(
        Integer,
        nullable=True,
        comment="Dia do mes (1-31) em que a ocorrencia e esperada — usado no match de folha",
    )
    tecnico_id = Column(
        Integer,
        ForeignKey("tecnicos.id", ondelete="SET NULL"),
        nullable=True,
        comment="Vinculo opcional com técnico da folha de pagamento",
    )
    ativo = Column(Boolean, default=True)

    # Relacionamentos
    categoria_despesa = relationship(
        "CategoriaDespesa", back_populates="lancamentos_recorrentes",
    )
    tecnico = relationship("Tecnico", back_populates="lancamentos_recorrentes")
    itens_despesa_realizados = relationship(
        "ItemDespesa", back_populates="lancamento_recorrente", lazy="select",
    )

    def realizado_no_mes(self, ano: int, mes: int) -> bool:
        """True se existe ItemDespesa vinculado cuja data_pagamento cai no mes informado."""
        for item in self.itens_despesa_realizados:
            ref = item.data_pagamento or item.data
            if ref and ref.year == ano and ref.month == mes:
                return True
        return False

    def data_projetada_no_mes(self, ano: int, mes: int):
        """Retorna a data esperada da ocorrencia no mes informado.
        Usa dia_pagamento_previsto ou o ultimo dia do mes como fallback.
        Ajusta dias invalidos (ex: 31/fev -> 28 ou 29)."""
        from calendar import monthrange
        _, ultimo_dia = monthrange(ano, mes)
        dia = self.dia_pagamento_previsto or ultimo_dia
        dia = min(dia, ultimo_dia)
        return date(ano, mes, dia)

    def __repr__(self):
        return f"<LancRecorrente {self.descricao} – R${self.valor_brl} ({self.frequencia})>"


# ─────────────────────────── Reembolso ──────────────────────

class Reembolso(Base):
    """
    Pagamento unico a um beneficiario que consolida varias despesas
    de categorias/centros de custo diferentes.

    Exemplo: reembolso de viagem do Joao no valor de R$ 1.500,00 cobrindo
    alimentacao (R$ 600), transporte (R$ 400) e hospedagem (R$ 500).

    Regras:
      - No banco saira 1 debito; internamente e distribuido entre N ItemDespesa filhos.
      - valor_total_brl = SUM(itens_despesa.valor_brl).
      - Conciliacao 1:1 com uma TransacaoBancaria OFX.
      - Ao conciliar, todos os ItemDespesa filhos sao marcados conciliados em cascata.
    """
    __tablename__ = "reembolsos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    beneficiario = Column(String(200), nullable=False)
    data_pagamento = Column(Date, nullable=False)
    valor_total_brl = Column(Numeric(14, 2), nullable=False)
    transacao_bancaria_id = Column(
        Integer,
        ForeignKey("transacoes_bancarias.id", ondelete="SET NULL"),
        nullable=True,
        comment="Debito OFX vinculado ao reembolso (quando conciliado)",
    )
    conciliado = Column(Boolean, default=False, nullable=False)
    observacao = Column(Text, default="")
    data_criacao = Column(DateTime, default=func.now())

    # Relacionamentos
    transacao_bancaria = relationship(
        "TransacaoBancaria", foreign_keys=[transacao_bancaria_id],
    )
    itens_despesa = relationship(
        "ItemDespesa",
        back_populates="reembolso",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def __repr__(self):
        status = "conciliado" if self.conciliado else "pendente"
        return f"<Reembolso {self.beneficiario} – R${self.valor_total_brl} ({status})>"
