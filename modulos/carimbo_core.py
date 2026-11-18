"""
Carimbo AVCD/Mundukide — logica pura (sem Streamlit)
====================================================
Usado pela tela "Carimbo de Documentos" do sistema e pelo programa
avulso (carimbo_exe/carimbo_app.py), para que os dois apliquem
exatamente o mesmo carimbo.

Carimbo:
  Linha 1 — Financiado pela AGENCIA VASCA DE COOPERACIÓN AL DESARROLLO (AVCD)
  Linha 2 — PRO-2025K2/0002
  Linha 3 — BRASIL

Tamanho: ~8,8 cm x 2,6 cm | Posicao: centro da pagina (com leve jitter)
Biblioteca: PyMuPDF (fitz)
"""

import io
import random

import fitz  # PyMuPDF

# ─── Constantes do carimbo ─────────────────────────────────────────────────

_CM       = 28.3465          # pontos por centimetro (1 inch = 72 pt; 1 cm = 72/2.54)
_LARG     = 8.8 * _CM        # largura  ~249.4 pt
_ALT      = 2.6 * _CM        # altura   ~73.7 pt
_MARG     = 1.0 * _CM        # margem   ~28.3 pt (usada apenas como referencia)
_JITTER_CENTRO = 8.0          # variacao maior no centro (~3 mm) para parecer manual

_LINHA1   = "Financiado pela AGENCIA VASCA DE COOPERACIÓN AL DESARROLLO (AVCD)"
_LINHA2   = "PRO-2025K2/0002"
_LINHA3   = "BRASIL"

_FONTE    = "Helvetica-Bold"  # nome correto no PyMuPDF >= 1.24
_FS1      = 7.0               # fontsize linha 1 (texto longo — quebra em 2 linhas)
_FS2      = 9.0               # fontsize linhas 2 e 3

# Layout interno do carimbo (em pontos)
_PAD_H    = 6.0               # margem horizontal interna
_H_L1     = 24.0              # altura alocada para linha 1 (2 linhas × ~12pt)
_H_L2     = 16.0              # altura alocada para linha 2
_H_L3     = 16.0              # altura alocada para linha 3
_GAP      = 3.0               # espacamento entre blocos de linha

# Aparencia de carimbo manual (simula carimbo de borracha aplicado a mao)
_COR_TINTA      = (0.0, 0.15, 0.50)   # azul escuro saturado — tinta de carimbo
_BORDA_W        = 1.7                 # largura media da borda
_ROT_MAX        = 2.5                 # rotacao maxima em graus (±)

# Parametros de "imperfeicao" — fazem o carimbo parecer borracha aplicada a mao
_BORDA_SEG_PT      = 2.5              # comprimento medio de cada segmento da borda (pt)
_BORDA_FALHA_PROB  = 0.08             # chance de cada segmento "nao pegar" (falha de tinta)
_BORDA_OP_MIN      = 0.65             # opacidade minima de um segmento
_BORDA_OP_MAX      = 1.00             # opacidade maxima de um segmento
_BORDA_W_MIN       = 1.1              # largura minima por segmento
_BORDA_W_MAX       = 2.6              # largura maxima por segmento (acumulo de tinta)
_BORDA_SPOT_PROB   = 0.06             # chance de "engrossamento" pontual na borda

# Sombra/sangramento da tinta absorvida pelo papel
_BLEED_BORDA_W     = 4.5              # largura da sombra borrada da borda
_BLEED_BORDA_OP    = 0.16             # opacidade da sombra (bem fraca)
_BLEED_TEXTO_OFFSET = 0.4             # offset (pt) da sombra do texto
_BLEED_TEXTO_OP    = 0.28             # opacidade da sombra do texto

# Respingos
_RESPINGOS_MIN     = 4
_RESPINGOS_MAX     = 12

# Texto principal — bem marcado, com leve variacao por linha
_TEXTO_OP_MIN      = 0.88
_TEXTO_OP_MAX      = 1.00


# ─── Helpers de aparencia manual ────────────────────────────────────────────

def _draw_borda_irregular(page, rect, cor, morph):
    """Desenha o retangulo da borda como dezenas de segmentos curtos com
    falhas, variando largura e opacidade — simula a tinta da borracha que
    nao toca o papel uniformemente. Tambem aplica sombra de sangramento
    da tinta absorvida pelo papel."""
    lados = [
        ((rect.x0, rect.y0), (rect.x1, rect.y0)),  # top
        ((rect.x1, rect.y0), (rect.x1, rect.y1)),  # right
        ((rect.x1, rect.y1), (rect.x0, rect.y1)),  # bottom
        ((rect.x0, rect.y1), (rect.x0, rect.y0)),  # left
    ]

    # Primeiro passe: sombra borrada por baixo (simula tinta sangrando no papel)
    for (xs, ys), (xe, ye) in lados:
        page.draw_line(
            fitz.Point(xs, ys),
            fitz.Point(xe, ye),
            color=cor,
            width=_BLEED_BORDA_W,
            stroke_opacity=_BLEED_BORDA_OP,
            morph=morph,
        )

    # Segundo passe: a borda em si, segmentada e irregular
    for (xs, ys), (xe, ye) in lados:
        comprimento = ((xe - xs) ** 2 + (ye - ys) ** 2) ** 0.5
        n_segs = max(8, int(comprimento / _BORDA_SEG_PT))

        for i in range(n_segs):
            # Falha de tinta: nao desenha esse segmento
            if random.random() < _BORDA_FALHA_PROB:
                continue

            t1 = i / n_segs
            t2 = (i + 1) / n_segs
            x1 = xs + (xe - xs) * t1
            y1 = ys + (ye - ys) * t1
            x2 = xs + (xe - xs) * t2
            y2 = ys + (ye - ys) * t2

            # Acumulo pontual de tinta: alguns segmentos saem mais grossos e opacos
            if random.random() < _BORDA_SPOT_PROB:
                op = 1.0
                w = random.uniform(_BORDA_W_MAX, _BORDA_W_MAX + 0.8)
            else:
                op = random.uniform(_BORDA_OP_MIN, _BORDA_OP_MAX)
                w = random.uniform(_BORDA_W_MIN, _BORDA_W_MAX)

            page.draw_line(
                fitz.Point(x1, y1),
                fitz.Point(x2, y2),
                color=cor,
                width=w,
                stroke_opacity=op,
                morph=morph,
            )


def _draw_respingos(page, rect, cor, morph):
    """Adiciona pequenos respingos/manchas de tinta dentro e ao redor do
    carimbo — simula vazamento da borracha."""
    n = random.randint(_RESPINGOS_MIN, _RESPINGOS_MAX)
    margem_externa = 5.0

    for _ in range(n):
        # Respingos predominantemente fora ou perto da borda
        if random.random() < 0.7:
            # Perto de algum dos lados
            lado = random.choice(["top", "bottom", "left", "right"])
            if lado == "top":
                x = random.uniform(rect.x0 - margem_externa, rect.x1 + margem_externa)
                y = random.uniform(rect.y0 - margem_externa, rect.y0 + 6)
            elif lado == "bottom":
                x = random.uniform(rect.x0 - margem_externa, rect.x1 + margem_externa)
                y = random.uniform(rect.y1 - 6, rect.y1 + margem_externa)
            elif lado == "left":
                x = random.uniform(rect.x0 - margem_externa, rect.x0 + 6)
                y = random.uniform(rect.y0 - margem_externa, rect.y1 + margem_externa)
            else:
                x = random.uniform(rect.x1 - 6, rect.x1 + margem_externa)
                y = random.uniform(rect.y0 - margem_externa, rect.y1 + margem_externa)
        else:
            # Disperso pelo carimbo
            x = random.uniform(rect.x0 - margem_externa, rect.x1 + margem_externa)
            y = random.uniform(rect.y0 - margem_externa, rect.y1 + margem_externa)

        raio = random.uniform(0.25, 1.1)
        op = random.uniform(0.30, 0.75)

        page.draw_circle(
            fitz.Point(x, y),
            radius=raio,
            color=cor,
            fill=cor,
            stroke_opacity=op,
            fill_opacity=op,
            morph=morph,
        )


# ─── Logica de carimbagem ──────────────────────────────────────────────────

def _carimbar_pagina(page: fitz.Page) -> None:
    """Aplica o carimbo com aparencia manual (in-place).

    Cada pagina recebe uma leve rotacao e variacao de posicao
    aleatorias para simular um carimbo de borracha aplicado a mao.
    """
    pw = page.rect.width
    ph = page.rect.height

    # Variacao de posicao por pagina (~3 mm por eixo) para parecer carimbo manual
    jx = random.uniform(-_JITTER_CENTRO, _JITTER_CENTRO)
    jy = random.uniform(-_JITTER_CENTRO, _JITTER_CENTRO)

    # Posicao base: centralizado na pagina
    x0 = (pw - _LARG) / 2 + jx
    y0 = (ph - _ALT) / 2 + jy
    x1 = x0 + _LARG
    y1 = y0 + _ALT

    caixa = fitz.Rect(x0, y0, x1, y1)

    # Rotacao aleatoria em torno do centro do carimbo
    angulo = random.uniform(-_ROT_MAX, _ROT_MAX)
    centro = fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)
    morph = (centro, fitz.Matrix(1, 1).prerotate(angulo))

    # Borda irregular com falhas de tinta
    _draw_borda_irregular(page, caixa, _COR_TINTA, morph)

    # Pequenos respingos de tinta ao redor
    _draw_respingos(page, caixa, _COR_TINTA, morph)

    # Centraliza verticalmente o grupo das 3 linhas dentro da caixa
    conteudo_h = _H_L1 + _GAP + _H_L2 + _GAP + _H_L3
    pad_v = (_ALT - conteudo_h) / 2

    tx0 = x0 + _PAD_H
    tx1 = x1 - _PAD_H
    sy  = y0 + pad_v

    r1 = fitz.Rect(tx0, sy, tx1, sy + _H_L1);  sy += _H_L1 + _GAP
    r2 = fitz.Rect(tx0, sy, tx1, sy + _H_L2);  sy += _H_L2 + _GAP
    r3 = fitz.Rect(tx0, sy, tx1, sy + _H_L3)

    # Cada linha de texto ganha uma opacidade levemente diferente —
    # algumas letras "saem" mais claras, como num carimbo de borracha.
    op1 = random.uniform(_TEXTO_OP_MIN, _TEXTO_OP_MAX)
    op2 = random.uniform(_TEXTO_OP_MIN, _TEXTO_OP_MAX)
    op3 = random.uniform(_TEXTO_OP_MIN, _TEXTO_OP_MAX)

    # Sombra de sangramento da tinta: cada linha e desenhada antes com leve
    # offset e opacidade baixa (simula tinta absorvida pelo papel ao redor
    # das letras). Em seguida, o texto principal por cima.
    def _texto_com_sangramento(rect_, texto, fontsize, op):
        offsets = [(_BLEED_TEXTO_OFFSET, 0), (0, _BLEED_TEXTO_OFFSET),
                   (-_BLEED_TEXTO_OFFSET * 0.5, _BLEED_TEXTO_OFFSET * 0.5)]
        for dx, dy in offsets:
            sombra = fitz.Rect(
                rect_.x0 + dx, rect_.y0 + dy,
                rect_.x1 + dx, rect_.y1 + dy,
            )
            page.insert_textbox(
                sombra, texto, fontname=_FONTE, fontsize=fontsize,
                color=_COR_TINTA, align=1, morph=morph,
                fill_opacity=_BLEED_TEXTO_OP,
            )
        # Texto principal, marcado, por cima
        page.insert_textbox(
            rect_, texto, fontname=_FONTE, fontsize=fontsize,
            color=_COR_TINTA, align=1, morph=morph,
            fill_opacity=op,
        )

    _texto_com_sangramento(r1, _LINHA1, _FS1, op1)
    _texto_com_sangramento(r2, _LINHA2, _FS2, op2)
    _texto_com_sangramento(r3, _LINHA3, _FS2, op3)


def _aplicar_carimbo(pdf_bytes: bytes) -> bytes:
    """Recebe bytes de um PDF, carimba todas as paginas e retorna os bytes resultantes."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page in doc:
        _carimbar_pagina(page)
    buf = io.BytesIO()
    doc.save(buf, garbage=4, deflate=True)
    doc.close()
    return buf.getvalue()
