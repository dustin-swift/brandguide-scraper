#!/bin/sh
# One-time setup so the app's Scan and Write buttons can run Claude Code in the background.
# It gives Claude Code its own sign-in and its own Figma connection on this Mac.
cd "$(dirname "$0")" || exit 1
CLAUDE=$(command -v claude)
if [ -z "$CLAUDE" ]; then
  CLAUDE=$(ls -d "$HOME/Library/Application Support/Claude/claude-code"/*/*/claude.app/Contents/MacOS/claude 2>/dev/null | sort -V | tail -1)
fi
if [ -z "$CLAUDE" ]; then
  echo "Claude Code was not found. Install the Claude desktop app or the Claude Code CLI, then run this again."
  exit 1
fi
echo "Using Claude Code at: $CLAUDE"
"$CLAUDE" mcp add --scope user --transport http figma https://mcp.figma.com/mcp 2>&1 | sed 's/^/  /'
cat <<'MSG'

Claude Code will now open. Sign in when it asks (or type /login), then type /exit.

  If you sign in with a Claude subscription and Figma is connected in your Claude account, that is all.
  If you sign in with a Console (API) account instead, also type /mcp, choose "figma", choose
  Authenticate, and approve the request in the browser.

MSG
exec "$CLAUDE"
