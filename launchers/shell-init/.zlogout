original_zdotdir="${AGENT_LAUNCHER_ORIGINAL_ZDOTDIR:-$HOME}"
if [[ "$original_zdotdir" != "$ZDOTDIR" && -f "$original_zdotdir/.zlogout" ]]; then
  source "$original_zdotdir/.zlogout"
fi
unset original_zdotdir
