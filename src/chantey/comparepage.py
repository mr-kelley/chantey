"""The comparison page: both versions engraved side by side (Verovio SVG),
every measure numbered, changed measures highlighted, audio players for
whatever renders exist. One self-contained local HTML file — chantey never
publishes anything (spec: Licensed material)."""

from __future__ import annotations

from pathlib import Path


def render_svg_pages(musicxml_path: Path) -> list[str]:
    import verovio  # deferred: heavy import, and diff/info don't need it

    tk = verovio.toolkit()
    tk.setOptions(
        {
            # measure numbers as data-n attributes → highlight is a CSS selector
            "svgAdditionalAttribute": ["measure@n"],
            "scale": 33,
            "footer": "none",
            # Number every measure: long scores (Marche Slave...) need cheap
            # references, and "change mm. x-y" is the whole user story.
            "mnumInterval": 1,
        }
    )
    if not tk.loadFile(str(musicxml_path)):
        from .errors import ChanteyError

        raise ChanteyError(f"Verovio could not load {musicxml_path.name}")
    return [tk.renderToSVG(p) for p in range(1, tk.getPageCount() + 1)]


def build_page(
    original: Path,
    revised: Path,
    changed: list[int],
    audio: list[tuple[str, str]],  # (label, relative href) — existing files only
    title: str,
) -> str:
    orig_pages = render_svg_pages(original)
    rev_pages = render_svg_pages(revised)

    targets = "g.note, g.chord, g.notehead, g.stem, g.beam, g.accid, g.dots, g.flag"
    hl = ", ".join(
        f'.{cls} g.measure[data-n="{n}"] :is({targets})' for cls in ("orig", "rev") for n in changed
    )
    audio_html = "\n".join(
        f'<div><label>{label}</label><audio controls src="{href}"></audio></div>'
        for label, href in audio
    )
    rows = "\n".join(
        f'<h2>Page {i + 1}</h2><div class="row">'
        f'<figure class="orig"><figcaption>Original</figcaption>{o}</figure>'
        f'<figure class="rev"><figcaption>Revised</figcaption>{r}</figure></div>'
        # pagination can differ after an edit; pair the common prefix
        for i, (o, r) in enumerate(zip(orig_pages, rev_pages, strict=False))
    )
    measures = ", ".join(f"m{n}" for n in changed)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{title} — original vs revised</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #fafaf7; }}
.row {{ display: flex; gap: 1rem; }}
figure {{ flex: 1; margin: 0; border: 1px solid #ccc; background: #fff; min-width: 0; }}
figure svg {{ width: 100%; height: auto; }}
figcaption {{ font-weight: 600; padding: .4rem .8rem; background: #eee; }}
.rev figcaption {{ background: #fde8e8; }}
{hl or ".none"} {{ fill: crimson; stroke: crimson; }}
.audio {{ display: grid; grid-template-columns: repeat(2, minmax(0,1fr));
  gap: .5rem 2rem; margin: 1rem 0 2rem; }}
.audio label {{ display: block; font-size: .9rem; margin-bottom: .2rem; }}
audio {{ width: 100%; }}
p.note {{ color: #555; max-width: 60rem; }}
</style></head><body>
<h1>{title} — original vs revised</h1>
<p class="note">Changed measures ({measures}) are shown in
<span style="color:crimson">crimson</span> in both columns; every measure is
numbered. Local working copy — not for publication.</p>
<div class="audio">
{audio_html}
</div>
{rows}
</body></html>"""


def find_audio(score_path: Path, label_prefix: str) -> list[tuple[str, str]]:
    """Audio renders sitting beside a score, as (label, relative-href)."""
    found = []
    for suffix, voice in ((".piano.wav", "piano"), (".synth.wav", "hornpipe synth")):
        candidate = score_path.with_suffix("").with_name(score_path.stem + suffix)
        if candidate.exists():
            found.append((f"{label_prefix} — {voice}", candidate.name))
    return found
