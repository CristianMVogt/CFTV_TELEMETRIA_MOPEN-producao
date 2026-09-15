#!/bin/bash
# macOS: duplo clique abre o terminal na pasta certa e roda o roteiro.
cd "$(dirname "$0")" || exit 1
read -p "Etiqueta da placa (Enter: le da placa ou da planilha pelo MAC): " ET
read -p "Carga de 2 A ligada na LPR? [s/N] " L
ARGS=""; [ -n "$ET" ] && ARGS="--etiqueta $ET"; [ "$L" != "s" ] && ARGS="$ARGS --sem-lpr"
python3 bancada_placa.py $ARGS
echo; read -p "Enter para fechar"
