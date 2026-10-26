#!/usr/bin/env bash
# Installs epiplan: virtualenv, browser, `epiplan` command, agent skill, MCP server registration.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/epiplan"

PYBIN=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then PYBIN="$c"; break; fi
done
[ -n "$PYBIN" ] || { echo "Python 3.10+ is required." >&2; exit 1; }

echo "==> virtualenv + dependencies"
"$PYBIN" -m venv "$HERE/.venv"
"$HERE/.venv/bin/pip" install -q --upgrade pip
"$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt"
"$HERE/.venv/bin/playwright" install chromium

echo "==> config in $CONF"
mkdir -p "$CONF"; chmod 700 "$CONF"
[ -f "$CONF/rules.md" ] || cp "$HERE/rules.example.md" "$CONF/rules.md"

echo "==> epiplan command"
mkdir -p "$HOME/.local/bin"
ln -sfn "$HERE/bin/epiplan" "$HOME/.local/bin/epiplan"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "    add ~/.local/bin to your PATH";; esac

echo "==> skill"
mkdir -p "$HOME/.claude/skills"
ln -sfn "$HERE/skills/planning" "$HOME/.claude/skills/planning"

echo "==> MCP server"
if command -v claude >/dev/null; then
  claude mcp remove epitech-planning --scope user >/dev/null 2>&1 || true
  claude mcp add epitech-planning --scope user \
    -e EPIPLAN_ENABLE_WRITES=1 -e EPIPLAN_MAX_WRITES=5 -e EPIPLAN_MODULE_ALLOWLIST= \
    -- "$HERE/.venv/bin/python" "$HERE/server.py"
else
  cat <<EOF
    'claude' not found. Register the server in your MCP client with:
      command: $HERE/.venv/bin/python
      args:    ["$HERE/server.py"]
      env:     EPIPLAN_ENABLE_WRITES=1, EPIPLAN_MAX_WRITES=5, EPIPLAN_MODULE_ALLOWLIST=
EOF
fi

cat <<EOF

Done. Next:
  1. epiplan login        (log in once; MFA works)
  2. edit $CONF/rules.md  (your registration preferences)
  3. restart your agent, then ask it to check your planning (/planning)
EOF
