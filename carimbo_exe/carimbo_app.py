"""
Carimbo Mundukide — programa avulso (sem o sistema financeiro)
==============================================================
Janela simples para aplicar o carimbo AVCD/Mundukide em PDFs, usando a
mesma logica da tela "Carimbo de Documentos" (modulos/carimbo_core.py).

O PDF original nunca e alterado: o resultado e salvo na mesma pasta,
com o final "_carimbado.pdf".

Gerar o .exe: ver carimbo_exe/gerar_exe.bat

Uso sem janela (para testes):
    CarimboMundukide.exe --sem-janela arquivo1.pdf arquivo2.pdf
"""

import os
import sys
from pathlib import Path

# Rodando pelo codigo-fonte: a pasta do projeto precisa estar no caminho
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modulos.carimbo_core import _aplicar_carimbo  # noqa: E402

_TITULO = "Carimbo Mundukide — AVCD"
_SUFIXO = "_carimbado"


def _caminho_saida(origem: Path) -> Path:
    """Nome do arquivo carimbado, sem nunca sobrescrever um existente."""
    destino = origem.with_name(f"{origem.stem}{_SUFIXO}.pdf")
    n = 2
    while destino.exists():
        destino = origem.with_name(f"{origem.stem}{_SUFIXO} ({n}).pdf")
        n += 1
    return destino


def carimbar_arquivo(origem: Path) -> Path:
    """Carimba um PDF e devolve o caminho do arquivo gerado."""
    saida = _aplicar_carimbo(origem.read_bytes())
    destino = _caminho_saida(origem)
    destino.write_bytes(saida)
    return destino


def _so_pdfs(caminhos) -> list[Path]:
    return [Path(c) for c in caminhos if str(c).lower().endswith(".pdf")]


def _rodar_sem_janela(caminhos) -> int:
    falhas = 0
    for origem in _so_pdfs(caminhos):
        try:
            carimbar_arquivo(origem)
        except Exception:
            falhas += 1
    return 1 if falhas else 0


def _rodar_janela(iniciais: list[Path]) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    arquivos: list[Path] = []
    ultima_pasta: list[Path] = []

    raiz = tk.Tk()
    raiz.title(_TITULO)
    raiz.geometry("640x460")
    raiz.minsize(520, 380)

    corpo = ttk.Frame(raiz, padding=14)
    corpo.pack(fill="both", expand=True)

    ttk.Label(corpo, text="Carimbo de Documentos",
              font=("Segoe UI", 14, "bold")).pack(anchor="w")
    ttk.Label(
        corpo,
        text=("Aplica o carimbo AVCD / Mundukide (PRO-2025K2/0002) em todas as "
              "paginas dos PDFs escolhidos.\nO original nao e alterado: o "
              "arquivo carimbado e salvo na mesma pasta, com o final "
              "\"_carimbado\"."),
        justify="left", wraplength=600,
    ).pack(anchor="w", pady=(4, 10))

    botoes = ttk.Frame(corpo)
    botoes.pack(fill="x")

    quadro = ttk.Frame(corpo)
    quadro.pack(fill="both", expand=True, pady=10)
    rolagem = ttk.Scrollbar(quadro)
    rolagem.pack(side="right", fill="y")
    lista = tk.Listbox(quadro, yscrollcommand=rolagem.set, activestyle="none",
                       font=("Segoe UI", 9))
    lista.pack(side="left", fill="both", expand=True)
    rolagem.config(command=lista.yview)

    status = ttk.Label(corpo, text="Nenhum arquivo selecionado.")
    status.pack(anchor="w")

    rodape = ttk.Frame(corpo)
    rodape.pack(fill="x", pady=(10, 0))

    def atualizar():
        lista.delete(0, "end")
        for a in arquivos:
            lista.insert("end", a.name)
        if arquivos:
            status.config(text=f"{len(arquivos)} arquivo(s) pronto(s) para carimbar.")
            btn_aplicar.state(["!disabled"])
        else:
            status.config(text="Nenhum arquivo selecionado.")
            btn_aplicar.state(["disabled"])

    def adicionar(caminhos):
        for p in _so_pdfs(caminhos):
            if p not in arquivos:
                arquivos.append(p)
        atualizar()

    def selecionar():
        escolhidos = filedialog.askopenfilenames(
            title="Selecione os PDFs", filetypes=[("Arquivos PDF", "*.pdf")])
        adicionar(escolhidos)

    def limpar():
        arquivos.clear()
        atualizar()

    def aplicar():
        if not arquivos:
            return
        btn_aplicar.state(["disabled"])
        btn_selecionar.state(["disabled"])
        erros = []
        ok = 0
        for i, origem in enumerate(arquivos):
            status.config(text=f"Carimbando {i + 1} de {len(arquivos)}: {origem.name}")
            raiz.update_idletasks()
            try:
                destino = carimbar_arquivo(origem)
                lista.delete(i)
                lista.insert(i, f"OK    {destino.name}")
                ultima_pasta[:] = [destino.parent]
                ok += 1
            except Exception as exc:
                lista.delete(i)
                lista.insert(i, f"ERRO  {origem.name}")
                erros.append(f"{origem.name}: {exc}")
        arquivos.clear()
        btn_selecionar.state(["!disabled"])
        if ultima_pasta:
            btn_pasta.state(["!disabled"])
        status.config(text=f"Concluido: {ok} carimbado(s), {len(erros)} com erro.")
        if erros:
            messagebox.showwarning(
                _TITULO,
                "Nao foi possivel carimbar:\n\n" + "\n".join(erros[:10]))
        else:
            messagebox.showinfo(
                _TITULO,
                f"{ok} arquivo(s) carimbado(s).\n\nSalvos na mesma pasta dos "
                "originais, com o final \"_carimbado\".")

    def abrir_pasta():
        if ultima_pasta:
            os.startfile(ultima_pasta[0])

    btn_selecionar = ttk.Button(botoes, text="Selecionar PDFs...", command=selecionar)
    btn_selecionar.pack(side="left")
    ttk.Button(botoes, text="Limpar lista", command=limpar).pack(side="left", padx=8)

    btn_aplicar = ttk.Button(rodape, text="Aplicar carimbo", command=aplicar)
    btn_aplicar.pack(side="left")
    btn_pasta = ttk.Button(rodape, text="Abrir pasta dos carimbados", command=abrir_pasta)
    btn_pasta.pack(side="left", padx=8)
    btn_pasta.state(["disabled"])

    atualizar()
    adicionar(iniciais)  # PDFs arrastados para cima do .exe
    raiz.mainloop()


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--sem-janela":
        sys.exit(_rodar_sem_janela(args[1:]))
    _rodar_janela(_so_pdfs(args))


if __name__ == "__main__":
    main()
