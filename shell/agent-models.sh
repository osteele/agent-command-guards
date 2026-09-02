# Model-first shortcuts for interactive Bash and Zsh shells.
# Subprocesses continue to resolve the native launchers from PATH.

case $- in
    *i*)
        unalias claude fable codex kimi glm 2>/dev/null || true

        claude() { command agent-model launch claude "$@"; }
        fable() { command agent-model launch fable "$@"; }
        codex() { command agent-model launch codex "$@"; }
        kimi() { command agent-model launch kimi "$@"; }
        glm() { command agent-model launch glm "$@"; }
        ;;
esac
