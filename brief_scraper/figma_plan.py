"""Everything about the Figma side: scan scripts, comparing new material with the board, write scripts.

The scripts are Figma Plugin API JavaScript. Claude Code runs them through the Figma connector
(`use_figma`); this module only generates them and interprets what they return.

The board's layout is discovered by pattern on every scan (pages by name, existing items as
templates), never by fixed ids, because the DesignOps template can change without notice.
"""
import json
import re
from datetime import date

from .extract import slug

ROLES = ["primary", "secondary", "tertiary", "text", "background"]
PAGE_KINDS = {"logos": r"logos?", "visuals": r"visuals?|imagery|images?", "icons": r"icons?"}
SECTION = {"icons": "Brand Icons", "visuals": "Image Library", "logos": "Brand Logos"}
WEIGHT_NAMES = {100: "Thin", 200: "Extra Light", 300: "Light", 400: "Regular", 500: "Medium", 600: "Semi Bold",
                700: "Bold", 800: "Extra Bold", 900: "Black"}
UPLOAD_PREFIX = "BSUP-"

_JS_HELPERS = """const hx = c => '#' + [c.r, c.g, c.b].map(v => Math.round(v * 255).toString(16).padStart(2, '0')).join('').toUpperCase();
const rgb = h => ({ r: parseInt(h.slice(1, 3), 16) / 255, g: parseInt(h.slice(3, 5), 16) / 255, b: parseInt(h.slice(5, 7), 16) / 255 });
"""


# ------------------------------------------------------------------------------------- scanning

def scan_core_script():
    return _JS_HELPERS + """const cols = await figma.variables.getLocalVariableCollectionsAsync();
const vars = await figma.variables.getLocalVariablesAsync();
const byId = Object.fromEntries(vars.map(v => [v.id, v]));
const colorCol = cols.find(c => /^colou?rs?$/i.test(c.name));
const roleCol = cols.find(c => vars.some(v => v.variableCollectionId === c.id && v.name === 'Primary'));
const first = (v, col) => v.valuesByMode[col.defaultModeId];
const colorVars = colorCol ? vars.filter(v => v.variableCollectionId === colorCol.id && v.resolvedType === 'COLOR').map(v => ({ n: v.name, hex: hx(first(v, colorCol)) })) : [];
const fontVars = roleCol ? Object.fromEntries(vars.filter(v => v.variableCollectionId === roleCol.id && v.resolvedType === 'STRING' && v.scopes.includes('FONT_FAMILY')).map(v => [v.name, first(v, roleCol)])) : {};
const paints = (await figma.getLocalPaintStylesAsync()).map(s => { const p = s.paints[0] || {}; const o = { n: s.name, d: (s.description || '').slice(0, 80) };
  if (p.type === 'SOLID') { o.hex = hx(p.color); const b = p.boundVariables && p.boundVariables.color; if (b && byId[b.id]) o.v = byId[b.id].name; }
  else if (p.gradientStops) o.g = p.gradientStops.map(g => [Math.round(g.position * 1000) / 1000, hx(g.color), Math.round(g.color.a * 100) / 100]);
  return o; });
const texts = (await figma.getLocalTextStylesAsync()).map(s => ({ n: s.name, f: s.fontName.family, s: s.fontName.style, z: s.fontSize, lh: s.lineHeight.unit === 'AUTO' ? null : Math.round(s.lineHeight.value * 10) / 10, lu: s.lineHeight.unit }));
return { file: figma.root.name, pages: figma.root.children.map(p => ({ id: p.id, name: p.name })), hasColorCollection: !!colorCol, colorVars, fontVars,
  paints, texts, effects: (await figma.getLocalEffectStylesAsync()).map(s => s.name) };"""


def scan_page_script(page_id):
    # Runs of numbered names ("UI/Icon 01" … "UI/Icon 72") come back as [prefix, first, width, count]
    # so a page of placeholders does not turn into hundreds of lines to hand back.
    return f"""const page = await figma.getNodeByIdAsync({json.dumps(page_id)});
if (!page) return {{ error: 'page not found' }};
await figma.setCurrentPageAsync(page);
const hasImage = n => 'fills' in n && Array.isArray(n.fills) && n.fills.some(f => f.type === 'IMAGE');
const sections = page.children.filter(n => n.type === 'SECTION').map(s => {{
  const kids = s.children.filter(c => c.type === 'COMPONENT' || c.type === 'FRAME');
  const groups = {{}};
  for (const k of kids) {{ const m = k.name.match(/^(.*?)(\\d+)$/); if (m) (groups[m[1]] = groups[m[1]] || []).push(m[2]); }}
  const seq = [], inSeq = new Set();
  for (const [prefix, nums] of Object.entries(groups)) {{
    const w = nums[0].length, sorted = nums.map(Number).sort((a, b) => a - b);
    if (nums.length >= 5 && nums.every(x => x.length === w) && sorted.every((x, i) => x === sorted[0] + i)) {{
      seq.push([prefix, sorted[0], w, nums.length]); nums.forEach(x => inSeq.add(prefix + x)); }}
  }}
  return {{ name: s.name, names: kids.filter(k => !inSeq.has(k.name)).map(k => k.name), seq,
    images: kids.filter(k => k.type === 'FRAME' && k.children.some(c => c.name === 'Image' && hasImage(c))).length }};
}});
const sets = page.children.filter(n => n.type === 'COMPONENT_SET').map(s => ({{ name: s.name, variants: s.children.map(c => c.name) }}));
return {{ page: page.name, sections, sets }};"""


def section_names(sec):
    out = list(sec.get("names", []))
    for prefix, first, width, count in sec.get("seq", []):
        out += [f"{prefix}{first + i:0{width}d}" for i in range(count)]
    return out


def scan_steps_core():
    return [{"id": "core", "kind": "script", "title": "Read variables, styles and pages", "code": scan_core_script()}]


def scan_steps_pages(core):
    steps = []
    for kind, pattern in PAGE_KINDS.items():
        page = next((p for p in core.get("pages", []) if re.search(rf"\b({pattern})\b", p["name"], re.I)), None)
        if page:
            steps.append({"id": kind, "kind": "script", "title": f"Read the {page['name']} page", "code": scan_page_script(page["id"])})
    return steps


# ------------------------------------------------------------------------------------ comparing

def _near(a, b, tol=2):
    try:
        return all(abs(int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) <= tol for i in (1, 3, 5))
    except (ValueError, IndexError):
        return False


def text_spec(el):
    """Family, weight, size for a text-style element, or None when the brief did not give all three."""
    props = {k.lower().replace("_", "-"): str(v) for k, v in (el.get("properties") or {}).items()}
    family = el.get("family") or props.get("font-family") or props.get("family") or props.get("font")
    size = el.get("size")
    if size is None:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:px|pt)\b", props.get("size", "") + " " + props.get("font-size", ""))
        size = float(m.group(1)) if m else None
    weight = el.get("weight")
    if weight is None:
        raw = (props.get("weight", "") + " " + props.get("font-weight", "") + " " + props.get("style", "")).lower().replace(" ", "")
        weight = next((w for w, n in sorted(WEIGHT_NAMES.items(), key=lambda kv: -len(kv[1])) if n.lower().replace(" ", "") in raw), None)
        m = re.search(r"\b([1-9]00)\b", raw)
        weight = int(m.group(1)) if m else (weight or 400)
    italic = bool(el.get("italic")) or "italic" in json.dumps(props).lower()
    if not family or not size:
        return None
    family = re.split(r"[,;]", str(family))[0].strip().strip("\"'")
    return {"family": family, "weight": int(weight), "italic": italic, "size": float(size),
            "line_height": el.get("line_height_percent")}


def compare(elements, state):
    """Annotate each element with how it relates to what is on the board. Returns {id: info}."""
    core = (state or {}).get("core") or {}
    pages = {k: (state or {}).get(k) or {} for k in PAGE_KINDS}
    paints = {p["n"].lower(): p for p in core.get("paints", [])}
    colour_vars = {v["n"].lower(): v for v in core.get("colorVars", [])}
    grads = [p for p in core.get("paints", []) if p.get("g")]
    names = {k: {slug(n) for sec in pages[k].get("sections", []) for n in section_names(sec)} for k in PAGE_KINDS}
    out = {}
    for el in elements:
        t, info = el["type"], {"status": "new", "target": None, "current": None, "writable": True}
        if t == "color" and el.get("hex"):
            style = paints.get(("brand/" + el["name"]).lower()) or next((p for n, p in paints.items() if slug(n.split("/", 1)[-1]) == slug(el["name"]) and p.get("hex")), None)
            var = colour_vars.get(slug(el["name"]))
            cur = (style or {}).get("hex") or (var or {}).get("hex")
            twin = next((p for p in core.get("paints", []) if p.get("hex") and _near(p["hex"], el["hex"])), None)
            if cur:
                info.update(status="same" if _near(cur, el["hex"]) else "changed", current=cur, target=(style or {}).get("n") or var["n"])
            elif twin:
                info.update(status="same-value", current=twin["hex"], target=twin["n"])
        elif t == "gradient" and el.get("stops"):
            want = [(round(s["position"], 2), s["hex"]) for s in el["stops"]]
            hit = next((g for g in grads if len(g["g"]) == len(want) and all(abs(a[0] - b[0]) < 0.02 and _near(a[1], b[1]) for a, b in zip(g["g"], want))), None)
            named = paints.get(gradient_style_name(el).lower())
            if hit:
                info.update(status="same", target=hit["n"])
            elif named:
                info.update(status="changed", target=named["n"])
        elif t == "text-style":
            spec = text_spec(el)
            if not spec:
                info.update(writable=False, status="reference", note="The brief gives no family and fixed size for this style, so it cannot be written as a text style.")
            else:
                info["spec"] = spec
        elif t in ("icon", "image", "graphic"):
            kind = "icons" if t == "icon" else "visuals"
            if slug(asset_layer_name(el)) in names[kind] or slug(el["name"]) in names[kind]:
                info.update(status="same", target=asset_layer_name(el))
        elif t == "logo":
            if slug(asset_layer_name(el)) in names["logos"]:
                info.update(status="same", target=asset_layer_name(el))
        else:
            info.update(writable=False, status="reference", note="Kept for reference; not written to Figma.")
        if t in ("icon", "image", "graphic", "logo") and not el.get("asset"):
            info.update(writable=False, status="reference")
        out[el["id"]] = info
    return out


def board_summary(state):
    core = (state or {}).get("core") or {}
    count = lambda k: sum(len(section_names(sec)) for sec in ((state or {}).get(k) or {}).get("sections", []))
    return {"file": core.get("file"), "colours": sum(1 for p in core.get("paints", []) if p.get("hex")),
            "gradients": sum(1 for p in core.get("paints", []) if p.get("g")), "text_styles": len(core.get("texts", [])),
            "icons": count("icons"), "visuals": count("visuals"),
            "logo_sets": [s["name"] for s in ((state or {}).get("logos") or {}).get("sets", [])],
            "pages_found": [k for k in PAGE_KINDS if (state or {}).get(k)],
            "roles": {v["n"]: v["hex"] for v in core.get("colorVars", []) if v["n"] in ROLES},
            "paints": core.get("paints", []), "texts": core.get("texts", []),
            "logo_slots": [f"{s['name']}|{v.split('=')[-1]}" for s in ((state or {}).get("logos") or {}).get("sets", []) for v in s.get("variants", [])]}


# -------------------------------------------------------------------------------------- writing

def gradient_style_name(el):
    name = re.sub(r"^gradient\s*/\s*", "", el["name"], flags=re.I).strip() or "Gradient"
    return "Gradient/" + " ".join(w if w.isupper() and len(w) <= 3 else w.capitalize() if w.isupper() else w for w in name.split())


def colour_style_name(el):
    name = el["name"].strip()
    return "Brand/" + (name.title() if name.isupper() else name)


def asset_layer_name(el):
    name = el["name"].strip()
    if el["type"] == "icon":
        return "Brand/Icon " + re.sub(r"^(brand/)?icons?\s*[/:]?\s*", "", name, flags=re.I).strip()
    if el["type"] == "logo":
        return "Logo/" + re.sub(r"^logos?\s*[/:]\s*", "", name, flags=re.I).strip()
    return "Image library/" + re.sub(r"^image\s*library\s*[/:]\s*", "", name, flags=re.I).strip()


def _stamp(source):
    return f"Brief Scraper · {source or 'brand materials'} · {date.today().isoformat()}"[:180]


def tokens_script(data):
    return _JS_HELPERS + "const D = " + json.dumps(data, ensure_ascii=False) + """;
const log = [], touched = [];
const cols = await figma.variables.getLocalVariableCollectionsAsync();
const vars = await figma.variables.getLocalVariablesAsync();
const colorCol = cols.find(c => /^colou?rs?$/i.test(c.name));
if (!colorCol && (D.colors.length || Object.keys(D.roles).length)) throw new Error('No "Color" variable collection in this file; colours were not written.');
const styles = await figma.getLocalPaintStylesAsync();
const colourVar = (name, hex) => {
  let v = vars.find(x => x.variableCollectionId === colorCol.id && x.name === name);
  if (!v) { v = figma.variables.createVariable(name, colorCol, 'COLOR'); v.scopes = []; vars.push(v); log.push('new variable ' + name); }
  v.setValueForMode(colorCol.defaultModeId, rgb(hex)); return v; };
for (const [role, hex] of Object.entries(D.roles)) { colourVar(role, hex); log.push('role ' + role + ' = ' + hex); }
for (const c of D.colors) {
  const v = colourVar(c.variable, c.hex);
  let s = styles.find(x => x.name.toLowerCase() === c.style.toLowerCase());
  if (!s) { s = figma.createPaintStyle(); s.name = c.style; s.description = c.stamp; styles.push(s); log.push('new colour style ' + c.style); } else log.push('updated ' + s.name);
  s.paints = [figma.variables.setBoundVariableForPaint({ type: 'SOLID', color: rgb(c.hex) }, 'color', v)]; touched.push(s.id);
}
for (const g of D.gradients) {
  let s = styles.find(x => x.name.toLowerCase() === g.style.toLowerCase());
  if (!s) { s = figma.createPaintStyle(); s.name = g.style; styles.push(s); log.push('new gradient ' + g.style); } else log.push('updated ' + s.name);
  s.description = g.stamp;
  const a = (g.angle == null ? 90 : g.angle) * Math.PI / 180, d = [Math.sin(a), -Math.cos(a)];
  const S = [0.5 - d[0] / 2, 0.5 - d[1] / 2], vx = d[0], vy = d[1];
  const T = [[vx, vy, -(S[0] * vx + S[1] * vy)], [-vy, vx, -(S[0] * -vy + S[1] * vx) + 0.5]];
  s.paints = [{ type: g.kind === 'radial' ? 'GRADIENT_RADIAL' : 'GRADIENT_LINEAR', gradientTransform: g.kind === 'radial' ? [[1, 0, 0], [0, 1, 0]] : T,
    gradientStops: g.stops.map(([position, hex]) => ({ position, color: Object.assign(rgb(hex), { a: 1 }) })) }]; touched.push(s.id);
}
if (D.text.length) {
  const fonts = await figma.listAvailableFontsAsync();
  const tstyles = await figma.getLocalTextStylesAsync();
  for (const t of D.text) {
    const fam = fonts.filter(f => f.fontName.family.toLowerCase() === t.family.toLowerCase());
    if (!fam.length) { log.push('SKIPPED ' + t.style + ': font "' + t.family + '" is not installed in Figma'); continue; }
    const norm = x => x.toLowerCase().replace(/[^a-z]/g, '');
    const font = fam.find(f => norm(f.fontName.style) === norm(t.fontStyle)) || fam.find(f => norm(f.fontName.style) === norm(t.fontStyle.replace(/italic/i, '')) ) || fam.find(f => /regular/i.test(f.fontName.style)) || fam[0];
    await figma.loadFontAsync(font.fontName);
    let s = tstyles.find(x => x.name.toLowerCase() === t.style.toLowerCase());
    if (!s) { s = figma.createTextStyle(); s.name = t.style; log.push('new text style ' + t.style); } else log.push('updated ' + s.name);
    s.fontName = font.fontName; s.fontSize = t.size; s.description = t.stamp;
    if (t.lineHeight) s.lineHeight = { unit: 'PERCENT', value: t.lineHeight };
    if (norm(font.fontName.style) !== norm(t.fontStyle)) log.push('note: ' + t.style + ' uses ' + font.fontName.style + '; "' + t.fontStyle + '" was not available');
    touched.push(s.id);
  }
}
return { log, touchedStyleIds: touched };"""


_PLACE_COMMON = """const page = await figma.getNodeByIdAsync(PAGE_ID);
await figma.setCurrentPageAsync(page);
const log = [], created = [], changed = [];
const loose = i => page.children.find(n => n.name.replace(/\\.[a-z0-9]+$/i, '') === PREFIX + i);
const bottom = () => Math.max(0, ...page.children.filter(n => !n.name.startsWith(PREFIX)).map(n => n.y + n.height));
async function section(name, like) {
  let sec = page.children.find(n => n.type === 'SECTION' && n.name === name);
  if (sec) return sec;
  sec = figma.createSection(); sec.name = name; page.appendChild(sec);
  sec.x = like ? like.x : 360; sec.y = bottom() + 60; sec.resizeWithoutConstraints(like ? like.width : 1040, 200);
  if (like) { sec.fills = JSON.parse(JSON.stringify(like.fills));
    const lab = like.children.find(c => c.type === 'TEXT');
    if (lab) { const l = lab.clone(); sec.appendChild(l); l.x = lab.x; l.y = lab.y;
      for (const seg of l.getStyledTextSegments(['fontName'])) await figma.loadFontAsync(seg.fontName);
      l.characters = name.toUpperCase(); } }
  log.push('new section ' + name); return sec;
}
const fit = (sec, pad) => { const kids = sec.children; if (!kids.length) return;
  sec.resizeWithoutConstraints(Math.max(sec.width, Math.max(...kids.map(k => k.x + k.width)) + pad), Math.max(...kids.map(k => k.y + k.height)) + pad); };
"""


def place_icons_script(page_id, step_id, items):
    return (f"const PAGE_ID = {json.dumps(page_id)}, PREFIX = {json.dumps(UPLOAD_PREFIX + step_id + '-')}, ITEMS = {json.dumps(items, ensure_ascii=False)}, NAME = {json.dumps(SECTION['icons'])};\n"
            + _PLACE_COMMON + """const like = page.children.find(n => n.type === 'SECTION' && n.name !== NAME && n.children.some(c => c.type === 'COMPONENT'));
const sec = await section(NAME, like);
const tmpl = (like || sec).children.find(c => c.type === 'COMPONENT');
const size = tmpl ? tmpl.width : 28, glyph = tmpl && tmpl.children[0] ? Math.max(tmpl.children[0].width, tmpl.children[0].height) : 18, step = size + 22;
for (let i = 0; i < ITEMS.length; i++) {
  const art = loose(i); if (!art) { log.push('MISSING upload for ' + ITEMS[i].name); continue; }
  let comp = sec.children.find(c => c.type === 'COMPONENT' && c.name.toLowerCase() === ITEMS[i].name.toLowerCase());
  if (comp) { for (const k of [...comp.children]) k.remove(); changed.push(comp.id); }
  else { comp = figma.createComponent(); comp.name = ITEMS[i].name; comp.resize(size, size); comp.fills = tmpl ? JSON.parse(JSON.stringify(tmpl.fills)) : []; comp.clipsContent = false;
    const n = sec.children.filter(c => c.type === 'COMPONENT').length; sec.appendChild(comp);
    const perRow = Math.max(1, Math.floor((sec.width - 56) / step)); comp.x = 28 + (n % perRow) * step; comp.y = 72 + Math.floor(n / perRow) * step; created.push(comp.id); }
  comp.description = ITEMS[i].stamp;
  comp.appendChild(art); art.name = 'Glyph'; if ('fills' in art) art.fills = [];
  art.rescale(glyph / Math.max(art.width, art.height)); art.x = (comp.width - art.width) / 2; art.y = (comp.height - art.height) / 2;
}
fit(sec, 28);
return { section: sec.id, created, changed, log };""")


def place_images_script(page_id, step_id, items):
    return (f"const PAGE_ID = {json.dumps(page_id)}, PREFIX = {json.dumps(UPLOAD_PREFIX + step_id + '-')}, ITEMS = {json.dumps(items, ensure_ascii=False)}, NAME = {json.dumps(SECTION['visuals'])};\n"
            + _PLACE_COMMON + """const isCard = c => c.type === 'FRAME' && c.children.some(k => k.name === 'Image');
const like = page.children.find(n => n.type === 'SECTION' && n.name !== NAME && n.children.some(isCard));
const sec = await section(NAME, like);
const tmpl = (like || sec).children.find(isCard);
if (!tmpl) throw new Error('No image card to copy on this page (a frame containing a layer named "Image").');
const fillOf = n => ('fills' in n && Array.isArray(n.fills) && n.fills.find(f => f.type === 'IMAGE')) || ('findOne' in n && (n.findOne(k => 'fills' in k && Array.isArray(k.fills) && k.fills.some(f => f.type === 'IMAGE')) || {}).fills || []).find(f => f.type === 'IMAGE');
const stepX = tmpl.width + 20, stepY = tmpl.height + 20;
for (let i = 0; i < ITEMS.length; i++) {
  const up = loose(i); if (!up) { log.push('MISSING upload for ' + ITEMS[i].name); continue; }
  const fill = fillOf(up); if (!fill) { log.push('no image found in upload for ' + ITEMS[i].name); up.remove(); continue; }
  let card = sec.children.find(c => isCard(c) && c.name.toLowerCase() === ITEMS[i].name.toLowerCase());
  if (card) changed.push(card.id);
  else { const n = sec.children.filter(isCard).length; card = tmpl.clone(); sec.appendChild(card); card.name = ITEMS[i].name;
    const perRow = Math.max(1, Math.floor((sec.width - 56 + 20) / stepX)); card.x = 28 + (n % perRow) * stepX; card.y = 58 + Math.floor(n / perRow) * stepY; created.push(card.id);
    const label = card.children.find(k => k.type === 'TEXT');
    if (label) { for (const seg of label.getStyledTextSegments(['fontName'])) await figma.loadFontAsync(seg.fontName); label.characters = ITEMS[i].label; } }
  card.children.find(k => k.name === 'Image').fills = [Object.assign({}, fill, { scaleMode: 'FILL' })];
  up.remove();
}
fit(sec, 28);
return { section: sec.id, created, changed, log };""")


def place_logos_script(page_id, step_id, items):
    return (f"const PAGE_ID = {json.dumps(page_id)}, PREFIX = {json.dumps(UPLOAD_PREFIX + step_id + '-')}, ITEMS = {json.dumps(items, ensure_ascii=False)}, NAME = {json.dumps(SECTION['logos'])};\n"
            + _PLACE_COMMON + """const like = page.children.find(n => n.type === 'SECTION' && n.name !== NAME);
let sec = null;
const contain = (art, w, h) => { art.rescale(Math.min(w / art.width, h / art.height)); };
for (let i = 0; i < ITEMS.length; i++) {
  const art = loose(i); if (!art) { log.push('MISSING upload for ' + ITEMS[i].name); continue; }
  if (ITEMS[i].slot) {
    const [setName, variant] = ITEMS[i].slot.split('|');
    const set = page.children.find(n => n.type === 'COMPONENT_SET' && n.name === setName);
    const comp = set && set.children.find(c => c.name.split('=').pop().trim() === variant);
    const box = comp && (comp.findOne(n => n.name === 'Brand artwork') || comp);
    if (!box) { log.push('slot not found: ' + ITEMS[i].slot + '; left ' + ITEMS[i].name + ' on the page'); continue; }
    const w = box.width, h = box.height;
    for (const k of [...box.children]) k.remove();
    box.appendChild(art); art.name = ITEMS[i].name; contain(art, w, h); art.x = 0; art.y = (h - art.height) / 2;
    changed.push(comp.id); log.push(ITEMS[i].slot + ' now shows ' + ITEMS[i].name);
  } else {
    sec = sec || await section(NAME, like);
    let comp = sec.children.find(c => c.type === 'COMPONENT' && c.name.toLowerCase() === ITEMS[i].name.toLowerCase());
    if (comp) { for (const k of [...comp.children]) k.remove(); changed.push(comp.id); }
    else { comp = figma.createComponent(); comp.name = ITEMS[i].name; comp.fills = []; sec.appendChild(comp);
      const n = sec.children.filter(c => c.type === 'COMPONENT').length - 1; comp.x = 28 + (n % 4) * 260; comp.y = 72 + Math.floor(n / 4) * 100; created.push(comp.id); }
    comp.description = ITEMS[i].stamp;
    contain(art, 220, 64); comp.resize(Math.max(1, art.width), Math.max(1, art.height)); comp.appendChild(art); art.x = 0; art.y = 0; art.name = 'Artwork';
  }
}
if (sec) fit(sec, 28);
return { section: sec && sec.id, created, changed, log };""")


def build_plan(brand, elements, compared, review, state):
    """Turn the approved review into ordered steps. Upload steps carry the element ids to send."""
    core = (state or {}).get("core") or {}
    page_id = {k: next((p["id"] for p in core.get("pages", []) if re.search(rf"\b({pat})\b", p["name"], re.I)), None) for k, pat in PAGE_KINDS.items()}
    actions = review.get("actions", {})
    chosen = [e for e in elements if actions.get(e["id"]) in ("add", "replace") and compared.get(e["id"], {}).get("writable")]
    src = lambda e: e.get("source_name", "")
    tokens = {"roles": {r: h for r, h in (review.get("roles") or {}).items() if r in ROLES and re.fullmatch(r"#[0-9A-Fa-f]{6}", h or "")},
              "colors": [], "gradients": [], "text": []}
    for e in chosen:
        if e["type"] == "color":
            tokens["colors"].append({"variable": slug(e["name"]), "style": colour_style_name(e), "hex": e["hex"].upper(), "stamp": _stamp(src(e))})
        elif e["type"] == "gradient":
            tokens["gradients"].append({"style": gradient_style_name(e), "kind": e.get("kind", "linear"), "angle": e.get("angle"),
                                        "stops": [[s["position"], s["hex"]] for s in e["stops"]], "stamp": _stamp(src(e))})
        elif e["type"] == "text-style":
            spec = compared[e["id"]].get("spec")
            target = (review.get("text_targets") or {}).get(e["id"]) or e["name"]
            if spec:
                style = WEIGHT_NAMES.get(spec["weight"], "Regular") + (" Italic" if spec["italic"] else "")
                tokens["text"].append({"style": target, "family": spec["family"], "fontStyle": style.replace("Regular Italic", "Italic"),
                                       "size": spec["size"], "lineHeight": spec["line_height"], "stamp": _stamp(src(e))})
    steps = []
    if any(tokens.values()):
        n = len(tokens["colors"]), len(tokens["gradients"]), len(tokens["text"]), len(tokens["roles"])
        steps.append({"id": "tokens", "kind": "script", "code": tokens_script(tokens),
                      "title": f"Write {n[0]} colours, {n[1]} gradients, {n[2]} text styles and {n[3]} colour roles"})
    slots = review.get("logo_slots") or {}
    groups = [("icons", "icon", place_icons_script, 40), ("visuals", "image", place_images_script, 30), ("logos", "logo", place_logos_script, 20)]
    skipped = []
    for kind, etype, maker, batch in groups:
        items = [e for e in chosen if (e["type"] == etype or (etype == "image" and e["type"] == "graphic")) and e.get("asset")]
        if not items:
            continue
        if not page_id[kind]:
            skipped.append(f"{len(items)} {kind}: no matching page found in the Figma file")
            continue
        for b in range(0, len(items), batch):
            part = items[b:b + batch]
            sid = f"{kind}{b // batch + 1}"
            meta = [{"name": asset_layer_name(e), "label": e["name"], "stamp": _stamp(src(e)), **({"slot": slots[e["id"]]} if kind == "logos" and slots.get(e["id"]) else {})} for e in part]
            steps.append({"id": sid + "-upload", "kind": "upload", "title": f"Upload {len(part)} {kind} files", "pageId": page_id[kind],
                          "count": len(part), "elements": [e["id"] for e in part]})
            steps.append({"id": sid + "-place", "kind": "script", "title": f"Place {len(part)} {kind} on the board", "code": maker(page_id[kind], sid, meta)})
    return {"steps": steps, "skipped": skipped}
