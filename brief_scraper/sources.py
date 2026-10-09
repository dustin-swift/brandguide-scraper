"""Turn whatever a user drops in (brand PDFs, asset files, text files, a URL, ads) into elements.

Every function returns (elements, assets). Elements use the same shape as extract.py produces.
"""
import hashlib
import html
import ipaddress
import re
import socket
import urllib.parse
import urllib.request
from pathlib import PurePosixPath

import pymupdf

from . import ai
from .extract import CMS, extract, rgb_of, slug

ASSET_TYPES = {".svg": ("svg", "image/svg+xml"), ".png": ("png", "image/png"), ".jpg": ("jpeg", "image/jpeg"),
               ".jpeg": ("jpeg", "image/jpeg"), ".gif": ("gif", "image/gif"), ".webp": ("webp", "image/webp")}
TEXT_TYPES = {".md", ".markdown", ".txt", ".json", ".css", ".scss", ".yaml", ".yml", ".tokens"}
_HEX = r"#[0-9A-Fa-f]{6}\b"


def kind_of(filename):
    ext = PurePosixPath(filename.lower()).suffix
    return "pdf" if ext == ".pdf" else "asset" if ext in ASSET_TYPES else "text" if ext in TEXT_TYPES else None


def _el(type_, name, **kw):
    el = {"type": type_, "name": name, "include": True, "origin": "specified", "confidence": "exact", "section": "",
          "source": {"pages": [], "method": kw.pop("method", "file")}, "cms": CMS.get(type_, [])}
    el.update(kw)
    return el


def from_pdf(path, use_ai, max_edge, progress):
    out = extract(path, max_image_edge=max_edge, progress=progress)
    assets = out.pop("assets")
    problems = []
    if use_ai and ai.available():
        taken = {e["id"] for e in out["elements"]}

        def add(el, asset=None):
            base, n = f"{el['type']}.{slug(el['name'])}", 1
            el["id"] = base
            while el["id"] in taken:
                n += 1
                el["id"] = f"{base}-{n}"
            taken.add(el["id"])
            el["cms"] = CMS.get(el["type"], [])
            if asset:
                assets[el["id"]] = asset
                el["asset"] = {"format": asset["format"], "mime": asset["mime"], "bytes": len(asset["data"])}
            out["elements"].append(el)
            return el

        problems = ai.enrich(path, out, add, progress)
    return out["elements"], assets, out["pages"], problems


def from_asset(rel_path, data):
    """One logo, icon or image file. Type is guessed from its path; the reviewer can change it."""
    path = PurePosixPath(rel_path)
    fmt, mime = ASSET_TYPES[path.suffix.lower()]
    low = rel_path.lower()
    name = re.sub(r"[_\-]+", " ", path.stem).strip() or "Asset"
    if fmt == "svg":
        text = data.decode("utf-8", "replace")
        if "<svg" not in text:
            raise ValueError("not an SVG file")
        type_ = "logo" if "logo" in low else "icon"
        asset = {"format": "svg", "mime": mime, "data": text}
        extra = {}
    else:
        pix = pymupdf.Pixmap(data)  # also proves the file really is an image
        type_ = "logo" if "logo" in low else "icon" if "icon" in low and max(pix.width, pix.height) <= 512 else "image"
        asset = {"format": fmt, "mime": mime, "data": data}
        extra = {"pixel_size": [pix.width, pix.height]}
    folder = str(path.parent) if str(path.parent) != "." else ""
    el = _el(type_, name, section=folder, asset={"format": fmt, "mime": mime, "bytes": len(data)}, **extra)
    return el, asset


def _parse_gradients(text):
    out = []
    for m in re.finditer(r"(?:([A-Za-z][\w /()-]{1,50}?)\s*[:|]\s*)?linear-gradient\(([^;\n]*?)\)(?=\s*[;|\n]|$)", text, re.M):
        body = m.group(2)
        angle = re.search(r"(-?\d+(?:\.\d+)?)deg", body)
        stops = re.findall(rf"({_HEX})\s*(\d+(?:\.\d+)?)?%?", body)
        if len(stops) < 2:
            continue
        n = len(stops)
        out.append(_el("gradient", (m.group(1) or f"Gradient {len(out) + 1}").strip(" -|*"), kind="linear",
                       angle=float(angle.group(1)) if angle else 180.0,
                       stops=[{"position": float(p) / 100 if p else i / (n - 1), "hex": h.upper()} for i, (h, p) in enumerate(stops)],
                       method="text"))
    return out


def _parse_colours(text):
    seen, out = set(), []
    pairs = [(re.sub(r"^(color|colour|clr)-", "", n).replace("-", " ").title(), h)
             for n, h in re.findall(rf"--([\w-]+)\s*:\s*({_HEX})", text)]
    rest = re.sub(rf"--[\w-]+\s*:\s*{_HEX}", "", text)
    pairs += re.findall(rf"^[\s\-*|>]*[\"']?([A-Za-z][\w /&.\-]{{1,40}}?)[\"']?\s*(?:[:=|]|\s-\s)\s*[\"']?({_HEX})", rest, re.M)
    for name, hx in pairs:
        key = (slug(name), hx.upper())
        if key in seen or "gradient" in name.lower():
            continue
        seen.add(key)
        out.append(_el("color", name.strip(), hex=hx.upper(), rgb=rgb_of(hx), method="text"))
    return out


def from_text(label, text, use_ai, origin="specified"):
    """A design.md, tokens file or notes. Claude reads it when available; otherwise plain pattern matching."""
    els = []
    if use_ai and ai.available():
        got = ai.read_text(text, label)
        for c in got["colors"]:
            hx = c["hex"] if c["hex"].startswith("#") else "#" + c["hex"]
            if re.fullmatch(_HEX, hx):
                els.append(_el("color", c["name"], hex=hx.upper(), rgb=rgb_of(hx), method="text", confidence="stated"))
        for g in got["gradients"]:
            stops = [{"position": max(0.0, min(1.0, s["position"])), "hex": s["hex"].upper()} for s in g["stops"] if re.fullmatch(_HEX, s["hex"])]
            if len(stops) >= 2:
                els.append(_el("gradient", g["name"], kind="linear", angle=g["angle"], stops=stops, method="text", confidence="stated"))
        for t in got["text_styles"]:
            els.append(_el("text-style", t["name"], family=t["family"], weight=t["weight"], size=t["size_px"], italic=t["italic"],
                           style="", method="text", confidence="stated"))
        for i, rule in enumerate(got["guidelines"][:40]):
            els.append(_el("guideline", rule[:60], rules=[rule], method="text", confidence="stated"))
    else:
        els = _parse_colours(text) + _parse_gradients(text)
    for e in els:
        e["origin"] = origin
        if e["type"] == "gradient":
            e["css"] = "linear-gradient(%gdeg, %s)" % (e["angle"], ", ".join(f"{s['hex']} {s['position'] * 100:g}%" for s in e["stops"]))
    return els, {}


def _public(url):
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Enter a full web address starting with http:// or https://")
    for info in socket.getaddrinfo(parts.hostname, None):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError("That address points at a private or local network, which this tool will not fetch.")


def _fetch(url, limit=3_000_000):
    _public(url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (brief-scraper)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(limit).decode(r.headers.get_content_charset() or "utf-8", "replace")


def from_url(url, use_ai):
    """A brand website: its CSS and visible text, read like any other text source and marked as observed."""
    page = _fetch(url)
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", page, re.S | re.I))
    for href in re.findall(r"<link[^>]+rel=[\"']?stylesheet[\"']?[^>]*href=[\"']([^\"']+)", page, re.I)[:4]:
        try:
            css += "\n" + _fetch(urllib.parse.urljoin(url, html.unescape(href)), 600_000)
        except Exception:
            pass
    body = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", page, flags=re.S | re.I)
    body = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))
    text = f"CSS:\n{css[:120_000]}\n\nPAGE TEXT:\n{body[:30_000]}"
    return from_text(url, text, use_ai, origin="observed")


def from_reference(name, data, mime):
    """An ad or layout: Claude estimates the colours in use. Excluded by default; a person decides."""
    got = ai.read_reference_image(data, mime)
    els = []
    for c in got["colors"]:
        hx = c["hex"] if c["hex"].startswith("#") else "#" + c["hex"]
        if re.fullmatch(_HEX, hx):
            els.append(_el("color", c["name"], hex=hx.upper(), rgb=rgb_of(hx), include=False, origin="observed", confidence="estimated",
                           method="ai-reading", note=f"Estimated by eye from {name}. {got['summary']}"))
    if got["fonts"]:
        els.append(_el("guideline", f"Type seen in {name}"[:60], rules=got["fonts"], include=False, origin="observed",
                       confidence="estimated", method="ai-reading"))
    return els, {}


def fingerprint(asset):
    data = asset["data"].encode("utf-8") if isinstance(asset["data"], str) else asset["data"]
    return hashlib.sha1(data).hexdigest()
