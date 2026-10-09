"""Write the reviewed elements out as one ingestible Markdown file (schema dsbrief/1)."""
import base64
import json
import re
from collections import Counter
from datetime import datetime, timezone

from .extract import CMS, slug

SCHEMA = "dsbrief/1"
SECTIONS = [("color", "Colours"), ("gradient", "Gradients"), ("font-family", "Font families"),
            ("text-style", "Text styles"), ("effect", "Effects"), ("spacing", "Spacing"), ("radius", "Radii"),
            ("motion", "Motion"), ("logo", "Logos"), ("icon", "Icons"), ("image", "Images"),
            ("graphic", "Graphics"), ("component", "Components"), ("guideline", "Guidelines")]
EXPECTED = {"color": "colours", "gradient": "gradients", "font-family": "font families",
            "text-style": "named text styles", "effect": "shadows and effects", "spacing": "spacing values",
            "motion": "motion", "logo": "logos", "icon": "icons", "image": "images", "component": "components"}
INTERNAL = {"include", "asset", "sig", "ai_reviewed", "sections", "chars", "uses"}


def _dump(body):
    """Pretty JSON, with short lists of numbers kept on one line."""
    text = json.dumps(body, indent=2, ensure_ascii=False)
    return re.sub(r"\[\s*((?:-?[\d.]+,\s*)*-?[\d.]+)\s*\]", lambda m: "[" + re.sub(r"\s+", " ", m.group(1)) + "]", text)


def _wrap(s, width=120):
    return "\n".join(s[i:i + width] for i in range(0, len(s), width))


def apply_edits(elements, edits):
    """Return the included elements with the reviewer's names and types, and fresh unique ids."""
    out, seen = [], Counter()
    for el in elements:
        e = dict(el)
        ed = edits.get(el["id"], {})
        if "include" in ed:
            e["include"] = bool(ed["include"])
        if ed.get("name", "").strip():
            e["name"] = ed["name"].strip()
        if ed.get("type") in ("icon", "logo", "image", "graphic") and e["type"] in ("icon", "logo", "image", "graphic"):
            e["type"] = ed["type"]
            e["cms"] = CMS[e["type"]]
        if not e["include"]:
            continue
        e["_source_id"] = el["id"]
        base = f"{e['type']}.{slug(e['name'])}"
        seen[base] += 1
        e["id"] = base if seen[base] == 1 else f"{base}-{seen[base]}"
        out.append(e)
    return out


PART_LIMIT = 950_000  # bytes; the plugin accepts .md files under 1 MB


def _size(text):
    return len(text.encode("utf-8"))


def _pack(blocks, limit):
    """Greedy-fill blocks (already-rendered strings) into groups that each stay under limit."""
    groups, cur, used = [], [], 0
    for b in blocks:
        n = _size(b) + 1
        if cur and used + n > limit:
            groups.append(cur)
            cur, used = [], 0
        cur.append(b)
        used += n
    if cur:
        groups.append(cur)
    return groups


def build_parts(result, assets, edits, source_name, limit=PART_LIMIT, asset_mode="blocks"):
    """Render the brief as one or more .md texts, each under `limit` bytes.

    Data parts come first: every element's JSON, with SVG artwork inline. Raster images follow in
    asset parts. An element's `asset.part` says which part holds its file. Joining all parts with a
    newline gives a document the single-file parser reads unchanged.

    asset_mode "data-url" instead writes each file as a `data:` address in the element's own
    `asset.url`, for a consumer that expects a fetchable URL and cannot follow references.
    """
    inline = asset_mode == "data-url"
    els = apply_edits(result["elements"], edits)
    counts = Counter(e["type"] for e in els)
    stamp = datetime.now(timezone.utc)
    bundle = stamp.strftime("%Y%m%dT%H%M%SZ")
    room = limit - 2500  # leave space for each part's own header

    rasters, omitted = [], []
    for e in els:
        a = assets.get(e["_source_id"])
        if not a or a["format"] == "svg" or inline:
            continue
        block = "\n".join([f"```base64 asset={e['id']} mime={a['mime']}",
                           _wrap(base64.standard_b64encode(a["data"]).decode()), "```", ""])
        if _size(block) > room:
            omitted.append(e["id"])
        else:
            rasters.append((e["id"], block))

    def data_blocks(part_of):
        out = []
        for t, title in SECTIONS:
            group = [e for e in els if e["type"] == t]
            for n, e in enumerate(group):
                body = {k: e[k] for k in ("id", "type", "name", "cms") if e.get(k)}
                body.update({k: v for k, v in e.items() if k not in INTERNAL and not k.startswith("_") and v not in (None, "", [], {})})
                a = assets.get(e["_source_id"])
                svg = a and a["format"] == "svg"
                if a and inline:
                    raw = a["data"].encode("utf-8") if svg else a["data"]
                    body["asset"] = {"url": f"data:{a['mime']};base64," + base64.standard_b64encode(raw).decode(),
                                     "format": a["format"], "mime": a["mime"]}
                    svg = False
                    if _size(_dump(body)) > room:
                        if e["id"] not in omitted:
                            omitted.append(e["id"])
                        continue
                elif a and (svg or e["id"] in part_of):
                    body["asset"] = {"ref": e["id"], "format": a["format"], "mime": a["mime"]}
                    if not svg:
                        body["asset"]["part"] = part_of[e["id"]]
                lines = [f"## {title} ({len(group)})", ""] if n == 0 else []
                lines += [f"### {e['id']}", "", "```json element", _dump(body), "```", ""]
                if svg:
                    lines += [f"```svg asset={e['id']}", a["data"], "```", ""]
                out.append("\n".join(lines))
        notes = []
        missing = [label for t, label in EXPECTED.items() if not counts.get(t)]
        if missing:
            notes.append("\n".join(["## Not found in this brief", ""] + [f"- {m}" for m in missing] + [""]))
        if omitted:
            notes.append("\n".join(["## Images left out", "", "Too large for one part even alone; lower the image size and export again.", ""]
                                   + [f"- {i}" for i in omitted] + [""]))
        return notes + out

    # part numbers barely change the JSON size, so sizing with placeholders gives the final layout
    n_data = len(_pack(data_blocks({i: 99 for i, _ in rasters}), room))
    raster_groups = _pack([b for _, b in rasters], room)
    part_of, k = {}, 0
    for g, group in enumerate(raster_groups):
        for _ in group:
            part_of[rasters[k][0]] = n_data + g + 1
            k += 1
    groups = _pack(data_blocks(part_of), room) + raster_groups
    total = len(groups)

    parts = []
    for n, group in enumerate(groups, 1):
        kind = "data" if n <= n_data else "assets"
        head = ["---", f"schema: {SCHEMA}", f"bundle: {bundle}", f"part: {n}", f"parts: {total}", f"part_kind: {kind}",
                f"source_file: {json.dumps(source_name)}", f"generated: {stamp.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                f"pages: {result['meta']['pages']}", f"elements: {len(els)}", f"counts: {json.dumps(dict(counts))}",
                "---", "", f"# Design system brief: {source_name} (part {n} of {total})", ""]
        if n == 1 and inline:
            head += ["Every element is one `json element` block. An element with artwork carries the file itself as a",
                     "`data:` address in `asset.url`. `confidence` is `exact` (read from the PDF's own data), `approximate`",
                     "(converted, for example from CMYK), `stated` (read from the brief's wording) or `estimated` (judged",
                     "from the page image).", ""]
        elif n == 1:
            head += ["Every element is one `json element` block. SVG artwork follows its element in an `svg asset=<id>`",
                     "block. Raster images live in later parts as `base64 asset=<id> mime=<type>` blocks; the element's",
                     "`asset.part` says which part. Load every part with the same `bundle` before resolving assets.",
                     "`confidence` is `exact` (read from the PDF's own data), `approximate` (converted, for example from",
                     "CMYK), `stated` (read from the brief's wording) or `estimated` (judged from the page image).", ""]
        parts.append("\n".join(head + group))
    return parts
