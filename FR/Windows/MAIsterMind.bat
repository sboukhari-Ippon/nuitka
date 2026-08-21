:; HERE="$(cd "$(dirname "$0")" && pwd)" #
:; echo "[MAIsterMind] Terminal WSL detecte : lancement direct de l'app (le double-clic Windows sur ce .bat mene au meme resultat)." #
:; cd "$HERE" || exit 1 #
:; chmod +x ./MAIsterMind_App ./MAIsterMind_App.py 2>/dev/null #
:; [ -x ./MAIsterMind_App ] && exec ./MAIsterMind_App #
:; [ -f ./MAIsterMind_App.py ] && exec python3 ./MAIsterMind_App.py #
:; echo "[MAIsterMind] MAIsterMind_App introuvable a cote de ce fichier." ; exit 1 #
@echo off
REM ===========================================================================
REM MAIsterMind - lanceur double-clic (Windows -> WSL).
REM
REM Fichier POLYGLOTTE : double-clique depuis l'explorateur Windows, cmd.exe
REM ignore les lignes ':;' (labels) et execute le batch ci-dessous ; lance
REM depuis un terminal WSL (./MAIsterMind.bat), bash execute les lignes ':;'
REM et demarre directement l'app.
REM
REM Cote Windows : l'app et les binaires sont des executables Linux, tout
REM tourne DANS WSL 2 (wsl.exe demarre la distribution tout seul, pas besoin
REM qu'elle "tourne" deja). Le chemin n'est JAMAIS passe en argument a wsl.exe
REM (sans -e, le shell Linux mange les backslashes de D:\... et le \ final de
REM %~dp0 avale meme le guillemet - bug constate) : on fait cd /d ICI, WSL
REM herite du dossier courant et le traduit lui-meme en /mnt/d/...
REM Fermer cette fenetre eteint l'app ; les runs, eux, vivent dans tmux (WSL)
REM et continuent - relance ce .bat pour les retrouver.
REM Messages en ASCII : cmd.exe lit ce fichier en page de codes OEM.
REM ===========================================================================
setlocal
title MAIsterMind

where wsl.exe >nul 2>nul
if errorlevel 1 (
    echo [MAIsterMind] WSL 2 est requis. PowerShell en administrateur :
    echo     wsl --install -d Ubuntu
    echo puis redemarre et relance ce fichier. Details : INSTALL.md
    pause
    exit /b 1
)

REM Une distribution demarre-t-elle ? (wsl.exe l'autostarte ; echec = aucune
REM distribution installee, ou WSL desactive)
wsl.exe -e true >nul 2>nul
if errorlevel 1 (
    echo [MAIsterMind] WSL est present mais aucune distribution Linux ne demarre.
    echo Installe Ubuntu - PowerShell en administrateur :
    echo     wsl --install -d Ubuntu
    echo puis redemarre et relance ce fichier. Details : INSTALL.md
    pause
    exit /b 1
)

cd /d "%~dp0"
if errorlevel 1 (
    echo [MAIsterMind] Dossier inaccessible depuis cmd - chemin reseau ?
    echo Copie le dossier sur un disque local et relance.
    pause
    exit /b 1
)

REM Le dossier courant Windows est herite et traduit par WSL (/mnt/...).
REM bash -lc : PATH complet de la session (node/opencode installes via nvm/npm).
REM L'app repare elle-meme les droits d'execution de son moteur au demarrage.
wsl.exe -e bash -lc "chmod +x ./MAIsterMind_App ./MAIsterMind_App.py 2>/dev/null; if [ -x ./MAIsterMind_App ]; then exec ./MAIsterMind_App; elif [ -f ./MAIsterMind_App.py ]; then exec python3 ./MAIsterMind_App.py; else echo '[MAIsterMind] MAIsterMind_App introuvable : ce .bat doit rester dans son dossier d installation.'; fi"

echo.
echo [MAIsterMind] App arretee. Les runs en cours continuent dans tmux (WSL).
pause
