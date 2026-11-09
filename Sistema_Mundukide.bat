@echo off
setlocal EnableDelayedExpansion
title Sistema Mundukide
cd /d "%~dp0"

echo ============================================
echo    Sistema Mundukide - iniciando...
echo ============================================
echo.

rem ----------------------------------------------------------------
rem 1. Localizar o Python. Se nao existir, instalar automaticamente.
rem ----------------------------------------------------------------
set "PY="
call :localizar_python
if not defined PY (
    echo Python nao encontrado neste computador. Instalando agora...
    echo ^(isso acontece so na primeira vez e pode levar alguns minutos^)
    echo.
    call :instalar_python
    call :localizar_python
)
if not defined PY (
    echo.
    echo ERRO: nao foi possivel instalar o Python automaticamente.
    echo Instale manualmente em https://www.python.org/downloads/
    echo e marque a opcao "Add python.exe to PATH" durante a instalacao.
    echo Depois clique novamente em Sistema_Mundukide.
    echo.
    pause
    exit /b 1
)
echo Python encontrado: %PY%

rem ----------------------------------------------------------------
rem 2. Conferir as bibliotecas do sistema. Instalar o que faltar.
rem ----------------------------------------------------------------
"%PY%" -c "import streamlit, pandas, sqlalchemy, psycopg2, ofxparse, plotly, fpdf, kaleido, fitz, openpyxl" >nul 2>&1
if errorlevel 1 (
    echo.
    echo Instalando as bibliotecas do sistema...
    echo ^(isso acontece so na primeira vez e pode levar alguns minutos^)
    echo.
    "%PY%" -m pip install --upgrade pip >nul 2>&1
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERRO: falha ao instalar as bibliotecas. Verifique a conexao com a internet
        echo e clique novamente em Sistema_Mundukide.
        echo.
        pause
        exit /b 1
    )
)

rem ----------------------------------------------------------------
rem 3. Abrir o sistema.
rem ----------------------------------------------------------------
echo.
echo Abrindo o sistema no navegador...
echo ^(mantenha esta janela aberta enquanto usa o sistema^)
echo.
"%PY%" -m streamlit run app.py
pause
exit /b 0


rem ================================================================
rem Funcoes auxiliares
rem ================================================================

:localizar_python
set "PY="
rem Primeiro procura no PATH, ignorando o atalho falso da Microsoft Store.
for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PY (
        echo %%P | findstr /i "WindowsApps" >nul || set "PY=%%P"
    )
)
rem Depois procura nos locais padrao de instalacao. Necessario logo apos
rem instalar, porque o PATH desta janela ainda nao foi atualizado.
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python311\python.exe"
) do (
    if not defined PY if exist %%P set "PY=%%~P"
)
exit /b 0

:instalar_python
rem Baixa o instalador oficial do python.org e instala somente para o
rem usuario atual. Nao pede senha de administrador.
set "INST=%TEMP%\python-3.13-instalador.exe"
echo Baixando o instalador do Python em python.org...
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.13.2/python-3.13.2-amd64.exe' -OutFile '%INST%'"
if not exist "%INST%" (
    echo Falha no download do instalador.
    exit /b 1
)
echo Instalando Python ^(aguarde, nao feche esta janela^)...
"%INST%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=0 Include_test=0
del "%INST%" >nul 2>&1
exit /b 0
