"""Local web app: upload a brief, review what was extracted, export the .md."""
import io
import logging
import os
import re
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path

import pymupdf
from flask import Flask, Response, abort, jsonify, request, send_from_directory

from . import ai, markdown, site
from .extract import CMS, extract, slug

ROOT = Path(__file__).resolve().parent
JOBS = {}
WORK = Path(tempfile.mkdtemp(prefix="brief-scraper-"))

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 400 * 1024 * 1024


def load_env():
    env = ROOT.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*?)\s*$", line)
            if m and not os.environ.get(m.group(1)):
                os.environ[m.group(1)] = m.group(2).strip("'\"")


def _adder(job):
    result = job["result"]
    taken = {e["id"] for e in result["elements"]}

    def add(el, asset=None):
        base = f"{el['type']}.{slug(el['name'])}"
        el_id, n = base, 1
        while el_id in taken:
            n += 1
            el_id = f"{base}-{n}"
        taken.add(el_id)
        el["id"] = el_id
        el["cms"] = CMS.get(el["type"], [])
        if asset:
            job["assets"][el_id] = asset
            el["asset"] = {"format": asset["format"], "mime": asset["mime"], "bytes": len(asset["data"])}
        result["elements"].append(el)
        return el

    return add


def _run(job, use_ai, max_edge):
    def progress(done, total, msg):
        job["progress"] = {"done": done, "total": total, "message": msg}

    try:
        out = extract(job["path"], max_image_edge=max_edge, progress=progress)
        job["assets"] = out.pop("assets")
        job["result"] = out
        if use_ai:
            job["stage"] = "ai"
            try:
                job["ai_problems"] = ai.enrich(job["path"], out, _adder(job), progress)
                job["ai_ran"] = True
            except Exception as e:  # keep the deterministic result even if the AI pass fails
                job["ai_error"] = str(e)
        job["state"] = "done"
    except Exception as e:
        job["state"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"


def _job(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    return job


@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/api/status")
def status():
    return jsonify({"ai_available": ai.available(), "model": ai.MODEL})


@app.post("/api/jobs")
def create_job():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Choose a PDF file."}), 400
    job_id = uuid.uuid4().hex[:12]
    path = WORK / f"{job_id}.pdf"
    f.save(path)
    use_ai = request.form.get("use_ai") == "1" and ai.available()
    max_edge = int(request.form.get("max_edge") or 1024)
    job = {"id": job_id, "path": str(path), "name": os.path.basename(f.filename), "state": "running",
           "stage": "extract", "progress": {"done": 0, "total": 1, "message": "Opening the PDF"}, "assets": {}}
    JOBS[job_id] = job
    threading.Thread(target=_run, args=(job, use_ai, max_edge), daemon=True).start()
    return jsonify({"id": job_id})


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    job = _job(job_id)
    out = {k: job.get(k) for k in ("id", "name", "state", "stage", "progress", "error", "ai_error", "ai_problems", "ai_ran")}
    if job["state"] == "done":
        out["result"] = job["result"]
    return jsonify(out)


@app.get("/api/jobs/<job_id>/asset/<path:el_id>")
def asset(job_id, el_id):
    a = _job(job_id)["assets"].get(el_id)
    if not a:
        abort(404)
    # assets come from an untrusted PDF: never let the browser run them as a document
    headers = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
               "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"}
    if request.args.get("download"):
        ext = {"svg": "svg", "png": "png", "jpeg": "jpg"}[a["format"]]
        headers["Content-Disposition"] = f'attachment; filename="{slug(request.args["download"])}.{ext}"'
    return Response(a["data"], mimetype=a["mime"], headers=headers)


@app.get("/api/jobs/<job_id>/page/<int:number>.png")
def page_png(job_id, number):
    job = _job(job_id)
    doc = pymupdf.open(job["path"])
    if not 1 <= number <= len(doc):
        abort(404)
    page = doc[number - 1]
    scale = 1400 / max(page.rect.width, page.rect.height)
    png = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")
    doc.close()
    return Response(png, mimetype="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/jobs/<job_id>/export")
def export(job_id):
    job = _job(job_id)
    if job["state"] != "done":
        abort(409)
    body = request.get_json(silent=True) or {}
    mode = "data-url" if body.get("asset_mode") == "data-url" else "blocks"
    parts = markdown.build_parts(job["result"], job["assets"], body.get("edits", {}), job["name"], asset_mode=mode)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(job["name"]).stem).strip("-") or "brief"
    if len(parts) == 1:
        return Response(parts[0], mimetype="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}.dsbrief.md"'})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, text in enumerate(parts, 1):
            z.writestr(f"{name}.dsbrief.part-{n:02d}-of-{len(parts):02d}.md", text)
    return Response(buf.getvalue(), mimetype="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{name}.dsbrief.zip"', "X-Brief-Parts": str(len(parts))})


@app.post("/api/jobs/<job_id>/site")
def build_site(job_id):
    job = _job(job_id)
    cfg = request.get_json(silent=True) or {}
    folder = site.build(WORK / f"site-{job_id}", cfg, job["assets"].get(cfg.get("logo_id")))
    return jsonify({"local_url": site.serve(folder), "public_url": site.STATE["url"]})


@app.post("/api/tunnel/<action>")
def tunnel(action):
    try:
        if action == "start":
            return jsonify({"public_url": site.tunnel_start()})
        site.tunnel_stop()
        return jsonify({"public_url": None})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400


def main():
    load_env()
    port = int(os.environ.get("PORT", "8765"))
    print(f"Brief scraper running at http://127.0.0.1:{port}  (AI pass: {'on' if ai.available() else 'off, no API key'})")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # progress polling floods the terminal otherwise
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
