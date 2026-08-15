shadow_dir="${AGENT_COMMAND_GUARDS_DIR:-}"
if [[ -n "$shadow_dir" && -d "$shadow_dir" ]]; then
  path=("$shadow_dir" ${path:#$shadow_dir})
  export PATH
fi
unset shadow_dir
