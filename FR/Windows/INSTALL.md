# =========================================================================
# WINDOWS : INSTALLATION VIA WSL 2 — UNE COMMANDE, PUIS DOUBLE-CLIC
# =========================================================================
# Ces binaires sont des exécutables Linux : tout tourne DANS WSL 2 (Ubuntu).
# Aucune installation de Python n'est nécessaire, et AUCUN chmod à taper,
# jamais : install.sh pose les droits une première fois, puis l'app remet
# elle-même son moteur en état à chaque démarrage.

# 0. (Une seule fois) Installer WSL 2 — PowerShell en administrateur,
#    puis redémarrer :
# wsl --install -d Ubuntu

# 1. Dans le terminal WSL (Ubuntu), depuis ce dossier, la SEULE commande :
sh install.sh
#    → droits d'exécution posés partout, prérequis installés via apt
#      (tmux, git, node 22), et MAIsterMind.bat copié sur ton Bureau Windows.

# 2. Si install.sh a signalé qu'AUCUN harness n'est présent : installe-en UN
#    DANS WSL (l'un suffit, les deux cohabitent sans problème) :
#      OpenCode : https://opencode.ai/docs      puis  opencode auth login
#      Codex    : npm install -g @openai/codex  puis  codex login
#    Le harness se choisit ensuite projet par projet, dans l'app (bouton Équiper).

# 3. Au quotidien : double-clique sur MAIsterMind.bat (Bureau, ou ce dossier
#    via l'explorateur Windows) — le navigateur s'ouvre tout seul.
#    La fenêtre noire montre les logs : la fermer éteint l'app, PAS les runs
#    (ils vivent dans tmux, côté WSL — relance le .bat pour les retrouver).

# Mode expert (terminal WSL) : ./MAIsterMind_App
# Les binaires restent utilisables en direct depuis la racine de TON projet :
#   /chemin/vers/ce/dossier/engine/Safe-Coding
