---
description: Read the current state of a brand's Figma file and send it to the Brief Scraper app (read-only)
argument-hint: "[brand name, optional]"
---

Scan the Figma board for the brand that is open in the Brief Scraper app. This only reads the Figma file.

Brand to use: $ARGUMENTS (if empty, the app uses whichever brand is open in it).

1. Ask the app what to run: `curl -s "http://127.0.0.1:8765/api/claude/scan?brand=<brand, URL-encoded, or omit>"`.
   If this fails to connect, tell the user to start the app with `./run.sh` and stop. If it returns an `error`, show it and stop.
2. Before the first Figma call, load the Figma usage guidance: read the MCP resource `skill://figma/figma-use/SKILL.md`
   (or use the /figma-use skill if installed).
3. The response has `fileKey` and `steps`. For each step, call the Figma connector's `use_figma` tool with that `fileKey`,
   the step's `code` passed exactly as given (do not edit or shorten it), the step's `title` as the description, and
   `skillNames: "resource:figma-use"`.
4. After each step, write the JSON that `use_figma` returned to a temporary file and post it back:
   `curl -s -X POST -H "Content-Type: application/json" --data-binary @<file> "http://127.0.0.1:8765<post_results_to>"`
   where the file contains `{"step": "<step id>", "result": <the returned JSON>}`.
5. The reply to the `core` step contains more `steps` (one per page). Run and post each of those the same way.
   These are independent, so run them in parallel.
6. When a reply says `"done": true`, summarise its `summary` for the user in two or three lines and tell them to
   return to the app's Review step.

If a `use_figma` call fails, report the error text and stop; do not rewrite the script.
