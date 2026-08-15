original_zdotdir="${AGENT_LAUNCHER_ORIGINAL_ZDOTDIR:-$HOME}"
if [[ "$original_zdotdir" != "$ZDOTDIR" && -f "$original_zdotdir/.zshenv" ]]; then
  source "$original_zdotdir/.zshenv"
fi
unset original_zdotdir
source "$ZDOTDIR/prepend-shadow.zsh"
