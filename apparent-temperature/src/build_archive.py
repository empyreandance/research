"""Generate apt-archive.html from dated JPEGs in assets/archive."""

import argparse
import os
import re
from datetime import datetime


def find_dated_maps(assets_dir):
    pattern = re.compile(r"apt_anomaly_(\d{8})\.(jpg|png)")
    maps = []
    for filename in os.listdir(assets_dir):
        m = pattern.match(filename)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y%m%d")
        except ValueError:
            continue
        maps.append({
            "filename": filename,
            "date": d,
            "date_str": d.strftime("%A, %B %-d, %Y"),
        })
    maps.sort(key=lambda x: x["date"], reverse=True)
    return maps


def render_html(maps):
    if not maps:
        meta = "No maps archived yet. Check back tomorrow!"
    else:
        meta = (f"{len(maps)} maps archived, from "
                f"{maps[-1]['date'].strftime('%B %-d, %Y')} to "
                f"{maps[0]['date'].strftime('%B %-d, %Y')}.")

    cards = "\n".join(
        f'        <li>\n'
        f'          <a href="assets/archive/{m["filename"]}" class="map-card-link">\n'
        f'            <div class="map-date">{m["date_str"]}</div>\n'
        f'            <div class="map-thumb-container">\n'
        f'              <img src="assets/archive/{m["filename"]}" alt="Apparent Temperature Calendar Anomaly: {m["date_str"]}" loading="lazy">\n'
        f'            </div>\n'
        f'          </a>\n'
        f'        </li>'
        for m in maps
    )

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
    .archive-meta { color:#888; font-size:0.95em; margin-bottom:30px; }
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
  <title>Apparent Temperature Calendar Anomaly Archive &ndash; Alex Cooke</title>
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
      <a href="apparent-temperature.html">Apparent Temperature Calendar Anomaly</a>
      <span class="separator">&rsaquo;</span>
      <span class="current">Archive</span>
    </div>

    <div class="content-section">
      <h2 class="section-title">Apparent Temperature Calendar Anomaly Archive</h2>
      <p class="archive-meta">{meta}</p>
      <ul class="map-gallery">
{cards}
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
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-dir", default="../assets/archive")
    parser.add_argument("--output", default="../apt-archive.html")
    args = parser.parse_args()
    os.makedirs(args.assets_dir, exist_ok=True)
    maps = find_dated_maps(args.assets_dir)
    with open(args.output, "w") as f:
        f.write(render_html(maps))
    print(f"Wrote {args.output} ({len(maps)} entries)")


if __name__ == "__main__":
    main()
