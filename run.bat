@echo off
REM Lanseaza aplicatia folosind venv-ul local "venv".
REM Daca venv-ul nu exista, ruleaza intai setup.bat.

if not exist "venv\Scripts\python.exe" (
    echo Venv-ul lipseste. Ruleaza intai setup.bat.
    pause
    exit /b 1
)

call venv\Scripts\python.exe main.py
