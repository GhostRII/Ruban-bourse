@echo off
cd /d "%~dp0"
REM Construit TickerBandeau.exe (un seul fichier, sans console) dans le dossier dist\
set PY=python
python --version >nul 2>&1 || set PY=py
%PY% --version || (echo Python introuvable. & pause & exit /b 1)
%PY% -m pip install -r requirements.txt pyinstaller || (echo Echec de l'installation des modules. & pause & exit /b 1)
%PY% -m PyInstaller --noconfirm --onefile --windowed --name TickerBandeau ticker_gui.py || (echo Echec de la construction. & pause & exit /b 1)
echo.
echo ===== Termine : dist\TickerBandeau.exe =====
explorer dist
pause
