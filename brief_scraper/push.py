"""Send one upload step's files from the Brief Scraper app to the upload URLs Figma handed out.

Usage:  python -m brief_scraper.push <step-id> <file containing the upload_assets response JSON>

Each file is posted under a marker name, so the placement script that follows can find it on the
Figma page. The outcome is reported back to the app.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

APP = f"http://127.0.0.1:{os.environ.get('PORT', '8765')}"


def call(path, body=None):
    req = urllib.request.Request(APP + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main():
    step, response_file = sys.argv[1], sys.argv[2]
    urls = re.findall(r"https://mcp\.figma\.com/mcp/upload/[^\"'\s\\]+", open(response_file).read())
    plan = call(f"/api/claude/apply/files?step={step}")
    files = plan["files"]
    if len(urls) != len(files):
        sys.exit(f"Expected {len(files)} upload URLs for step {step}, found {len(urls)}.")
    sent, failed = 0, []
    with tempfile.TemporaryDirectory() as tmp:
        for url, f in zip(urls, files):
            local = os.path.join(tmp, f["filename"])
            with urllib.request.urlopen(APP + f["path"], timeout=60) as r, open(local, "wb") as out:
                out.write(r.read())
            done = subprocess.run(["curl", "-s", "-o", os.devnull, "-w", "%{http_code}", "-F", f"file=@{local};type={f['mime']}", url],
                                  capture_output=True, text=True).stdout.strip()
            if done == "200":
                sent += 1
            else:
                failed.append(f"{f['filename']} (HTTP {done})")
    result = {"uploaded": sent, "of": len(files), "failed": failed}
    call(f"/api/claude/apply/result?brand={plan['brand']}", {"step": step, **({"error": result} if failed else {"result": result})})
    print(json.dumps(result))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
