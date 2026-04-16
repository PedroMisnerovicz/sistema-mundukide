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
    valor_brl = Column(
        Numeric(14, 2),
        nullable=False,
        comment="Parcela em BRL alocada a esta categoria/centro de custo",
    )
    descricao = Column(String(255), default="")
    data = Column(
        Date,
        nullable=False,
        comment="Data da despesa (herdada da transação ou informada)",
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

    def __repr__(self):
        return f"<LancRecorrente {self.descricao} – R${self.valor_brl} ({self.frequencia})>"
