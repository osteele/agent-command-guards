original_zdotdir="${AGENT_LAUNCHER_ORIGINAL_ZDOTDIR:-$HOME}"
if [[ "$original_zdotdir" != "${${(%):-%x}:h}" && -f "$original_zdotdir/.zshrc" ]]; then
  source "$original_zdotdir/.zshrc"
fi
unset original_zdotdir
# Source the prepend script from this file's own directory: user
# configuration may repoint ZDOTDIR or clobber any helper variable, but it
# cannot change where this file came from.
source "${${(%):-%x}:h}/prepend-shadow.zsh"
