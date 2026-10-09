"""Local web app: brands, materials, review against the Figma board, and the hand-off to Claude Code.

Nothing a client supplies is stored. A brand is a name and a Figma link (brands.json); materials
and extracted assets live in memory and a temp folder for as long as the app runs. The Figma file
is the record: each session starts from a fresh scan of it.
"""
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pymupdf
from flask import Flask, Response, abort, jsonify, request, send_from_directory

from . import ai, figma_plan, runner, sources
from .extract import slug

ROOT = Path(__file__).resolve().parent
BRANDS_FILE = ROOT.parent / "brands.json"
WORK = Path(tempfile.mkdtemp(prefix="brief-scraper-"))
SESSIONS = {}
ACTIVE = {"id": None}
LOCK = threading.Lock()

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 800 * 1024 * 1024


def load_env():
    env = ROOT.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*?)\s*$", line)
            if m and not os.environ.get(m.group(1)):
                os.environ[m.group(1)] = m.group(2).strip("'\"")


# ------------------------------------------------------------------------------------- brands

def brands():
    return json.loads(BRANDS_FILE.read_text()) if BRANDS_FILE.exists() else []


def file_key(url):
    m = re.search(r"figma\.com/(?:design|file)/([0-9A-Za-z]{22,128})", url or "")
    return m.group(1) if m else None


def brand_or_404(brand_id):
    b = next((b for b in brands() if b["id"] == brand_id), None)
    if not b:
        abort(404)
    return b


def session(brand_id):
    return SESSIONS.setdefault(brand_id, {"sources": [], "elements": [], "assets": {}, "pdfs": {}, "seen": {},
                                          "figma": {"pending": {}, "state": None, "scanned_at": None, "expected": []},
                                          "plan": None, "results": {}, "review": None})


@app.get("/api/brands")
def list_brands():
    return jsonify({"brands": brands(), "ai_available": ai.available(), "model": ai.MODEL, "active": ACTIVE["id"]})


@app.post("/api/brands")
def add_brand():
    body = request.get_json(silent=True) or {}
    name, url = (body.get("name") or "").strip(), (body.get("figma_url") or "").strip()
    key = file_key(url)
    if not name or not key:
        return jsonify({"error": "Give the brand a name and a Figma design link (figma.com/design/…)."}), 400
    items = brands()
    b = {"id": uuid.uuid4().hex[:10], "name": name[:80], "figma_url": url, "file_key": key}
    items.append(b)
    BRANDS_FILE.write_text(json.dumps(items, indent=2))
    return jsonify(b)


@app.delete("/api/brands/<brand_id>")
def remove_brand(brand_id):
    BRANDS_FILE.write_text(json.dumps([b for b in brands() if b["id"] != brand_id], indent=2))
    SESSIONS.pop(brand_id, None)
    return jsonify({"ok": True})


# ------------------------------------------------------------------------------------ materials

def _merge(s, src, elements, assets, pages=None):
    """Add one source's elements to the session, dropping exact repeats of what is already there."""
    added = 0
    with LOCK:
        for el in elements:
            asset = assets.get(el.get("id")) if el.get("id") else el.pop("_asset", None)
            if el["type"] == "color":
                key = ("color", slug(el["name"]), el.get("hex"))
            elif el["type"] == "gradient":
                key = ("gradient", json.dumps([(round(x["position"], 2), x["hex"]) for x in el.get("stops") or []]) + slug(el["name"]) * (not el.get("stops")), el.get("angle"))
            elif asset:
                key = ("asset", sources.fingerprint(asset))
            else:
                key = (el["type"], slug(el["name"]), src["id"])
            if key in s["seen"]:
                s["seen"][key].setdefault("also_in", []).append(src["name"])
                continue
            el["id"] = f"{src['id']}:{el.get('id') or el['type'] + '.' + slug(el['name'])}"
            base, n, taken = el["id"], 1, {e["id"] for e in s["elements"]}
            while el["id"] in taken:
                n += 1
                el["id"] = f"{base}-{n}"
            el["source_id"], el["source_name"] = src["id"], src["name"]
            if asset:
                s["assets"][el["id"]] = asset
                el["asset"] = {"format": asset["format"], "mime": asset["mime"], "bytes": len(asset["data"])}
            s["seen"][key] = el
            s["elements"].append(el)
            added += 1
    return added


def _process(s, src, payload, use_ai, reference, max_edge):
    def progress(done, total, msg):
        src["message"] = msg

    try:
        kind = src["kind"]
        if kind == "pdf":
            els, assets, pages, problems = sources.from_pdf(payload, use_ai, max_edge, progress)
            s["pdfs"][src["id"]] = payload
            for e in els:
                e["pdf"] = src["id"]
            src["count"] = _merge(s, src, els, assets)
            if problems:
                src["note"] = f"Claude could not read {len(problems)} page(s)."
        elif kind == "url":
            els, _ = sources.from_url(payload, use_ai)
            src["count"] = _merge(s, src, els, {})
        else:
            els = []
            for name, data in payload:
                k = sources.kind_of(name)
                try:
                    if k == "text":
                        got, _ = sources.from_text(name, data.decode("utf-8", "replace"), use_ai)
                        els += got
                    elif k == "asset" and reference:
                        if not ai.available():
                            raise ValueError("reading ads and layouts needs the Claude pass")
                        got, _ = sources.from_reference(name, data, sources.ASSET_TYPES[Path(name).suffix.lower()][1])
                        els += got
                    elif k == "asset":
                        el, asset = sources.from_asset(name, data)
                        el["_asset"] = asset
                        els.append(el)
                except Exception as e:
                    src.setdefault("skipped", []).append(f"{name}: {e}")
            src["count"] = _merge(s, src, els, {})
        src["state"], src["message"] = "done", ""
    except Exception as e:
        src["state"], src["message"] = "error", f"{type(e).__name__}: {e}"
    s["plan"] = None  # anything approved earlier no longer matches the material


@app.post("/api/brands/<brand_id>/sources")
def add_source(brand_id):
    brand_or_404(brand_id)
    s = session(brand_id)
    use_ai = request.form.get("use_ai") == "1" and ai.available()
    reference = request.form.get("reference") == "1"
    max_edge = int(request.form.get("max_edge") or 1600)
    made = []

    def start(name, kind, payload):
        src = {"id": "s" + uuid.uuid4().hex[:6], "name": name, "kind": kind, "state": "running", "message": "Queued", "count": 0}
        s["sources"].append(src)
        made.append(src["id"])
        threading.Thread(target=_process, args=(s, src, payload, use_ai, reference, max_edge), daemon=True).start()

    url = (request.form.get("url") or "").strip()
    if url:
        start(url, "url", url)
    loose, unknown = [], []
    for f in request.files.getlist("files"):
        name = f.filename or "file"
        kind = sources.kind_of(name)
        if Path(name).name.startswith(".") or kind is None:
            unknown.append(name)
        elif kind == "pdf":
            path = WORK / f"{uuid.uuid4().hex[:10]}.pdf"
            f.save(path)
            start(Path(name).name, "pdf", str(path))
        else:
            loose.append((name, f.read()))
    if loose:
        top = loose[0][0].split("/")[0] if "/" in loose[0][0] else None
        label = (f"{top}/ " if top else "") + f"({len(loose)} file{'s' if len(loose) != 1 else ''})" if len(loose) > 1 or top else loose[0][0]
        start(("Ads and layouts: " if reference else "") + label, "files", loose)
    return jsonify({"started": made, "unsupported": [u for u in unknown if not Path(u).name.startswith(".")]})


@app.delete("/api/brands/<brand_id>/sources/<source_id>")
def remove_source(brand_id, source_id):
    s = session(brand_id)
    with LOCK:
        gone = {e["id"] for e in s["elements"] if e.get("source_id") == source_id}
        s["elements"] = [e for e in s["elements"] if e["id"] not in gone]
        for i in gone:
            s["assets"].pop(i, None)
        s["seen"] = {k: v for k, v in s["seen"].items() if v["id"] not in gone}
        s["sources"] = [x for x in s["sources"] if x["id"] != source_id]
        s["plan"] = None
    return jsonify({"ok": True})


@app.get("/api/brands/<brand_id>")
def brand_state(brand_id):
    b = brand_or_404(brand_id)
    ACTIVE["id"] = brand_id
    s = session(brand_id)
    state = s["figma"]["state"]
    plan = s["plan"]
    return jsonify({
        "brand": b, "sources": s["sources"], "elements": s["elements"],
        "compared": figma_plan.compare(s["elements"], state) if state else {},
        "board": figma_plan.board_summary(state) if state else None,
        "scanned_at": s["figma"]["scanned_at"], "scan_running": bool(s["figma"]["expected"]), "runner": runner.status(brand_id),
        "plan": plan and {"skipped": plan["skipped"], "steps": [
            {"id": st["id"], "title": st["title"], "kind": st["kind"], "result": s["results"].get(st["id"])} for st in plan["steps"]]},
    })


@app.get("/api/brands/<brand_id>/asset/<path:el_id>")
def asset(brand_id, el_id):
    a = session(brand_id)["assets"].get(el_id)
    if not a:
        abort(404)
    # assets come from untrusted files: never let the browser run them as a document
    return Response(a["data"], mimetype=a["mime"], headers={
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"})


@app.get("/api/brands/<brand_id>/page/<source_id>/<int:number>.png")
def page_png(brand_id, source_id, number):
    path = session(brand_id)["pdfs"].get(source_id)
    if not path:
        abort(404)
    doc = pymupdf.open(path)
    if not 1 <= number <= len(doc):
        abort(404)
    page = doc[number - 1]
    scale = 1400 / max(page.rect.width, page.rect.height)
    png = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")
    doc.close()
    return Response(png, mimetype="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/brands/<brand_id>/approve")
def approve(brand_id):
    b = brand_or_404(brand_id)
    s = session(brand_id)
    if not s["figma"]["state"]:
        return jsonify({"error": "Scan the Figma file first."}), 409
    review = request.get_json(silent=True) or {}
    elements = []
    for e in s["elements"]:
        e = dict(e)
        edit = (review.get("edits") or {}).get(e["id"], {})
        if (edit.get("name") or "").strip():
            e["name"] = edit["name"].strip()
        if edit.get("type") in ("icon", "logo", "image", "graphic") and e["type"] in ("icon", "logo", "image", "graphic"):
            e["type"] = edit["type"]
        elements.append(e)
    compared = figma_plan.compare(elements, s["figma"]["state"])
    s["plan"] = figma_plan.build_plan(b, elements, compared, review, s["figma"]["state"])
    s["results"], s["review"] = {}, review
    return jsonify({"steps": len(s["plan"]["steps"]), "skipped": s["plan"]["skipped"]})


# ------------------------------------------------------------- the hand-off to Claude Code

@app.post("/api/brands/<brand_id>/run/<kind>")
def run(brand_id, kind):
    b = brand_or_404(brand_id)
    if kind == "stop":
        runner.stop(brand_id)
        return jsonify({"ok": True})
    if kind not in ("scan", "apply"):
        abort(404)
    if kind == "apply" and not session(brand_id)["plan"]:
        return jsonify({"error": "Nothing is approved yet."}), 409
    ACTIVE["id"] = brand_id
    try:
        runner.start(brand_id, b["name"], kind, int(os.environ.get("PORT", "8765")))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


def _target():
    want = (request.args.get("brand") or "").strip().lower()
    items = brands()
    b = next((b for b in items if want and want in (b["id"], b["name"].lower())), None) or next((b for b in items if b["id"] == ACTIVE["id"]), None)
    if not b:
        abort(Response(json.dumps({"error": "No brand is open in the Brief Scraper app. Open one at http://127.0.0.1:8765 first."}), 409, mimetype="application/json"))
    return b, session(b["id"])


@app.get("/api/claude/scan")
def claude_scan():
    b, s = _target()
    s["figma"].update(pending={}, expected=["core"])
    return jsonify({"brand": b["name"], "fileKey": b["file_key"], "steps": figma_plan.scan_steps_core(),
                    "post_results_to": f"/api/claude/scan/result?brand={b['id']}"})


@app.post("/api/claude/scan/result")
def claude_scan_result():
    b, s = _target()
    body = request.get_json(silent=True) or {}
    step, result = body.get("step"), body.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return jsonify({"error": "result must be the JSON the script returned"}), 400
    fig = s["figma"]
    fig["pending"][step] = result
    nxt = []
    if step == "core":
        nxt = figma_plan.scan_steps_pages(result or {})
        fig["expected"] = [st["id"] for st in nxt]
    else:
        fig["expected"] = [x for x in fig["expected"] if x != step]
    if not fig["expected"]:
        fig["state"], fig["scanned_at"], fig["pending"] = fig["pending"], time.strftime("%Y-%m-%d %H:%M"), {}
        s["plan"] = None
        return jsonify({"done": True, "summary": {k: v for k, v in figma_plan.board_summary(fig["state"]).items() if k not in ("paints", "texts")}})
    return jsonify({"done": False, "steps": nxt, "waiting_for": fig["expected"]})


@app.get("/api/claude/apply")
def claude_apply():
    b, s = _target()
    if not s["plan"]:
        return jsonify({"error": f"Nothing is approved for {b['name']} yet. Review and press Approve in the app first."}), 409
    steps = [{k: v for k, v in st.items() if k != "elements"} for st in s["plan"]["steps"] if st["id"] not in s["results"]]
    return jsonify({"brand": b["name"], "fileKey": b["file_key"], "steps": steps, "skipped": s["plan"]["skipped"],
                    "post_results_to": f"/api/claude/apply/result?brand={b['id']}"})


@app.get("/api/claude/apply/files")
def claude_apply_files():
    b, s = _target()
    step = next((st for st in (s["plan"] or {}).get("steps", []) if st["id"] == request.args.get("step") and st["kind"] == "upload"), None)
    if not step:
        abort(404)
    ext = {"svg": "svg", "png": "png", "jpeg": "jpg", "gif": "gif", "webp": "webp"}
    prefix = figma_plan.UPLOAD_PREFIX + step["id"].replace("-upload", "") + "-"
    return jsonify({"brand": b["id"], "files": [
        {"filename": f"{prefix}{i}.{ext[s['assets'][eid]['format']]}", "mime": s["assets"][eid]["mime"],
         "path": f"/api/brands/{b['id']}/asset/{eid}"} for i, eid in enumerate(step["elements"])]})


@app.post("/api/claude/apply/result")
def claude_apply_result():
    b, s = _target()
    body = request.get_json(silent=True) or {}
    s["results"][body.get("step")] = {"ok": not body.get("error"), "detail": body.get("error") or body.get("result"), "at": time.strftime("%H:%M:%S")}
    left = [st["id"] for st in (s["plan"] or {}).get("steps", []) if st["id"] not in s["results"]]
    return jsonify({"remaining": left})


@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


def main():
    load_env()
    port = int(os.environ.get("PORT", "8765"))
    print(f"Brief Scraper running at http://127.0.0.1:{port}  (Claude pass: {'on' if ai.available() else 'off, no API key'})")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # progress polling floods the terminal otherwise
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
