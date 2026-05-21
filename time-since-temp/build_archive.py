"""Generate time-since-temp-archive.html from dated JPEGs in assets/archive.

The daily run writes two JPEGs per day — one per side:

    temp_streak_high_YYYYMMDD.jpg
    temp_streak_low_YYYYMMDD.jpg

This script groups those by date and renders a gallery where the visitor
toggles between high and low sides at the top of the page. Each day gets a
single card per side; days with only one side present (because of a render
glitch or partial run) still render with whichever side exists.
"""

import argparse
import os
import re
from datetime import datetime

# Filename pattern: temp_streak_<side>_<YYYYMMDD>.<ext>
_FILE_RE = re.compile(r"temp_streak_(high|low)_(\d{8})\.(jpg|png)")


def find_dated_maps(assets_dir):
    """
    Scan the archive directory and return a list of dated entries, newest first.

    Each entry looks like:
        {
            "date": datetime,
            "date_label": "Wednesday, May 21, 2026",
            "high": "temp_streak_high_20260521.jpg" or None,
            "low":  "temp_streak_low_20260521.jpg"  or None,
        }
    """
    by_date = {}
    for filename in os.listdir(assets_dir):
        m = _FILE_RE.match(filename)
        if not m:
            continue
        side, date_str, _ext = m.groups()
        try:
            d = datetime.strptime(date_str, "%Y%m%d")
        except ValueError:
            continue
        entry = by_date.setdefault(date_str, {
            "date": d,
            "date_label": d.strftime("%A, %B %-d, %Y"),
            "high": None,
            "low": None,
        })
        entry[side] = filename

    entries = list(by_date.values())
    entries.sort(key=lambda x: x["date"], reverse=True)
    return entries


def _card(entry, side):
    """Render a single archive card for one date+side, or empty string if absent."""
    fn = entry.get(side)
    if not fn:
        return ""
    side_label = "Forecast High" if side == "high" else "Forecast Low"
    return (
        f'        <li>\n'
        f'          <a href="assets/archive/{fn}" class="map-card-link">\n'
        f'            <div class="map-date">{entry["date_label"]}</div>\n'
        f'            <div class="map-thumb-container">\n'
        f'              <img src="assets/archive/{fn}" '
        f'alt="Time Since {side_label}: {entry["date_label"]}" loading="lazy">\n'
        f'            </div>\n'
        f'          </a>\n'
        f'        </li>'
    )


def render_html(entries):
    if not entries:
        meta = "No maps archived yet. Check back tomorrow!"
    else:
        meta = (f"{len(entries)} days archived, from "
                f"{entries[-1]['date'].strftime('%B %-d, %Y')} to "
                f"{entries[0]['date'].strftime('%B %-d, %Y')}.")

    high_cards = "\n".join(filter(None, (_card(e, "high") for e in entries)))
    low_cards = "\n".join(filter(None, (_card(e, "low") for e in entries)))

    # CSS: same base as the calendar-anomaly archive style + the side-toggle
    # used on the main page so the archive feels like one page with the same
    # high/low switcher.
    css = """
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
           background:#0a0a0a; color:#e0e0e0; line-height:1.6; min-height:100vh;
           display:flex; flex-direction:column; }
    .container { max-width:900px; margin:0 auto; padding:20px; flex:1; }
    header { text-align:center; padding:60px 0 40px; border-bottom:1px solid #222; margin-bottom:40px; }
    header h1 { font-size:2.5em; font-weight:300; letter-spacing:-1px; color:#fff; margin-bottom:10px; }
    header h1 a { color:inherit; text-decoration:none; transition:opacity 0.3s; }
    header .subtitle { color:#888; font-size:1.1em; }
    nav { text-align:center; margin-bottom:40px; }
    nav a { color:#ccc; text-decoration:none; margin:0 15px; font-size:1.1em;
            padding:5px 0; border-bottom:2px solid transparent;
            transition:color 0.3s, border-bottom-color 0.3s; }
    nav a:hover, nav a.active { color:#fff; border-bottom-color:#fff; }
    .subnav { text-align:center; margin-top:-25px; margin-bottom:40px;
              font-size:0.95em; color:#666; }
    .subnav a { color:#888; text-decoration:none; border-bottom:1px solid #333;
                transition:color 0.3s, border-bottom-color 0.3s; }
    .subnav a:hover { color:#fff; border-bottom-color:#fff; }
    .subnav .separator { margin:0 8px; color:#444; }
    .subnav .current { color:#ccc; }
    .content-section { animation:fadeIn 0.8s ease forwards; opacity:0; transform:translateY(20px); }
    @keyframes fadeIn { to { opacity:1; transform:translateY(0); } }
    .section-title { font-size:1.8em; font-weight:300; color:#fff; margin-bottom:10px;
                     border-bottom:1px solid #333; padding-bottom:15px; }
    .archive-meta { color:#888; font-size:0.95em; margin-bottom:20px; }

    .side-toggle {
      display: inline-flex; gap: 0; margin-bottom: 25px;
      border-radius: 6px; overflow: hidden; border: 1px solid #333;
    }
    .side-toggle button {
      background: #111; color: #999; border: none;
      padding: 8px 22px; font-size: 0.95em; cursor: pointer;
      transition: background 0.3s, color 0.3s; font-family: inherit;
    }
    .side-toggle button:hover { background: #1a1a1a; color: #fff; }
    .side-toggle button.active { background: #2a2a2a; color: #fff; }
    .side-low { display: none; }

    .map-gallery { list-style:none; }
    .map-gallery li { background:#111; margin-bottom:20px; border-radius:8px;
                      border-left:3px solid #333; overflow:hidden;
                      transition:background 0.3s, border-left-color 0.3s; }
    .map-gallery li:hover { background:#151515; border-left-color:#666; }
    .map-card-link { display:block; text-decoration:none; color:inherit; }
    .map-date { font-size:1.15em; font-weight:500; color:#fff; padding:20px 25px 10px; }
    .map-thumb-container { padding:0 25px 20px; }
    .map-thumb-container img { width:100%; height:auto; display:block;
                                border-radius:4px; border:1px solid #222; }
    footer { text-align:center; padding:40px 0; color:#666; font-size:0.9em;
             border-top:1px solid #222; margin-top:80px; }
    footer a { color:#999; text-decoration:none; border-bottom:1px solid #444;
               transition:color 0.3s, border-bottom-color 0.3s; }
    footer a:hover { color:#fff; border-bottom-color:#fff; }
    @media (max-width: 768px) {
      .container { padding:15px; }
      header h1 { font-size:2em; }
      .section-title { font-size:1.5em; }
      nav a { margin:0 8px; font-size:0.95em; }
      .map-date { padding:15px 20px 8px; }
      .map-thumb-container { padding:0 20px 15px; }
    }
    @media (max-width: 480px) {
      nav a { margin:0 5px; font-size:0.85em; }
    }
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Time Since Temperature Archive &ndash; Alex Cooke</title>
  <style>{css}</style>
</head>
<body>
  <div class="container">
    <header>
      <h1><a href="https://www.alexcooke.co">Alex Cooke</a></h1>
      <p class="subtitle">Research</p>
    </header>

    <nav>
      <a href="index.html">About</a>
      <a href="meteorology.html">Meteorology</a>
      <a href="music.html">Music</a>
      <a href="presentations.html">Presentations</a>
      <a href="maps-and-tools.html" class="active">Maps &amp; Tools</a>
    </nav>

    <div class="subnav">
      <a href="maps-and-tools.html">Maps &amp; Tools</a>
      <span class="separator">&rsaquo;</span>
      <a href="time-since-temp.html">Time Since Temperature</a>
      <span class="separator">&rsaquo;</span>
      <span class="current">Archive</span>
    </div>

    <div class="content-section">
      <h2 class="section-title">Time Since Temperature Archive</h2>
      <p class="archive-meta">{meta}</p>

      <div class="side-toggle" role="tablist">
        <button class="active" data-side="high" onclick="showSide('high')" role="tab">Forecast High</button>
        <button data-side="low" onclick="showSide('low')" role="tab">Forecast Low</button>
      </div>

      <ul class="map-gallery side-high">
{high_cards}
      </ul>

      <ul class="map-gallery side-low">
{low_cards}
      </ul>
    </div>

    <footer>
      <p>
        <a href="contact.html">Contact Me</a>
        <span style="margin: 0 20px; color: #444;">&bull;</span>
        &copy; 2026 Alex Cooke. All rights reserved.
      </p>
    </footer>
  </div>

  <script>
    function showSide(side) {{
      document.querySelectorAll('.side-high').forEach(el => {{
        el.style.display = side === 'high' ? '' : 'none';
      }});
      document.querySelectorAll('.side-low').forEach(el => {{
        el.style.display = side === 'low' ? '' : 'none';
      }});
      document.querySelectorAll('.side-toggle button').forEach(btn => {{
        btn.classList.toggle('active', btn.dataset.side === side);
      }});
    }}
  </script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-dir", default="../assets/archive")
    parser.add_argument("--output", default="../time-since-temp-archive.html")
    args = parser.parse_args()
    os.makedirs(args.assets_dir, exist_ok=True)
    entries = find_dated_maps(args.assets_dir)
    with open(args.output, "w") as f:
        f.write(render_html(entries))
    print(f"Wrote {args.output} ({len(entries)} dated entries)")


if __name__ == "__main__":
    main()
