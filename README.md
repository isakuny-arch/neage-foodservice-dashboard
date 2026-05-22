# neage Foodservice Dashboard

This repository contains a scraper and an interactive dashboard for the
**外食カテゴリ** of the [値上げ備忘録](https://neage.jp) website.  It allows you to
collect price history for Japanese foodservice chains starting from 1996 and
visualise the resulting data as time series and rankings.

## How it works

* `scrape_neage_foodservice.py` – A Python script that crawls the foodservice
  index page (`https://neage.jp/gaisyoku/index.html`), visits each linked
  product page, extracts historical price tables, and emits a flat list of
  records.  Each record includes the chain name, product name, variant (e.g.
  size or region), date, price and the source URL.  A baseline row is
  inserted for each series if the earliest observed date is after 1996 so
  that price indices can be calculated consistently.

* `dashboard.html` – A self‑contained HTML file that loads the scraped data
  (`data/neage_foodservice_events.json`), computes price indices (1996 = 100)
  for each series, and renders interactive charts and tables using Chart.js
  and DataTables.  No build system or server is required – simply open
  `dashboard.html` in your browser (or publish via GitHub Pages) once the
  JSON file exists.

* `.github/workflows/build-dashboard.yml` – A GitHub Actions workflow that
  installs Python dependencies, runs the scraper with a 0.7‑second delay
  between requests, writes the JSON and CSV outputs into the `data` folder,
  and then deploys the repository to GitHub Pages using the `actions/deploy-pages`
  action.  The workflow is triggered manually via *Run workflow* in the
  Actions tab.

## Getting started

1. **Install dependencies (optional)** – If you wish to run the scraper
   locally, create a virtual environment and install the dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Run the scraper** – Execute the script to download data from the
   foodservice category:

   ```bash
   python scrape_neage_foodservice.py --start-year 1996 \
     --out data/neage_foodservice_events.json \
     --csv data/neage_foodservice_events.csv
   ```

3. **View the dashboard** – Open `dashboard.html` directly in your browser.
   The page will load the JSON file from the `data` folder and render
   interactive graphs and tables.  You can filter by chain, see the latest
   price index ranking, and browse all records with links back to the source
   pages.

4. **Publish to GitHub Pages** – Push this repository to GitHub, then run the
   `Build neage Foodservice Dashboard` workflow from the Actions tab.  When it
   completes successfully, enable GitHub Pages under *Settings → Pages* and
   select **GitHub Actions** as the source.  Your dashboard will be available
   at `https://<username>.github.io/<repo>/dashboard.html`.

## Notes

* The scraper inserts a 0.7‑second delay between page requests to be
  respectful of the target site.  Nevertheless, please use this tool
  responsibly and avoid overloading `neage.jp`.
* Some product pages contain multiple price columns (e.g. store size or
  region).  These columns are captured as separate series labelled with
  their column headers.
* Records prior to 1996 are not included by default, but the earliest
  available price is used to generate a baseline row for 1996 if needed.
* If you encounter pages with unexpected structures or parsing errors, the
  script will print a message and skip those pages rather than failing the
  entire run.