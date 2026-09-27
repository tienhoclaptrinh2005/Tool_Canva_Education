@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Gen docs - Payslip PDF

set "TOOL_DIR=%~dp0"
set "BUNDLED_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
set "VENV_PY=%TOOL_DIR%.venv\Scripts\python.exe"

if exist "%BUNDLED_PY%" (
    "%BUNDLED_PY%" -c "import pypdf, reportlab" >nul 2>nul
    if not errorlevel 1 (
        "%BUNDLED_PY%" "%TOOL_DIR%gen_docs.py" %*
        set "EXIT_CODE=!ERRORLEVEL!"
        goto :finish
    )
)

if not exist "%VENV_PY%" (
    echo Dang tao moi truong Python lan dau...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv "%TOOL_DIR%.venv"
    ) else (
        python -m venv "%TOOL_DIR%.venv"
    )
    if errorlevel 1 (
        echo LOI: Khong tao duoc moi truong Python.
        set "EXIT_CODE=1"
        goto :finish
    )
    "%VENV_PY%" -m pip install --disable-pip-version-check -r "%TOOL_DIR%requirements.txt"
    if errorlevel 1 (
        echo LOI: Khong cai duoc thu vien can thiet.
        set "EXIT_CODE=1"
        goto :finish
    )
)

"%VENV_PY%" "%TOOL_DIR%gen_docs.py" %*
set "EXIT_CODE=!ERRORLEVEL!"

:finish
echo.
if "!EXIT_CODE!"=="0" (
    echo Hoan tat.
) else (
    echo Tool dung voi ma loi !EXIT_CODE!.
)
pause
exit /b !EXIT_CODE!
