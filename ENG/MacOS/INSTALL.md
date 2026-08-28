# =========================================================================
# MACOS: INSTALLATION — ONE COMMAND, THEN DOUBLE-CLICK
# =========================================================================
# Two archives exist: arm64 (Mac M1 and later) and x64 (Intel Mac) — take
# the one matching your Mac. No Python installation is needed.
# And NO chmod nor xattr to type, ever: install.sh sets the permissions AND
# clears the Gatekeeper quarantine once, then the app repairs its own engine
# at every startup.

# 1. From this folder, the ONLY command to type (Terminal):
sh install.sh
#    → permissions + quarantine cleared on the whole folder, prerequisites
#      installed via Homebrew (tmux, git, node@22 + PATH), MAIsterMind.app
#      linked into ~/Applications.

# 2. If install.sh reported that NO harness is present: install ONE
#    (one is enough, both can coexist):
#      OpenCode: https://opencode.ai/docs      then  opencode auth login
#      Codex   : npm install -g @openai/codex  then  codex login
#    The harness is then chosen project by project, in the app (Equip button).

# 3. Day to day: double-click MAIsterMind.app — the browser opens by itself.
#    Without a terminal, the app logs to .mm-app/launcher.log and shuts down
#    via the ⏻ button in "Status & settings". Runs live in tmux: quitting
#    the app kills NONE of them.

# Without install.sh (Finder only): on the first double-click of an unsigned
# app, macOS refuses — System Settings > Privacy & Security >
# "Open Anyway" (once). The app then repairs everything else.

# Expert mode (terminal): ./MAIsterMind_App
# The binaries remain directly usable from the root of YOUR project:
#   /path/to/this/folder/engine/Coding

# Rescue (only if a binary is "killed" at launch — ad hoc signature broken
# by the transfer): re-sign it then relaunch:
# codesign --force -s - engine/Coding
