# =========================================================================
# WINDOWS: INSTALLATION VIA WSL 2 — ONE COMMAND, THEN DOUBLE-CLICK
# =========================================================================
# These binaries are Linux executables: everything runs INSIDE WSL 2 (Ubuntu).
# No Python installation is needed, and NO chmod to type, ever: install.sh
# sets the permissions once, then the app repairs its own engine at every
# startup.

# 0. (Once) Install WSL 2 — PowerShell as administrator, then reboot:
# wsl --install -d Ubuntu

# 1. In the WSL (Ubuntu) terminal, from this folder, the ONLY command:
sh install.sh
#    → execution permissions set everywhere, prerequisites installed via apt
#      (tmux, git, node 22), and MAIsterMind.bat copied to your Windows Desktop.

# 2. If install.sh reported that NO harness is present: install ONE
#    INSIDE WSL (one is enough, both can coexist):
#      OpenCode: https://opencode.ai/docs      then  opencode auth login
#      Codex   : npm install -g @openai/codex  then  codex login
#    The harness is then chosen project by project, in the app (Equip button).

# 3. Day to day: double-click MAIsterMind.bat (Desktop, or this folder via
#    Windows Explorer) — the browser opens by itself.
#    The black window shows the logs: closing it quits the app, NOT the runs
#    (they live in tmux, WSL side — relaunch the .bat to find them again).

# Expert mode (WSL terminal): ./MAIsterMind_App
# The binaries remain directly usable from the root of YOUR project:
#   /path/to/this/folder/engine/Safe-Coding
