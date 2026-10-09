"""Build a one-page brand site the plugin's Website URL scraper can read, and put it online briefly.

What the scraper takes from a page (found by testing): the header logo, every labelled colour
swatch, the colour roles, and three type styles from the CSS of h1, h2 and body text.
"""
import atexit
import html
import re
import shutil
import subprocess
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8770
STATE = {"dir": None, "httpd": None, "tunnel": None, "url": None}
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _font(name):
    return re.sub(r"[^\w \-]", "", str(name)).strip() or "Arial"


def _colour(c):
    return {"name": str(c.get("name", "")).strip()[:60] or c.get("hex", ""), "hex": c["hex"]} if _HEX.match(str(c.get("hex", ""))) else None


def build(out_dir, cfg, logo):
    """Write index.html (and the logo file) into out_dir. `logo` is the asset dict or None."""
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    (out / "assets").mkdir(parents=True)
    e = html.escape
    name = str(cfg.get("name") or "Brand").strip()[:120]
    logo_tag = ""
    if logo:
        ext = {"svg": "svg", "png": "png", "jpeg": "jpg"}[logo["format"]]
        data = logo["data"].encode("utf-8") if isinstance(logo["data"], str) else logo["data"]
        (out / "assets" / f"logo.{ext}").write_bytes(data)
        logo_tag = f'<img class="logo" id="logo" src="assets/logo.{ext}" alt="{e(name)} logo">'
    roles = {k: _colour(v) for k, v in (cfg.get("roles") or {}).items() if k in ("primary", "secondary", "tertiary", "text", "background")}
    roles = {k: v for k, v in roles.items() if v}
    colours = [c for c in map(_colour, cfg.get("colors") or []) if c]
    t = cfg.get("type") or {}

    def rule(key, fallback):
        s = t.get(key) or {}
        return (f"font-family:\"{_font(s.get('family', 'Arial'))}\",Arial,sans-serif;font-size:{int(float(s.get('size') or fallback))}px;"
                f"font-weight:{int(s.get('weight') or 400)};line-height:1.2")

    def label(key):
        s = t.get(key) or {}
        return f"{e(_font(s.get('family', 'Arial')))}, weight {int(s.get('weight') or 400)}, {int(float(s.get('size') or 16))}px"

    role_hex = lambda k, d: roles[k]["hex"] if k in roles else d
    sw = lambda title, hx: (f'<figure><div class="sw" style="background:{hx}"></div>'
                            f"<figcaption><strong>{e(title)}</strong><br>{hx.upper()}</figcaption></figure>")
    css_vars = "".join(f"--color-{k}:{v['hex']};" for k, v in roles.items())
    css_vars += "".join(f"--color-{re.sub(r'[^a-z0-9]+', '-', c['name'].lower()).strip('-')}:{c['hex']};" for c in colours)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(name)} Brand Guidelines</title>
<meta name="description" content="{e(name)} brand system: logo, colours and typography.">
<meta name="robots" content="noindex,nofollow">
<style>
:root{{{css_vars}}}
body{{margin:0;{rule("body", 18)};color:{role_hex("text", "#1E1E1E")};background:{role_hex("background", "#FFFFFF")}}}
header{{display:flex;align-items:center;gap:24px;padding:20px 40px;background:#fff;border-bottom:1px solid #ddd}} header img{{height:64px}}
main{{max-width:1200px;margin:0 auto;padding:24px 40px 80px}}
h1{{{rule("display", 48)};margin:32px 0 8px;color:{role_hex("primary", "#111111")}}}
h2{{{rule("headline", 32)};margin:56px 0 16px;color:{role_hex("secondary", "#111111")}}}
p{{margin:0 0 12px}} .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:20px}}
figure{{margin:0}} .sw{{height:110px;border-radius:12px;border:1px solid rgba(0,0,0,.08)}} figcaption{{font:14px Arial,sans-serif;margin-top:6px;color:#1E1E1E}}
</style></head><body>
<header>{logo_tag}<span>{e(name)} Brand Guidelines</span></header>
<main>
<h1>{e(name)} Brand Guidelines</h1>
<p>Logo, colour palette and typography for {e(name)}.</p>
<section id="colour-roles"><h2>Colour roles</h2><div class="grid">{"".join(sw(k.title() + (" color" if k in ("text", "background") else ""), v["hex"]) for k, v in roles.items())}</div></section>
<section id="colours"><h2>Colour palette</h2><div class="grid">{"".join(sw(c["name"], c["hex"]) for c in colours)}</div></section>
<section id="typography"><h2>Typography</h2>
<p>Display: {label("display")}.</p><p>Heading: {label("headline")}.</p><p>Body: {label("body")}.</p></section>
</main></body></html>"""
    (out / "index.html").write_text(page, encoding="utf-8")
    return out


def serve(directory):
    """Serve only this folder on 127.0.0.1; the tunnel points here, never at the app itself."""
    if STATE["httpd"]:
        STATE["httpd"].shutdown()
        STATE["httpd"].server_close()
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    handler.log_message = lambda *a, **k: None
    STATE["httpd"] = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    STATE["dir"] = str(directory)
    threading.Thread(target=STATE["httpd"].serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{PORT}/"


def tunnel_start():
    if STATE["tunnel"] and STATE["tunnel"].poll() is None and STATE["url"]:
        return STATE["url"]
    if not STATE["httpd"]:
        raise RuntimeError("Build the brand page first.")
    exe = shutil.which("cloudflared") or next((p for p in ("/usr/local/bin/cloudflared", "/opt/homebrew/bin/cloudflared") if Path(p).exists()), None)
    if not exe:
        raise RuntimeError("cloudflared is not installed. Install it with: brew install cloudflared")
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    STATE["tunnel"], STATE["url"] = proc, None
    found = {}

    def watch():
        for line in proc.stderr:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not found.get("url"):
                found["url"] = m.group(0)
            if "Registered tunnel connection" in line:
                found["ready"] = True

    threading.Thread(target=watch, daemon=True).start()
    for _ in range(80):
        if (found.get("url") and found.get("ready")) or proc.poll() is not None:
            break
        time.sleep(0.5)
    if found.get("url") and found.get("ready"):
        # the address is announced before its DNS record exists; handing it out early gets a failed
        # lookup cached by whoever tries it first
        time.sleep(8)
        STATE["url"] = found["url"]
    if not STATE["url"]:
        tunnel_stop()
        raise RuntimeError("The tunnel did not come up. Check the internet connection and try again.")
    return STATE["url"]


def tunnel_stop():
    proc = STATE["tunnel"]
    if proc and proc.poll() is None:
        proc.terminate()
    STATE["tunnel"], STATE["url"] = None, None


atexit.register(tunnel_stop)
