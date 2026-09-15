@echo off
rem Windows: duplo clique roda o roteiro na pasta do repositorio.
cd /d "%~dp0"
set /p ET="Etiqueta da placa (Enter: le da placa ou da planilha pelo MAC): "
set /p L="Carga de 2 A ligada na LPR? [s/N] "
set ARGS=
if not "%ET%"=="" set ARGS=--etiqueta %ET%
if /i not "%L%"=="s" set ARGS=%ARGS% --sem-lpr
python bancada_placa.py %ARGS%
pause
