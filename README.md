# Brief Scraper

Breaks a client's brand materials down into individual colours, gradients, type styles, logos, icons and
images, compares them with what is already in the brand's Figma design-system file, and writes the changes
you approve straight to that file.

It has two halves that run side by side on your Mac:

- **The app** (this repo, in your browser): brands, materials, the review, the plan.
- **Claude Code with the Figma connector**: reads the Figma file and writes to it, using your own Figma login.

## Set up (once)

1. Clone this repo and open the folder in Claude Code. The Figma connector must be connected in Claude Code.
2. Optional but recommended: create a file named `.env` in this folder containing `ANTHROPIC_API_KEY=...`.
   Without it, exact values are still read from PDFs, but naming, components, websites, text files and ads are not.
3. Run `./setup.sh` once. It gives Claude Code its own sign-in and Figma connection on this Mac, which is what
   lets the app's **Scan** and **Write to Figma** buttons work. (Without it you can still type `/scan-brand` and
   `/apply-brand` in Claude Code yourself.)
4. Start the app:

```bash
./run.sh
```

Then open http://127.0.0.1:8765.

## A session

1. **Brand.** Add a brand (a name and its Figma file link) or open an existing one.
2. **Materials.** Drop in brand PDFs, asset folders, images, SVGs, text or `.md` files, or give a website address.
   Tick "ads or layouts" for finished work that should only be read as evidence.
3. **Figma board.** Press **Scan the Figma board**. It reads the file and changes nothing.
4. **Review.** Each new item shows how it relates to the board: new, differs, or already there. Decide what to
   add, replace or ignore, set the colour roles, choose where logos and text styles go, then press Approve.
5. **Apply.** Save a version in Figma first (File → Save to version history); the Figma connection cannot do
   that for you. Then press **Write to Figma**. It writes the approved changes. Everything it adds is labelled "Brief Scraper" in the item's description.
6. In Figma, press **Update & rescan** in the DesignOps plugin.

Nothing a client supplies is stored. `brands.json` holds only names and Figma links; materials stay in memory
and a temp folder until the app stops. The Figma file is the record, which is why every session starts with a scan.

## What gets written, and where

| Item | In the Figma file |
|---|---|
| Colours | A variable in the `Color` collection, a `Brand/<Name>` colour style bound to it, and a specimen card on the Colours page |
| Colour roles | The role variables (`Primary`, `Secondary`, `Tertiary`, `Text color`, `Background color`) are pointed at the chosen colours |
| Gradients | `Gradient/<Name>` colour styles |
| Text styles | An existing text style you choose, or a new one. Skipped if the font is not installed in Figma |
| Icons | Components named `Brand/Icon <Name>` in a "Brand Icons" section on the Icons page |
| Images | Image cards in an "Image Library" section on the Visuals page |
| Logos | A chosen variant of an existing logo component set, or a component in a "Brand Logos" section |

| Components | A new variant on the matching master component (Headers, Buttons, Image, Disclaimer, Roundels, Cards, Price Points, Base Plate, Gradients), set from the values the brief states: fill, border, corner radius, text colour, case, font, sample label |

A component is only written when the brief states at least one of those values. The template's own variants are
never edited. Components with no matching master (pop-ups, counters and so on), guidelines, fonts, effects and
spacing are shown for reference and are not written.

Brand gradients are also shown as variants on the Gradients master component.

### Clearing the template's placeholders

A new DesignOps file arrives with dummy content: numbered icons with no artwork, image cards with no picture, and
gradient styles described as placeholders. Review offers a tick box for each kind. When ticked, those dummies are
deleted at the end of the write, along with any section left empty, so the board holds only brand content.

- A kind is only cleared when the brand has real items of that kind on the board; otherwise the dummies stay.
- Only items that match those three descriptions are removed. Anything with artwork or a picture in it is kept.
- This is the one place the tool deletes. Save a version in Figma first (File > Save to version history).

The layout of the Figma file is found by pattern on each run (the page whose name contains "Icons", an existing
item to copy), never by fixed positions, so it follows changes to the DesignOps template where it can and says so
where it cannot.

## How exact values are read from a PDF

| Element | Source | Confidence |
|---|---|---|
| Colours | Text labels (name, HEX, RGB, CMYK, Pantone), checked against the swatch behind the label | exact |
| Gradients | The PDF's own gradient definitions: stops and angle | exact (approximate if defined in CMYK) |
| Fonts | Embedded fonts with every weight present | exact |
| Icons, logos | Vector paths grouped by proximity, written as SVG | exact |
| Images, raster logos | Embedded images with transparency | exact, at the embedded resolution |

A scanned PDF has none of this; the app warns, and only the Claude pass can read those.
