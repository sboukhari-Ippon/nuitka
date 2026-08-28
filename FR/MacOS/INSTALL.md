# =========================================================================
# MACOS : INSTALLATION — UNE COMMANDE, PUIS DOUBLE-CLIC
# =========================================================================
# Deux archives existent : arm64 (Mac M1 et suivants) et x64 (Mac Intel) —
# prends celle de ton Mac. Aucune installation de Python n'est nécessaire.
# Et AUCUN chmod ni xattr à taper, jamais : install.sh pose droits ET lève la
# quarantaine Gatekeeper une première fois, puis l'app remet elle-même son
# moteur en état à chaque démarrage.

# 1. Depuis ce dossier, la SEULE commande à taper (Terminal) :
sh install.sh
#    → droits d'exécution + quarantaine levée sur tout le dossier, prérequis
#      installés via Homebrew (tmux, git, node@22 + PATH), lien
#      MAIsterMind.app posé dans ~/Applications.

# 2. Si install.sh a signalé qu'AUCUN harness n'est présent : installe-en UN
#    (l'un suffit, les deux cohabitent sans problème) :
#      OpenCode : https://opencode.ai/docs      puis  opencode auth login
#      Codex    : npm install -g @openai/codex  puis  codex login
#    Le harness se choisit ensuite projet par projet, dans l'app (bouton Équiper).

# 3. Au quotidien : double-clique sur MAIsterMind.app — le navigateur s'ouvre
#    tout seul. Sans terminal, l'app journalise dans .mm-app/launcher.log et
#    s'éteint par le bouton ⏻ de « Statut & réglages ». Les runs, eux, vivent
#    dans tmux : éteindre l'app n'en tue AUCUN.

# Sans install.sh (tout au Finder) : au premier double-clic d'une app non
# signée, macOS refuse — Réglages Système > Confidentialité et sécurité >
# « Ouvrir quand même » (une seule fois). L'app répare ensuite le reste.

# Mode expert (terminal) : ./MAIsterMind_App
# Les binaires restent utilisables en direct depuis la racine de TON projet :
#   /chemin/vers/ce/dossier/engine/Coding

# Secours (uniquement si un binaire est « killed » au lancement — signature
# ad hoc cassée par le transfert) : re-signe-le puis relance :
# codesign --force -s - engine/Coding
