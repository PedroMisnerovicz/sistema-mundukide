# MUNDUKIDE — Sistema de Controle Financeiro

## Contexto e Objetivo

Aplicacao web para controle financeiro e prestacao de contas de um projeto internacional nao reembolsavel (financiado pela Espanha). O foco principal e nao extrapolar tetos orcamentarios rigidos e garantir conciliacao bancaria precisa.

Atue como um Engenheiro de Software Senior especialista em Python, Pandas, SQLAlchemy e Streamlit.

---

## Stack Tecnologico

| Camada | Tecnologia |
|--------|-----------|
| Front-end/App | Streamlit (layout wide) |
| Manipulacao de dados | Pandas |
| Leitura de extratos | ofxparse |
| Graficos | Plotly (gauge charts, barras horizontais) |
| Geracao de PDF | fpdf2 + kaleido (export de graficos Plotly como PNG) |
| ORM | SQLAlchemy 2.0 |
| Banco de dados | PostgreSQL (Supabase nuvem) + SQLite (local) |

---

## Estrutura de Arquivos

```
3.SISTEMA_MUNDUKIDE/
  app.py                  # Ponto de entrada — navegacao por sidebar + tela de senha
  models.py               # Modelos SQLAlchemy (7 entidades)
  database.py             # Engine dual (PostgreSQL nuvem / SQLite local)
  mundukide.db            # SQLite local (NAO vai pro GitHub)
  requirements.txt        # Dependencias
  .gitignore              # Protege arquivos sensiveis
  .streamlit/
    secrets.toml           # Senha e URL do banco (NAO vai pro GitHub)
  CLAUDE.md               # Este arquivo
  modulos/
    __init__.py
    cadastros.py           # Cadastros de CC, Categorias, Remessas + Visao Geral
    lancamentos.py         # Lancamentos manuais de despesas
    importacao_ofx.py      # Upload e parse de extratos OFX
    conciliacao.py         # Conciliacao bancaria + splits
    folha_pagamento.py     # Calculo de folha com encargos BR
    fluxo_caixa.py         # Projecao de fluxo de caixa
    dashboard.py           # Dashboard gerencial + exportacao PDF
    carimbo_pdf.py         # Carimbo AVCD em documentos PDF
```

---

## Regras de Negocio (CRITICAS)

### 1. Receita e Remessas
- O valor do projeto vem em Euros e sera pago em **3 remessas**.
- Antes de qualquer remessa recebida, projecoes usam cambio fixo de **R$ 6,00/EUR**.
- Ao receber, o cambio efetivado e calculado automaticamente: `BRL_credito / EUR_remessa`.

### 2. Regra dos 80% (Gatilho de Liberacao — FIFO)
- A 2a remessa so e liberada quando **80% da 1a remessa** tiver sido gasta.
- A 3a remessa so e liberada quando **80% da 2a remessa** tiver sido gasta.
- O calculo e FIFO: gastos totais sao alocados sequencialmente nas remessas.
- O Dashboard exibe Termometro de Liberacao com gauges coloridos e alertas.

### 3. Teto Orcamentario por Centro de Custo
- Cada Centro de Custo possui um **teto em EUR inegociavel**.
- O teto BRL e calculado pelo cambio medio ponderado das remessas recebidas.
- Alertas automaticos quando gasto ultrapassa 90% ou 100% do teto.

### 4. Split de Despesas
- Uma unica transacao bancaria pode ser dividida (split) em multiplas categorias/centros de custo.
- A soma dos splits **deve obrigatoriamente** fechar com o valor da transacao original.

### 5. Conciliacao Bancaria
- O projeto usa conta bancaria exclusiva.
- Importacao OFX com deduplicacao por FITID.
- Transacao so e marcada como conciliada quando 100% alocada.

---

## Modelos de Dados (models.py)

### CentroCusto
- `id`, `codigo` (unico), `nome`, `teto_eur` (Numeric 14,2), `descricao`
- Relacao 1:N com CategoriaDespesa (cascade delete)

### CategoriaDespesa
- `id`, `nome`, `centro_custo_id` (FK), `descricao`
- Relacao N:1 com CentroCusto, 1:N com ItemDespesa
- Nao possui teto proprio — controle e no nivel do Centro de Custo

### Remessa
- `id`, `numero` (1-3, unico), `valor_eur`, `cambio_efetivado`, `valor_brl`, `data_recebimento`, `recebida` (bool), `transacao_bancaria_id` (FK nullable), `observacao`
- Vinculavel a um credito OFX via conciliacao

### TransacaoBancaria
- `id`, `fitid` (unico — OFX), `data`, `descricao`, `valor` (signed), `tipo` (DEBIT/CREDIT), `conciliada` (bool), `data_importacao`
- Relacao 1:N com ItemDespesa (cascade delete)
- Properties: `valor_splits`, `saldo_pendente`

### ItemDespesa
- `id`, `transacao_bancaria_id` (FK nullable), `categoria_despesa_id` (FK), `valor_brl`, `descricao`, `data`, `conciliado` (bool)
- Dois modos: manual (`transacao_bancaria_id = NULL`) ou vinculado a OFX
- Property: `centro_custo` (acesso direto ao CC via categoria)

---

## Funcionalidades por Modulo

### 1. Cadastros (`modulos/cadastros.py`)

**4 abas:**

| Aba | Funcionalidades |
|-----|----------------|
| Centros de Custo | CRUD completo. Exibe teto EUR e projecao BRL (cambio medio). Impede exclusao se categorias vinculadas. |
| Categorias de Despesas | CRUD completo. Vinculada a um Centro de Custo. Agrupamento visual por CC. Impede exclusao se itens vinculados. |
| Remessas | Registro das 3 remessas. Campos habilitados conforme status "recebida". Calculo automatico de valor_brl. Auto-cria os 3 registros se nao existem. |
| Visao Geral | Metricas consolidadas (total EUR, BRL, cambio medio). Tabela de execucao por CC com % gasto. Alertas de divergencia orcamentaria. |

### 2. Lancamentos (`modulos/lancamentos.py`)

**2 abas:**

| Aba | Funcionalidades |
|-----|----------------|
| Novo Lancamento | Formulario: data, categoria (dropdown agrupado por CC), valor BRL, descricao. Cria ItemDespesa manual (nao conciliado). |
| Lancamentos Registrados | Filtro por status (Todos/Pendentes/Conciliados). Tabela com metricas. Edicao e exclusao somente de lancamentos nao conciliados. |

### 3. Importacao OFX (`modulos/importacao_ofx.py`)

**2 abas:**

| Aba | Funcionalidades |
|-----|----------------|
| Upload OFX | Upload de arquivo .ofx. Parse com ofxparse. Preview das transacoes. Resumo (debitos, creditos, saldo). Botao de importacao com deduplicacao por FITID. |
| Extrato Importado | Visualizacao de todas transacoes importadas. Filtro por status. Metricas de totais e pendencias. |

### 4. Conciliacao (`modulos/conciliacao.py`)

**3 abas:**

| Aba | Funcionalidades |
|-----|----------------|
| Debitos Pendentes | Para cada debito nao conciliado: (A) Vincular lancamento manual existente, (B) Criar novo split inline. Metricas de alocacao. Botao "Finalizar" quando saldo = 0. |
| Creditos (Entradas) | Vincular credito OFX a uma Remessa. Calculo automatico do cambio efetivado. Preview antes de confirmar. Opcao de desvincular. |
| Ja Conciliadas | Visualizacao read-only de transacoes conciliadas com detalhes dos itens vinculados. Opcao de desfazer conciliacao. |

### 5. Dashboard (`modulos/dashboard.py`)

**4 secoes + exportacao PDF:**

| Secao | Funcionalidades |
|-------|----------------|
| Visao Geral (Metricas) | 5 cards: Projeto EUR, Projeto BRL, Cambio Medio, Total Gasto, Saldo em Conta. |
| Termometro de Liberacao | 3 gauges Plotly (1 por remessa). Calculo FIFO. Cores: verde (>=80%), amarelo (50-80%), vermelho (<50%). Alertas de elegibilidade para proxima remessa. |
| Execucao por Centro de Custo | Grafico de barras horizontais (Teto BRL vs Gasto BRL). Alertas de estouro (>100%) e proximidade (>90%). |
| Detalhamento por Categoria | Tabela expansivel por CC com gasto e % por categoria. |
| **Exportacao PDF** | Botao "Exportar PDF" gera relatorio completo A4 paisagem com todos os graficos (gauge + barras) renderizados como imagem via kaleido. Download como `dashboard_mundukide_YYYYMMDD.pdf`. |

---

## Navegacao (app.py)

Sidebar com radio buttons:
1. Cadastros
2. Lancamentos
3. Importacao OFX
4. Conciliacao
5. Dashboard

Cada opcao carrega o `render()` do modulo correspondente.

---

## Dependencias (requirements.txt)

```
streamlit>=1.30.0
pandas>=2.0.0
sqlalchemy>=2.0.0
psycopg2-binary>=2.9.0
ofxparse>=0.21
plotly>=5.18.0
fpdf2
kaleido
pymupdf>=1.24.0
```

---

## Banco de Dados

- **Nuvem (producao):** PostgreSQL no Supabase (projeto `mundukide`, regiao Sao Paulo)
- **Local (desenvolvimento):** SQLite (`mundukide.db`, criado automaticamente)
- **Deteccao automatica:** `database.py` le `st.secrets["database"]["url"]`. Se existir, usa PostgreSQL; senao, usa SQLite.
- **Inicializacao:** `init_db()` executado no startup — cria tabelas se nao existem
- **Foreign Keys:** Habilitadas via PRAGMA no SQLite; nativas no PostgreSQL

---

## Deploy e Infraestrutura

### Streamlit Community Cloud
- **Repositorio:** github.com/PedroMisnerovicz/sistema-mundukide (branch `main`)
- **Arquivo principal:** `app.py`
- **Secrets configurados no painel do Streamlit Cloud:**
  - `[passwords] app_password` — senha de acesso ao app
  - `[database] url` — URL de conexao ao PostgreSQL (Supabase, session pooler)
- **Autenticacao:** tela de senha no inicio do app.py usando `st.secrets`

### Supabase
- **Projeto:** mundukide
- **Regiao:** South America (Sao Paulo) — sa-east-1
- **Conexao:** usar Session Pooler (host: aws-1-sa-east-1.pooler.supabase.com:5432)

### Arquivos protegidos (.gitignore)
- `*.db` — banco de dados local
- `.streamlit/secrets.toml` — senha e URL do banco
- `migrar_dados.py` — script de migracao com credenciais
- `__pycache__/`, `.claude/`

### Como atualizar o sistema
1. Alterar arquivos no computador local
2. Testar com `streamlit run app.py`
3. Enviar: `git add .` → `git commit -m "descricao"` → `git push`
4. Streamlit Cloud atualiza automaticamente em 2-3 minutos

### Como trocar a senha do app
- **Local:** editar `.streamlit/secrets.toml`
- **Nuvem:** share.streamlit.io → app → "..." → Settings → Secrets

### Como trocar a senha do banco
- Alterar no Supabase (Project Settings → Database)
- Atualizar a URL nos Secrets do Streamlit Cloud (lembrar de codificar caracteres especiais: `#` → `%23`, `@` → `%40`)

---

## Convencoes de Desenvolvimento

- Cada modulo e independente e exporta uma funcao `render()`.
- Widgets Streamlit com chaves dinamicas (usando ID do registro) para evitar conflito de cache.
- Valores monetarios armazenados como `Decimal` para precisao.
- Lancamentos conciliados sao protegidos contra edicao/exclusao.
- O sistema auto-cria as 3 remessas no primeiro acesso ao cadastro.
