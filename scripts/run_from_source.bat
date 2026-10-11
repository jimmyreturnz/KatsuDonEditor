@echo off
setlocal
cd /d "%~dp0\.."
if not exist .venv py -3.12 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
call tools\compile_translations.bat
python main.py
endlocal
