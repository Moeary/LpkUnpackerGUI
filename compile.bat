@echo off
REM LpkUnpackerGUI Compiler Script
setlocal enabledelayedexpansion

echo ===== LpkUnpackerGUI Compiler =====
echo Starting compilation process...
echo.

REM Check if Python is available
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Error: Python not found in PATH!
    echo Please ensure Python is installed and added to PATH.
    pause
    exit /b 1
)

echo Checking Python version...
python --version

REM Check if required packages are installed
echo.
echo Checking dependencies...
python -c "import nuitka" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Installing Nuitka...
    pip install nuitka ordered-set
)

python -c "import PyQt5" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Installing PyQt5 dependencies...
    pip install -r requirements.txt
)

echo.
echo Building executable with Nuitka...
echo This may take 5-10 minutes. Please be patient...
echo.

REM Main compilation command
python -m nuitka --onefile ^
    --enable-plugin=pyqt5 ^
    --output-dir=build ^
    --windows-disable-console ^
    --include-data-dir=./Img=Img ^
    --include-package=qfluentwidgets ^
    --include-package=filetype ^
    --windows-icon-from-ico=Img/icon.ico ^
    --nofollow-import-to=numpy,matplotlib,scipy,pandas,tkinter ^
    --python-flag=no_site ^
    --python-flag=no_docstrings ^
    --remove-output ^
    LpkUnpackerGUI.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo Error: Compilation failed with error code %ERRORLEVEL%.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ===== Compilation Successful! =====
echo Executable location: build\LpkUnpackerGUI.exe
echo.
pause