#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
neage.jp 外食カテゴリ用スクレイパー v3

前回版が0件になった主因:
- neage.jp の価格表は <table> ではなく、本文テキスト上の
  「商品名」「年月日 価格（税込）」「YYYY年... 価格」
  という行構造で並んでいるページが多い。
- そのため、テーブル前提の抽出では raw records = 0 になる。

この版では BeautifulSoup.get_text("\n") の行を解析する。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://neage.jp/gaisyoku/index.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; neage-foodservice-dashboard/0.3; "
        "respectful research scraper)"
    ),
    "Accept-Language": "ja,en;q=0.8",
}

STOP_WORDS = (
    "参考サイト",
    "参考リンク",
    "マクドナルド値上げ・値下げの解説",
    "値上げ・値下げの解説",
    "値上げの解説",
    "あわせて読みたい",
    "関連記事",
    "ホーム",
    "食品の値上げ情報",
    "外食の値上げ情報",
    "日用品の値上げ情報",
    "サービスの値上げ情報",
    "公共料金などの値上げ",
    "その他の値上げ情報",
)

IGNORE_LINES = {
    "",
    "年月日",
    "価格",
    "価格（税込）",
    "価格（税込)",
    "年月日 価格（税込）",
    "年月日 価格（税込)",
    "S M L",
    "S　M　L",
}

DATE_RE = re.compile(
    r"^(?P<prefix>～)?(?P<year>(?:19|20)\d{2})年"
    r"(?:(?P<month>\d{1,2})月)?"
    r"(?:(?P<day>\d{1,2})日)?"
    r"(?:時点|現在|頃|から|より|[～〜~－-])?"
    r"\s*(?P<rest>.*)$"
)

# 価格: 190円 / 260～290円 / 520円？ など
PRICE_RE = re.compile(
    r"(?P<a>\d{1,4}(?:,\d{3})?)"
    r"(?:\s*[～〜~－-]\s*(?P<b>\d{1,4}(?:,\d{3})?))?"
    r"\s*円"
    r"(?:[？?])?"
)

# 地域・タイプ: 通常店：450円 / 準：340円 / 都：370円 など
VARIANT_PRICE_RE = re.compile(
    r"(?P<label>通常店|準都心店|都心店|通|準|都|S|M|L|小|中|大)"
    r"\s*[：:]\s*"
    r"(?P<a>\d{1,4}(?:,\d{3})?)"
    r"(?:\s*[～〜~－-]\s*(?P<b>\d{1,4}(?:,\d{3})?))?"
    r"\s*円"
    r"(?:[？?])?"
)

PRICE_TOKEN_RE = re.compile(
    r"(?:\d{1,4}(?:,\d{3})?(?:\s*[～〜~－-]\s*\d{1,4}(?:,\d{3})?)?\s*円[？?]?|[―－—-])"
)


def clean(text: str) -> str:
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def fetch(url: str, timeout: int = 30) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def stable_id(*parts: str) -> str:
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()[:14]


def normalize_variant(label: str) -> str:
    m = {
        "通": "通常店",
        "準": "準都心店",
        "都": "都心店",
    }
    return m.get(label, label)


def price_value(a: str, b: Optional[str] = None) -> tuple[float, Optional[float], Optional[float]]:
    a2 = float(a.replace(",", ""))
    if b:
        b2 = float(b.replace(",", ""))
        return (a2 + b2) / 2.0, a2, b2
    return a2, None, None


def date_from_match(m: re.Match) -> str:
    year = int(m.group("year"))
    month = int(m.group("month") or 1)
    day = int(m.group("day") or 1)
    month = max(1, min(month, 12))
    day = max(1, min(day, 28))
    return f"{year:04d}-{month:02d}-{day:02d}"


def discover_pages(index_url: str, max_pages: int = 200) -> list[str]:
    html = fetch(index_url)
    soup = BeautifulSoup(html, "html.parser")
    found = set()
    queue = deque([index_url])
    seen = set()

    # 入口ページで見えるリンクに加え、カテゴリページも軽くたどる。
    while queue and len(seen) < max_pages:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        try:
            html = fetch(url)
        except Exception:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"].split("#")[0])
            p = urlparse(href)
            if p.netloc != "neage.jp":
                continue
            if not p.path.startswith("/gaisyoku/"):
                continue
            if not p.path.endswith(".html"):
                continue
            if href == index_url:
                continue
            found.add(href)
            # index 的なページだけたどる
            if href.endswith("/index.html") and href not in seen:
                queue.append(href)
        time.sleep(0.1)

    # index.html は抽出対象から外す
    return sorted(u for u in found if not u.endswith("/index.html"))


def extract_meta(lines: list[str], soup: BeautifulSoup, url: str) -> dict:
    h1 = soup.find("h1")
    title = clean(h1.get_text(" ")) if h1 else ""
    company = ""
    chain = ""
    for line in lines[:60]:
        if line.startswith("運営会社"):
            company = clean(line.replace("運営会社", "", 1))
        elif line.startswith("店名"):
            chain = clean(line.replace("店名", "", 1))
    if not chain:
        chain = re.sub(r"の値上げ.*$", "", title).strip() or title or url.rsplit("/", 1)[-1].replace(".html", "")
    return {"page_title": title, "company": company, "chain": chain}


def is_stop(line: str) -> bool:
    if not line:
        return False
    if line.startswith("#") or line.startswith("##") or line.startswith("###"):
        return True
    return any(w in line for w in STOP_WORDS)


def looks_like_product_title(line: str) -> bool:
    if not line:
        return False
    if is_stop(line):
        return False
    if line in IGNORE_LINES:
        return False
    if DATE_RE.match(line):
        return False
    if PRICE_RE.search(line):
        return False
    if line.startswith(("運営会社", "店名", "創業")):
        return False
    if len(line) > 70:
        return False
    # 日本語・英数字を含む短い見出し
    return bool(re.search(r"[ぁ-んァ-ン一-龥A-Za-z0-9]", line))


def is_header_line(line: str) -> bool:
    # get_textの分割方法によって「年月日」「価格（税込）」が別行になることがある
    return ("年月日" in line and "価格" in line) or line == "年月日"


@dataclass
class Record:
    chain: str
    company: str
    product: str
    variant: str
    date: str
    price: float
    price_min: Optional[float]
    price_max: Optional[float]
    price_raw: str
    index: Optional[float]
    source_url: str
    page_title: str
    is_baseline_1996: bool = False
    series_id: str = ""


def add_record(records: list[Record], meta: dict, product: str, variant: str, date: str, price: float,
               price_min: Optional[float], price_max: Optional[float], raw: str, url: str):
    variant = normalize_variant(variant or "標準")
    product = product or "不明商品"
    series_id = stable_id(meta["chain"], product, variant, url)
    records.append(
        Record(
            chain=meta["chain"],
            company=meta["company"],
            product=product,
            variant=variant,
            date=date,
            price=round(float(price), 2),
            price_min=price_min,
            price_max=price_max,
            price_raw=raw,
            index=None,
            source_url=url,
            page_title=meta["page_title"],
            series_id=series_id,
        )
    )


def parse_prices_from_rest(records: list[Record], meta: dict, product: str, date: str, rest: str,
                           url: str, size_headers: Optional[list[str]] = None):
    rest = clean(rest)
    if not rest:
        return

    # 1) 通常店：450円 / 準：340円 などのラベル付き価格
    labelled = list(VARIANT_PRICE_RE.finditer(rest))
    if labelled:
        for m in labelled:
            val, mn, mx = price_value(m.group("a"), m.group("b"))
            add_record(records, meta, product, m.group("label"), date, val, mn, mx, rest, url)
        # ラベル付き価格がある場合でも、行頭に「190円 通：330円」のような
        # ラベルなし価格が混じることがある。これは標準/Sとして拾う。
        prefix = rest[: labelled[0].start()].strip()
        plain = list(PRICE_RE.finditer(prefix))
        if plain:
            for i, pm in enumerate(plain):
                val, mn, mx = price_value(pm.group("a"), pm.group("b"))
                variant = size_headers[i] if size_headers and i < len(size_headers) else ("標準" if i == 0 else f"価格{i+1}")
                add_record(records, meta, product, variant, date, val, mn, mx, rest, url)
        return

    # 2) S M Lのようなサイズ列
    tokens = PRICE_TOKEN_RE.findall(rest)
    price_matches = list(PRICE_RE.finditer(rest))
    if size_headers and tokens:
        price_i = 0
        for i, token in enumerate(tokens):
            token = clean(token)
            if token in ("―", "－", "—", "-"):
                continue
            pm = PRICE_RE.search(token)
            if not pm:
                continue
            val, mn, mx = price_value(pm.group("a"), pm.group("b"))
            variant = size_headers[i] if i < len(size_headers) else f"価格{price_i+1}"
            add_record(records, meta, product, variant, date, val, mn, mx, rest, url)
            price_i += 1
        return

    # 3) 通常の1列価格、または複数価格
    if price_matches:
        for i, pm in enumerate(price_matches):
            val, mn, mx = price_value(pm.group("a"), pm.group("b"))
            variant = "標準" if len(price_matches) == 1 else f"価格{i+1}"
            add_record(records, meta, product, variant, date, val, mn, mx, rest, url)


def extract_page_records(url: str) -> tuple[list[Record], str]:
    html = fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    raw_lines = [clean(x) for x in soup.get_text("\n").splitlines()]
    lines = [x for x in raw_lines if x]

    meta = extract_meta(lines, soup, url)
    records: list[Record] = []

    product = ""
    active = False
    current_date: Optional[str] = None
    size_headers: Optional[list[str]] = None
    pre_header_product: Optional[str] = None
    price_line_count_after_header = 0

    # 価格表の構造:
    # 商品名
    # 年月日 価格（税込）
    # 1971年7月～ 80円
    # ...
    for i, line in enumerate(lines):
        if is_stop(line) and active:
            break

        # 商品名候補を保存。次に「年月日」が来たらこれを商品名として採用。
        if looks_like_product_title(line):
            # 「S M L」はサイズヘッダなので商品名候補から除外
            if re.fullmatch(r"[SML小中大](\s+[SML小中大])+", line):
                pass
            else:
                pre_header_product = line

        if is_header_line(line):
            if pre_header_product:
                product = pre_header_product
            active = True
            current_date = None
            size_headers = None
            price_line_count_after_header = 0
            continue

        if not active:
            continue

        # サイズヘッダー: S M L / 並 大 特盛 など
        if re.fullmatch(r"(S|M|L|小|中|大)(\s+(S|M|L|小|中|大))+", line):
            size_headers = line.split()
            continue

        # 次の商品見出しに移った可能性。
        # 価格表の途中にある地域価格行は商品名扱いしない。
        if looks_like_product_title(line):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if is_header_line(nxt):
                # 次ループで商品名として採用される
                active = False
                current_date = None
                size_headers = None
                continue

        dm = DATE_RE.match(line)
        if dm:
            current_date = date_from_match(dm)
            rest = clean(dm.group("rest"))
            parse_prices_from_rest(records, meta, product, current_date, rest, url, size_headers)
            price_line_count_after_header += 1
            continue

        # 日付の次行に「準都心店：470円」「都心店：500円」等が来るケース
        if current_date and PRICE_RE.search(line):
            parse_prices_from_rest(records, meta, product, current_date, line, url, size_headers)
            price_line_count_after_header += 1
            continue

        # 価格表に入ったあと、しばらく価格が出なければ表終了とみなす
        if active and price_line_count_after_header > 0 and not PRICE_RE.search(line):
            # ただし空白・補足っぽい行は無視
            pass

    return records, meta.get("chain", "")


def normalise_records(records: list[Record], start_year: int) -> list[Record]:
    cutoff = f"{start_year:04d}-01-01"
    groups: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        groups[r.series_id].append(r)

    out: list[Record] = []
    for sid, arr in groups.items():
        arr = sorted(arr, key=lambda r: r.date)
        before = [r for r in arr if r.date < cutoff]
        after = [r for r in arr if r.date >= cutoff]

        if before:
            base = before[-1]
            baseline = Record(**asdict(base))
            baseline.date = cutoff
            baseline.is_baseline_1996 = True
            baseline.price_raw = f"1996年基準行（直前観測値: {base.date} / {base.price_raw}）"
            after = [baseline] + after

        if not after:
            continue

        base_price = after[0].price
        seen = set()
        for r in after:
            sig = (r.series_id, r.date, r.price, r.variant, r.product)
            if sig in seen:
                continue
            seen.add(sig)
            r.index = round((r.price / base_price) * 100.0, 2) if base_price else None
            out.append(r)

    return sorted(out, key=lambda r: (r.chain, r.product, r.variant, r.date))


def write_outputs(records: list[Record], failed: list[dict], out_json: str, out_csv: str, failed_json: str):
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    os.makedirs(os.path.dirname(failed_json), exist_ok=True)

    rows = [asdict(r) for r in records]

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    if rows:
        with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    else:
        with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
            f.write("")

    with open(failed_json, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "failed_count": len(failed),
                "failed": failed,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=1996)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--out", default="data/neage_foodservice_events.json")
    parser.add_argument("--csv", default="data/neage_foodservice_events.csv")
    parser.add_argument("--failed", default="data/neage_foodservice_failed.json")
    parser.add_argument("--delay", type=float, default=0.75)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    print(f"[INFO] discovering pages from {args.base_url}")
    pages = discover_pages(args.base_url)
    if args.limit:
        pages = pages[: args.limit]
    print(f"[INFO] candidate pages: {len(pages)}")

    all_records: list[Record] = []
    failed: list[dict] = []

    for i, url in enumerate(pages, 1):
        try:
            recs, chain = extract_page_records(url)
            print(f"[INFO] {i}/{len(pages)} {url} -> {len(recs)} raw records")
            if not recs:
                failed.append({"url": url, "reason": "no_records_extracted"})
            all_records.extend(recs)
        except Exception as e:
            print(f"[WARN] {i}/{len(pages)} {url} -> {type(e).__name__}: {e}")
            failed.append({"url": url, "reason": f"{type(e).__name__}: {e}"})
        time.sleep(args.delay)

    normalised = normalise_records(all_records, args.start_year)
    series_count = len({r.series_id for r in normalised})

    write_outputs(normalised, failed, args.out, args.csv, args.failed)

    print(f"[DONE] records={len(normalised)} series={series_count} failed={len(failed)}")
    print(f"[DONE] wrote {args.out}")
    print(f"[DONE] wrote {args.csv}")
    print(f"[DONE] wrote {args.failed}")


if __name__ == "__main__":
    main()
