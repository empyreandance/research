#!/usr/bin/env python3
"""
build_archive.py — Generate an HTML archive page of all dated temperature maps.

This script scans the assets folder for dated PNG files (temp_anomaly_YYYYMMDD.png),
sorts them newest-first, and generates a static HTML gallery page that matches
the site's existing design.

Called automatically by the GitHub Actions workflow after each daily map is generated.
Can also be run manually:

    python build_archive.py --assets-dir ../assets --output ../archive.html
"""

import os
import re
import argparse
from datetime import datetime


def find_dated_maps(assets_dir):
    """
    Scan a directory for temperature map PNGs with date stamps.

    Parameters
    ----------
    assets_dir : str
        Path to the assets directory

    Returns
    -------
    list of dict
        Sorted newest-first. Each dict has 'filename', 'date', 'date_str'.
    """
    pattern = re.compile(r"temp_anomaly_(\d{8})\.(jpg|png)")
    maps = []

    for filename in os.listdir(assets_dir):
        match = pattern.match(filename)
        if match:
            date_str = match.group(1)
            try:
                date = datetime.strptime(date_str, "%Y%m%d")
                maps.append({
                    "filename": filename,
                    "date": date,
                    "date_str": date.strftime("%A, %B %-d, %Y"),
                    "date_short": date.strftime("%b %-d, %Y"),
                    "date_iso": date.strftime("%Y-%m-%d")
                })
            except ValueError:
                continue

    # Sort newest first
    maps.sort(key=lambda m: m["date"], reverse=True)
    return maps


def generate_archive_html(maps):
    """
    Generate the full HTML for the archive page.

    Parameters
    ----------
    maps : list of dict
        From find_dated_maps()

    Returns
    -------
    str
        Complete HTML document
    """

    # Build the gallery cards
    cards = []
    for m in maps:
        cards.append(f"""        <li>
          <a href="assets/archive/{m['filename']}" class="map-card-link">
            <div class="map-date">{m['date_str']}</div>
            <div class="map-thumb-container">
              <img src="assets/archive/{m['filename']}" alt="Temperature Calendar Anomaly: {m['date_str']}" loading="lazy">
            </div>
          </a>
        </li>""")

    cards_html = "\n".join(cards)
    count = len(maps)

    if count > 0:
        newest = maps[0]["date_str"]
        oldest = maps[-1]["date_str"]
        range_text = f"{count} maps archived, from {oldest} to {newest}."
    else:
        range_text = "No maps archived yet. Check back tomorrow!"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Temperature Map Archive – Alex Cooke</title>
  <style>
    /* Base reset */
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
      background-color: #0a0a0a;
      color: #e0e0e0;
      line-height: 1.6;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }}

    .container {{
      max-width: 900px;
      margin: 0 auto;
      padding: 20px;
      flex: 1;
    }}

    header {{
      text-align: center;
      padding: 60px 0 40px;
      border-bottom: 1px solid #222;
      margin-bottom: 40px;
    }}

    header h1 {{
      font-size: 2.5em;
      font-weight: 300;
      letter-spacing: -1px;
      color: #fff;
      margin-bottom: 10px;
    }}

    header h1 a {{
      color: inherit;
      text-decoration: none;
      transition: opacity 0.3s;
    }}

    header .subtitle {{
      color: #888;
      font-size: 1.1em;
    }}

    nav {{
      text-align: center;
      margin-bottom: 40px;
    }}

    nav a {{
      color: #ccc;
      text-decoration: none;
      margin: 0 15px;
      font-size: 1.1em;
      padding: 5px 0;
      border-bottom: 2px solid transparent;
      transition: color 0.3s, border-bottom-color 0.3s;
    }}

    nav a:hover, nav a.active {{
      color: #fff;
      border-bottom-color: #fff;
    }}

    .subnav {{
      text-align: center;
      margin-top: -25px;
      margin-bottom: 40px;
      font-size: 0.95em;
      color: #666;
    }}

    .subnav a {{
      color: #888;
      text-decoration: none;
      border-bottom: 1px solid #333;
      transition: color 0.3s, border-bottom-color 0.3s;
    }}

    .subnav a:hover {{
      color: #fff;
      border-bottom-color: #fff;
    }}

    .subnav .separator {{
      margin: 0 8px;
      color: #444;
    }}

    .subnav .current {{
      color: #ccc;
    }}

    .content-section {{
      animation: fadeIn 0.8s ease forwards;
      opacity: 0;
      transform: translateY(20px);
    }}

    @keyframes fadeIn {{
      to {{ opacity: 1; transform: translateY(0); }}
    }}

    .section-title {{
      font-size: 1.8em;
      font-weight: 300;
      color: #fff;
      margin-bottom: 10px;
      border-bottom: 1px solid #333;
      padding-bottom: 15px;
    }}

    .archive-meta {{
      color: #888;
      font-size: 0.95em;
      margin-bottom: 30px;
    }}

    .map-gallery {{
      list-style: none;
    }}

    .map-gallery li {{
      background: #111;
      margin-bottom: 20px;
      border-radius: 8px;
      border-left: 3px solid #333;
      overflow: hidden;
      transition: background 0.3s, border-left-color 0.3s;
    }}

    .map-gallery li:hover {{
      background: #151515;
      border-left-color: #666;
    }}

    .map-card-link {{
      display: block;
      text-decoration: none;
      color: inherit;
    }}

    .map-date {{
      font-size: 1.15em;
      font-weight: 500;
      color: #fff;
      padding: 20px 25px 10px;
    }}

    .map-thumb-container {{
      padding: 0 25px 20px;
    }}

    .map-thumb-container img {{
      width: 100%;
      height: auto;
      display: block;
      border-radius: 4px;
      border: 1px solid #222;
    }}

    footer {{
      text-align: center;
      padding: 40px 0;
      color: #666;
      font-size: 0.9em;
      border-top: 1px solid #222;
      margin-top: 80px;
    }}

    footer a {{
      color: #999;
      text-decoration: none;
      border-bottom: 1px solid #444;
      transition: color 0.3s, border-bottom-color 0.3s;
    }}

    footer a:hover {{
      color: #fff;
      border-bottom-color: #fff;
    }}

    @media (max-width: 768px) {{
      .container {{ padding: 15px; }}
      header h1 {{ font-size: 2em; }}
      .section-title {{ font-size: 1.5em; }}
      nav a {{ margin: 0 8px; font-size: 0.95em; }}
      .map-date {{ padding: 15px 20px 8px; }}
      .map-thumb-container {{ padding: 0 20px 15px; }}
    }}

    @media (max-width: 480px) {{
      nav a {{ margin: 0 5px; font-size: 0.85em; }}
    }}
  </style>
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
      <a href="temperature-calendar.html">Temperature Calendar</a>
      <span class="separator">&rsaquo;</span>
      <span class="current">Archive</span>
    </div>

    <div class="content-section">
      <h2 class="section-title">Temperature Calendar Archive</h2>
      <p class="archive-meta">{range_text}</p>
      <ul class="map-gallery">
{cards_html}
      </ul>
    </div>

    <footer>
      <p>
        <a href="contact.html">Contact Me</a>
        <span style="margin: 0 20px; color: #444;">&bull;</span>
        &copy; 2025 Alex Cooke. All rights reserved.
      </p>
    </footer>
  </div>
</body>
</html>"""

    return html


def main():
    parser = argparse.ArgumentParser(
        description="Generate the temperature map archive HTML page."
    )
    parser.add_argument(
        "--assets-dir",
        type=str,
        default="../assets",
        help="Path to the assets directory containing dated PNG files"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="../archive.html",
        help="Path to write the generated HTML file"
    )
    args = parser.parse_args()

    if not os.path.isdir(args.assets_dir):
        print(f"Assets directory not found: {args.assets_dir}")
        print("Creating it — the archive will be empty until the first map is generated.")
        os.makedirs(args.assets_dir, exist_ok=True)

    print(f"Scanning {args.assets_dir} for dated maps...")
    maps = find_dated_maps(args.assets_dir)
    print(f"Found {len(maps)} dated maps.")

    html = generate_archive_html(maps)

    with open(args.output, "w") as f:
        f.write(html)

    print(f"Archive page written to: {args.output}")


if __name__ == "__main__":
    main()
