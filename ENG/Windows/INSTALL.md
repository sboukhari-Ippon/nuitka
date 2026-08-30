# =========================================================================
# UBUNTU / DEBIAN: INSTALLATION — ONE COMMAND, THEN DOUBLE-CLICK
# =========================================================================
# No Python installation is needed: the app and the orchestrators are
# standalone binaries. And NO chmod to type, ever: install.sh sets the
# permissions once, then the app repairs its own engine at every startup.

# 1. From this folder, the ONLY command to type:
sh install.sh
#    → execution permissions set everywhere, prerequisites installed via apt
#      (tmux, git, node 22 — 20.19 minimum; sudo password may be asked),
#      "MAIsterMind" entry added to the applications menu.

# 2. If install.sh reported that NO harness is present: install ONE
#    (one is enough, both can coexist):
#      OpenCode: https://opencode.ai/docs      then  opencode auth login
#      Codex   : npm install -g @openai/codex  then  codex login
#                (BETA: less proven than OpenCode on real runs — your feedback is welcome:
#                gates, permissions, models. OpenCode remains the reference.)
#    The harness is then chosen project by project, in the app (Equip button).

# 3. Day to day: launch "MAIsterMind" from the applications menu —
#    the browser opens by itself, the app discovers the engine/ binaries,
#    equips your projects and follows the runs.
#    Without a terminal, the app logs to .mm-app/launcher.log and shuts down
#    via the ⏻ button in "Status & settings". Runs live in tmux: quitting
#    the app kills NONE of them.

# Expert mode (terminal): ./MAIsterMind_App
# The binaries remain directly usable from the root of YOUR project:
#   /path/to/this/folder/engine/Coding
