#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
値上げ備忘録 neage.jp 外食カテゴリ scraper v2

- https://neage.jp/gaisyoku/index.html を入口に外食カテゴリ配下を再帰的に収集
- HTML table だけでなく、本文テキスト化された価格表も抽出
- 商品 × サイズ × 地域価格を別系列として出力
- 1996年以前の直近観測値がある系列は 1996-01-01 の基準行を合成
- 取得できないページ・抽出0件ページは failed に記録
- data/neage_foodservice_events.json と CSV を生成

出力JSONはダッシュボード互換の「配列」です。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

START_URL = "https://neage.jp/gaisyoku/index.html"
BASE_HOST = "neage.jp"
START_YEAR_DEFAULT = 1996

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; neage-foodservice-dashboard/0.2; respectful research bot)",
    "Accept-Language": "ja,en;q=0.8",
}

GENERIC_LINES = {
    "年月日", "価格", "価格（税込）", "年月日 価格（税込）", "税込", "税抜", "S M L",
    "外食", "飲食店", "ファーストフード", "テイクアウト", "居酒屋", "カテゴリ一覧",
    "ホーム", "トップページへ", "外食トップへ", "あわせて読みたい関連記事",
}

VARIANT_NAMES = [
    "通常店", "準都心店", "都心店", "標準店", "地方都市店", "都市型店",
    "店内", "持ち帰り", "テイクアウト", "税込", "標準",
    "S", "M", "L", "SS", "LL", "小", "並", "大", "特大",
]

DATE_RE = re.compile(
    r"(?P<lead>～)?(?P<year>(?:19|20)\d{2})\s*年\s*(?P<month>\d{1,2})?\s*(?:月)?\s*(?P<tail>時点|頃|ごろ|～)?"
)
PRICE_RE = re.compile(r"(?P<a>\d{1,4}(?:,\d{3})?)(?:\s*[～〜~－\-]\s*(?P<b>\d{1,4}(?:,\d{3})?))?\s*円\??")
VARIANT_PRICE_RE = re.compile(
    r"(?P<variant>通常店|準都心店|都心店|標準店|地方都市店|都市型店|店内|持ち帰り|テイクアウト|S|M|L|SS|LL|小|並|大|特大)\s*[：:]\s*"
    r"(?P<price>\d{1,4}(?:,\d{3})?(?:\s*[～〜~－\-]\s*\d{1,4}(?:,\d{3})?)?\s*円\??)"
)


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def fetch(url: str, timeout: int = 30) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() in {"iso-8859-1", "ascii"}:
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def normalise_url(base: str, href: str) -> str | None:
    if not href or href.startswith("#"):
        return None
    url = urljoin(base, href).split("#", 1)[0]
    p = urlparse(url)
    if p.netloc and p.netloc != BASE_HOST:
        return None
    if not p.path.startswith("/gaisyoku/"):
        return None
    if not (p.path.endswith(".html") or p.path.endswith("/")):
        return None
    return url


def discover_pages(start_url: str, max_pages: int, delay: float) -> list[str]:
    """外食カテゴリ配下の .html ページを再帰的に集める。"""
    seen: set[str] = set()
    discovered: set[str] = set()
    q: deque[str] = deque([start_url])

    while q and len(seen) < max_pages:
        url = q.popleft()
        if url in seen:
            continue
        seen.add(url)
        try:
            html = fetch(url)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] discover failed: {url}: {exc}")
            continue

        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            u = normalise_url(url, a.get("href", ""))
            if not u:
                continue
            discovered.add(u)
            if u not in seen and len(seen) + len(q) < max_pages:
                q.append(u)
        time.sleep(delay)

    # index系は本文にも価格表がないことが多いため後ろに回す/除外する
    pages = sorted(u for u in discovered if not u.endswith("/index.html") and u != start_url)
    return pages


def page_meta(soup: BeautifulSoup) -> dict[str, str]:
    title = clean_text(soup.find("h1").get_text(" ")) if soup.find("h1") else ""
    all_text = clean_text(soup.get_text(" "))
    shop = ""
    company = ""
    m = re.search(r"店名\s+([^\s]+)", all_text)
    if m:
        shop = clean_text(m.group(1))
    m = re.search(r"運営会社\s+([^\s]+)", all_text)
    if m:
        company = clean_text(m.group(1))
    if not shop:
        shop = re.sub(r"の値上げ.*$", "", title).strip() or title or "不明チェーン"
    return {"title": title, "chain": shop, "company": company}


def parse_date_from_line(line: str) -> tuple[str, str, str] | None:
    m = DATE_RE.search(line)
    if not m:
        return None
    year = int(m.group("year"))
    month = int(m.group("month") or 1)
    month = min(max(month, 1), 12)
    date = f"{year:04d}-{month:02d}-01"
    rest = (line[: m.start()] + " " + line[m.end() :]).strip()
    return date, clean_text(rest), clean_text(m.group(0))


def price_value(price_text: str) -> tuple[float | None, float | None, float | None]:
    m = PRICE_RE.search(price_text)
    if not m:
        return None, None, None
    a = float(m.group("a").replace(",", ""))
    b = float(m.group("b").replace(",", "")) if m.group("b") else None
    if b is None:
        return a, None, None
    return (a + b) / 2.0, a, b


def extract_variant_prices(text: str, default_variant: str = "標準") -> list[dict[str, Any]]:
    """1行中の価格を variant 別に抽出。"""
    out: list[dict[str, Any]] = []

    # 通常店：480円 / 準都心店：500円 のような表記
    for m in VARIANT_PRICE_RE.finditer(text):
        val, lo, hi = price_value(m.group("price"))
        if val is not None:
            out.append({
                "variant": clean_text(m.group("variant")),
                "price": val,
                "price_min": lo,
                "price_max": hi,
                "raw_price": clean_text(m.group("price")),
            })
    if out:
        return out

    # 通常の「190円」「240～270円」
    m = PRICE_RE.search(text)
    if m:
        val, lo, hi = price_value(m.group(0))
        if val is not None:
            out.append({
                "variant": default_variant,
                "price": val,
                "price_min": lo,
                "price_max": hi,
                "raw_price": clean_text(m.group(0)),
            })
    return out


def line_has_price(line: str) -> bool:
    return bool(PRICE_RE.search(line) or VARIANT_PRICE_RE.search(line))


def looks_like_item_heading(line: str, next_lines: list[str]) -> bool:
    line = clean_text(line)
    if not line or line in GENERIC_LINES:
        return False
    if len(line) > 42:
        return False
    if line_has_price(line) or DATE_RE.search(line):
        return False
    if any(x in line for x in ["運営会社", "店名", "創業", "Copyright", "関連記事", "値上げ情報", "カテゴリ", "トップ"]):
        return False
    joined_next = " ".join(next_lines[:3])
    if "年月日" in joined_next and "価格" in joined_next:
        return True
    # S/M/L 表の直前にも商品名が来る
    if any(DATE_RE.search(n) and line_has_price(n) for n in next_lines[:4]):
        return True
    return False


def parse_multicol_date_line(line: str, size_header: list[str]) -> list[dict[str, Any]]:
    """例: '1980年～ 140円 ― 250円' を S/M/L に対応付ける。"""
    parsed = parse_date_from_line(line)
    if not parsed:
        return []
    date, rest, _date_raw = parsed
    # 価格トークンとダッシュを順序維持で抽出
    tokens = re.findall(r"\d{1,4}(?:,\d{3})?(?:\s*[～〜~－\-]\s*\d{1,4}(?:,\d{3})?)?\s*円\??|[―—-]", rest)
    if len(tokens) < 2 or len(size_header) < 2:
        return []
    out = []
    for variant, tok in zip(size_header, tokens):
        if tok in {"―", "—", "-"}:
            continue
        val, lo, hi = price_value(tok)
        if val is None:
            continue
        out.append({
            "date": date,
            "variant": variant,
            "price": val,
            "price_min": lo,
            "price_max": hi,
            "raw_price": clean_text(tok),
        })
    return out


def extract_text_records(url: str, soup: BeautifulSoup, start_year: int) -> list[dict[str, Any]]:
    meta = page_meta(soup)
    # テキスト行に分解。HTMLテーブルでなくても抽出できるようにする。
    raw_lines = [clean_text(x) for x in soup.get_text("\n").split("\n")]
    lines = [x for x in raw_lines if x]

    records: list[dict[str, Any]] = []
    current_item = ""
    in_price_block = False
    size_header: list[str] = []
    last_date: str | None = None
    last_date_raw: str | None = None

    for i, line in enumerate(lines):
        next_lines = lines[i + 1 : i + 5]

        if looks_like_item_heading(line, next_lines):
            current_item = line
            in_price_block = False
            size_header = []
            last_date = None
            last_date_raw = None
            continue

        if "年月日" in line and "価格" in line:
            in_price_block = True
            # 同じ行に S M L が含まれるケースも拾う
            cols = [x for x in re.split(r"\s+", line) if x in {"S", "M", "L", "SS", "LL"}]
            if cols:
                size_header = cols
            continue

        if in_price_block and re.fullmatch(r"(?:S|M|L|SS|LL)(?:\s+(?:S|M|L|SS|LL))+", line):
            size_header = line.split()
            continue

        if not in_price_block or not current_item:
            continue

        # 新しい商品ブロックに移った可能性
        if looks_like_item_heading(line, next_lines):
            current_item = line
            in_price_block = False
            size_header = []
            last_date = None
            last_date_raw = None
            continue

        parsed_date = parse_date_from_line(line)
        if parsed_date:
            date, rest, date_raw = parsed_date
            last_date = date
            last_date_raw = date_raw

            # S/M/L など複数列表
            multi = parse_multicol_date_line(line, size_header)
            if multi:
                for p in multi:
                    records.append(make_record(url, meta, current_item, p["variant"], p["date"], p["price"], p["raw_price"], line, p.get("price_min"), p.get("price_max"), False))
                continue

            # 同一行の通常価格・地域別価格
            for p in extract_variant_prices(rest or line):
                records.append(make_record(url, meta, current_item, p["variant"], date, p["price"], p["raw_price"], line, p.get("price_min"), p.get("price_max"), False))
            continue

        # 日付の次行に「準都心店：470円」のように続くケース
        if last_date and line_has_price(line):
            for p in extract_variant_prices(line):
                records.append(make_record(url, meta, current_item, p["variant"], last_date, p["price"], p["raw_price"], line, p.get("price_min"), p.get("price_max"), False))
            continue

        # 長い説明文が来たら価格ブロック終了とみなす
        if len(line) > 60 and not line_has_price(line):
            in_price_block = False
            size_header = []
            last_date = None
            last_date_raw = None

    return records


def make_record(url: str, meta: dict[str, str], item: str, variant: str, date: str, price: float, raw_price: str, raw_line: str, price_min: float | None, price_max: float | None, synthetic: bool) -> dict[str, Any]:
    return {
        "chain": meta["chain"],
        "company": meta.get("company", ""),
        "product": item,
        "item": item,
        "variant": variant or "標準",
        "date": date,
        "price": round(float(price), 2),
        "price_min": price_min,
        "price_max": price_max,
        "index": None,
        "source_url": url,
        "raw_price": raw_price,
        "raw_line": raw_line,
        "page_title": meta.get("title", ""),
        "is_baseline_1996": synthetic,
    }


def series_key(r: dict[str, Any]) -> tuple[str, str, str, str]:
    return (r["chain"], r["product"], r["variant"], r["source_url"])


def normalise(records: list[dict[str, Any]], start_year: int) -> list[dict[str, Any]]:
    cutoff = f"{start_year:04d}-01-01"
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        groups[series_key(r)].append(r)

    out: list[dict[str, Any]] = []
    for _key, arr in groups.items():
        # 同日・同価格重複除去
        unique = {}
        for r in arr:
            sig = (r["date"], r["price"], r["variant"], r["product"])
            unique[sig] = r
        arr = sorted(unique.values(), key=lambda x: x["date"])
        if not arr:
            continue

        before = [r for r in arr if r["date"] < cutoff]
        after = [r for r in arr if r["date"] >= cutoff]

        if before:
            base = dict(before[-1])
            base["date"] = cutoff
            base["raw_line"] = f"{start_year}年基準として合成 / 直前観測値: {before[-1]['date']} {before[-1]['raw_line']}"
            base["is_baseline_1996"] = True
            after = [base] + after

        if not after:
            continue

        base_price = after[0]["price"]
        for r in after:
            r = dict(r)
            r["index"] = round((r["price"] / base_price) * 100, 2) if base_price else None
            out.append(r)

    return sorted(out, key=lambda x: (x["chain"], x["product"], x["variant"], x["date"]))


def write_outputs(records: list[dict[str, Any]], json_path: str, csv_path: str, failed_path: str, failed: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(json_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    if records:
        fields = list(records[0].keys())
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(records)
    else:
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("")

    with open(failed_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "failed": failed}, f, ensure_ascii=False, indent=2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-year", type=int, default=START_YEAR_DEFAULT)
    ap.add_argument("--out", default="data/neage_foodservice_events.json")
    ap.add_argument("--csv", default="data/neage_foodservice_events.csv")
    ap.add_argument("--failed", default="data/neage_foodservice_failed.json")
    ap.add_argument("--start-url", default=START_URL)
    ap.add_argument("--delay", type=float, default=0.7)
    ap.add_argument("--max-pages", type=int, default=180)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    print(f"[INFO] discovering pages from {args.start_url}")
    pages = discover_pages(args.start_url, args.max_pages, args.delay)
    if args.limit:
        pages = pages[: args.limit]
    print(f"[INFO] candidate pages: {len(pages)}")

    all_records: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for idx, url in enumerate(pages, start=1):
        try:
            html = fetch(url)
            soup = BeautifulSoup(html, "html.parser")
            recs = extract_text_records(url, soup, args.start_year)
            if not recs:
                failed.append({"url": url, "reason": "no_records_extracted"})
            all_records.extend(recs)
            print(f"[INFO] {idx}/{len(pages)} {url} -> {len(recs)} raw records")
        except Exception as exc:  # noqa: BLE001
            failed.append({"url": url, "reason": str(exc)})
            print(f"[WARN] {idx}/{len(pages)} {url} -> {exc}")
        time.sleep(args.delay)

    records = normalise(all_records, args.start_year)
    write_outputs(records, args.out, args.csv, args.failed, failed)

    series_count = len({series_key(r) for r in records})
    print(f"[DONE] records={len(records)} series={series_count} failed={len(failed)}")
    print(f"[DONE] wrote {args.out}")
    print(f"[DONE] wrote {args.csv}")
    print(f"[DONE] wrote {args.failed}")


if __name__ == "__main__":
    main()
