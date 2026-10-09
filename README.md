# Brief Scraper

Reads a client brand book PDF and turns it into one Markdown file (`*.dsbrief.md`) that the Figma
plugin's Design System Scraper can ingest: one tagged block per element.

## Run

```bash
./run.sh
```

Then open http://127.0.0.1:8765, drop in a PDF, review, and press **Export**. The plugin only accepts `.md` files under 1 MB, so a brief with images downloads as a `.zip` of numbered parts; unzip it and give the plugin every part.

To enable the optional AI pass, put `ANTHROPIC_API_KEY=...` in a `.env` file in this folder and restart.
With it on, each page image and its text are sent to the Claude API. With it off, nothing leaves the machine.

## What is read, and how

| Element | Source | Confidence |
|---|---|---|
| Colours | Text labels (name, HEX, RGB, CMYK, Pantone), cross-checked against the swatch fill behind the label | exact |
| Gradients | The PDF's shading objects: stops, angle, and handle positions relative to the filled shape | exact (approximate if defined in CMYK) |
| Font families | Embedded fonts, with every weight present | exact |
| Text styles | Font, size, leading and colour of text as set in the brief itself (excluded by default) | exact, but observed rather than specified |
| Icons, logos, graphics | Vector paths grouped by proximity and written as SVG | exact |
| Images, raster logos | Embedded images, with transparency, as PNG or JPEG | exact, at the embedded resolution |
| Components, type roles, effects, guidelines | AI pass only | stated or estimated |

A scanned PDF has none of this data. The app warns when a file looks scanned; only the AI pass can read those.

## File format: `dsbrief/1`

- Front matter: `schema`, `bundle`, `part`, `parts`, `part_kind` (`data` or `assets`), `source_file`, `generated`, `pages`, `elements`, `counts`.
- Data parts come first and hold every element, with SVG artwork inline. Raster images follow in asset parts; an element's `asset.part` names the part holding its file. Parts of one export share a `bundle` value, so the plugin can check it has all `parts` before ingesting.
- Each element is a level-3 heading followed by a fenced block whose info string is `json element`.
- An element with an `asset` is followed by a fenced block tagged `asset=<element id>`:
  `svg` holds markup; `base64` holds an encoded image and also carries `mime=<type>`.

Common fields: `id`, `type`, `name`, `cms` (which CMS tabs it feeds), `origin`
(`specified`, `observed`, `interpreted`), `confidence` (`exact`, `approximate`, `stated`, `estimated`),
`section` (the page heading it came from), `source.pages`, `source.method`.

Types: `color`, `gradient`, `font-family`, `text-style`, `effect`, `spacing`, `radius`, `motion`,
`logo`, `icon`, `image`, `graphic`, `component`, `guideline`.

Gradient `handles` are the start and end points as fractions of the filled shape's box, so a Figma
`gradientTransform` can be built without re-deriving the angle. `stops[].color` names the palette
colour a stop matches.

### Reading it in the plugin

```js
// pass the text of every part, in any order
function parseBrief(parts) {
  const md = parts.join("\n");
  const assets = {};
  for (const m of md.matchAll(/^```(svg|base64) asset=(\S+)(?: mime=(\S+))?\n([\s\S]*?)\n```/gm)) {
    assets[m[2]] = { format: m[1], mime: m[3] || "image/svg+xml", data: m[1] === "base64" ? m[4].replace(/\s+/g, "") : m[4] };
  }
  const elements = [...md.matchAll(/^```json element\n([\s\S]*?)\n```/gm)].map(m => JSON.parse(m[1]));
  for (const el of elements) if (el.asset) el.asset.data = assets[el.asset.ref]?.data;
  return elements;
}
```
