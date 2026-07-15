#!/usr/bin/env bash
set -euo pipefail

# Launch one of the cookbook superproject's MCP servers (pm-daemon or
# gcp-monitor) from an agent session started inside this Backend checkout.
#
# The server implementations live in the cookbook repo
# (adamtasteslikegood/tasteslikegoodtheangularsvegancookbook) under
# scripts/pm/ and scripts/monitoring/; this repo is normally checked out as
# its Backend/ submodule. The parent launchers are already cwd-independent —
# they resolve their own repo root from BASH_SOURCE, cd there, and read the
# cookbook .env — so the only job here is *finding* them.

server="${1:-}"
shift || true

case "$server" in
  pm-daemon) launcher_rel="scripts/pm/run_pm_daemon.sh" ;;
  gcp-monitor) launcher_rel="scripts/monitoring/run_gcp_monitor.sh" ;;
  *)
    echo "usage: ${BASH_SOURCE[0]} {pm-daemon|gcp-monitor} [args...]" >&2
    exit 2
    ;;
esac

backend_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Preferred: git knows the superproject when this checkout is the submodule.
superproject="$(git -C "$backend_root" rev-parse --show-superproject-working-tree 2>/dev/null || true)"

# Fallback: walk up from this checkout until a directory contains the
# launcher. Covers linked worktrees (Backend/.claude/worktrees/<name>), where
# git cannot resolve the superproject because the worktree is not at the path
# the parent repo's gitlink points to.
if [[ -z "$superproject" || ! -f "$superproject/$launcher_rel" ]]; then
  superproject=""
  dir="$backend_root"
  while [[ "$dir" != "/" ]]; do
    dir="$(dirname "$dir")"
    if [[ -f "$dir/$launcher_rel" ]]; then
      superproject="$dir"
      break
    fi
  done
fi

if [[ -z "$superproject" ]]; then
  cat >&2 <<EOF
Cannot find the cookbook superproject that provides the '$server' MCP server.
This repo must be checked out as the Backend/ submodule of
adamtasteslikegood/tasteslikegoodtheangularsvegancookbook, which contains the
server implementation at $launcher_rel. In a standalone checkout these
servers are unavailable — clone the cookbook repo and start the session from
its Backend/ directory instead.
EOF
  exit 1
fi

exec bash "$superproject/$launcher_rel" "$@"
