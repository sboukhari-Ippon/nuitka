# =========================================================================
# UBUNTU / DEBIAN : INSTALLATION — UNE COMMANDE, PUIS DOUBLE-CLIC
# =========================================================================
# Aucune installation de Python n'est nécessaire : l'app et les orchestrateurs
# sont des binaires autonomes. Et AUCUN chmod à taper, jamais : install.sh pose
# les droits une première fois, puis l'app remet elle-même son moteur en état
# à chaque démarrage.

# 1. Depuis ce dossier, la SEULE commande à taper :
sh install.sh
#    → droits d'exécution posés partout, prérequis installés via apt
#      (tmux, git, node 22 — mot de passe sudo demandé au besoin),
#      entrée « MAIsterMind » ajoutée au menu d'applications.

# 2. Si install.sh a signalé qu'AUCUN harness n'est présent : installe-en UN
#    (l'un suffit, les deux cohabitent sans problème) :
#      OpenCode : https://opencode.ai/docs      puis  opencode auth login
#      Codex    : npm install -g @openai/codex  puis  codex login
#    Le harness se choisit ensuite projet par projet, dans l'app (bouton Équiper).

# 3. Au quotidien : lance « MAIsterMind » depuis le menu d'applications —
#    le navigateur s'ouvre tout seul, l'app découvre les binaires de engine/,
#    équipe tes projets et suit les runs.
#    Sans terminal, l'app journalise dans .mm-app/launcher.log et s'éteint par
#    le bouton ⏻ de « Statut & réglages ». Les runs, eux, vivent dans tmux :
#    éteindre l'app n'en tue AUCUN.

# Mode expert (terminal) : ./MAIsterMind_App
# Les binaires restent utilisables en direct depuis la racine de TON projet :
#   /chemin/vers/ce/dossier/engine/Safe-Coding
