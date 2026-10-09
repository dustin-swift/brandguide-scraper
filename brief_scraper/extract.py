"""Deterministic extraction: reads exact values out of the PDF's own objects.

Nothing here guesses from pixels. Whatever the PDF does not state (role names,
component specs, anything on a scanned page) is left to the optional AI pass.
"""
import hashlib
import re
import statistics
import unicodedata
from collections import Counter, defaultdict

import pymupdf

from .gradients import page_gradients

CMS = {
    "color": ["master-tokens/colours", "variables", "styles"],
    "gradient": ["master-tokens/colours", "styles", "master-components/gradients"],
    "font-family": ["master-tokens/typography"],
    "text-style": ["master-tokens/typography", "styles"],
    "effect": ["master-tokens/shadow", "styles"],
    "spacing": ["variables"],
    "radius": ["variables"],
    "motion": ["master-tokens/motion"],
    "logo": ["master-tokens/logos"],
    "icon": ["master-tokens/icons"],
    "image": ["master-tokens/visuals"],
    "graphic": ["master-tokens/visuals"],
    "component": ["master-components"],
    "guideline": [],
}

_WEIGHTS = [("extralight", 200), ("ultralight", 200), ("thin", 100), ("hairline", 100), ("semibold", 600),
            ("demibold", 600), ("extrabold", 800), ("ultrabold", 800), ("heavy", 800), ("black", 900),
            ("bold", 700), ("medium", 500), ("light", 300), ("book", 400), ("regular", 400), ("roman", 400)]
_KEY = re.compile(r"^(HEX|RGB|CMYK|PMS|PANTONE)\b[\s:#]*(.*)$", re.I)
_HEX = re.compile(r"^#?([0-9A-Fa-f]{6})$")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unnamed"


def hex_of(rgb):
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(c * 255))) for c in rgb[:3])


def int_hex(v):
    return "#%06X" % v


def rgb_of(hx):
    hx = hx.lstrip("#")
    return [int(hx[i:i + 2], 16) for i in (0, 2, 4)]


def near(a, b, tol=6):
    return max(abs(x - y) for x, y in zip(rgb_of(a), rgb_of(b))) <= tol


def parse_font(ps):
    ps = re.sub(r"^[A-Z]{6}\+", "", ps)
    ps = re.sub(r"(PSMT|PS|MT)$", "", ps)
    fam, _, sty = ps.replace(",", "-").partition("-")
    sty = re.sub(r"(PSMT|PS|MT)$", "", sty)
    split = lambda s: re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s).strip()
    style = split(sty) or "Regular"
    low = style.lower().replace(" ", "")
    weight = next((w for k, w in _WEIGHTS if k in low), 400)
    return {"family": split(fam), "style": style, "weight": weight,
            "italic": "italic" in low or "oblique" in low}


# --------------------------------------------------------------------------- text

def page_lines(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            spans = [s for s in l["spans"] if s["text"].strip()]
            if not spans:
                continue
            big = max(spans, key=lambda s: len(s["text"]))
            out.append({"text": unicodedata.normalize("NFKC", "".join(s["text"] for s in l["spans"])).strip(),
                        "bbox": tuple(l["bbox"]), "size": big["size"], "font": big["font"], "color": big["color"],
                        "flags": big["flags"], "fonts": [(s["font"], s["flags"], len(s["text"].strip())) for s in spans]})
    return out


def _is_furniture(text, repeated, number):
    return text in repeated or text == str(number)


def _value_for(key, text):
    if key == "hex":
        m = _HEX.match(text.replace(" ", ""))
        return "#" + m.group(1).upper() if m else None
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)]
    if key == "rgb":
        return [int(n) for n in nums] if len(nums) == 3 and all(n <= 255 for n in nums) else None
    if key == "cmyk":
        return nums if len(nums) == 4 and all(n <= 100 for n in nums) else None
    return text.strip() or None  # pantone


def _parse_colour_group(lines):
    """Turn one column of label lines (name / HEX / value / CMYK / value ...) into specs."""
    specs, cur, pending = [], None, None

    def flush():
        if cur and any(k in cur for k in ("hex", "rgb", "cmyk")):
            specs.append(cur)

    for ln in lines:
        text = ln["text"].strip()
        m = _KEY.match(text)
        if m:
            key = {"hex": "hex", "rgb": "rgb", "cmyk": "cmyk"}.get(m.group(1).lower(), "pantone")
            if cur is None:
                cur = {"name": None, "line": ln}
            pending = key
            if m.group(2).strip():
                val = _value_for(key, m.group(2))
                if val is not None:
                    cur[key], pending = val, None
            continue
        if pending and cur is not None:
            val = _value_for(pending, text)
            if val is not None:
                cur[pending], pending = val, None
                continue
        if text.startswith("#") and _HEX.match(text):
            if cur is None:
                cur = {"name": None, "line": ln}
            cur["hex"] = "#" + text[1:].upper()
            continue
        if len(text) <= 40 and len(text.split()) <= 5:  # a name starts a new spec
            flush()
            cur, pending = {"name": text, "line": ln}, None
    flush()
    return specs


def page_colour_specs(lines, W):
    groups = []
    for ln in sorted(lines, key=lambda l: (l["bbox"][1], l["bbox"][0])):
        x0, y0, _, y1 = ln["bbox"]
        home = None
        for g in groups:
            if abs(g["x0"] - x0) < 0.02 * W and -3 <= y0 - g["y1"] < 1.6 * ln["size"]:
                home = g
                break
        if home:
            home["lines"].append(ln)
            home["y1"] = y1
        else:
            groups.append({"x0": x0, "y1": y1, "lines": [ln]})
    specs = []
    for g in groups:
        joined = " ".join(l["text"] for l in g["lines"])
        if re.search(r"\b(HEX|RGB|CMYK|PMS|PANTONE)\b", joined, re.I) or re.search(r"#[0-9A-Fa-f]{6}\b", joined):
            specs.extend(_parse_colour_group(g["lines"]))
    return specs


def _swatch_under(drawings, bbox):
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    best = None
    for d in drawings:
        r = d["rect"]
        if d.get("fill") and r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1:
            if best is None or r.get_area() < best["rect"].get_area():
                best = d
    return hex_of(best["fill"]) if best else None


# ------------------------------------------------------------------------ vectors

def _f(v):
    return ("%.2f" % v).rstrip("0").rstrip(".")


def _svg_path(d, ox, oy):
    parts, cur = [], None
    pt = lambda p: f"{_f(p.x - ox)} {_f(p.y - oy)}"
    same = lambda a, b: a is not None and abs(a.x - b.x) < 0.01 and abs(a.y - b.y) < 0.01
    for it in d["items"]:
        k = it[0]
        if k == "l":
            if not same(cur, it[1]):
                parts.append("M" + pt(it[1]))
            parts.append("L" + pt(it[2]))
            cur = it[2]
        elif k == "c":
            if not same(cur, it[1]):
                parts.append("M" + pt(it[1]))
            parts.append(f"C{pt(it[2])} {pt(it[3])} {pt(it[4])}")
            cur = it[4]
        elif k == "re":
            r = it[1]
            parts.append(f"M{_f(r.x0 - ox)} {_f(r.y0 - oy)}H{_f(r.x1 - ox)}V{_f(r.y1 - oy)}H{_f(r.x0 - ox)}Z")
            cur = None
        elif k == "qu":
            q = it[1]
            parts.append(f"M{pt(q.ul)}L{pt(q.ur)}L{pt(q.lr)}L{pt(q.ll)}Z")
            cur = None
    if d.get("closePath") and parts and not parts[-1].endswith("Z"):
        parts.append("Z")
    attrs = [f'd="{"".join(parts)}"']
    if d["type"] in ("f", "fs") and d.get("fill"):
        attrs.append(f'fill="{hex_of(d["fill"])}"')
        if d.get("even_odd"):
            attrs.append('fill-rule="evenodd"')
        if (d.get("fill_opacity") or 1) < 1:
            attrs.append(f'fill-opacity="{_f(d["fill_opacity"])}"')
    else:
        attrs.append('fill="none"')
    if d["type"] in ("s", "fs") and d.get("color"):
        attrs.append(f'stroke="{hex_of(d["color"])}" stroke-width="{_f(d.get("width") or 1)}"')
        cap = d.get("lineCap")
        cap = cap[0] if isinstance(cap, (tuple, list)) else cap
        if cap in (1, 2):
            attrs.append('stroke-linecap="%s"' % ("round" if cap == 1 else "square"))
        if d.get("lineJoin") in (1, 2):
            attrs.append('stroke-linejoin="%s"' % ("round" if d["lineJoin"] == 1 else "bevel"))
        if (d.get("stroke_opacity") or 1) < 1:
            attrs.append(f'stroke-opacity="{_f(d["stroke_opacity"])}"')
    return "<path " + " ".join(attrs) + "/>"


def page_vector_clusters(page, lines):
    W, area = page.rect.width, page.rect.get_area()
    gap = 0.006 * W
    try:
        drawings = page.get_drawings()
    except Exception:
        return [], []
    if len(drawings) > 4000:
        return [], drawings
    keep = []
    for d in drawings:
        r = d["rect"]
        if d.get("fill") is None and d.get("color") is None:
            continue
        if not page.rect.intersects(r) and not (r.width == 0 or r.height == 0):
            continue
        if r.width * r.height > 0.15 * area:
            continue
        single = len(d["items"]) == 1 and d["items"][0][0] in ("re", "l")
        if single and min(r.width, r.height) > 0.03 * W:
            continue  # a panel or swatch, not artwork
        if max(r.width, r.height) > 0.12 * W and min(r.width, r.height) < 3:
            continue  # a rule
        keep.append(d)
    keep.sort(key=lambda d: d["rect"].x0)
    parent = list(range(len(keep)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(keep):
        ra = a["rect"]
        for j in range(i + 1, len(keep)):
            rb = keep[j]["rect"]
            if rb.x0 > ra.x1 + gap:
                break
            if rb.y0 <= ra.y1 + gap and ra.y0 <= rb.y1 + gap:
                parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i, d in enumerate(keep):
        groups[find(i)].append(d)
    clusters = []
    for ds in groups.values():
        x0, y0 = min(d["rect"].x0 for d in ds), min(d["rect"].y0 for d in ds)
        x1, y1 = max(d["rect"].x1 for d in ds), max(d["rect"].y1 for d in ds)
        w, h = x1 - x0, y1 - y0
        if max(w, h) < 0.008 * W or max(w, h) > 0.5 * W or min(w, h) < 2 or len(ds) > 600:
            continue
        if len(ds) == 1 and len(ds[0]["items"]) <= 1:
            continue
        pad = max([(d.get("width") or 0) / 2 for d in ds if d["type"] in ("s", "fs")] + [0])
        ds.sort(key=lambda d: d.get("seqno", 0))
        body = "".join(_svg_path(d, x0 - pad, y0 - pad) for d in ds)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_f(w + 2 * pad)} {_f(h + 2 * pad)}" '
               f'width="{_f(w + 2 * pad)}" height="{_f(h + 2 * pad)}">{body}</svg>')
        if len(svg) > 400_000:
            continue
        colours = sorted({hex_of(c) for d in ds for c in (d.get("fill"), d.get("color")) if c})
        caption = None
        for ln in lines:
            lx0, ly0, lx1, _ = ln["bbox"]
            if len(ln["text"]) <= 40 and y1 - 3 <= ly0 <= y1 + 0.035 * W and x0 - 0.3 * w <= (lx0 + lx1) / 2 <= x1 + 0.3 * w:
                if caption is None or ly0 < caption["bbox"][1]:
                    caption = ln
        clusters.append({"bbox": [round(v, 1) for v in (x0, y0, x1, y1)], "svg": svg, "paths": len(ds),
                         "colors": colours, "caption": caption["text"] if caption else None,
                         "sig": (round(w), round(h), len(ds), tuple(colours), hashlib.sha1(body.encode()).hexdigest())})
    clusters.sort(key=lambda c: (round(c["bbox"][1] / (0.03 * W)), c["bbox"][0]))
    return clusters, drawings


# ------------------------------------------------------------------------- raster

def export_image(doc, xref, max_edge):
    info = doc.extract_image(xref)
    pix = pymupdf.Pixmap(doc, xref)
    if pix.colorspace is None or pix.colorspace.n != 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if info.get("smask"):
        mask = pymupdf.Pixmap(doc, info["smask"])
        if (mask.width, mask.height) != (pix.width, pix.height):
            mask = pymupdf.Pixmap(mask, pix.width, pix.height, None)
        if mask.n == 1 and not mask.alpha:
            pix = pymupdf.Pixmap(pix, mask)
    edge = max(pix.width, pix.height)
    if max_edge and edge > max_edge:
        scale = max_edge / edge
        pix = pymupdf.Pixmap(pix, max(1, round(pix.width * scale)), max(1, round(pix.height * scale)), None)
    if pix.alpha:
        return "png", "image/png", pix.tobytes("png"), pix.width, pix.height
    return "jpeg", "image/jpeg", pix.tobytes("jpeg", jpg_quality=85), pix.width, pix.height


# --------------------------------------------------------------------------- main

def extract(path, max_image_edge=1024, progress=lambda done, total, msg: None):
    doc = pymupdf.open(path)
    n_pages = len(doc)
    raw = []
    for i, page in enumerate(doc):
        raw.append({"lines": page_lines(page), "W": page.rect.width, "H": page.rect.height})
    counts = Counter(t for p in raw for t in {l["text"] for l in p["lines"]})
    repeated = {t for t, c in counts.items() if c >= max(3, 0.5 * n_pages)}

    # the heading style used on most pages identifies each page's section title
    tops = []
    for k, p in enumerate(raw):
        cand = [l for l in p["lines"] if l["bbox"][1] < 0.35 * p["H"] and not _is_furniture(l["text"], repeated, k + 1)]
        tops.append(max(cand, key=lambda l: l["size"]) if cand else None)
    style_votes = Counter((t["font"], round(t["size"])) for t in tops if t)
    title_style = style_votes.most_common(1)[0][0] if style_votes else None

    def title_of(k):
        p = raw[k]
        hit = [l["text"] for l in p["lines"] if (l["font"], round(l["size"])) == title_style]
        if hit:
            return " ".join(hit[:4])
        return tops[k]["text"] if tops[k] else ""

    embedded = set()
    for page in doc:
        for f in page.get_fonts():
            embedded.add(re.sub(r"^[A-Z]{6}\+", "", f[3]))
    resolved = {}

    def resolve_font(name, flags=0):
        """Span font names can arrive truncated; narrow the embedded candidates by the span's flags."""
        if (name, flags) in resolved:
            return resolved[(name, flags)]
        hits = [name] if name in embedded else [b for b in embedded if b.startswith(name)]
        if len(hits) > 1:
            for test in (lambda f: f["italic"] == bool(flags & 2), lambda f: (f["weight"] >= 600) == bool(flags & 16)):
                narrowed = [b for b in hits if test(parse_font(b))]
                hits = narrowed or hits
        out = parse_font(hits[0] if hits else name)
        if len(hits) > 1:  # same family, weight not recoverable
            out = dict(out, style="Unresolved weight", ambiguous=True)
        resolved[(name, flags)] = out
        return out

    elements, assets, pages = [], {}, []
    ids = Counter()

    def add(el, asset=None):
        base = f"{el['type']}.{slug(el['name'])}"
        ids[base] += 1
        el["id"] = base if ids[base] == 1 else f"{base}-{ids[base]}"
        el["cms"] = CMS.get(el["type"], [])
        if asset:
            assets[el["id"]] = asset
            el["asset"] = {"format": asset["format"], "mime": asset["mime"],
                           "bytes": len(asset["data"]) if isinstance(asset["data"], (bytes, str)) else 0}
        elements.append(el)
        return el

    colours, gradients = {}, {}
    font_use = defaultdict(lambda: {"chars": 0, "pages": set()})
    style_use = defaultdict(lambda: {"chars": 0, "pages": set(), "sample": "", "leading": []})
    fills = Counter()
    vec_seen, img_seen = {}, {}
    all_text = []

    for k, page in enumerate(doc):
        num = k + 1
        progress(k, n_pages, f"Reading page {num} of {n_pages}")
        p = raw[k]
        W = p["W"]
        lines = [l for l in p["lines"] if not _is_furniture(l["text"], repeated, num)]
        section = title_of(k)
        low = section.lower()
        pages.append({"number": num, "title": section, "width": W, "height": p["H"],
                      "text": "\n".join(l["text"] for l in lines)})
        all_text.append(pages[-1]["text"])

        clusters, drawings = page_vector_clusters(page, lines)
        for d in drawings:
            if d.get("fill") and d["rect"].width * d["rect"].height > 0.0005 * W * p["H"]:
                fills[hex_of(d["fill"])] += 1

        # colours stated as text labels
        for spec in page_colour_specs(lines, W):
            hx, conf = spec.get("hex"), "exact"
            if not hx and spec.get("rgb"):
                hx = "#%02X%02X%02X" % tuple(spec["rgb"])
            if not hx and spec.get("cmyk"):
                c, m, y, kk = [v / 100 for v in spec["cmyk"]]
                hx, conf = hex_of(((1 - c) * (1 - kk), (1 - m) * (1 - kk), (1 - y) * (1 - kk))), "approximate"
            if not hx:
                continue
            swatch = _swatch_under(drawings, spec["line"]["bbox"])
            name = spec["name"] or hx
            key = (name.lower(), hx)
            if key in colours:
                c = colours[key]
                if num not in c["source"]["pages"]:
                    c["source"]["pages"].append(num)
                if section and section not in c["sections"]:
                    c["sections"].append(section)
                continue
            el = {"type": "color", "name": name, "hex": hx, "rgb": spec.get("rgb") or rgb_of(hx),
                  "include": True, "origin": "specified", "confidence": conf, "section": section,
                  "sections": [section] if section else [], "source": {"pages": [num], "method": "text-label"}}
            for extra in ("cmyk", "pantone"):
                if spec.get(extra):
                    el[extra] = spec[extra]
            if swatch:
                el["swatch_hex"] = swatch
                el["swatch_matches"] = near(swatch, hx, 10)
            colours[key] = add(el)

        # gradients
        for g in page_gradients(doc, page):
            key = (g["kind"], tuple((round(s["position"], 2), s["hex"]) for s in g["stops"]),
                   None if g["angle"] is None else round(g["angle"] / 5))
            if key in gradients:
                if num not in gradients[key]["source"]["pages"]:
                    gradients[key]["source"]["pages"].append(num)
                continue
            gradients[key] = {"type": "gradient", "name": "", "kind": g["kind"], "angle": g["angle"],
                              "stops": g["stops"], "handles": g["handles"], "bbox": g["bbox"],
                              "source_colorspace": g["source_colorspace"], "section": section,
                              "origin": "specified", "confidence": "exact" if g["source_colorspace"] == "rgb" else "approximate",
                              "source": {"pages": [num], "method": "pdf-shading"}}

        # typography actually set in the document
        blocks = defaultdict(list)
        for l in lines:
            for span_font, flags, chars in l["fonts"]:
                fam = resolve_font(span_font, flags)["family"]
                font_use[fam]["chars"] += chars
                font_use[fam]["pages"].add(num)
            f = resolve_font(l["font"], l["flags"])
            font = (f["family"], f["style"], f["weight"], f["italic"])
            sk = (font, round(l["size"] * 2) / 2, int_hex(l["color"]))
            s = style_use[sk]
            s["chars"] += len(l["text"])
            s["pages"].add(num)
            if len(l["text"]) > len(s["sample"]) and len(l["text"]) < 90:
                s["sample"] = l["text"]
            blocks[(sk, round(l["bbox"][0]))].append(l["bbox"][1])
        for (sk, _), ys in blocks.items():
            ys.sort()
            for a, b in zip(ys, ys[1:]):
                if 0.8 * sk[1] < b - a < 2.2 * sk[1]:
                    style_use[sk]["leading"].append((b - a) / sk[1])

        # vector artwork
        for c in clusters:
            if c["sig"] in vec_seen:
                vec_seen[c["sig"]]["source"]["pages"].append(num)
                continue
            vec_seen[c["sig"]] = {"type": "graphic", "name": c["caption"] or "", "caption": c["caption"],
                                  "section": section, "bbox": c["bbox"], "paths": c["paths"], "colors": c["colors"],
                                  "origin": "specified", "confidence": "exact",
                                  "source": {"pages": [num], "method": "vector-paths"}, "_svg": c["svg"]}

        # raster images
        for info in page.get_image_info(xrefs=True):
            xref, bb = info["xref"], info["bbox"]
            if not xref or min(info["width"], info["height"]) < 48 or max(bb[2] - bb[0], bb[3] - bb[1]) < 0.03 * W:
                continue
            if xref in img_seen:
                if num not in img_seen[xref]["source"]["pages"]:
                    img_seen[xref]["source"]["pages"].append(num)
                continue
            img_seen[xref] = {"type": "image", "name": "", "section": section, "bbox": [round(v, 1) for v in bb],
                              "pixel_size": [info["width"], info["height"]], "origin": "specified",
                              "confidence": "exact", "source": {"pages": [num], "method": "embedded-image"},
                              "_xref": xref}

    progress(n_pages, n_pages, "Assembling elements")
    corpus = " ".join(all_text).lower()
    named = [c for c in colours.values()]

    def colour_name(hx):
        return next((c["name"] for c in named if near(c["hex"], hx)), None)

    for g in gradients.values():
        labels = [colour_name(s["hex"]) for s in g["stops"]]
        for s, lab in zip(g["stops"], labels):
            if lab:
                s["color"] = lab
        g["name"] = " to ".join(lab or s["hex"] for s, lab in zip(g["stops"], labels))
        g["include"] = all(labels) or "gradient" in g["section"].lower()
        g["css"] = ("linear-gradient(%sdeg, %s)" % (("%g" % g["angle"]), ", ".join(
            f"{s['hex']} {s['position'] * 100:g}%" for s in g["stops"]))) if g["kind"] == "linear" else (
            "radial-gradient(circle, %s)" % ", ".join(f"{s['hex']} {s['position'] * 100:g}%" for s in g["stops"]))
        add(g)

    total_chars = sum(v["chars"] for v in font_use.values()) or 1
    family_styles = defaultdict(list)
    for base in sorted(embedded):
        f = parse_font(base)
        family_styles[f["family"]].append({"style": f["style"], "weight": f["weight"], "italic": f["italic"], "postscript": base})
    for name, styles in sorted(family_styles.items(), key=lambda kv: -font_use[kv[0]]["chars"]):
        use = font_use[name]
        mentioned = name.lower() in corpus
        share = use["chars"] / total_chars
        add({"type": "font-family", "name": name, "family": name,
             "styles": sorted(styles, key=lambda s: (s["weight"], s["italic"])),
             "usage_share": round(share, 4), "named_in_text": mentioned,
             "include": mentioned or share >= 0.01, "origin": "specified" if mentioned else "observed",
             "confidence": "exact", "section": "", "source": {"pages": sorted(use["pages"])[:12], "method": "embedded-font"}})

    ranked = sorted(style_use.items(), key=lambda kv: -kv[1]["chars"])
    for (font, size, colour), use in [r for r in ranked if r[1]["chars"] >= 40 and r[0][1] >= 6][:12]:
        f = dict(zip(("family", "style", "weight", "italic"), font))
        lead = round(statistics.median(use["leading"]) * 100) if use["leading"] else None
        add({"type": "text-style", "name": f"Observed/{size:g} {f['family']} {f['style']}", "family": f["family"],
             "style": f["style"], "weight": f["weight"], "italic": f["italic"], "size": size,
             "line_height_percent": lead, "color": colour, "color_name": colour_name(colour), "sample": use["sample"],
             "chars": use["chars"], "include": False, "origin": "observed", "confidence": "exact", "section": "",
             "note": "How the brief itself is typeset; not a style the brief specifies.",
             "source": {"pages": sorted(use["pages"])[:12], "method": "text-spans"}})

    extra = [(hx, n) for hx, n in fills.most_common(40) if not colour_name(hx) and hx not in ("#FFFFFF", "#000000")][:10]
    for hx, n in extra:
        add({"type": "color", "name": f"Observed {hx}", "hex": hx, "rgb": rgb_of(hx), "include": False,
             "origin": "observed", "confidence": "exact", "section": "", "uses": n,
             "note": "Used as a fill in the document but never labelled.",
             "source": {"pages": [], "method": "vector-fill"}})

    recurring = max(3, 0.4 * n_pages)
    page_w = raw[0]["W"] if raw else 1
    seq = Counter()

    def classify(el):
        low = el["section"].lower()
        rec = len(el["source"]["pages"]) >= recurring
        el["recurring"] = rec
        w, h = el["bbox"][2] - el["bbox"][0], el["bbox"][3] - el["bbox"][1]
        if el["type"] == "graphic" and h < 0.012 * page_w and w > 1.8 * h:
            el["note"] = "Looks like text converted to outlines (a caption or label), not artwork."
            kind = "graphic"
        elif "icon" in low:
            kind = "icon"
        elif "logo" in low or "lockup" in low or rec:
            kind = "logo"
        elif el["type"] == "image":
            kind = "image"
        else:
            kind = "graphic"
        return kind

    for el in vec_seen.values():
        el["type"] = classify(el)
        el["include"] = el["type"] in ("icon", "logo")
        if not el["name"]:
            seq[(el["type"], el["section"])] += 1
            el["name"] = f"{el['section'] or el['type'].title()} {seq[(el['type'], el['section'])]:02d}"
        svg = el.pop("_svg")
        el["source"]["pages"] = el["source"]["pages"][:12]
        add(el, {"format": "svg", "mime": "image/svg+xml", "data": svg})

    for el in img_seen.values():
        xref = el.pop("_xref")
        try:
            fmt, mime, data, w, h = export_image(doc, xref, max_image_edge)
        except Exception:
            continue
        el["type"] = classify(el)
        low = el["section"].lower()
        el["include"] = el["type"] == "logo" or any(w_ in low for w_ in ("image", "photo", "visual", "library"))
        el["exported_size"] = [w, h]
        seq[(el["type"], el["section"])] += 1
        el["name"] = f"{el['section'] or el['type'].title()} {seq[(el['type'], el['section'])]:02d}"
        el["source"]["pages"] = el["source"]["pages"][:12]
        add(el, {"format": fmt, "mime": mime, "data": data})

    text_chars = sum(len(t) for t in all_text)
    meta = {"pages": n_pages, "producer": doc.metadata.get("creator") or doc.metadata.get("producer") or "",
            "text_chars": text_chars, "looks_scanned": text_chars < 40 * n_pages,
            "page_size": [raw[0]["W"], raw[0]["H"]] if raw else None}
    doc.close()
    return {"meta": meta, "elements": elements, "assets": assets, "pages": pages}
