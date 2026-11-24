@echo off
setlocal
where py.exe >nul 2>&1
if errorlevel 1 goto python
py -3 -X utf8 "%~dp0scripts\install.py" %*
exit /b %errorlevel%

:python
where python.exe >nul 2>&1
if errorlevel 1 goto missing
python -X utf8 "%~dp0scripts\install.py" %*
exit /b %errorlevel%

:missing
echo Install Python 3.11 or newer, then run this command again. 1>&2
exit /b 1
