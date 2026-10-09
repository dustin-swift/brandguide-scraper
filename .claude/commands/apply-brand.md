---
description: Write the changes approved in the Brief Scraper app to the brand's Figma file
argument-hint: "[brand name, optional]"
---

Apply the changes the user approved in the Brief Scraper app to the brand's Figma file. Running this command is the
user's go-ahead to write to that file; do exactly the steps the app lists and nothing else.

Use whichever Figma connector is available and signed in (its tools may be named `mcp__claude_ai_Figma__…` or
`mcp__figma__…`); if one reports that it needs authentication, use the other.

Brand to use: $ARGUMENTS (if empty, the app uses whichever brand is open in it).

1. Get the plan: `curl -s "http://127.0.0.1:8765/api/claude/apply?brand=<brand, URL-encoded, or omit>"`.
   If it cannot connect, tell the user to start the app with `./run.sh` and stop. If it returns an `error`, show it and stop.
2. Tell the user in one line what will be written (the step titles), then begin. Before the first Figma call, load the
   Figma usage guidance: call the Figma connector's
   `get_figma_skill` tool with `skill://figma/figma-use/SKILL.md` (or read that MCP resource, or use the /figma-use skill).
3. Work through `steps` strictly in order, one at a time. Do not run them in parallel: later steps depend on earlier ones.

   **A step with `"kind": "script"`**: call `use_figma` with the plan's `fileKey`, the step's `code` exactly as given
   (never edit it), its `title` as the description, and `skillNames: "resource:figma-use"`. Then post the outcome:
   `curl -s -X POST -H "Content-Type: application/json" --data-binary @<file> "http://127.0.0.1:8765<post_results_to>"`
   with a file in the `.tmp/` folder containing `{"step": "<id>", "result": <returned JSON>}`, or `{"step": "<id>", "error": "<message>"}`
   if the call failed.

   **A step with `"kind": "upload"`**: call the Figma connector's `upload_assets` tool with `fileKey`, `count` set to
   the step's `count`, and `currentPageId` set to the step's `pageId`. Save the tool's whole JSON response to a
   file in the `.tmp/` folder of this project, then run from the brief-scraper folder:
   `.venv/bin/python -m brief_scraper.push <step id> <that file>`
   It sends the files and reports the result to the app itself. The upload URLs expire after ten minutes, so run it
   straight away.

4. If a step fails, post the error, stop, and tell the user what failed. Do not retry with changed code and do not
   continue to later steps. Running `/apply-brand` again resumes from the first step without a result.
5. When every step has a result, tell the user it is done, mention any lines in the results that start with
   `SKIPPED` or `MISSING`, and remind them to press **Update & rescan** in the DesignOps plugin in Figma.
