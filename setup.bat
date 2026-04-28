@echo off
REM Creeaza un venv local "venv" si instaleaza dependentele.
REM Necesita Python 3.8+ instalat si in PATH.

echo === Verificare Python ===
where python >nul 2>nul
if errorlevel 1 (
    echo EROARE: Python nu este in PATH. Instaleaza Python 3.8+ de pe python.org.
    pause
    exit /b 1
)

echo === Creare venv local ===
python -m venv venv
if errorlevel 1 (
    echo EROARE: Nu pot crea venv. Verifica permisiunile.
    pause
    exit /b 1
)

echo === Instalare dependinte (PyQt5 + pylogix) ===
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo EROARE: Instalare dependinte esuata. Verifica conexiunea internet.
    pause
    exit /b 1
)

echo.
echo === GATA ===
echo Pt rulare ulterioara: ruleaza run.bat (sau direct: python main.py dupa "venv\Scripts\activate")
pause
