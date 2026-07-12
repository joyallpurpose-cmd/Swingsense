@echo off
REM SwingSense - publish latest scan to GitHub Pages (run after a scan)
cd /d "%~dp0"
py build_static.py || python build_static.py
git add docs/
git add -f docs/data.json
git commit -m "scan: %date%"
git push
echo.
echo Published. Phone URL: https://YOUR-USERNAME.github.io/YOUR-REPO/
pause
