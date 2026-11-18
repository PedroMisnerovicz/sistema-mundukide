@echo off
rem Gera o CarimboMundukide.exe (programa avulso do carimbo AVCD).
rem Usa um ambiente Python separado, so com o necessario, para o .exe
rem ficar pequeno. O resultado fica nesta pasta (carimbo_exe).

setlocal
set "AQUI=%~dp0"
set "PROJETO=%AQUI%.."
set "TRABALHO=%TEMP%\carimbo_mundukide_build"

if not exist "%TRABALHO%\venv\Scripts\python.exe" (
    python -m venv "%TRABALHO%\venv" || goto :erro
)
"%TRABALHO%\venv\Scripts\python.exe" -m pip install --quiet --upgrade pip pymupdf pyinstaller || goto :erro

"%TRABALHO%\venv\Scripts\python.exe" -m PyInstaller ^
    --noconfirm --clean --onefile --windowed ^
    --name CarimboMundukide ^
    --paths "%PROJETO%" ^
    --distpath "%TRABALHO%\dist" ^
    --workpath "%TRABALHO%\build" ^
    --specpath "%TRABALHO%" ^
    "%AQUI%carimbo_app.py" || goto :erro

copy /Y "%TRABALHO%\dist\CarimboMundukide.exe" "%AQUI%CarimboMundukide.exe" || goto :erro
echo.
echo Pronto: %AQUI%CarimboMundukide.exe
exit /b 0

:erro
echo.
echo Falha ao gerar o .exe.
exit /b 1
