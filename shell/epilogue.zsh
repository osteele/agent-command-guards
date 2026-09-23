# Agent session epilogue for the launcher-served harnesses (kimi, opencode,
# codex, omp, agy).
#
# These agents render on the alternate screen, so exiting restores a scrollback
# with no sign of which session just ended. They fire no end-of-session hook of
# their own -- Claude Code's SessionEnd has no counterpart here -- so the
# launcher leaves a card of what it knew at launch (agent-launcher) and this
# renders it at the next prompt, the first moment the main screen is back and
# the only place the exit status is visible.
#
# Register once, after prompt setup. Zsh invokes each precmd hook with the
# original command status and restores the calling status/pipeline afterwards.
# Do not use a prompt theme's cached status: it may be absent or stale.
# Re-sourcing moves this hook last without registering it twice.
autoload -Uz add-zsh-hook

# Keyed by TERM_SESSION_ID, the one identifier the launcher and an interactive
# shell agree on. Keep this derivation identical to the one in agent-launcher.
# Computed once: a $(...) here would fork on every prompt.
() {
  local key="${TERM_SESSION_ID:-}"
  [[ -z $key ]] && key="${TTY:t}"
  [[ -z $key ]] && key=default
  typeset -g _AGENT_EPILOGUE_FILE="${AGENT_EPILOGUE_DIR:-$HOME/.cache/agent-command-guards/epilogue}/${key//[^A-Za-z0-9._-]/_}.card"
}

_agent_epilogue() {
  # Capture before any command. Zsh preserves $? and $pipestatus around the
  # registered hook; returning zero lets other prompt/periodic hooks run.
  local st=$?
  [[ -f $_AGENT_EPILOGUE_FILE ]] || return 0
  # Claim before reading: two shells sharing a terminal key must not both
  # render the same card. A failed claim leaves it for a later prompt.
  local pending="${_AGENT_EPILOGUE_FILE}.$$"
  mv -- "$_AGENT_EPILOGUE_FILE" "$pending" 2>/dev/null || return 0

  # One argument per line, exactly as the launcher wrote them. Read into an
  # array rather than word-split: a project path may contain spaces, and zsh
  # does not split an unquoted expansion anyway.
  local -a card
  card=("${(@f)$(<$pending)}")
  rm -f -- "$pending"
  (( ${#card} )) || return 0

  "${_AGENT_EPILOGUE_RENDERER}" "${card[@]}" --status "$st" 2>/dev/null
  return 0
}

# Resolved once, at source time: the renderer sits beside this file's
# repository, and a card is useless without it.
typeset -g _AGENT_EPILOGUE_RENDERER="${${(%):-%x}:A:h:h}/agent-epilogue"
if [[ -x $_AGENT_EPILOGUE_RENDERER ]]; then
  add-zsh-hook -d precmd _agent_epilogue 2>/dev/null
  add-zsh-hook precmd _agent_epilogue
fi
