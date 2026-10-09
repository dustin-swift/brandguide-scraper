"""Recover gradient definitions (stops, angle, placement) from a PDF page.

get_drawings() does not report shadings, so this walks the page's content
stream (and nested form XObjects) tracking the transform and clip in force at
each `sh` operator, then reads the stops from the shading's function objects.
"""
import math
import re

import pymupdf

_WS = b" \t\r\n\x0c\x00"
_DELIM = b"()<>[]{}/%"
_NUM = re.compile(r"-?\d*\.?\d+(?:[eE][-+]?\d+)?")
_EI = re.compile(rb"\sEI(?=\s|$)")
_PAINT = {"n", "f", "F", "f*", "B", "B*", "b", "b*", "S", "s"}


def _tokenize(data):
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c in _WS:
            i += 1
        elif c == 0x25:  # % comment
            while i < n and data[i] not in b"\r\n":
                i += 1
        elif c == 0x28:  # ( string )
            depth, i = 1, i + 1
            while i < n and depth:
                if data[i] == 0x5C:
                    i += 1
                elif data[i] == 0x28:
                    depth += 1
                elif data[i] == 0x29:
                    depth -= 1
                i += 1
            yield "str", None
        elif c == 0x3C:  # << or <hex>
            if data[i + 1:i + 2] == b"<":
                i += 2
            else:
                j = data.find(b">", i)
                i = n if j < 0 else j + 1
                yield "str", None
        elif c == 0x3E:
            i += 2 if data[i + 1:i + 2] == b">" else 1
        elif c in b"[]{})":
            i += 1
        elif c == 0x2F:  # /Name
            j = i + 1
            while j < n and data[j] not in _WS and data[j] not in _DELIM:
                j += 1
            yield "name", data[i + 1:j].decode("latin1")
            i = j
        else:
            j = i
            while j < n and data[j] not in _WS and data[j] not in _DELIM:
                j += 1
            tok = data[i:j].decode("latin1")
            i = max(j, i + 1)
            try:
                yield "num", float(tok)
            except ValueError:
                if tok == "BI":  # inline image: skip its binary payload
                    m = _EI.search(data, i)
                    i = n if not m else m.end()
                else:
                    yield "op", tok


def _nums(s):
    return [float(x) for x in _NUM.findall(s or "")]


def _get(doc, ref, key):
    return doc.xref_get_key(ref[0], ref[1] + key)


def _sub(doc, ref, key):
    """Follow a dictionary entry whether it is inline or an indirect object."""
    t, v = _get(doc, ref, key)
    if t == "xref":
        return (int(v.split()[0]), "")
    if t == "dict":
        return (ref[0], ref[1] + key + "/")
    return None


def _page_resources(doc, page):
    ref = (page.xref, "")
    for _ in range(8):
        res = _sub(doc, ref, "Resources")
        if res:
            return res
        ref = _sub(doc, ref, "Parent")
        if not ref:
            return None
    return None


def _walk(doc, data, res, ctm, clip, out, depth=0):
    stack, operands, pts, want_clip = [], [], [], False
    for kind, val in _tokenize(data):
        if kind != "op":
            operands.append((kind, val))
            continue
        nums = [v for k, v in operands if k == "num"]
        names = [v for k, v in operands if k == "name"]
        operands = []
        if val == "q":
            stack.append((ctm, clip))
        elif val == "Q":
            if stack:
                ctm, clip = stack.pop()
        elif val == "cm" and len(nums) >= 6:
            ctm = pymupdf.Matrix(*nums[-6:]) * ctm
        elif val in ("m", "l") and len(nums) >= 2:
            pts.append(pymupdf.Point(*nums[-2:]) * ctm)
        elif val in ("c", "v", "y") and len(nums) >= 4:
            tail = nums[-6:] if val == "c" and len(nums) >= 6 else nums[-4:]
            pts.extend(pymupdf.Point(tail[k], tail[k + 1]) * ctm for k in range(0, len(tail), 2))
        elif val == "re" and len(nums) >= 4:
            x, y, w, h = nums[-4:]
            pts.extend(pymupdf.Point(px, py) * ctm for px, py in ((x, y), (x + w, y), (x + w, y + h), (x, y + h)))
        elif val in ("W", "W*"):
            want_clip = True
        elif val in _PAINT:
            if want_clip and pts:
                box = pymupdf.Rect(min(p.x for p in pts), min(p.y for p in pts),
                                   max(p.x for p in pts), max(p.y for p in pts))
                clip = box if clip is None else (clip & box)
            pts, want_clip = [], False
        elif val == "sh" and names and res:
            sh = _sub(doc, res, "Shading/" + names[-1])
            if sh:
                out.append((sh, ctm, clip))
        elif val == "Do" and names and res and depth < 6:
            xo = _sub(doc, res, "XObject/" + names[-1])
            if xo and xo[1] == "" and _get(doc, xo, "Subtype")[1] == "/Form":
                m = _nums(_get(doc, xo, "Matrix")[1])
                sub_ctm = pymupdf.Matrix(*m) * ctm if len(m) == 6 else ctm
                try:
                    stream = doc.xref_stream(xo[0]) or b""
                except Exception:
                    stream = b""
                if b"sh" in stream or b"Do" in stream:
                    _walk(doc, stream, _sub(doc, xo, "Resources") or res, sub_ctm, clip, out, depth + 1)


def _components(doc, ref):
    t, v = _get(doc, ref, "ColorSpace")
    for name, n in (("/DeviceRGB", 3), ("/DeviceGray", 1), ("/DeviceCMYK", 4), ("/CalRGB", 3), ("/CalGray", 1), ("/Lab", 0)):
        if v.startswith(name) or v.startswith("[" + name):
            return n
    if "/ICCBased" in v or t == "xref":
        m = re.search(r"(\d+) 0 R", v)
        if m:
            obj = doc.xref_object(int(m.group(1)), compressed=True)
            if "/ICCBased" in obj:
                m = re.search(r"/ICCBased\s*(\d+) 0 R", obj)
                obj = doc.xref_object(int(m.group(1)), compressed=True) if m else obj
            n = re.search(r"/N (\d)", obj)
            if n:
                return int(n.group(1))
    return 0  # Separation, DeviceN, Indexed, Lab: not decoded here


def _eval(doc, fref, x):
    ftype = int(_get(doc, fref, "FunctionType")[1])
    dom = _nums(_get(doc, fref, "Domain")[1]) or [0.0, 1.0]
    x = min(max(x, dom[0]), dom[1])
    if ftype == 2:
        c0 = _nums(_get(doc, fref, "C0")[1]) or [0.0]
        c1 = _nums(_get(doc, fref, "C1")[1]) or [1.0]
        n = (_nums(_get(doc, fref, "N")[1]) or [1.0])[0]
        return [a + (x ** n) * (b - a) for a, b in zip(c0, c1)]
    if ftype == 3:
        subs = [int(m) for m in re.findall(r"(\d+) 0 R", _get(doc, fref, "Functions")[1])]
        bounds = _nums(_get(doc, fref, "Bounds")[1])
        enc = _nums(_get(doc, fref, "Encode")[1])
        if len(subs) != len(bounds) + 1:
            raise ValueError("inline stitched functions")
        edges = [dom[0]] + bounds + [dom[1]]
        k = next((i for i in range(len(subs)) if x < edges[i + 1]), len(subs) - 1)
        lo, hi = edges[k], edges[k + 1]
        e0, e1 = (enc[2 * k], enc[2 * k + 1]) if len(enc) >= 2 * k + 2 else (0.0, 1.0)
        u = e0 if hi == lo else e0 + (x - lo) * (e1 - e0) / (hi - lo)
        return _eval(doc, (subs[k], ""), u)
    if ftype == 0:
        size = int(_nums(_get(doc, fref, "Size")[1])[0])
        rng = _nums(_get(doc, fref, "Range")[1])
        if int(_nums(_get(doc, fref, "BitsPerSample")[1])[0]) != 8 or fref[1]:
            raise ValueError("unsupported sampled function")
        data, m = doc.xref_stream(fref[0]), len(rng) // 2
        pos = (x - dom[0]) / ((dom[1] - dom[0]) or 1) * (size - 1)
        i0 = min(int(pos), size - 1)
        i1, frac = min(i0 + 1, size - 1), pos - int(pos)
        out = []
        for c in range(m):
            a, b = data[i0 * m + c] / 255, data[i1 * m + c] / 255
            v = a + (b - a) * frac
            out.append(rng[2 * c] + v * (rng[2 * c + 1] - rng[2 * c]))
        return out
    raise ValueError("unsupported function type")


def _to_rgb(comps, n):
    if n == 1:
        return (comps[0],) * 3
    if n == 4:
        c, m, y, k = comps[:4]
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return tuple(comps[:3])


def _hex(rgb):
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(c * 255))) for c in rgb)


def _stops(doc, sh):
    n = _components(doc, sh)
    fref = _sub(doc, sh, "Function")
    if not n or not fref:
        return None, n
    dom = _nums(_get(doc, sh, "Domain")[1]) or [0.0, 1.0]
    span = (dom[1] - dom[0]) or 1.0
    ts = {0.0, 1.0} | {i / 16 for i in range(17)}
    if _get(doc, fref, "FunctionType")[1] == "3":
        ts |= {(b - dom[0]) / span for b in _nums(_get(doc, fref, "Bounds")[1])}
    pts = [(t, _to_rgb(_eval(doc, fref, dom[0] + t * span), n)) for t in sorted(ts)]
    # keep only the stops a straight-line interpolation cannot reproduce
    keep = [pts[0]]
    for i in range(1, len(pts) - 1):
        (t0, c0), (t1, c1), (t2, c2) = keep[-1], pts[i], pts[i + 1]
        f = (t1 - t0) / (t2 - t0)
        if max(abs(c0[k] + (c2[k] - c0[k]) * f - c1[k]) for k in range(3)) > 0.012:
            keep.append(pts[i])
    keep.append(pts[-1])
    return [{"position": round(t, 4), "hex": _hex(c)} for t, c in keep], n


def page_gradients(doc, page):
    """Return every gradient painted on the page, in page (top-left) coordinates."""
    res = _page_resources(doc, page)
    if not res:
        return []
    hits = []
    try:
        _walk(doc, page.read_contents(), res, pymupdf.Matrix(1, 0, 0, 1, 0, 0), None, hits)
    except Exception:
        return []
    to_page = page.transformation_matrix
    out = []
    for sh, ctm, clip in hits:
        try:
            stype = int(_get(doc, sh, "ShadingType")[1])
            coords = _nums(_get(doc, sh, "Coords")[1])
            if stype not in (2, 3) or len(coords) < (4 if stype == 2 else 6):
                continue
            stops, n = _stops(doc, sh)
        except Exception:
            continue
        if not stops or len({s["hex"] for s in stops}) < 2:
            continue
        m = ctm * to_page
        box = (clip * to_page).normalize() if clip is not None else None
        g = {"stops": stops, "source_colorspace": {1: "gray", 3: "rgb", 4: "cmyk"}.get(n, "other"),
             "bbox": [round(v, 1) for v in box] if box is not None else None}
        if stype == 2:
            p0, p1 = pymupdf.Point(coords[0], coords[1]) * m, pymupdf.Point(coords[2], coords[3]) * m
            g["kind"] = "linear"
            g["angle"] = round(math.degrees(math.atan2(p1.x - p0.x, -(p1.y - p0.y))) % 360, 1)
        else:
            p0, p1 = pymupdf.Point(coords[0], coords[1]) * m, pymupdf.Point(coords[3], coords[4]) * m
            g["kind"] = "radial"
            g["angle"] = None
        if box is not None and box.width > 0 and box.height > 0:
            # gradient handles as fractions of the filled shape, for an exact gradientTransform
            g["handles"] = [[round((p.x - box.x0) / box.width, 4), round((p.y - box.y0) / box.height, 4)] for p in (p0, p1)]
        else:
            g["handles"] = None
        out.append(g)
    return out
