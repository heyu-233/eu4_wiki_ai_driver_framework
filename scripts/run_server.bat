@echo off
setlocal
cd /d "%~dp0.."
set PYTHONUNBUFFERED=1
if not defined LLM_USE_PROXY (
  set HTTP_PROXY=
  set HTTPS_PROXY=
  set ALL_PROXY=
  set http_proxy=
  set https_proxy=
  set all_proxy=
)
if not exist logs mkdir logs
set PYTHON_EXE=
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "D:\miniconda3\python.exe" set "PYTHON_EXE=D:\miniconda3\python.exe"
if not defined PYTHON_EXE set "PYTHON_EXE=python"
"%PYTHON_EXE%" -u app\server.py 1>>logs\kb_server.out.log 2>>logs\kb_server.err.log
