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
# master-component category -> how its page is named in the DesignOps template
COMPONENT_PAGES = {"headers": r"headers?", "buttons": r"buttons?", "image": r"images?", "disclaimer": r"disclaimers?",
                   "roundels": r"roundels?", "cards": r"cards?", "price-points": r"price\s*points?", "base-plate": r"base\s*plates?",
                   "gradients": r"gradients?"}
SPEC_KEYS = ("fill", "text_color", "border_color", "border_width", "corner_radius", "text_case", "font_family", "font_weight",
             "font_size", "sample_text")
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
const resolve = v => { let val = first(v, roleCol), n = 0;
  while (val && val.type === 'VARIABLE_ALIAS' && n++ < 6) { const t = byId[val.id]; if (!t) return null; const c = cols.find(x => x.id === t.variableCollectionId); val = t.valuesByMode[c.defaultModeId]; }
  return val && typeof val === 'object' && 'r' in val ? hx(val) : null; };
const RN = { 'Primary': 'primary', 'Secondary': 'secondary', 'Tertiary': 'tertiary', 'Text color': 'text', 'Background color': 'background' };
const roles = roleCol ? Object.fromEntries(vars.filter(v => v.variableCollectionId === roleCol.id && RN[v.name]).map(v => [RN[v.name], resolve(v)])) : {};
const fontVars = roleCol ? Object.fromEntries(vars.filter(v => v.variableCollectionId === roleCol.id && v.resolvedType === 'STRING' && v.scopes.includes('FONT_FAMILY')).map(v => [v.name, first(v, roleCol)])) : {};
const paints = (await figma.getLocalPaintStylesAsync()).map(s => { const p = s.paints[0] || {}; const o = { n: s.name, d: (s.description || '').slice(0, 80) };
  if (p.type === 'SOLID') { o.hex = hx(p.color); const b = p.boundVariables && p.boundVariables.color; if (b && byId[b.id]) o.v = byId[b.id].name; }
  else if (p.gradientStops) o.g = p.gradientStops.map(g => [Math.round(g.position * 1000) / 1000, hx(g.color), Math.round(g.color.a * 100) / 100]);
  return o; });
const placeholderGradients = paints.filter(p => p.g && /placeholder/i.test(p.d)).map(p => p.n);
const texts = (await figma.getLocalTextStylesAsync()).map(s => ({ n: s.name, f: s.fontName.family, s: s.fontName.style, z: s.fontSize, lh: s.lineHeight.unit === 'AUTO' ? null : Math.round(s.lineHeight.value * 10) / 10, lu: s.lineHeight.unit }));
return { file: figma.root.name, pages: figma.root.children.map(p => ({ id: p.id, name: p.name })), hasColorCollection: !!colorCol, colorVars, roles, fontVars, placeholderGradients,
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
  const comp = kids.find(k => k.type === 'COMPONENT');
  const drawn = n => n.type === 'VECTOR' || n.type === 'BOOLEAN_OPERATION' || hasImage(n);
  const dummy = k => k.type === 'COMPONENT' ? !k.findOne(drawn) : k.children.some(c => c.name === 'Image') && !k.children.some(c => (c.name === 'Image' && hasImage(c)) || c.name === 'Artwork');
  const pic = kids.filter(k => k.type === 'FRAME').map(k => k.children.find(c => c.name === 'Image')).find(Boolean);
  return {{ name: s.name, img: pic ? [Math.round(pic.width), Math.round(pic.height)] : null, ph: kids.filter(dummy).length, names: kids.filter(k => !inSeq.has(k.name)).map(k => k.name), seq,
    tmpl: comp ? [Math.round(comp.width), comp.children[0] ? Math.round(Math.max(comp.children[0].width, comp.children[0].height)) : 0] : null,
    images: kids.filter(k => k.type === 'FRAME' && k.children.some(c => c.name === 'Image' && hasImage(c))).length }};
}});
const box = c => {{ const b = c.findOne(n => n.name === 'Brand artwork') || c; return [Math.round(b.width), Math.round(b.height)]; }};
const sets = page.children.filter(n => n.type === 'COMPONENT_SET').map(s => ({{ name: s.name, variants: s.children.map(c => c.name), box: s.children[0] ? box(s.children[0]) : null }}));
return {{ page: page.name, sections, sets }};"""


def section_names(sec):
    out = list(sec.get("names", []))
    for prefix, first, width, count in sec.get("seq", []):
        out += [f"{prefix}{first + i:0{width}d}" for i in range(count)]
    return out


GUIDELINES_PAGE = "→ Guidelines"
_HASH_JS = "const hash = t => { let n = 5381; for (let i = 0; i < t.length; i++) n = (n * 33 + t.charCodeAt(i)) >>> 0; return n; };\n"


def _hash(text):
    """Same sum as _HASH_JS, so a card on the board can be compared with a guideline without reading its text back."""
    n = 5381
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):
        n = (n * 33 + int.from_bytes(data[i:i + 2], "little")) & 0xFFFFFFFF
    return n


def guideline_card(el):
    """Name, group and body text of the card a guideline becomes; None when there is nothing to say."""
    name = re.sub(r"\s+", " ", el.get("name") or "").strip()
    lines = [el["description"].strip()] if el.get("description") else []
    lines += [f"{k}: {v}" for k, v in (el.get("props") or {}).items() if isinstance(v, (str, int, float)) and str(v).strip()]
    lines += ["• " + r.strip() for r in el.get("rules") or [] if r and r.strip()]
    if not name or not lines:
        return None
    group = name.split("/", 1)[0].strip() if "/" in name else (el.get("section") or "General").strip() or "General"
    pages = (el.get("source") or {}).get("pages") or []
    source = " · ".join(x for x in (el.get("source_name", ""), ("p" + ", p".join(str(n) for n in pages[:4])) if pages else "") if x)
    return {"name": "Guideline/" + name[:90], "title": name.split("/", 1)[-1].strip()[:90], "group": group[:60], "body": "\n".join(lines)[:3000], "source": source[:120]}


def scan_guidelines_script(page_id):
    return f"const PAGE_ID = {json.dumps(page_id)};\n" + _HASH_JS + """const page = await figma.getNodeByIdAsync(PAGE_ID);
await figma.setCurrentPageAsync(page);
const cards = page.findAll(n => n.type === 'FRAME' && n.name.startsWith('Guideline/'));
return { cards: cards.map(c => { const b = c.children.find(k => k.type === 'TEXT' && k.name === 'Body'); return [c.name, b ? hash(b.characters) : 0]; }) };"""


def guidelines_script(page_id, items, dry=False):
    """Write each guideline as a text card on a Guidelines page, grouped by topic. Cards are updated by name, never removed."""
    return (f"const PAGE_ID = {json.dumps(page_id)}, PAGE_NAME = {json.dumps(GUIDELINES_PAGE)}, DRY = {json.dumps(dry)};\n"
            "const ITEMS = " + json.dumps(items, ensure_ascii=False) + ";\n") + """const log = [], touched = [];
let page = PAGE_ID ? await figma.getNodeByIdAsync(PAGE_ID) : figma.root.children.find(p => /guidelines?/i.test(p.name));
if (DRY) return { wouldCreatePage: !page, cards: ITEMS.length, groups: [...new Set(ITEMS.map(i => i.group))] };
if (!page) { page = figma.createPage(); page.name = PAGE_NAME;
  // keep it with the token pages, ahead of the master components
  const at = figma.root.children.findIndex(p => /master components/i.test(p.name));
  if (at > 0) figma.root.insertChild(at, page);
  log.push('new page ' + PAGE_NAME); }
await figma.setCurrentPageAsync(page);
let F = 'Inter';
try { await figma.loadFontAsync({ family: 'Inter', style: 'Regular' }); await figma.loadFontAsync({ family: 'Inter', style: 'Bold' }); }
catch (e) { F = 'Arial'; await figma.loadFontAsync({ family: 'Arial', style: 'Regular' }); await figma.loadFontAsync({ family: 'Arial', style: 'Bold' }); }
const ink = { r: .09, g: .1, b: .12 }, soft = { r: .42, g: .45, b: .5 }, solid = c => [{ type: 'SOLID', color: c }];
const stack = (name, gap, pad) => { const f = figma.createFrame(); f.name = name; f.layoutMode = 'VERTICAL'; f.itemSpacing = gap;
  f.paddingTop = f.paddingBottom = f.paddingLeft = f.paddingRight = pad; f.primaryAxisSizingMode = 'AUTO'; f.counterAxisSizingMode = 'AUTO'; f.fills = []; return f; };
const text = (parent, name, style, size, colour) => { const t = figma.createText(); t.name = name; t.fontName = { family: F, style }; t.fontSize = size; t.fills = solid(colour);
  parent.appendChild(t); t.layoutSizingHorizontal = 'FILL'; t.textAutoResize = 'HEIGHT'; return t; };
const set = async (t, chars) => { for (const seg of t.getStyledTextSegments(['fontName'])) await figma.loadFontAsync(seg.fontName); t.characters = chars; };
let root = page.children.find(n => n.type === 'FRAME' && n.name === 'Brand Guidelines');
if (!root) { root = stack('Brand Guidelines', 64, 64); root.fills = solid({ r: .96, g: .96, b: .97 }); page.appendChild(root);
  root.x = page.children.length > 1 ? Math.max(...page.children.filter(n => n !== root).map(n => n.x + n.width)) + 200 : 0; root.y = 0; }
for (const it of ITEMS) {
  let group = root.children.find(n => n.name === 'Guideline group/' + it.group);
  if (!group) { group = stack('Guideline group/' + it.group, 24, 0); root.appendChild(group);
    const head = figma.createText(); head.name = 'Group title'; head.fontName = { family: F, style: 'Bold' }; head.fontSize = 32; head.fills = solid(ink); head.characters = it.group; group.appendChild(head);
    const row = figma.createFrame(); row.name = 'Cards'; row.fills = []; row.layoutMode = 'HORIZONTAL'; row.layoutWrap = 'WRAP'; row.itemSpacing = 24; row.counterAxisSpacing = 24;
    group.appendChild(row); row.resize(1832, 100); row.primaryAxisSizingMode = 'FIXED'; row.counterAxisSizingMode = 'AUTO'; }
  const row = group.children.find(n => n.name === 'Cards');
  let card = row.children.find(n => n.name === it.name) || root.findOne(n => n.type === 'FRAME' && n.name === it.name);
  const fresh = !card;
  if (fresh) { card = stack(it.name, 12, 24); card.fills = solid({ r: 1, g: 1, b: 1 }); card.cornerRadius = 12; row.appendChild(card);
    card.resize(440, 100); card.counterAxisSizingMode = 'FIXED'; card.primaryAxisSizingMode = 'AUTO';
    text(card, 'Title', 'Bold', 18, ink); const b = text(card, 'Body', 'Regular', 13, ink); b.lineHeight = { unit: 'PERCENT', value: 150 }; text(card, 'Source', 'Regular', 11, soft); }
  const part = n => card.children.find(k => k.type === 'TEXT' && k.name === n);
  if (part('Title')) await set(part('Title'), it.title);
  if (part('Body')) await set(part('Body'), it.body);
  if (part('Source')) await set(part('Source'), [it.source, it.stamp].filter(Boolean).join('\\n'));
  touched.push(card.id); log.push((fresh ? 'new guideline ' : 'updated ') + it.name);
}
return { page: page.name, pageId: page.id, cards: touched.length, groups: root.children.length, log };"""


def usage_notes(guidelines, elements):
    """Short rules that name one colour, gradient or text style, or one kind of master component.

    Returns ({style name: [rules]}, {component category: [rules]}). A rule is attached only when it names the
    item outright, so general rules stay on the Guidelines page alone."""
    named = {}
    for e in elements:
        style = colour_style_name(e) if e["type"] == "color" and e.get("hex") else gradient_style_name(e) if e["type"] == "gradient" else e["name"] if e["type"] == "text-style" else None
        label = re.sub(r"^(gradient|brand|type)\s*/\s*", "", e.get("name") or "", flags=re.I).strip()
        if style and len(label) >= 3 and not label.lower().startswith("observed"):
            named[style] = re.compile(rf"(?<![\w-]){re.escape(label)}(?![\w-])", re.I)
    styles, sets = {}, {}
    for g in guidelines:
        for rule in [r.strip() for r in g.get("rules") or [] if r and 0 < len(r.strip()) <= 180]:
            for style, pat in named.items():
                if pat.search(rule) and rule not in styles.setdefault(style, []):
                    styles[style].append(rule)
        for cat, pat in COMPONENT_PAGES.items():
            if cat != "image" and re.search(rf"\b({pat})\b", g.get("name") or "", re.I):
                sets.setdefault(cat, []).extend(r.strip() for r in g.get("rules") or [] if r and len(r.strip()) <= 180)
    return {k: v[:5] for k, v in styles.items() if v}, {k: v[:6] for k, v in sets.items() if v}


_NOTE_JS = """const withNotes = (old, rules) => { const keep = (old || '').split('\\n').filter(l => l && !l.startsWith('Usage: ') && !l.startsWith('• '));
  return ['Usage: ' + rules[0], ...rules.slice(1).map(r => '• ' + r), ...keep].join('\\n').slice(0, 900); };
"""


def style_notes_script(notes):
    """Put the rules that name a style into that style's description, where Figma shows them on hover."""
    return "const NOTES = " + json.dumps(notes, ensure_ascii=False) + ";\n" + _NOTE_JS + """const log = [];
const styles = [...await figma.getLocalPaintStylesAsync(), ...await figma.getLocalTextStylesAsync()];
for (const [name, rules] of Object.entries(NOTES)) {
  const st = styles.find(x => x.name.toLowerCase() === name.toLowerCase());
  if (!st) { log.push('SKIPPED usage note for ' + name + ': no such style on the board'); continue; }
  st.description = withNotes(st.description, rules); log.push('usage note on ' + st.name + ' (' + rules.length + ')');
}
return { log };"""


def component_notes_script(page_id, rules):
    return f"const PAGE_ID = {json.dumps(page_id)}, RULES = {json.dumps(rules, ensure_ascii=False)};\n" + _NOTE_JS + """const page = await figma.getNodeByIdAsync(PAGE_ID);
await figma.setCurrentPageAsync(page);
const set = page.children.find(n => n.type === 'COMPONENT_SET');
if (!set) return { log: ['SKIPPED usage note: no master component on ' + page.name] };
set.description = withNotes(set.description, RULES);
return { set: set.name, log: ['usage note on the ' + set.name + ' master component (' + RULES.length + ')'] };"""


def scan_steps_core():
    return [{"id": "core", "kind": "script", "title": "Read variables, styles and pages", "code": scan_core_script()}]


def scan_steps_pages(core):
    steps = []
    for kind, pattern in PAGE_KINDS.items():
        page = next((p for p in core.get("pages", []) if re.search(rf"\b({pattern})\b", p["name"], re.I)), None)
        if page:
            steps.append({"id": kind, "kind": "script", "title": f"Read the {page['name']} page", "code": scan_page_script(page["id"])})
    page = _guidelines_page(core)
    if page:
        steps.append({"id": "guidelines", "kind": "script", "title": f"Read the {page['name']} page", "code": scan_guidelines_script(page["id"])})
    return steps


def _guidelines_page(core):
    return next((p for p in core.get("pages", []) if re.search(r"\bguidelines?\b", p["name"], re.I)), None)


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
        words = dict({n.lower().replace(" ", ""): w for w, n in WEIGHT_NAMES.items()}, heavy=800, demibold=600, demi=600, book=400, ultralight=200, hairline=100)
        word = next((k for k in sorted(words, key=len, reverse=True) if k in raw), None)
        m = re.search(r"\b([1-9]00)\b", raw)
        weight = int(m.group(1)) if m else words.get(word, 400)
        if word in ("heavy", "book", "demi", "demibold"):
            style_name = word.title().replace("Demibold", "DemiBold")
    italic = bool(el.get("italic")) or "italic" in json.dumps(props).lower()
    style_name = locals().get("style_name")
    if not family or not size:
        return None
    family = re.split(r"[,;]", str(family))[0].strip().strip("\"'")
    return {"family": family, "weight": int(weight), "italic": italic, "size": float(size),
            "line_height": el.get("line_height_percent"), "style_name": style_name}


def component_spec(el, palette):
    """What a brief states about a component, reduced to values a master-component variant can take.

    Uses the structured `spec` Claude returns where present, and otherwise reads the free-form
    properties. Colours may be given as hex or by the brand's own colour names.
    """
    raw = {k: str(v).strip() for k, v in (el.get("spec") or {}).items() if str(v).strip()}
    props = {re.sub(r"[\s_]+", "-", k.lower()): str(v) for k, v in (el.get("properties") or {}).items()}

    def first(key, *hints):
        if raw.get(key):
            return raw[key]
        return next((v for k, v in props.items() if any(h == k or h in k for h in hints)), "")

    def colour(value):
        m = re.search(r"#[0-9A-Fa-f]{6}\b", value or "")
        if m:
            return m.group(0).upper()
        low = slug(value or "")
        hit = [c for c in palette if slug(c["name"]) and slug(c["name"]) in low]
        return max(hit, key=lambda c: len(c["name"]))["hex"].upper() if hit else None

    def number(value):
        m = re.search(r"(\d+(?:\.\d+)?)", value or "")
        return float(m.group(1)) if m else None

    radius_text = first("corner_radius", "radius", "shape", "corner").lower()
    radius = 999.0 if re.search(r"pill|fully|circle|circular|999", radius_text) else number(radius_text)
    font = first("font_family", "font-family", "family", "typeface")
    weight_text = (first("font_weight", "weight") + " " + (props.get("text-style", "") if not raw.get("font_weight") else "")).lower().replace(" ", "")
    words = dict({n.lower().replace(" ", ""): w for w, n in WEIGHT_NAMES.items()}, heavy=800, demibold=600, book=400)
    word = next((k for k in sorted(words, key=len, reverse=True) if k in weight_text), None)
    spec = {
        "fill": colour(first("fill", "fill", "background", "surface")),
        "textColor": colour(first("text_color", "text-color", "text-colour", "label-color", "font-color")),
        "stroke": colour(first("border_color", "border", "stroke", "outline")),
        "strokeWeight": number(first("border_width", "border-width", "stroke-width", "stroke-weight")),
        "radius": radius,
        "textCase": "UPPER" if re.search(r"upper|all\s*caps|capital", first("text_case", "case", "capital").lower()) else None,
        "fontFamily": re.split(r"[,;]", font)[0].strip().strip("\"'") or None,
        "fontWeight": words.get(word) if word else None,
        "fontStyle": (word.title() if word in ("heavy", "book") else WEIGHT_NAMES.get(words[word])) if word else None,
        "fontSize": number(first("font_size", "font-size", "text-size")) if re.search(r"\d", first("font_size", "font-size", "text-size")) else None,
        "sample": first("sample_text", "label", "sample")[:60] or None,
    }
    return {k: v for k, v in spec.items() if v not in (None, "")}


def compare(elements, state):
    """Annotate each element with how it relates to what is on the board. Returns {id: info}."""
    core = (state or {}).get("core") or {}
    pages = {k: (state or {}).get(k) or {} for k in PAGE_KINDS}
    paints = {p["n"].lower(): p for p in core.get("paints", [])}
    colour_vars = {v["n"].lower(): v for v in core.get("colorVars", [])}
    grads = [p for p in core.get("paints", []) if p.get("g")]
    names = {k: {slug(n) for sec in pages[k].get("sections", []) for n in section_names(sec)} for k in PAGE_KINDS}
    out = {}
    cards = {n.lower(): h for n, h in ((state or {}).get("guidelines") or {}).get("cards", [])}
    palette = [{"name": e["name"], "hex": e["hex"]} for e in elements if e["type"] == "color" and e.get("hex")]
    comp_pages = {c: next((pg["name"] for pg in core.get("pages", []) if re.search(rf"\b({pat})\b", pg["name"], re.I)), None) for c, pat in COMPONENT_PAGES.items()}
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
        elif t == "component":
            cat, spec = el.get("category"), component_spec(el, palette)
            if cat not in COMPONENT_PAGES:
                info.update(writable=False, status="reference", note="No matching master component in the DesignOps template, so this is kept for reference.")
            elif not comp_pages.get(cat):
                info.update(writable=False, status="reference", note=f"This Figma file has no {cat} master-component page.")
            elif not spec:
                info.update(writable=False, status="reference", note="The brief describes this component but states no value that can be set (fill, radius, text colour, case or font).")
            else:
                info.update(spec=spec, target=comp_pages[cat].lstrip("→ ").strip(),
                            note="Added as a new variant of the master component, using only the values the brief states. The template's own variants are left as they are.")
        elif t == "guideline":
            card = guideline_card(el)
            if not card:
                info.update(writable=False, status="reference", note="Nothing to write: this guideline has no rules or description.")
            else:
                on_board = cards.get(card["name"].lower())
                info.update(target=GUIDELINES_PAGE, note="Written as a text card on a Guidelines page in the Figma file. Rules that name a colour, gradient, text style or master component are also added to its description. DesignOps may not show the page.")
                if on_board is not None:
                    info.update(status="same" if on_board == _hash(card["body"]) else "changed")
        elif t == "font-family":
            info.update(writable=False, status="reference", note="Set as the board's Headline, Copy or Footnote font in the Brand fonts panel.")
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
            "roles": core.get("roles") or {v["n"]: v["hex"] for v in core.get("colorVars", []) if v["n"] in ROLES},
            "paints": core.get("paints", []), "texts": core.get("texts", []), "font_vars": core.get("fontVars") or {},
            "placeholders": {"icons": sum(sec.get("ph", 0) for sec in ((state or {}).get("icons") or {}).get("sections", [])),
                             "visuals": sum(sec.get("ph", 0) for sec in ((state or {}).get("visuals") or {}).get("sections", [])),
                             "gradients": len(core.get("placeholderGradients", []))},
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
if (Object.keys(D.roles).length) {
  // Roles live in their own collection ("Primary", "Text color"…) and alias colour variables whose
  // names vary from file to file, so each role gets its own colour variable and is re-pointed at it.
  const RN = { primary: 'Primary', secondary: 'Secondary', tertiary: 'Tertiary', text: 'Text color', background: 'Background color' };
  const roleCol = cols.find(c => vars.some(v => v.variableCollectionId === c.id && v.name === 'Primary'));
  if (!roleCol) log.push('SKIPPED colour roles: this file has no role variables (Primary, Secondary, …)');
  else {
    const rv = Object.fromEntries(Object.entries(RN).map(([k, n]) => [k, vars.find(v => v.variableCollectionId === roleCol.id && v.name === n)]));
    const aliasOf = (v, modeId) => { const a = v && v.valuesByMode[modeId]; return a && a.type === 'VARIABLE_ALIAS' ? a.id : null; };
    const def = Object.fromEntries(Object.keys(RN).map(k => [k, aliasOf(rv[k], roleCol.defaultModeId)]));
    const fresh = {};
    for (const [role, hex] of Object.entries(D.roles)) { if (rv[role]) fresh[role] = colourVar(role, hex); else log.push('SKIPPED role ' + role + ': no such role variable'); }
    for (const role of Object.keys(fresh)) for (const m of roleCol.modes) {
      const cur = aliasOf(rv[role], m.modeId);
      let target = fresh[role];
      if (cur && cur !== def[role]) {  // this mode borrows another role's colour (dark mode swaps text and background)
        const owners = Object.keys(def).filter(k => def[k] === cur);
        target = owners.length === 1 ? (fresh[owners[0]] || null) : fresh[role];
      }
      if (target) rv[role].setValueForMode(m.modeId, figma.variables.createVariableAlias(target));
    }
    for (const role of Object.keys(fresh)) log.push('role ' + role + ' = ' + D.roles[role]);
  }
}
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
// The template's font variables hold a family name as text, so they can be set before the font is installed.
for (const [name, family] of Object.entries(D.fonts || {})) {
  const v = vars.find(x => x.name === name && x.resolvedType === 'STRING' && x.scopes.includes('FONT_FAMILY'));
  if (!v) { log.push('SKIPPED font ' + name + ': no such font variable'); continue; }
  const col = cols.find(c => c.id === v.variableCollectionId);
  for (const m of col.modes) { const cur = v.valuesByMode[m.modeId]; if (!(cur && cur.type === 'VARIABLE_ALIAS')) v.setValueForMode(m.modeId, family); }
  log.push('font ' + name + ' = ' + family);
}
if (D.text.length) {
  const fonts = await figma.listAvailableFontsAsync();
  const tstyles = await figma.getLocalTextStylesAsync();
  for (const t of D.text) {
    const family = n => fonts.filter(f => f.fontName.family.toLowerCase() === n.toLowerCase());
    let fam = family(t.family), sub = false;
    if (!fam.length && t.fallback) { fam = family(t.fallback); sub = fam.length > 0; }
    if (!fam.length) { log.push('SKIPPED ' + t.style + ': font "' + t.family + '" is not installed in Figma'); continue; }
    const norm = x => x.toLowerCase().replace(/[^a-z]/g, '');
    const W = { thin: 100, extralight: 200, light: 300, regular: 400, book: 400, medium: 500, semibold: 600, demibold: 600, bold: 700, extrabold: 800, heavy: 800, black: 900 };
    const wOf = st => { const k = Object.keys(W).sort((a, b) => b.length - a.length).find(k => norm(st).includes(k)); return k ? W[k] : 400; };
    const ital = st => /italic|oblique/i.test(st);
    // exact style name if it exists, otherwise the nearest weight with the same slant
    const font = fam.find(f => norm(f.fontName.style) === norm(t.fontStyle))
      || [...fam].sort((a, b) => (ital(a.fontName.style) !== ital(t.fontStyle)) - (ital(b.fontName.style) !== ital(t.fontStyle)) || Math.abs(wOf(a.fontName.style) - t.weight) - Math.abs(wOf(b.fontName.style) - t.weight))[0];
    await figma.loadFontAsync(font.fontName);
    let s = tstyles.find(x => x.name.toLowerCase() === t.style.toLowerCase());
    if (!s) { s = figma.createTextStyle(); s.name = t.style; log.push('new text style ' + t.style); } else log.push('updated ' + s.name);
    s.fontName = font.fontName; s.fontSize = t.size;
    s.description = sub ? ('SUBSTITUTE FONT: should be ' + t.family + ' ' + t.fontStyle + '. ' + t.stamp).slice(0, 250) : t.stamp;
    if (sub) log.push('note: ' + t.style + ' written in ' + font.fontName.family + ' ' + font.fontName.style + ' because "' + t.family + '" is not installed; swap the font once it is');
    if (t.lineHeight) s.lineHeight = { unit: 'PERCENT', value: t.lineHeight };
    if (!sub && norm(font.fontName.style) !== norm(t.fontStyle)) log.push('note: ' + t.style + ' uses ' + font.fontName.style + '; "' + t.fontStyle + '" was not available');
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
// Vector files are uploaded already at their final size. rescale() is avoided on purpose: the Figma app
// kept drawing rescaled artwork at its old geometry until someone nudged the frame.
// An uploaded picture arrives in a box of a fixed shape, with the picture cropped to fill it.
// Give the box the picture's own proportions so nothing is cut off.
const natural = async art => { const f = Array.isArray(art.fills) && art.fills.find(x => x.type === 'IMAGE'); if (!f) return;
  const sz = await figma.getImageByHash(f.imageHash).getSizeAsync(); if (sz.width && sz.height) art.resize(Math.max(1, art.height * sz.width / sz.height), art.height); };
const scaleTo = (art, f) => { if (Math.abs(f - 1) < 0.02) return;
  art.resize(Math.max(0.01, art.width * f), Math.max(0.01, art.height * f));
  if ('findAll' in art) for (const n of art.findAll(k => 'strokeWeight' in k && typeof k.strokeWeight === 'number' && Array.isArray(k.strokes) && k.strokes.length > 0)) n.strokeWeight = n.strokeWeight * f; };
const fit = (sec, pad) => { const kids = sec.children; if (!kids.length) return;
  sec.resizeWithoutConstraints(Math.max(sec.width, Math.max(...kids.map(k => k.x + k.width)) + pad), Math.max(...kids.map(k => k.y + k.height)) + pad); };
"""


def colours_board_script(page_id, items):
    """Add or refresh a specimen card on the Colours page for each colour, bound to its variable."""
    return (_JS_HELPERS + f"const PAGE_ID = {json.dumps(page_id)}, ITEMS = {json.dumps(items, ensure_ascii=False)};\n" + """const page = await figma.getNodeByIdAsync(PAGE_ID);
await figma.setCurrentPageAsync(page);
const log = [], created = [], changed = [];
const vars = await figma.variables.getLocalVariablesAsync();
const cols = await figma.variables.getLocalVariableCollectionsAsync();
const colorCol = cols.find(c => /^colou?rs?$/i.test(c.name));
const isSpec = n => n.type === 'FRAME' && n.children.some(k => k.name === 'Color block');
const group = page.findOne(n => n.type === 'FRAME' && n.children.some(isSpec));
if (!group) throw new Error('No colour specimen to copy on this page (a frame containing a layer named "Color block").');
const specs = () => group.children.filter(isSpec);
const tmpl = specs()[0];
const uniq = a => [...new Set(a.map(Math.round))].sort((x, y) => x - y);
const xs = uniq(specs().map(c => c.x)), ys = uniq(specs().map(c => c.y));
const perRow = Math.max(1, xs.length), dx = xs.length > 1 ? xs[1] - xs[0] : tmpl.width + 18, dy = ys.length > 1 ? ys[1] - ys[0] : tmpl.height + 20;
const padBottom = Math.max(16, group.height - Math.max(...specs().map(c => c.y + c.height)));
const setText = async (t, str) => { for (const seg of t.getStyledTextSegments(['fontName'])) await figma.loadFontAsync(seg.fontName); t.characters = str; };
const prefix = tmpl.name.includes('/') ? tmpl.name.slice(0, tmpl.name.lastIndexOf('/') + 1) : 'Colour specimen/';
for (const it of ITEMS) {
  const v = colorCol && vars.find(x => x.variableCollectionId === colorCol.id && x.name === it.variable);
  let card = specs().find(c => c.name.toLowerCase() === (prefix + it.name).toLowerCase());
  if (card) changed.push(card.id);
  else { const n = specs().length; card = tmpl.clone(); group.appendChild(card); card.name = prefix + it.name;
    card.x = xs[0] + (n % perRow) * dx; card.y = ys[0] + Math.floor(n / perRow) * dy; created.push(card.id); }
  const block = card.children.find(k => k.name === 'Color block');
  const paint = { type: 'SOLID', color: rgb(it.hex) };
  block.fills = [v ? figma.variables.setBoundVariableForPaint(paint, 'color', v) : paint];
  const label = card.findOne(k => k.type === 'TEXT' && k.name === 'Colour Name');
  if (label) await setText(label, it.name);
}
// every HEX label is plain text, so rewrite each one from the colour its block now shows
for (const c of specs()) {
  const f = c.children.find(k => k.name === 'Color block').fills[0], t = c.findOne(k => k.type === 'TEXT' && k.name === 'Colour HEX');
  if (f && f.type === 'SOLID' && t) { const h = hx(f.color).toLowerCase(); if (t.characters !== h) { await setText(t, h); if (!created.includes(c.id) && !changed.includes(c.id)) changed.push(c.id); } }
}
const need = Math.max(...specs().map(c => c.y + c.height)) + padBottom;
if (need > group.height) {
  const grow = need - group.height; group.resize(group.width, need);
  const sec = group.parent; if (sec && sec.type === 'SECTION') sec.resizeWithoutConstraints(sec.width, sec.height + grow);
}
return { group: group.id, created, changed, log };""")


def components_script(page_id, items):
    """Add (or refresh) one variant per brand component on a master-component page."""
    return (_JS_HELPERS + f"const PAGE_ID = {json.dumps(page_id)}, ITEMS = {json.dumps(items, ensure_ascii=False)};\n" + """const page = await figma.getNodeByIdAsync(PAGE_ID);
await figma.setCurrentPageAsync(page);
const log = [], created = [], changed = [];
const set = page.children.find(n => n.type === 'COMPONENT_SET');
if (!set) throw new Error('No master component set on this page.');
const vars = await figma.variables.getLocalVariablesAsync();
const cols = await figma.variables.getLocalVariableCollectionsAsync();
const colorCol = cols.find(c => /^colou?rs?$/i.test(c.name));
const fonts = await figma.listAvailableFontsAsync();
const prop = Object.keys(set.componentPropertyDefinitions)[0] || 'Variant';
const variants = () => set.children.filter(c => c.type === 'COMPONENT');
const uniq = a => [...new Set(a.map(Math.round))].sort((x, y) => x - y);
const xs = uniq(variants().map(c => c.x)), ys = uniq(variants().map(c => c.y)), first = variants()[0];
const perRow = Math.max(1, xs.length), dx = xs.length > 1 ? xs[1] - xs[0] : first.width + 20, dy = ys.length > 1 ? ys[1] - ys[0] : first.height + 20;
const pad = Math.max(0, set.width - Math.max(...variants().map(c => c.x + c.width)));
// a colour is bound to the brand's variable when one holds exactly that value
const paintFor = hex => { const p = { type: 'SOLID', color: rgb(hex) };
  const v = colorCol && vars.find(x => { if (x.variableCollectionId !== colorCol.id || x.resolvedType !== 'COLOR') return false; const val = x.valuesByMode[colorCol.defaultModeId]; return val && 'r' in val && hx(val) === hex.toUpperCase(); });
  return v ? figma.variables.setBoundVariableForPaint(p, 'color', v) : p; };
const norm = x => x.toLowerCase().replace(/[^a-z]/g, '');
const W = { thin: 100, extralight: 200, light: 300, regular: 400, book: 400, medium: 500, semibold: 600, demibold: 600, bold: 700, extrabold: 800, heavy: 800, black: 900 };
const wOf = st => { const k = Object.keys(W).sort((a, b) => b.length - a.length).find(k => norm(st).includes(k)); return k ? W[k] : 400; };
const loadOwn = async t => { for (const seg of t.getStyledTextSegments(['fontName'])) await figma.loadFontAsync(seg.fontName); };
const attempt = (what, name, fn) => { try { fn(); } catch (e) { log.push('note: could not set ' + what + ' on ' + name + ' (' + String(e && e.message || e).slice(0, 80) + ')'); } };
for (const it of ITEMS) {
  const s = it.spec, vname = prop + '=' + it.name;
  let comp = variants().find(c => c.name.toLowerCase() === vname.toLowerCase());
  if (comp) changed.push(comp.id);
  else { const base = (it.base && variants().find(c => c.name.toLowerCase().endsWith('=' + it.base))) || first;
    const n = variants().length; comp = base.clone(); set.appendChild(comp); comp.name = vname;
    comp.x = xs[0] + (n % perRow) * dx; comp.y = ys[0] + Math.floor(n / perRow) * dy; created.push(comp.id); }
  comp.description = it.stamp;
  const slots = comp.findAll(k => k.name.startsWith('slot:'));
  const surface = slots.find(k => k.type !== 'TEXT' && !/image/i.test(k.name)), texts = slots.filter(k => k.type === 'TEXT');
  const main = texts.find(k => /label|title/i.test(k.name)) || texts[0];
  if (surface) {
    if (s.fill) attempt('fill', it.name, () => { surface.fills = [paintFor(s.fill)]; });
    if (s.stroke) attempt('border', it.name, () => { surface.strokes = [paintFor(s.stroke)]; surface.strokeWeight = s.strokeWeight || 1; });
    if (s.radius != null && 'cornerRadius' in surface) attempt('corner radius', it.name, () => { surface.cornerRadius = Math.min(s.radius, Math.min(surface.width, surface.height) / 2 > 0 && s.radius >= 999 ? 999 : s.radius); });
  }
  let font = null;
  if (s.fontFamily) { const fam = fonts.filter(f => f.fontName.family.toLowerCase() === s.fontFamily.toLowerCase());
    if (!fam.length) log.push('note: ' + it.name + ' keeps the template font because "' + s.fontFamily + '" is not installed in Figma');
    else { const want = s.fontStyle || 'Regular', wt = s.fontWeight || 400;
      font = (fam.find(f => norm(f.fontName.style) === norm(want)) || [...fam].sort((a, b) => Math.abs(wOf(a.fontName.style) - wt) - Math.abs(wOf(b.fontName.style) - wt))[0]).fontName;
      await figma.loadFontAsync(font); } }
  for (const t of texts) {
    await loadOwn(t);
    if (s.textColor) attempt('text colour', it.name, () => { t.fills = [paintFor(s.textColor)]; });
    if (font) attempt('font', it.name, () => { t.fontName = font; });
    if (s.textCase) attempt('text case', it.name, () => { t.textCase = s.textCase; });
  }
  if (main) { if (s.fontSize) attempt('font size', it.name, () => { main.fontSize = s.fontSize; }); if (s.sample) attempt('sample text', it.name, () => { main.characters = s.sample.replace(/\\s+\\/\\s+/g, '\\n'); }); }
  // Some masters (headers, for one) are text only. Where the brief gives a fill, or light text that would
  // vanish on the specimen, a backing shape goes behind the text so the variant shows what the brief describes.
  const lum = h => { const c = rgb(h); return 0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b; };
  const lightText = s.textColor && lum(s.textColor) > 0.8;
  if (!surface && main && (s.fill || s.stroke || lightText)) {
    let back = comp.children.find(k => k.name === 'Brand backing');
    if (!back) { back = figma.createRectangle(); back.name = 'Brand backing'; comp.insertChild(0, back); }
    const box = texts.filter(t => t.visible), x0 = Math.min(...box.map(t => t.x)), y0 = Math.min(...box.map(t => t.y));
    const x1 = Math.max(...box.map(t => t.x + t.width)), y1 = Math.max(...box.map(t => t.y + t.height));
    back.x = Math.max(0, x0 - 20); back.y = Math.max(0, y0 - 14); back.resize(Math.min(comp.width - back.x, x1 - x0 + 40), Math.min(comp.height - back.y, y1 - y0 + 28));
    const fill = s.fill || (lightText ? it.backdrop : null);
    back.fills = fill ? [paintFor(fill)] : [];
    back.strokes = s.stroke ? [paintFor(s.stroke)] : []; if (s.stroke) back.strokeWeight = s.strokeWeight || 1;
    back.cornerRadius = s.radius != null ? Math.min(s.radius, back.height / 2) : 0;
    if (!s.fill && lightText) log.push('note: ' + it.name + ' has light text and no stated fill, so it is shown on ' + it.backdrop + ' to stay readable');
  }
  const tag = comp.children.find(k => k.type === 'TEXT' && k.name === 'Variant label');
  if (tag) { await loadOwn(tag); tag.characters = it.name.toUpperCase(); }
}
set.resizeWithoutConstraints(Math.max(...variants().map(c => c.x + c.width)) + pad, Math.max(...variants().map(c => c.y + c.height)) + pad);
return { set: set.name, created, changed, log };""")


def cleanup_page_script(page_id, kind, dry=False):
    """Remove the template's dummy icons or image cards, once real brand items are on the page."""
    return f"const PAGE_ID = {json.dumps(page_id)}, KIND = {json.dumps(kind)}, DRY = {json.dumps(dry)};\n" + """const page = await figma.getNodeByIdAsync(PAGE_ID);
await figma.setCurrentPageAsync(page);
const hasImage = n => 'fills' in n && Array.isArray(n.fills) && n.fills.some(f => f.type === 'IMAGE');
const drawn = n => n.type === 'VECTOR' || n.type === 'BOOLEAN_OPERATION' || hasImage(n);
const isItem = k => KIND === 'icons' ? k.type === 'COMPONENT' : k.type === 'FRAME' && k.children.some(c => c.name === 'Image');
// a dummy icon has no artwork in it; a dummy image card has no picture
const isDummy = k => KIND === 'icons' ? !k.findOne(drawn) : !k.children.some(c => (c.name === 'Image' && hasImage(c)) || c.name === 'Artwork');
const sections = page.children.filter(n => n.type === 'SECTION');
const items = sections.flatMap(sec => sec.children.filter(isItem));
const dummies = items.filter(isDummy), real = items.length - dummies.length;
if (!real) return { removed: 0, kept: dummies.length, log: ['SKIPPED: there are no brand ' + KIND + ' on this page yet, so the template placeholders were kept'] };
const emptied = sections.filter(sec => sec.children.filter(isItem).every(isDummy)).map(sec => sec.name);
const names = dummies.slice(0, 6).map(k => k.name);
if (!DRY) { for (const k of dummies) k.remove();
  for (const sec of sections) if (!sec.removed && !sec.children.some(isItem)) sec.remove(); }
return { removed: dummies.length, brandItemsKept: real, sectionsRemoved: emptied, examples: names, dryRun: DRY,
  log: [(DRY ? 'would remove ' : 'removed ') + dummies.length + ' placeholder ' + KIND + (emptied.length ? ' and the empty section' + (emptied.length > 1 ? 's ' : ' ') + emptied.join(', ') : '')] };"""


def gradients_board_script(page_id, cleanup, dry=False):
    """Give every brand gradient a variant on the Gradients master component; optionally drop the placeholders."""
    return f"const PAGE_ID = {json.dumps(page_id)}, CLEANUP = {json.dumps(cleanup)}, DRY = {json.dumps(dry)};\n" + """const page = await figma.getNodeByIdAsync(PAGE_ID);
await figma.setCurrentPageAsync(page);
const log = [], created = [], removed = [];
const set = page.children.find(n => n.type === 'COMPONENT_SET');
if (!set) throw new Error('No gradients master component on this page.');
const styles = await figma.getLocalPaintStylesAsync();
const isGradient = st => st.paints.some(p => p.type.startsWith('GRADIENT'));
const isDummy = st => /placeholder/i.test(st.description || '');
const brand = styles.filter(st => isGradient(st) && !isDummy(st)), dummies = styles.filter(st => isGradient(st) && isDummy(st));
const prop = Object.keys(set.componentPropertyDefinitions)[0] || 'Variant';
const variants = () => set.children.filter(c => c.type === 'COMPONENT');
const uniq = a => [...new Set(a.map(Math.round))].sort((x, y) => x - y);
const xs = uniq(variants().map(c => c.x)), ys = uniq(variants().map(c => c.y)), first = variants()[0];
const perRow = Math.max(1, xs.length), dx = xs.length > 1 ? xs[1] - xs[0] : first.width + 20, dy = ys.length > 1 ? ys[1] - ys[0] : first.height + 20;
const pad = Math.max(0, set.width - Math.max(...variants().map(c => c.x + c.width)));
const slotOf = c => c.findOne(k => k.name.startsWith('slot:') && k.type !== 'TEXT');
if (!DRY) for (const st of brand) {
  const label = st.name.replace(/^gradient\s*\/\s*/i, '');
  let comp = variants().find(c => c.name.toLowerCase() === (prop + '=' + label).toLowerCase());
  if (!comp) { comp = first.clone(); set.appendChild(comp); comp.name = prop + '=' + label; created.push(comp.id); }
  const slot = slotOf(comp); if (slot) await slot.setFillStyleIdAsync(st.id);
  const tag = comp.children.find(k => k.type === 'TEXT' && k.name === 'Variant label');
  if (tag) { for (const seg of tag.getStyledTextSegments(['fontName'])) await figma.loadFontAsync(seg.fontName); tag.characters = label.toUpperCase(); }
}
if (CLEANUP) {
  if (!brand.length) log.push('SKIPPED: there are no brand gradients yet, so the template placeholders were kept');
  else {
    const ids = new Set(dummies.map(st => st.id));
    const gone = variants().filter(c => { const sl = slotOf(c); return sl && typeof sl.fillStyleId === 'string' && ids.has(sl.fillStyleId); });
    removed.push(...gone.map(c => c.name), ...dummies.map(st => st.name));
    if (!DRY) { for (const c of gone) c.remove(); for (const st of dummies) st.remove(); }
    log.push((DRY ? 'would remove ' : 'removed ') + dummies.length + ' placeholder gradient styles and ' + gone.length + ' variants that showed them');
  }
}
if (!DRY) { variants().forEach((c, i) => { c.x = xs[0] + (i % perRow) * dx; c.y = ys[0] + Math.floor(i / perRow) * dy; });
  set.resizeWithoutConstraints(Math.max(...variants().map(c => c.x + c.width)) + pad, Math.max(...variants().map(c => c.y + c.height)) + pad); }
return { set: set.name, brandGradients: brand.map(st => st.name), created, removed, dryRun: DRY, log };"""


def place_icons_script(page_id, step_id, items):
    return (f"const PAGE_ID = {json.dumps(page_id)}, PREFIX = {json.dumps(UPLOAD_PREFIX + step_id + '-')}, ITEMS = {json.dumps(items, ensure_ascii=False)}, NAME = {json.dumps(SECTION['icons'])};\n"
            + _PLACE_COMMON + """const like = page.children.find(n => n.type === 'SECTION' && n.name !== NAME && n.children.some(c => c.type === 'COMPONENT'));
const sec = await section(NAME, like);
const tmpl = (like || sec).children.find(c => c.type === 'COMPONENT');
const size = tmpl ? tmpl.width : 28, glyph = tmpl && tmpl.children[0] ? Math.max(tmpl.children[0].width, tmpl.children[0].height) : 18, step = size + 22;
for (let i = 0; i < ITEMS.length; i++) {
  const art = loose(i); if (!art) { log.push('MISSING upload for ' + ITEMS[i].name); continue; }
  await natural(art);
  let comp = sec.children.find(c => c.type === 'COMPONENT' && c.name.toLowerCase() === ITEMS[i].name.toLowerCase());
  if (comp) { for (const k of [...comp.children]) k.remove(); changed.push(comp.id); }
  else { comp = figma.createComponent(); comp.name = ITEMS[i].name; comp.resize(size, size); comp.fills = tmpl ? JSON.parse(JSON.stringify(tmpl.fills)) : []; comp.clipsContent = false;
    const n = sec.children.filter(c => c.type === 'COMPONENT').length; sec.appendChild(comp);
    const perRow = Math.max(1, Math.floor((sec.width - 56) / step)); comp.x = 28 + (n % perRow) * step; comp.y = 72 + Math.floor(n / perRow) * step; created.push(comp.id); }
  comp.description = ITEMS[i].stamp;
  comp.appendChild(art); art.name = 'Glyph'; if ('fills' in art) art.fills = [];
  scaleTo(art, glyph / Math.max(art.width, art.height)); art.x = (comp.width - art.width) / 2; art.y = (comp.height - art.height) / 2;
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
  const fill = fillOf(up);
  let card = sec.children.find(c => isCard(c) && c.name.toLowerCase() === ITEMS[i].name.toLowerCase());
  if (card) changed.push(card.id);
  else { const n = sec.children.filter(isCard).length; card = tmpl.clone(); sec.appendChild(card); card.name = ITEMS[i].name;
    const perRow = Math.max(1, Math.floor((sec.width - 56 + 20) / stepX)); card.x = 28 + (n % perRow) * stepX; card.y = 58 + Math.floor(n / perRow) * stepY; created.push(card.id);
    const label = card.children.find(k => k.type === 'TEXT');
    if (label) { for (const seg of label.getStyledTextSegments(['fontName'])) await figma.loadFontAsync(seg.fontName); label.characters = ITEMS[i].label; } }
  const pic = card.children.find(k => k.name === 'Image');
  for (const old of card.children.filter(k => k.name === 'Artwork')) old.remove();
  if (fill) { pic.fills = [Object.assign({}, fill, { scaleMode: 'FILL' })]; up.remove(); continue; }
  // vector artwork has no picture to pour into the card, so the artwork itself sits in the picture area
  card.insertChild(card.children.indexOf(pic) + 1, up); up.name = 'Artwork';
  if (card.layoutMode && card.layoutMode !== 'NONE') up.layoutPositioning = 'ABSOLUTE';
  scaleTo(up, Math.min(1, pic.width * 0.9 / up.width, pic.height * 0.9 / up.height));
  up.x = pic.x + (pic.width - up.width) / 2; up.y = pic.y + (pic.height - up.height) / 2;
  if (pic.fills.some(f => f.type === 'IMAGE')) pic.fills = [{ type: 'SOLID', color: { r: .93, g: .93, b: .94 } }];
}
fit(sec, 28);
return { section: sec.id, created, changed, log };""")


def place_logos_script(page_id, step_id, items):
    return (f"const PAGE_ID = {json.dumps(page_id)}, PREFIX = {json.dumps(UPLOAD_PREFIX + step_id + '-')}, ITEMS = {json.dumps(items, ensure_ascii=False)}, NAME = {json.dumps(SECTION['logos'])};\n"
            + _PLACE_COMMON + """const like = page.children.find(n => n.type === 'SECTION' && n.name !== NAME);
let sec = null;
const contain = (art, w, h) => scaleTo(art, Math.min(w / art.width, h / art.height));
for (let i = 0; i < ITEMS.length; i++) {
  const art = loose(i); if (!art) { log.push('MISSING upload for ' + ITEMS[i].name); continue; }
  await natural(art);
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


def target_box(kind, slot, state):
    """The box a vector should fill when it lands on the board, so it never needs scaling there."""
    page = (state or {}).get(kind) or {}
    if kind == "icons":
        glyph = next((sec["tmpl"][1] for sec in page.get("sections", []) if sec.get("tmpl") and sec["tmpl"][1] and sec["name"] != SECTION["icons"]), 18)
        return [glyph, glyph]
    if kind == "logos":
        if slot:
            box = next((st.get("box") for st in page.get("sets", []) if st["name"] == slot.split("|")[0]), None)
            return box or [106, 23]
        return [220, 64]
    if kind == "visuals":
        # vector artwork sits inside a card's picture area, with a margin
        w, h = next((sec["img"] for sec in page.get("sections", []) if sec.get("img")), None) or [240, 160]
        return [max(16, round(w * 0.8)), max(16, round(h * 0.8))]
    return None


def build_plan(brand, elements, compared, review, state):
    """Turn the approved review into ordered steps. Upload steps carry the element ids to send."""
    core = (state or {}).get("core") or {}
    page_id = {k: next((p["id"] for p in core.get("pages", []) if re.search(rf"\b({pat})\b", p["name"], re.I)), None) for k, pat in PAGE_KINDS.items()}
    actions = review.get("actions", {})
    chosen = [e for e in elements if actions.get(e["id"]) in ("add", "replace") and compared.get(e["id"], {}).get("writable")]
    src = lambda e: e.get("source_name", "")
    tokens = {"roles": {r: h for r, h in (review.get("roles") or {}).items() if r in ROLES and re.fullmatch(r"#[0-9A-Fa-f]{6}", h or "")},
              "colors": [], "gradients": [], "text": [],
              "fonts": {k: v.strip()[:80] for k, v in (review.get("font_roles") or {}).items() if k in (core.get("fontVars") or {}) and (v or "").strip()}}
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
                style = (spec.get("style_name") or WEIGHT_NAMES.get(spec["weight"], "Regular")) + (" Italic" if spec["italic"] else "")
                tokens["text"].append({"style": target, "family": spec["family"], "fontStyle": style.replace("Regular Italic", "Italic"),
                                       "size": spec["size"], "lineHeight": spec["line_height"], "stamp": _stamp(src(e)), "weight": spec["weight"],
                                       "fallback": review.get("font_fallback") or None})
    steps = []
    if any(tokens.values()):
        n = len(tokens["colors"]), len(tokens["gradients"]), len(tokens["text"]), len(tokens["roles"])
        fonts_note = f", and set {len(tokens['fonts'])} font{'s' if len(tokens['fonts']) != 1 else ''}" if tokens["fonts"] else ""
        steps.append({"id": "tokens", "kind": "script", "code": tokens_script(tokens),
                      "title": f"Write {n[0]} colours, {n[1]} gradients, {n[2]} text styles and {n[3]} colour roles" + fonts_note})
    colour_page = next((pg["id"] for pg in core.get("pages", []) if re.search(r"\bcolou?rs?\b", pg["name"], re.I)), None)
    if tokens["colors"] or tokens["roles"]:
        if colour_page:
            cards = [{"name": c["style"].split("/", 1)[-1], "variable": c["variable"], "hex": c["hex"]} for c in tokens["colors"]]
            steps.append({"id": "colour-board", "kind": "script", "code": colours_board_script(colour_page, cards),
                          "title": f"Update the Colours page ({len(cards)} specimen{'s' if len(cards) != 1 else ''}, and every HEX label)"})
        else:
            skipped_early = "the Colours page: no page with that name was found, so only variables and styles were written"
    palette = [{"name": e["name"], "hex": e["hex"]} for e in elements if e["type"] == "color" and e.get("hex")]
    _lum = lambda h: 0.2126 * int(h[1:3], 16) + 0.7152 * int(h[3:5], 16) + 0.0722 * int(h[5:7], 16)
    backdrop = min((c["hex"].upper() for c in palette if re.fullmatch(r"#[0-9A-Fa-f]{6}", c["hex"])), key=_lum, default="#1A1A1A")
    for cat, pat in COMPONENT_PAGES.items():
        comps = [e for e in chosen if e["type"] == "component" and e.get("category") == cat and compared[e["id"]].get("spec")]
        page = next((pg for pg in core.get("pages", []) if re.search(rf"\b({pat})\b", pg["name"], re.I)), None)
        if comps and page:
            items = []
            for e in comps:
                spec = component_spec(e, palette)
                name = re.sub(r"^\s*(buttons?|headers?|cards?|roundels?|disclaimers?|gradients?)\s*[/:]\s*", "", e["name"], flags=re.I).strip() or e["name"]
                items.append({"name": name[:40], "spec": spec, "backdrop": backdrop, "base": "pill" if spec.get("radius", 0) >= 999 else None,
                              "stamp": (_stamp(src(e)) + ((" · " + e["description"]) if e.get("description") else ""))[:280]})
            steps.append({"id": f"components-{cat}", "kind": "script", "code": components_script(page["id"], items),
                          "title": f"Add {len(items)} variant{'s' if len(items) != 1 else ''} to the {page['name'].lstrip('→ ').strip()} master component"})
    slots = review.get("logo_slots") or {}
    groups = [("icons", "icon", place_icons_script, 40), ("visuals", "image", place_images_script, 30), ("logos", "logo", place_logos_script, 20)]
    skipped = [locals()["skipped_early"]] if "skipped_early" in locals() else []
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
            fits = [target_box(kind, m.get("slot"), state) for m in meta]
            steps.append({"id": sid + "-upload", "kind": "upload", "title": f"Upload {len(part)} {kind} files", "pageId": page_id[kind],
                          "count": len(part), "elements": [e["id"] for e in part], "fits": fits})
            steps.append({"id": sid + "-place", "kind": "script", "title": f"Place {len(part)} {kind} on the board", "code": maker(page_id[kind], sid, meta)})
    notes = [dict(c, stamp=_stamp(src(e))) for e in chosen if e["type"] == "guideline" for c in [guideline_card(e)] if c]
    if notes:
        gp = _guidelines_page(core)
        steps.append({"id": "guidelines", "kind": "script", "code": guidelines_script(gp["id"] if gp else None, notes),
                      "title": f"Write {len(notes)} guideline{'s' if len(notes) != 1 else ''} to the Guidelines page" + ("" if gp else " (new page)")})
    # rules that name one style or one kind of component also go in its description
    style_notes, set_notes = usage_notes([e for e in chosen if e["type"] == "guideline"], elements)
    if style_notes:
        steps.append({"id": "style-notes", "kind": "script", "code": style_notes_script(style_notes),
                      "title": f"Add usage notes to {len(style_notes)} style{'s' if len(style_notes) != 1 else ''}"})
    for cat, rules in set_notes.items():
        pg = next((x for x in core.get("pages", []) if re.search(rf"\b({COMPONENT_PAGES[cat]})\b", x["name"], re.I)), None)
        if pg:
            steps.append({"id": f"notes-{cat}", "kind": "script", "code": component_notes_script(pg["id"], rules),
                          "title": f"Add usage notes to the {pg['name'].lstrip('→ ').strip()} master component"})
    # last of all, and only if asked: clear out the template's dummy items
    cleanup = review.get("cleanup") or {}
    grad_page = next((pg for pg in core.get("pages", []) if re.search(COMPONENT_PAGES["gradients"], pg["name"], re.I) and not re.search(r"colou?r", pg["name"], re.I)), None)
    if tokens["gradients"] or cleanup.get("gradients"):
        if grad_page:
            steps.append({"id": "gradients-board", "kind": "script", "code": gradients_board_script(grad_page["id"], bool(cleanup.get("gradients"))),
                          "title": "Show the brand gradients on the Gradients master component" + (" and remove the placeholder gradients" if cleanup.get("gradients") else "")})
        elif cleanup.get("gradients"):
            skipped.append("placeholder gradients: no Gradients page found")
    for kind in ("icons", "visuals"):
        if cleanup.get(kind):
            if page_id[kind]:
                steps.append({"id": f"cleanup-{kind}", "kind": "script", "code": cleanup_page_script(page_id[kind], kind),
                              "title": f"Remove the template's placeholder {kind}"})
            else:
                skipped.append(f"placeholder {kind}: no matching page found")
    return {"steps": steps, "skipped": skipped}
