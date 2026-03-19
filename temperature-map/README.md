# Temperature Calendar Map

A daily-updating map that shows how far "ahead" or "behind" schedule temperatures are across the US — not in degrees, but in **days**. If the forecast high for July 1 matches the normal high for July 15, that spot shows **+14** (two weeks ahead of schedule). Way more intuitive than "5 degrees above normal."

---

## How It Works

1. A pre-built climatology file holds the 1991-2020 normal daily high temperature at every grid cell across CONUS, for all 365 days of the year.
2. Every morning, a GitHub Actions workflow downloads the NWS gridded forecast high temperature.
3. For each grid cell, the script finds the date in the climatological calendar where that forecast temperature is normal, and computes the difference in days from today.
4. The result is rendered as a color-coded map and committed to the repo, where GitHub Pages serves it.

---

## Setting Up Your Mac for This Project

These instructions assume you have never done any of this before. Follow them in order.

### 1. Open Terminal

Press **⌘ + Space** to open Spotlight, type **Terminal**, press Enter. A window with a command-line prompt will appear. All of the commands below get typed (or pasted) into this window.

### 2. Install Homebrew (the Mac package manager)

Homebrew is a tool that lets you install software from the command line. If you already have it, this step will tell you so.

Paste this entire line into Terminal and press Enter:

```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

It will ask for your Mac password (the one you use to log in). **When you type the password, nothing appears on screen — that's normal.** Just type it and press Enter.

If it says Homebrew is already installed, great, skip ahead.

After it finishes, it may print some lines saying "Add Homebrew to your PATH." If it does, copy and paste those lines into Terminal exactly as shown. They usually look something like:

```
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

### 3. Install Python

macOS comes with Python, but it's better to install a fresh copy via Homebrew so you're on a current version and don't mess with the system Python.

```
brew install python
```

After it finishes, verify it worked:

```
python3 --version
```

You should see something like `Python 3.12.x` or `3.13.x`. Any version 3.10 or higher is fine.

### 4. Install the eccodes library

The `cfgrib` Python package (which reads GRIB2 weather files) needs a C library called eccodes installed on your system first. Without this, pip will install cfgrib but it won't actually work.

```
brew install eccodes
```

### 5. Navigate into your project folder

Your project lives in Dropbox. Open it in Terminal:

```
cd "/Users/alexcooke/Dropbox/NWS/Research/Temperature Anomaly Map"
```

(The quotes are required because the path has spaces in it.)

### 6. Create a Python virtual environment

A virtual environment is an isolated space for this project's Python packages. It prevents them from interfering with other Python projects on your Mac.

```
python3 -m venv venv
```

This creates a folder called `venv` inside your project directory. You now need to **activate** it:

```
source venv/bin/activate
```

After activation, your Terminal prompt will change — you'll see `(venv)` at the beginning of the line. **This tells you the virtual environment is active.** Every time you open a new Terminal window to work on this project, you need to run these two commands again:

```
cd "/Users/alexcooke/Dropbox/NWS/Research/Temperature Anomaly Map"
source venv/bin/activate
```

### 7. Copy the project files into the folder

Copy these files into your project folder:

- `config.py`
- `build_climatology.py`
- `requirements.txt`

You can do this by dragging them in Finder, or by using the `cp` command if you downloaded them elsewhere. For example, if they're in your Downloads folder:

```
cp ~/Downloads/config.py "/Users/alexcooke/Dropbox/NWS/Research/Temperature Anomaly Map/"
cp ~/Downloads/build_climatology.py "/Users/alexcooke/Dropbox/NWS/Research/Temperature Anomaly Map/"
cp ~/Downloads/requirements.txt "/Users/alexcooke/Dropbox/NWS/Research/Temperature Anomaly Map/"
```

### 8. Install Python dependencies

Make sure your virtual environment is active (you see `(venv)` in your prompt), then:

```
pip install -r requirements.txt
```

This will download and install about 10-15 packages and their dependencies. It takes a minute or two. You'll see a lot of text scrolling by — that's normal.

**If you see errors about cartopy:** Cartopy sometimes has trouble building on Mac. Try this:

```
brew install geos proj
pip install cartopy
```

**If you see errors about cfgrib:** Make sure you ran `brew install eccodes` in step 4. Then try:

```
pip install cfgrib
```

### 9. Test the API connection

Before running the full build (which takes 15 minutes), let's make sure the ACIS API is responding correctly:

```
python build_climatology.py --test
```

This queries a single state (Kansas) and shows you exactly what the API returns. You should see output like:

```
TEST MODE — Querying Kansas as a test
=====================================
SUCCESS! Got 147 stations from Kansas.

Station 1: ABILENE 1 N
  Coordinates: [-97.22, 38.93]
  First 5 values (Jan 1-5): [['38.5'], ['38.8'], ['39.1'], ['39.4'], ['39.7']]
  ...
```

The important things to check:
- **Did it connect?** If you see `SUCCESS!` and a station count, the API is working.
- **Do the values look like temperatures?** Kansas in January should be around 30-45°F, and in July around 90-100°F. If you're seeing numbers like 0.5 or 300, something is off.
- **Does it say "Everything looks good" at the end?** If so, proceed to the full build.

### 10. Run the full climatology build

```
python build_climatology.py
```

This will:
1. Query the ACIS API for all 48 contiguous states plus DC (~15 minutes)
2. Interpolate the station data onto a regular grid (~3-5 minutes)
3. Save the result as `climatology.nc`

You'll see a progress bar for each step. When it finishes, you'll have a file called `climatology.nc` in your project folder. **This file is permanent.** You only build it once.

If the download gets interrupted partway through, the script saves its progress. Run it again with:

```
python build_climatology.py --skip-download
```

This skips the API download and uses the cached data from the partial run.

### 11. Verify the climatology file

After the build completes, you can do a quick sanity check. Open a Python prompt:

```
python3
```

Then paste these lines:

```python
import xarray as xr
ds = xr.open_dataset("climatology.nc")
print(ds)
print(f"\nJuly 1 (day 182) temperature range: "
      f"{float(ds.normal_maxt.sel(day_of_year=182).min()):.0f}°F to "
      f"{float(ds.normal_maxt.sel(day_of_year=182).max()):.0f}°F")
```

You should see the dataset dimensions (365 × ~104 × ~236) and a July 1 temperature range of roughly 70°F (northern Maine/mountains) to 105°F (desert Southwest). Type `exit()` to leave the Python prompt.

---

## Phase 2: Running the Daily Map

Once `climatology.nc` exists, you can generate maps.

### 12. Copy `daily_run.py` into your project folder

Just like you did with the Phase 1 files, copy `daily_run.py` into the project:

```
cp ~/Downloads/daily_run.py "/Users/alexcooke/Dropbox/NWS/Research/Temperature Anomaly Map/"
```

### 13. Test the map rendering (no forecast download needed)

Before trying a real forecast, make sure the map rendering works with fake data:

```
cd "/Users/alexcooke/Dropbox/NWS/Research/Temperature Anomaly Map"
source venv/bin/activate
python daily_run.py --test
```

This creates a test map at `output/temp_anomaly_TEST.png` using a synthetic gradient pattern. Open it in Finder or Preview to check:

```
open output/temp_anomaly_TEST.png
```

You should see a CONUS map with blue on one side grading to red on the other, state borders, a colorbar at the bottom, and a title. If this looks right, the rendering pipeline works.

### 14. Run with a real forecast

```
python daily_run.py
```

This will:
1. Download the current NDFD maximum temperature forecast (~5-10 seconds)
2. Regrid it to match your climatology (~10 seconds)
3. Compute the day-offset anomaly at every grid cell (~1-2 minutes)
4. Render and save the map

When it finishes, you'll have two files in the `output/` folder:
- `temp_anomaly_20260319.png` (dated version)
- `temp_anomaly_latest.png` (always the most recent — this is what your website points to)

Open it:

```
open output/temp_anomaly_latest.png
```

### 15. What to look for in the output

The map should show the CONUS with grid cells colored on a blue-to-red scale. Check for:
- **Plausible values:** In early spring you might see parts of the South already running ahead of schedule (+10 to +20) while the northern Plains are behind (-10 to -20). Extreme values beyond ±30 are possible during heat waves or cold snaps.
- **Clean edges:** The CONUS outline should look reasonable — no giant colored blocks in the ocean or in Canada/Mexico.
- **No solid white areas in the middle of the country:** White means zero anomaly (right on schedule), which is fine, but large blank (missing data) patches in places like Kansas or Ohio would indicate a regridding problem.

If something looks off, paste the terminal output here and we'll debug it.

---

## Phase 3: Automating with GitHub Actions

This is the final step. After this, the map updates itself every morning with no intervention from you.

The idea: GitHub Actions is a free automation service built into every GitHub repo. You give it a YAML file that says "every morning at 7 AM Eastern, spin up a Linux machine, install my Python stuff, run my script, and commit the resulting PNG back to the repo." GitHub Pages then serves the updated image automatically.

### 16. Understand your repo structure

Your site at research.alexcooke.co is served from the `empyreandance/research` repo on GitHub. The temperature map project will live inside that same repo. After setup, the repo will look something like this:

```
research/
├── .github/
│   └── workflows/
│       └── daily_temp_map.yml      ← The automation recipe
├── temperature-map/
│   ├── build_climatology.py        ← Already built (Phase 1)
│   ├── daily_run.py                ← Already tested (Phase 2)
│   ├── config.py
│   ├── requirements.txt
│   ├── climatology.nc              ← The big file you built in Phase 1
│   └── output/
│       └── temp_anomaly_latest.png ← Updated daily by the bot
├── assets/
│   └── temp_anomaly_latest.png     ← Copied here for your site to use
├── index.html                      ← (your existing site files)
└── ...
```

### 17. Clone your site repo locally (if you haven't already)

If you already have your site repo cloned on your Mac, skip to step 18. Otherwise, open Terminal and clone it:

```
cd ~/Desktop
git clone https://github.com/empyreandance/research.git
cd research
```

### 18. Copy the temperature-map project into the repo

Copy your entire working project folder into the repo. From Terminal:

```
cp -r "/Users/alexcooke/Dropbox/NWS/Research/Temperature Anomaly Map" ~/Desktop/research/temperature-map
```

This copies everything — scripts, config, climatology file, and all. If your repo is cloned somewhere other than `~/Desktop/research`, adjust the path accordingly.

**Important:** The `climatology.nc` file is ~30-50 MB. GitHub allows files up to 100 MB, so this is fine. It will be a one-time upload that never changes.

**Also important:** Make sure the `venv/` folder does NOT get copied into the repo. It's large and shouldn't be in version control. Either delete it from the copy, or add it to `.gitignore` (see step 20).

```
rm -rf ~/Desktop/research/temperature-map/venv
```

### 19. Add the GitHub Actions workflow file

Create the workflow directory structure and copy the YAML file:

```
mkdir -p ~/Desktop/research/.github/workflows
cp ~/Downloads/daily_temp_map.yml ~/Desktop/research/.github/workflows/
```

Or if you downloaded it to your project folder:

```
mkdir -p ~/Desktop/research/.github/workflows
cp "/Users/alexcooke/Dropbox/NWS/Research/Temperature Anomaly Map/daily_temp_map.yml" ~/Desktop/research/.github/workflows/
```

### 20. Create a .gitignore file

This tells Git which files to skip. Create a file called `.gitignore` in the repo root (or add these lines to your existing one):

```
# Python virtual environment (large, not needed in the repo)
temperature-map/venv/

# Cached data (re-downloaded each run)
temperature-map/cache/

# Python bytecode
__pycache__/
*.pyc
```

You can create this from Terminal:

```
cd ~/Desktop/research
cat >> .gitignore << 'EOF'
temperature-map/venv/
temperature-map/cache/
__pycache__/
*.pyc
EOF
```

### 21. Commit and push everything

```
cd ~/Desktop/research
git add .
git commit -m "Add temperature calendar map project"
git push
```

This will take a minute because of the climatology.nc file. That's normal — it's a one-time upload.

### 22. Test the workflow manually

Go to your repo on github.com in a web browser:

1. Click the **Actions** tab at the top of the repo page
2. In the left sidebar, click **Daily Temperature Map**
3. Click the **Run workflow** dropdown button on the right
4. Click the green **Run workflow** button

This triggers the workflow immediately instead of waiting for the morning schedule. You can watch it run in real time — click on the running job to see the log output. It should take about 3-5 minutes.

**If it succeeds:** You'll see a green checkmark. The bot will have committed a new PNG to your repo. Check the `assets/` folder — there should be a `temp_anomaly_latest.png` in there.

**If it fails:** Click on the failed job to see the error log. The most common issues are:

- **"Permission denied" or "push failed"**: The workflow needs write access. Go to your repo Settings → Actions → General → scroll down to "Workflow permissions" → select "Read and write permissions" → Save.
- **"Module not found" errors**: Usually means a dependency is missing from requirements.txt. Paste the error here and I'll fix it.
- **"GRIB download failed"**: The NOAA server was temporarily down. Re-run the workflow — it almost always works on retry.

### 23. Add the map to your website

Once the workflow is producing maps, add an `<img>` tag to your site that points to the latest map. Since your site is at research.alexcooke.co, the tag would be:

```html
<img src="assets/temp_anomaly_latest.png"
     alt="Temperature Calendar Anomaly Map"
     style="max-width: 100%; height: auto;">
```

If the image path doesn't resolve correctly, you may need to use the full path relative to your site root. Commit and push that change, and your site will show the map. It updates automatically every morning when the bot commits a new PNG.

### 24. Verify the daily schedule

The workflow is set to run at 12:00 UTC every day (7:00 AM Eastern, 4:00 AM Pacific). After you've confirmed the manual trigger works, just wait for the next scheduled run and check that a new commit appears.

**Note about GitHub Actions scheduling:** GitHub doesn't guarantee exact timing for cron schedules — during periods of high demand, your job might run 5-30 minutes late. This is normal and doesn't affect anything since the NDFD forecast is valid all day.

### 25. You're done!

The system is now fully automated:
- Every morning, GitHub Actions downloads the latest NWS forecast
- It computes the "days ahead/behind schedule" anomaly for every grid cell
- It renders the map and commits it to your repo
- GitHub Pages serves the updated image to your site
- You do nothing

If you ever want to change the run time, edit the `cron` line in `.github/workflows/daily_temp_map.yml`. Use [crontab.guru](https://crontab.guru/) to build cron expressions — it's the easiest way to get the syntax right.

---

## Troubleshooting

### Phase 1 (build_climatology.py)

**"command not found: python3"**
Homebrew may have installed Python under a different name. Try `python --version`. If that works, use `python` everywhere instead of `python3`.

**"No module named 'xxx'"**
Your virtual environment probably isn't activated. Run `source venv/bin/activate` and try again.

**"ERROR: Only got X stations" (low number)**
The ACIS server might have been overloaded. Wait an hour and try again. If it keeps happening, try running the test mode (`--test`) to see what the API is returning.

**The test shows weird temperature values**
If values look like departures from normal (small numbers like -2.3, +1.5) instead of actual temperatures (like 42, 91), the API is returning departures instead of normals. Let me know and we'll adjust the API query parameters.

**"Killed" or runs out of memory**
The interpolation step uses several GB of RAM with ~8000 stations. If your Mac has 8 GB, close other apps while it runs. If it still fails, we can reduce the grid resolution by changing `GRID_SPACING` in `config.py` from 0.25 to 0.5.

### Phase 2 (daily_run.py)

**"ERROR downloading NDFD" or timeout**
The NOAA GRIB server is sometimes slow or temporarily down. Wait a few minutes and try again. If it persists, the server URL may have changed — let me know.

**"Error opening GRIB2" or cfgrib errors**
Make sure eccodes is installed (`brew install eccodes`). If you see errors about missing keys or unknown variables, the NDFD file format may have changed slightly — paste the full error here.

**The map is all one color or all NaN**
This usually means the forecast temperatures and climatology temperatures are in different units (one in Fahrenheit, the other in Kelvin or Celsius). The script tries to auto-detect this, but if it guesses wrong, the anomalies will be wildly off. Check the terminal output for the line that says "Converting from..." — it should say Fahrenheit or convert correctly.

**The anomaly computation takes a very long time (more than 5 minutes)**
The current algorithm loops over every grid cell in Python, which is slow but correct. If this is a problem, let me know and we can vectorize it with numpy for a 50-100x speedup.

### Phase 3 (GitHub Actions)

**Workflow doesn't appear in the Actions tab**
Make sure the YAML file is at exactly `.github/workflows/daily_temp_map.yml` in your repo (not inside the `temperature-map/` subfolder). The `.github` folder must be at the repo root.

**"Permission denied" or "push failed"**
The workflow needs write access to push commits. In your repo on github.com: Settings → Actions → General → scroll to "Workflow permissions" → select "Read and write permissions" → Save.

**Workflow runs but map doesn't update on the site**
Check that the `assets/temp_anomaly_latest.png` path in the workflow matches the `<img>` tag path on your site. Also confirm GitHub Pages is building from the correct branch (usually `main`).

**Workflow fails with "No space left on device"**
The GitHub Actions runner has ~14 GB of disk space. The climatology.nc file plus dependencies shouldn't come close to this, but if it happens, we can add a cleanup step.

**The scheduled run doesn't trigger**
GitHub Actions cron schedules can be delayed up to 30-60 minutes during high-demand periods. If a scheduled run doesn't appear at all, check that the workflow file has no syntax errors by going to Actions → Daily Temperature Map and looking for error banners. Also note that GitHub disables scheduled workflows if the repo has had no activity (commits, issues, etc.) in 60 days — just push any commit to re-enable.
