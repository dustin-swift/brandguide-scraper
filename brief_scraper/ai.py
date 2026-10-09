"""Optional AI pass: names, classifies and interprets what the PDF does not state.

Exact values always come from extract.py. Claude sees one rendered page plus the
elements already recovered from it, and returns (a) better names/types for those
elements and (b) elements only a reader can recover: components, type roles,
effects, usage rules, and anything on a page that is a flat scan.
"""
import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor

import anthropic
import pymupdf

from .extract import CMS, near, slug

MODEL = "claude-opus-5-5"
COMPONENT_CATEGORIES = ["headers", "buttons", "image", "disclaimer", "roundels", "cards", "price-points",
                        "base-plate", "gradients", "other"]
NEW_TYPES = ["color", "gradient", "text-style", "effect", "spacing", "radius", "motion", "component", "guideline"]
RELABEL_TYPES = ["icon", "logo", "image", "graphic", "text-style", "color", "gradient", "font-family"]

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["page_role", "relabel", "elements"],
    "properties": {
        "page_role": {"type": "string"},
        "relabel": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "name", "type", "include"],
            "properties": {"id": {"type": "string"}, "name": {"type": "string"},
                           "type": {"type": "string", "enum": RELABEL_TYPES}, "include": {"type": "boolean"}}}},
        "elements": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["type", "name", "category", "description", "properties", "rules", "values_from", "bbox"],
            "properties": {
                "type": {"type": "string", "enum": NEW_TYPES},
                "name": {"type": "string"},
                "category": {"type": "string"},
                "description": {"type": "string"},
                "properties": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["key", "value"],
                    "properties": {"key": {"type": "string"}, "value": {"type": "string"}}}},
                "rules": {"type": "array", "items": {"type": "string"}},
                "values_from": {"type": "string", "enum": ["text", "visual-estimate"]},
                "bbox": {"type": "array", "items": {"type": "number"}}}}},
    },
}

SYSTEM = f"""You are helping turn a client's brand guidelines PDF into structured design-system data that a \
Figma plugin will ingest. A deterministic parser has already read exact values out of the PDF: labelled colours, \
gradient stops, embedded fonts, vector artwork and embedded images. You are given one page as an image, its text, \
and the elements the parser recovered from that page. Your job is the part that needs a reader.

Return three things.

page_role: a few words on what this page is for (for example "primary colour palette", "button component rules", \
"campaign example layouts").

relabel: for parser elements whose name, type or inclusion you can improve by looking at the page. Vector and \
image assets arrive with a guessed type and often a placeholder name: say whether each is an icon, a logo, an \
image (photography or illustration the brand supplies), or a graphic (decoration, chart parts, page furniture, \
pieces of an example layout), and give it the name the page gives it. Set include to false for anything that is \
not a reusable brand asset. A "text-style" element named "Observed/..." is how the document itself is typeset; \
include it and rename it to a role such as "Type/Heading" only when the page says that style is the brand's \
style for that role. Leave out elements you would not change.

elements: things the parser cannot see. Use these types:
- component: a reusable UI or layout piece the brief defines (button, header, card, disclaimer, roundel, price \
point, pop-up, counter, container and so on). Put its kind in category, one of {", ".join(COMPONENT_CATEGORIES)}. \
List each stated property as a key and value (corner-radius, fill, text-style, alignment, padding, variants).
- text-style: a type role the brief specifies (family, weight, size, tracking, case), when it is stated.
- effect, spacing, radius, motion: tokens the brief specifies.
- color or gradient: only when the page shows one that is missing from the parser elements, which happens on \
scanned or flattened pages. Give hex in properties.
- guideline: a usage rule worth carrying into the design system (clear space, minimum size, do and don't, \
which logo on which background). Put each rule as its own string in rules.

Take values from the page's text wherever the text states them and set values_from to "text". When you read a \
value off the image because nothing states it (a colour sampled by eye, a radius judged from a drawing), set \
values_from to "visual-estimate" so a person can check it. Do not invent values the page does not support, and \
do not repeat an element the parser already has. brand_palette lists every colour the parser read from the \
whole document, with exact values: never return one of those as a new color, and refer to them by those names. \
The same component or type role is often described on several pages; use the same name for it each time so the \
pages can be merged. bbox is the \
element's region on the page image as [x0, y0, x1, y1] fractions of width and height, or an empty array when the \
element has no single region. Pages of pure example layouts, legal text or section dividers usually need nothing: \
return empty arrays for them."""


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _page_png(doc, number, long_edge=1568):
    page = doc[number - 1]
    scale = long_edge / max(page.rect.width, page.rect.height)
    return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")


def _brief(el, page):
    out = {"id": el["id"], "type": el["type"], "name": el["name"]}
    for k in ("hex", "caption", "paths", "pixel_size", "size", "family", "style", "angle", "recurring"):
        if el.get(k) is not None:
            out[k] = el[k]
    if el.get("bbox"):
        out["bbox"] = [round(el["bbox"][0] / page["width"], 3), round(el["bbox"][1] / page["height"], 3),
                       round(el["bbox"][2] / page["width"], 3), round(el["bbox"][3] / page["height"], 3)]
    return out


def _ask(client, png, page, found, palette):
    payload = {"page_number": page["number"], "page_title": page["title"], "page_text": page["text"],
               "brand_palette": palette, "parser_elements": found}
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        # a classifier decline is re-run server-side on Anthropic's recommended fallback model
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        extra_body={"fallbacks": "default"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.standard_b64encode(png).decode()}},
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
        ]}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined this page")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("the response was cut off")
    return json.loads(next(b.text for b in response.content if b.type == "text"))


def enrich(path, result, add_element, progress=lambda done, total, msg: None, workers=4):
    """Run the AI pass in place on an extract() result. Returns a list of per-page problems."""
    client = anthropic.Anthropic(max_retries=4)
    doc = pymupdf.open(path)
    by_id = {e["id"]: e for e in result["elements"]}
    on_page = {p["number"]: [] for p in result["pages"]}
    for e in result["elements"]:
        first = (e["source"]["pages"] or [None])[0]
        if first in on_page and e["type"] != "font-family":
            on_page[first].append(e)
    palette = [{"name": e["name"], "hex": e["hex"]} for e in result["elements"]
               if e["type"] == "color" and e["origin"] == "specified"]
    merged = {}  # (type, key) -> element already added by the AI pass
    pngs = {p["number"]: _page_png(doc, p["number"]) for p in result["pages"]}
    problems, done = [], [0]
    total = len(result["pages"])

    def work(page):
        found = [_brief(e, page) for e in on_page[page["number"]]][:150]
        return page, _ask(client, pngs[page["number"]], page, found, palette)

    fatal = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, p) for p in result["pages"]]
        for fut, page in zip(futures, result["pages"]):
            done[0] += 1
            progress(done[0], total, f"AI pass: page {page['number']} of {total}")
            try:
                _, answer = fut.result()
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                fatal = f"The API key was rejected: {e.message}"
                for f in futures:
                    f.cancel()
                break
            except anthropic.RateLimitError:
                problems.append({"page": page["number"], "error": "rate limited after retries"})
                continue
            except anthropic.APIStatusError as e:
                problems.append({"page": page["number"], "error": f"API error {e.status_code}: {e.message}"})
                continue
            except anthropic.APIConnectionError:
                problems.append({"page": page["number"], "error": "could not reach the API"})
                continue
            except Exception as e:  # a bad page should not sink the whole run
                problems.append({"page": page["number"], "error": str(e)})
                continue
            page["role"] = answer.get("page_role", "")
            for r in answer.get("relabel", []):
                el = by_id.get(r["id"])
                if not el:
                    continue
                asset_types = ("icon", "logo", "image", "graphic")
                if el["type"] in asset_types and r["type"] in asset_types:
                    el["type"] = r["type"]
                    el["cms"] = CMS[r["type"]]
                if r["name"].strip():
                    el["name"] = r["name"].strip()
                el["include"] = r["include"]
                el["ai_reviewed"] = True
            for a in answer.get("elements", []):
                el = {"type": a["type"], "name": a["name"].strip() or a["type"], "description": a["description"],
                      "properties": {p["key"]: p["value"] for p in a["properties"]}, "rules": a["rules"],
                      "include": True, "origin": "interpreted",
                      "confidence": "stated" if a["values_from"] == "text" else "estimated",
                      "section": page["title"], "source": {"pages": [page["number"]], "method": "ai-reading"}}
                if a["type"] == "component":
                    cat = a["category"] if a["category"] in COMPONENT_CATEGORIES else "other"
                    el["category"] = cat
                hx = el["properties"].get("hex")
                if a["type"] == "color" and hx:
                    el["hex"] = hx if hx.startswith("#") else "#" + hx
                if a["type"] == "color":
                    known = [c for c in palette if c["name"].lower() == el["name"].lower()
                             or (el.get("hex") and len(el["hex"]) == 7 and near(c["hex"], el["hex"], 8))]
                    if known or not el.get("hex"):
                        continue
                key = (a["type"], slug(el["name"]))
                if a["type"] == "component" and el["properties"].get("text"):
                    key = (a["type"], el["category"], slug(el["properties"]["text"]))
                if key in merged:
                    first = merged[key]
                    if page["number"] not in first["source"]["pages"]:
                        first["source"]["pages"].append(page["number"])
                    for k, v in el["properties"].items():
                        first["properties"].setdefault(k, v)
                    first["rules"] += [r for r in el["rules"] if r not in first["rules"]]
                    if el["confidence"] == "stated":
                        first["confidence"] = "stated"
                    continue
                merged[key] = el
                asset = None
                bb = a["bbox"]
                if len(bb) == 4 and a["type"] == "component" and bb[2] > bb[0] and bb[3] > bb[1]:
                    pg = doc[page["number"] - 1]
                    clip = pymupdf.Rect(bb[0] * pg.rect.width, bb[1] * pg.rect.height,
                                        bb[2] * pg.rect.width, bb[3] * pg.rect.height) & pg.rect
                    if clip.width > 4 and clip.height > 4:
                        scale = min(2.0, 900 / max(clip.width, clip.height))
                        png = pg.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip).tobytes("png")
                        asset = {"format": "png", "mime": "image/png", "data": png}
                        el["bbox"] = [round(v, 1) for v in clip]
                        el["asset_role"] = "reference-crop"
                added = add_element(el, asset)
                if a["type"] == "component":
                    added["cms"] = ["master-components/" + el["category"]]
    doc.close()
    if fatal:
        raise RuntimeError(fatal)
    return problems


# ---------------------------------------------------------------- other kinds of material

TEXT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["colors", "gradients", "text_styles", "guidelines"],
    "properties": {
        "colors": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["name", "hex"],
                                               "properties": {"name": {"type": "string"}, "hex": {"type": "string"}}}},
        "gradients": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "angle", "stops"],
            "properties": {"name": {"type": "string"}, "angle": {"type": "number"},
                           "stops": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["position", "hex"],
                                                                 "properties": {"position": {"type": "number"}, "hex": {"type": "string"}}}}}}},
        "text_styles": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "family", "weight", "size_px", "italic"],
            "properties": {"name": {"type": "string"}, "family": {"type": "string"}, "weight": {"type": "integer"},
                           "size_px": {"type": "number"}, "italic": {"type": "boolean"}}}},
        "guidelines": {"type": "array", "items": {"type": "string"}},
    },
}

TEXT_SYSTEM = """You extract brand design-system values from text a client supplied: a design.md or tokens file, \
notes, or the text and CSS of a website. Return only what the text states.

colors: each named brand colour with its hex as #RRGGBB. Use the name the text gives; for a CSS variable, turn \
the variable name into a readable name. Leave out greys used only for borders or shadows on a website, and leave \
out pure white and black unless the text presents them as brand colours.
gradients: each gradient with its CSS angle in degrees (90 is left to right) and its stops, position from 0 to 1.
text_styles: each type role that has a font family and a fixed size in pixels. Weight is 100 to 900. Skip roles \
whose size is not stated as a fixed number.
guidelines: short usage rules worth keeping.

Do not invent values. Return empty arrays for anything the text does not contain."""

IMAGE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["colors", "fonts", "summary"],
    "properties": {
        "colors": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["name", "hex"],
                                               "properties": {"name": {"type": "string"}, "hex": {"type": "string"}}}},
        "fonts": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}

IMAGE_SYSTEM = """You are shown a finished piece of brand work such as an ad or a layout. It is evidence of how \
the brand is used, not a specification. List the main brand colours you see, at most six, each with a short \
descriptive name and your best estimate of its hex as #RRGGBB. Ignore colours that belong to photography. In \
fonts, describe the typefaces in plain terms (for example "rounded geometric sans, bold, uppercase headline"); \
name a typeface only if you can identify it confidently. In summary, say in one sentence what the piece is."""


def _structured(system, content, schema, effort="low"):
    client = anthropic.Anthropic(max_retries=4)
    response = client.messages.create(
        model=MODEL, max_tokens=16000, system=system,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"}, extra_body={"fallbacks": "default"},
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError("the model declined this material" if response.stop_reason == "refusal" else "the response was cut off")
    return json.loads(next(b.text for b in response.content if b.type == "text"))


def read_text(text, label):
    """Colours, gradients, type roles and rules stated in a text source."""
    return _structured(TEXT_SYSTEM, [{"type": "text", "text": f"Source: {label}\n\n{text[:180_000]}"}], TEXT_SCHEMA)


def read_reference_image(data, mime):
    """Estimated colours and type from an ad or layout. Everything it returns is an estimate."""
    return _structured(IMAGE_SYSTEM, [
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.standard_b64encode(data).decode()}},
        {"type": "text", "text": "Describe the brand colours and type in this piece."}], IMAGE_SCHEMA)
