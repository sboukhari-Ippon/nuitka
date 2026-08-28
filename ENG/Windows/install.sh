#!/bin/sh
# =============================================================================
# MAIsterMind — installation en UNE commande :   sh install.sh
# -----------------------------------------------------------------------------
# Ce script est le SEUL geste technique demandé, une seule fois :
#   1. il remet toute la distribution en état d'exécution (chmod, et quarantaine
#      Gatekeeper sur macOS) — personne ne tape jamais chmod +x à la main ;
#   2. il vérifie les prérequis (tmux, git, node, opencode) et installe via
#      apt/brew ce qui peut l'être ;
#   3. il pose le lanceur DOUBLE-CLIC natif de ta plateforme :
#        - macOS   : la MAIsterMind.app du dossier (+ lien dans ~/Applications)
#        - Ubuntu  : entrée « MAIsterMind » dans le menu d'applications
#        - Windows : MAIsterMind.bat copié sur le Bureau (l'app tourne dans WSL)
#   Ensuite : double-clic, le navigateur s'ouvre, c'est tout.
#   (Identique dans les 6 variantes : il détecte lui-même la plateforme.)
# =============================================================================
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1
OS="$(uname -s)"
IS_WSL=0
[ "$OS" = "Linux" ] && grep -qi microsoft /proc/version 2>/dev/null && IS_WSL=1

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }

step "1/3 · Droits d'exécution (aucun chmod manuel, jamais)"
# L'app (binaire compilé ou source .py), les orchestrateurs du moteur, le bundle macOS.
chmod +x ./MAIsterMind_App 2>/dev/null
chmod +x ./MAIsterMind_App.py 2>/dev/null
chmod +x ./install.sh 2>/dev/null
if [ -d ./engine ]; then
    # Les orchestrateurs s'appellent MAIsterMind* ou Yolo* : binaires (sans extension)
    # comme sources .py (mode dev, lancées par leur shebang). Un motif sans
    # correspondance reste littéral en sh : le test -f ci-dessous l'absorbe.
    for f in ./engine/MAIsterMind* ./engine/Yolo*; do
        [ -f "$f" ] && chmod +x "$f" 2>/dev/null
    done
fi
[ -f ./MAIsterMind.app/Contents/MacOS/MAIsterMind ] && chmod +x ./MAIsterMind.app/Contents/MacOS/MAIsterMind 2>/dev/null
if [ "$OS" = "Darwin" ]; then
    # Quarantaine posée par le navigateur et propagée par Archive Utility à
    # l'extraction : levée sur TOUT le dossier (binaires, bundle .app inclus).
    xattr -dr com.apple.quarantine "$HERE" 2>/dev/null
    say "✓ Droits + quarantaine Gatekeeper levée sur le dossier."
else
    say "✓ Droits d'exécution posés."
fi

step "2/3 · Prérequis (tmux, git, node, un harness d'agent)"
MISSING=""
for tool in tmux git node; do
    command -v "$tool" >/dev/null 2>&1 || MISSING="$MISSING $tool"
done
if [ -n "$MISSING" ]; then
    if [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
        say "→ Installation via Homebrew :$MISSING"
        # node@22 : version supportée par les deux harness (voir INSTALL.md)
        brew install tmux git node@22 || say "⚠️  Installation brew incomplète : voir INSTALL.md"
        if ! command -v node >/dev/null 2>&1; then
            # node@22 est keg-only : il faut l'ajouter au PATH une fois pour toutes.
            NODE_BIN="$(brew --prefix node@22 2>/dev/null)/bin"
            if [ -x "$NODE_BIN/node" ]; then
                echo "export PATH=\"$NODE_BIN:\$PATH\"" >> "$HOME/.zshrc"
                export PATH="$NODE_BIN:$PATH"
                say "✓ node@22 ajouté au PATH (~/.zshrc)."
            fi
        fi
    elif [ "$OS" = "Linux" ] && command -v apt >/dev/null 2>&1; then
        say "→ Installation via apt (mot de passe sudo possible) :$MISSING"
        sudo apt update && sudo apt install -y tmux git curl ca-certificates || say "⚠️  Installation apt incomplète : voir INSTALL.md"
        if ! command -v node >/dev/null 2>&1; then
            say "→ Node.js 22 (dépôt NodeSource)"
            curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs
        fi
    else
        say "⚠️  Manquant :$MISSING — installe-les (voir INSTALL.md) puis relance ce script."
    fi
else
    say "✓ tmux, git, node présents."
fi
# Node : la PRÉSENCE ne suffit pas. L'outillage JS courant (vite 7+, vitest 3+) exige
# Node ≥ 20.19 ; et le node vu par un shell de login interactif (celui de l'agent dans
# tmux, nvm/fnm/volta chargés) doit être le même que celui vu ici — sinon les verdicts
# des orchestrateurs tournent sous un autre Node que l'agent (incident du 23/08/2026).
if command -v node >/dev/null 2>&1; then
    NODE_HERE="$(command -v node)"
    NODE_VERSION="$(node --version 2>/dev/null)"
    NODE_MAJOR="$(printf '%s' "$NODE_VERSION" | sed -n 's/^v\{0,1\}\([0-9]*\).*/\1/p')"
    say "✓ node $NODE_VERSION ($NODE_HERE)."
    if [ -n "$NODE_MAJOR" ] && [ "$NODE_MAJOR" -lt 20 ] 2>/dev/null; then
        say "⚠️  Node $NODE_VERSION est trop ancien pour l'outillage JS courant : installe Node 22 (nvm install 22 / NodeSource / brew node@22)."
    fi
    LOGIN_SHELL="${SHELL:-/bin/bash}"
    NODE_LOGIN="$("$LOGIN_SHELL" -lic 'command -v node' </dev/null 2>/dev/null | tail -1)"
    if [ -n "$NODE_LOGIN" ] && [ "$NODE_LOGIN" != "$NODE_HERE" ]; then
        say "⚠️  Ton shell de login résout un autre node ($NODE_LOGIN) : l'app et ses orchestrateurs utiliseront celui-là (PATH du shell de login placé en tête au démarrage)."
    fi
fi
# Harness d'agent : OpenCode ET/OU Codex CLI. UN SEUL suffit — c'est à
# l'équipement du projet (dans l'app) que le harness se choisit. On ne réclame
# donc que s'il n'y en a AUCUN.
HARNESS_FOUND=0
if command -v opencode >/dev/null 2>&1; then
    say "✓ opencode présent ($(opencode --version 2>/dev/null | head -1))."
    HARNESS_FOUND=1
fi
if command -v codex >/dev/null 2>&1; then
    say "✓ codex présent ($(codex --version 2>/dev/null | head -1))."
    HARNESS_FOUND=1
fi
if [ "$HARNESS_FOUND" = "0" ]; then
    say "⚠️  Aucun harness d'agent IA : installe-en UN (l'un suffit)."
    say "      OpenCode : https://opencode.ai/docs      puis  opencode auth login"
    say "      Codex    : npm install -g @openai/codex  puis  codex login"
fi

step "3/3 · Lanceur double-clic"
APP_LABEL="MAIsterMind"
if [ "$OS" = "Darwin" ]; then
    if [ -d ./MAIsterMind.app ]; then
        mkdir -p "$HOME/Applications"
        ln -sfn "$HERE/MAIsterMind.app" "$HOME/Applications/MAIsterMind.app" 2>/dev/null
        say "✓ Double-clique sur MAIsterMind.app (dans ce dossier, ou via ~/Applications)."
    else
        say "⚠️  MAIsterMind.app absent du dossier — double-clique sur le binaire MAIsterMind_App (un Terminal s'ouvrira)."
    fi
elif [ "$IS_WSL" = "1" ]; then
    # L'app vit dans WSL ; le double-clic, côté Windows. Le MAIsterMind.bat LIVRÉ
    # se lance depuis ce dossier (il se repère par %~dp0) ; celui du Bureau est
    # GÉNÉRÉ avec le chemin WSL en dur — une simple copie pointerait vers… le Bureau.
    DESKTOP=""
    WINHOME="$(cmd.exe /c "echo %USERPROFILE%" 2>/dev/null | tr -d '\r')"
    [ -n "$WINHOME" ] && DESKTOP="$(wslpath -u "$WINHOME" 2>/dev/null)/Desktop"
    if [ -n "$DESKTOP" ] && [ -d "$DESKTOP" ] && {
        # Le chemin (Linux) est un cd DANS bash — jamais un argument de wsl.exe :
        # sans -e le shell mange les backslashes, et --cd n'accepte pas tous les
        # chemins Linux. wsl.exe autostarte la distribution au besoin.
        printf '@echo off\r\n'
        printf 'REM Genere par install.sh - lance MAIsterMind depuis son dossier d installation (WSL).\r\n'
        printf 'title MAIsterMind\r\n'
        printf 'where wsl.exe >nul 2>nul || (echo [MAIsterMind] WSL 2 est requis - PowerShell admin : wsl --install -d Ubuntu & pause & exit /b 1)\r\n'
        printf 'wsl.exe -e bash -lic "cd '"'"'%s'"'"' && { chmod +x ./MAIsterMind_App ./MAIsterMind_App.py 2>/dev/null; if [ -x ./MAIsterMind_App ]; then exec ./MAIsterMind_App; elif [ -f ./MAIsterMind_App.py ]; then exec python3 ./MAIsterMind_App.py; fi; }; echo [MAIsterMind] App introuvable dans %s"\r\n' "$HERE" "$HERE"
        printf 'echo.\r\n'
        printf 'echo [MAIsterMind] App arretee. Les runs en cours continuent dans tmux (WSL).\r\n'
        printf 'pause\r\n'
    } > "$DESKTOP/MAIsterMind.bat" 2>/dev/null; then
        say "✓ « MAIsterMind.bat » généré sur ton Bureau Windows : double-clique dessus."
    else
        say "→ Double-clique sur MAIsterMind.bat (dans ce dossier, via l'explorateur Windows)."
    fi
elif [ "$OS" = "Linux" ]; then
    # Entrée de menu (fiable partout) : le double-clic d'un .desktop posé dans un
    # dossier est bloqué par GNOME tant qu'il n'est pas « autorisé », le menu non.
    # L'Exec passe par un wrapper généré (chemin en dur) plutôt que par une ligne Exec à
    # quoting fragile. bash -lic et pas -lc : un shell de login NON interactif s'arrête
    # à la garde « case $- in *i*) » du ~/.bashrc Ubuntu standard AVANT de charger nvm —
    # l'app voyait alors le Node système (v18) et pas celui de l'utilisateur (v22). Le -i
    # rend le shell interactif ; ses deux avertissements « pas de contrôle de tâche » sur
    # stderr sont attendus sans terminal et sans effet.
    APPS_DIR="$HOME/.local/share/applications"
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$APPS_DIR" "$BIN_DIR"
    cat > "$BIN_DIR/maistermind-launch" <<WRAPPER
#!/bin/sh
# Généré par install.sh : cible Exec du .desktop MAIsterMind.
# -lic (login + interactif) : sinon ~/.bashrc s'arrête avant de charger nvm/fnm/volta.
cd "$HERE" || exit 1
exec /bin/bash -lic 'if [ -x ./MAIsterMind_App ]; then exec ./MAIsterMind_App; else exec python3 ./MAIsterMind_App.py; fi'
WRAPPER
    chmod +x "$BIN_DIR/maistermind-launch"
    cat > "$APPS_DIR/maistermind.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$APP_LABEL
Comment=Cockpit des orchestrateurs MAIsterMind · MAIsterMind orchestrator cockpit
Exec=$BIN_DIR/maistermind-launch
Path=$HERE
Terminal=false
Categories=Development;
Icon=applications-development
DESKTOP
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" 2>/dev/null
    say "✓ « MAIsterMind » ajouté au menu d'applications (l'app journalise dans .mm-app/launcher.log)."
fi

step "🏭 Installation terminée."
say "Au quotidien : lance MAIsterMind (double-clic), le navigateur s'ouvre tout seul."
say "Mode expert : ./MAIsterMind_App en terminal ; binaires du moteur utilisables en direct depuis engine/."
