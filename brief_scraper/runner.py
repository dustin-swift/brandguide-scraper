"""Run the scan and apply routines by starting Claude Code in the background, so the app has a button.

Claude Code needs its own sign-in on each Mac (see setup.sh); the desktop app's sign-in is not shared
with a background run. Signed in with a Claude subscription it gets the account's Figma connector
(tools named mcp__claude_ai_Figma__*). Otherwise it uses the Figma MCP server that setup.sh adds
under the name "figma", which then needs authorising with /mcp.
"""
import glob
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = {}  # brand id -> {"kind", "state", "log", "started", "problem"}
ALLOWED = ["mcp__claude_ai_Figma", "mcp__figma", "ReadMcpResourceTool", "ListMcpResourcesTool", "Read", "Write",
           "Bash(curl:*)", "Bash(.venv/bin/python -m brief_scraper.push:*)", "Bash(mkdir:*)"]
SETUP_HINT = "Run ./setup.sh in the brief-scraper folder once to sign Claude Code in and connect it to Figma."


def find_claude():
    found = shutil.which("claude")
    if found:
        return found
    bundled = glob.glob(os.path.expanduser("~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"))
    version = lambda p: [int(x) for x in re.findall(r"\d+", p.split("claude-code/")[1].split("/")[0])]
    return max(bundled, key=version) if bundled else None


def _prompt(kind, brand_name):
    body = (ROOT / ".claude" / "commands" / f"{kind}-brand.md").read_text()
    body = re.sub(r"\A---.*?---\s*", "", body, flags=re.S)
    return body.replace("$ARGUMENTS", brand_name)


def _diagnose(text):
    low = text.lower()
    if "not logged in" in low or "/login" in low:
        return "Claude Code is not signed in on this Mac. " + SETUP_HINT
    if "mcp__figma" in low and ("not found" in low or "no such tool" in low or "unknown tool" in low) or "no mcp servers" in low:
        return "Claude Code has no Figma connection. " + SETUP_HINT
    if "haven't granted" in low or "requested permissions" in low:
        return "Claude Code was not allowed to use a tool it needed. The details are below; send them to whoever maintains this tool."
    if ("needs authentication" in low or "unauthorized" in low) and "mcp__claude_ai_figma" not in low:
        return "Claude Code's Figma connection needs signing in again. " + SETUP_HINT
    return None


def status(brand_id):
    run = RUNS.get(brand_id)
    out = {"available": bool(find_claude()), "hint": None if find_claude() else "Claude Code was not found on this Mac."}
    if run:
        out.update({k: run[k] for k in ("kind", "state", "problem", "started")}, log=run["log"][-25:])
    return out


def start(brand_id, brand_name, kind, port):
    exe = find_claude()
    if not exe:
        raise RuntimeError("Claude Code was not found on this Mac. Install it, or run the command in the Claude desktop app.")
    if RUNS.get(brand_id, {}).get("state") == "running":
        raise RuntimeError("Claude Code is already working on this brand.")
    (ROOT / ".tmp").mkdir(exist_ok=True)
    run = RUNS[brand_id] = {"kind": kind, "state": "running", "log": [], "started": time.strftime("%H:%M:%S"), "problem": None, "proc": None}
    cmd = [exe, "-p", _prompt(kind, brand_name), "--allowedTools", ",".join(ALLOWED), "--permission-mode", "acceptEdits", "--max-turns", "120"]
    env = dict(os.environ, PORT=str(port))
    env.pop("ANTHROPIC_API_KEY", None)  # the background Claude uses its own sign-in, not the app's API key

    def work():
        try:
            proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            run["proc"] = proc
            for line in proc.stdout:
                if line.strip():
                    run["log"].append(line.rstrip()[:400])
            code = proc.wait()
            text = "\n".join(run["log"])
            run["problem"] = _diagnose(text) or (None if code == 0 else f"Claude Code stopped with an error (exit {code}). The last lines are shown below.")
            run["state"] = "failed" if run["problem"] else "done"
        except Exception as e:
            run["state"], run["problem"] = "failed", f"Could not start Claude Code: {e}"

    threading.Thread(target=work, daemon=True).start()


def stop(brand_id):
    run = RUNS.get(brand_id)
    if run and run.get("proc") and run["proc"].poll() is None:
        run["proc"].terminate()
        run["state"], run["problem"] = "failed", "Stopped."
