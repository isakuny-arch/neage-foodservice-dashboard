#!/usr/bin/env python3
"""
Scraper for neage.jp foodservice pages.

This script crawls the external foodservice category of the "値上げ備忘録" site
(https://neage.jp/gaisyoku/index.html) and extracts historical price data for
each listed menu item.  Records are normalised into a flat structure and
exported as both JSON and CSV.  A baseline row is added for each series
starting in or after the requested start year so that price indices can be
computed relative to that year.  A delay is inserted between requests to
respect the target site's resources.

Usage:
    python scrape_neage_foodservice.py --start-year 1996 \
      --out data/neage_foodservice_events.json \
      --csv data/neage_foodservice_events.csv

The script requires the `requests`, `beautifulsoup4` and `pandas` libraries.
"""

import argparse
import csv
import json
import os
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


def parse_price_string(value: str) -> float:
    """Extract numeric price from a string (e.g. '600円' -> 600.0)."""
    digits = ''.join(ch for ch in value if ch.isdigit() or ch == '.')
    return float(digits) if digits else None


def parse_table(url: str) -> list:
    """Parse a product page and return a list of price records."""
    resp = requests.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    # Attempt to identify the product and chain names from header tags.
    title_tag = soup.find(['h1', 'h2'])
    title = title_tag.get_text(strip=True) if title_tag else ''

    table = soup.find('table')
    if not table:
        return []

    # Build header from the first row.  Subsequent columns usually denote
    # different variants (e.g. size or location-specific prices).
    rows = table.find_all('tr')
    header_cells = rows[0].find_all(['th', 'td'])
    headers = [cell.get_text(strip=True) for cell in header_cells]

    data = []
    for row in rows[1:]:
        cols = row.find_all(['th', 'td'])
        if not cols:
            continue
        date_str = cols[0].get_text(strip=True)
        # Normalise date formats like '2026.02.01' or '2024年4月'
        date_obj = None
        for fmt in ('%Y.%m.%d', '%Y.%m', '%Y/%m/%d', '%Y年%m月%d日', '%Y年%m月'):
            try:
                date_obj = datetime.strptime(date_str, fmt)
                break
            except ValueError:
                continue
        if not date_obj:
            # Skip rows with unparseable dates
            continue
        for idx, cell in enumerate(cols[1:], start=1):
            price_text = cell.get_text(strip=True)
            price = parse_price_string(price_text)
            if price is None:
                continue
            variant = headers[idx] if idx < len(headers) else f'Variant {idx}'
            data.append({
                'chain': title,
                'product': title,
                'variant': variant,
                'date': date_obj.date().isoformat(),
                'price': price,
                'source_url': url,
            })
    return data


def crawl(base_url: str, start_year: int) -> list:
    """Crawl the foodservice index page and return aggregated records."""
    resp = requests.get(base_url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    # Extract unique product page links.  Many anchor tags point back to the
    # same page, so deduplicate them.
    pages = set()
    for link in soup.find_all('a', href=True):
        href = link['href']
        # Skip index anchors and external links to other categories
        if href.startswith('#'):
            continue
        url = urljoin(base_url, href)
        if url.endswith('.html') and url != base_url:
            pages.add(url)

    dataset = []
    for page_url in sorted(pages):
        try:
            records = parse_table(page_url)
        except Exception as exc:
            # Skip pages that cannot be parsed but record the failure
            print(f"Failed to parse {page_url}: {exc}")
            continue
        if not records:
            continue

        # Group records by (chain, product, variant) and determine baseline
        series_map = {}
        for rec in records:
            key = (rec['chain'], rec['product'], rec['variant'])
            series_map.setdefault(key, []).append(rec)
        for key, series in series_map.items():
            # Sort series chronologically
            series_sorted = sorted(series, key=lambda r: r['date'])
            # Determine baseline price for records on or before the start year
            baseline_price = None
            baseline_year = None
            for rec in series_sorted:
                year = int(rec['date'][:4])
                if year <= start_year:
                    baseline_price = rec['price']
                    baseline_year = year
                else:
                    break
            if baseline_price is None:
                # Use the first available price as baseline if no early record
                baseline_price = series_sorted[0]['price']
                baseline_year = int(series_sorted[0]['date'][:4])

            # Add synthetic baseline row if earliest record is after start year
            if baseline_year > start_year:
                baseline_rec = series_sorted[0].copy()
                baseline_rec['date'] = f"{start_year}-01-01"
                dataset.append(baseline_rec)

            # Append actual records (filtering by start year)
            for rec in series_sorted:
                if int(rec['date'][:4]) >= start_year:
                    dataset.append(rec)
        # Pause to reduce load on the server
        time.sleep(0.7)

    return dataset


def write_outputs(records: list, json_path: str, csv_path: str) -> None:
    """Write the scraped records to JSON and CSV files."""
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(json_path, 'w', encoding='utf-8') as jf:
        json.dump(records, jf, ensure_ascii=False, indent=2)
    if records:
        fieldnames = list(records[0].keys())
        with open(csv_path, 'w', encoding='utf-8', newline='') as cf:
            writer = csv.DictWriter(cf, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)


def main() -> None:
    parser = argparse.ArgumentParser(description='Scrape neage.jp foodservice data')
    parser.add_argument('--start-year', type=int, default=1996, help='First year to include')
    parser.add_argument('--out', default='data/neage_foodservice_events.json', help='Path to output JSON')
    parser.add_argument('--csv', default='data/neage_foodservice_events.csv', help='Path to output CSV')
    parser.add_argument('--base-url', default='https://neage.jp/gaisyoku/index.html', help='Foodservice index URL')
    args = parser.parse_args()
    records = crawl(args.base_url, args.start_year)
    write_outputs(records, args.out, args.csv)


if __name__ == '__main__':
    main()