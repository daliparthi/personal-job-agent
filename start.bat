@echo off
rem Job Agent for Windows (macOS: start.command, Linux: start.sh). Options go to run.py, e.g.  start.bat --profile Alex
setlocal
cd /d "%~dp0"
set "PY=py -3"
where py >nul 2>&1 || set "PY=python"
if not exist ".venv\Scripts\python.exe" (
  %PY% -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 || (
    echo Job Agent needs Python 3.10 or newer: https://www.python.org/downloads/
    goto :fail
  )
  echo Creating Python environment...
  %PY% -m venv .venv || goto :fail
)
rem (Re)install packages on the first run and whenever requirements.txt changes.
fc /b requirements.txt .venv\requirements.installed >nul 2>&1
if errorlevel 1 (
  echo Installing Python packages...
  ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || goto :fail
  copy /y requirements.txt .venv\requirements.installed >nul
)
set NEED_MODELS=
if not exist "static\vendor\web-llm\index.js" set NEED_MODELS=1
if not exist "models\onnx\Qwen2.5-0.5B-Instruct\onnx\model_quantized.onnx" if not exist "models\onnx\Qwen2.5-0.5B-Instruct\onnx\model_quantized.onnx.part1" set NEED_MODELS=1
if defined NEED_MODELS (
  echo Bundling the Qwen2.5-0.5B model files - one time, about 1.1 GB...
  ".venv\Scripts\python.exe" fetch_models.py || goto :fail
)
".venv\Scripts\python.exe" run.py %*
if errorlevel 1 pause
goto :eof
:fail
echo.
echo Setup failed. See the messages above.
pause
