#!/usr/bin/env python3
"""
Zenodo-based publications refresher for _data/publications.yml.

Academia.edu blocks scraping (403), so this script uses the public Zenodo REST
API (https://zenodo.org/api/records) as the source of truth for new works.

What it does
  1. Collects every Zenodo record for Dr. Syed Muntasir Mamun, querying by ORCID
     and by name, paging through all results and honouring Zenodo rate limits.
  2. Keeps ONLY records where he is among the creators (ORCID match, or a creator
     name containing "syed", "muntasir" and "mamun"). Everything else is
     reported and dropped.
  3. De-duplicates by conceptrecid and DOI (latest version wins).
  4. Merges into the existing publications.yml without removing or rewriting
     existing entries:
       - an existing entry matches a record by DOI, Zenodo URL, Academia URL
         (from the record's related identifiers / description) or normalised
         title; matched entries only gain missing `doi` / `zenodo_url` fields,
         their `url` is never changed;
       - unmatched records are added as new entries (newest first) with the
         same schema the Academia scraper writes
         (title, authors, section, url, thumbnail_url, doi, zenodo_url).
  5. Rebuilds the `sections` view exactly like scripts/scrape_publications.py
     (first 50 per section, in all_publications order), and updates
     total_publications and last_updated.
  6. Refuses to write if the entry count would drop.

Usage
  python scripts/zenodo_publications.py [--dry-run] [--output PATH] [--cache FILE]

Only publications.yml (or --output) is written; books.yml is never touched.
"""

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml

API = "https://zenodo.org/api/records"
ORCID = "0000-0001-6845-2853"
QUERIES = [
    f'creators.orcid:"{ORCID}"',
    'creators.name:"Mamun, Syed Muntasir"',
    '"Syed Muntasir Mamun"',
    '"Mamun, Syed Muntasir"',
]
PAGE_SIZE = 25          # max page size for anonymous Zenodo API clients
MIN_INTERVAL = 2.2      # seconds between requests (guest limit is ~30/min)
OWNER_DISPLAY = "Dr. Syed Muntasir Mamun"
SECTION_LIMIT = 50
PROTECTED_FILES = ("books.yml",)

# Zenodo resource type -> site section (sections used by the Academia scraper).
SECTION_MAP = {
    "publication-book": "Books",
    "publication-thesis": "Thesis Chapters",
    "presentation": "Conference Presentations",
    "poster": "Conference Presentations",
    "lesson": "Teaching Documents",
}
DEFAULT_SECTION = "Papers"


def norm(t):
    return " ".join(re.findall(r"[a-z0-9]+", html.unescape(t or "").lower()))


def norm_doi(d):
    d = (d or "").strip().lower()
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", d)


def is_owner(creator):
    if (creator.get("orcid") or "").strip() == ORCID:
        return True
    toks = set(norm(creator.get("name")).split())
    return {"syed", "muntasir", "mamun"} <= toks


class Zenodo:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "drmuntasir.github.io publications refresher"
        self.s.headers["Accept"] = "application/json"
        self.last = 0.0
        self.requests = 0

    def get(self, params):
        for attempt in range(8):
            wait = MIN_INTERVAL - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.requests += 1
            try:
                r = self.s.get(API, params=params, timeout=60)
            except requests.RequestException as e:
                print(f"  ! request error ({e}); retrying", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                ra = int(r.headers.get("Retry-After") or 60)
                print(f"  ! HTTP {r.status_code}; sleeping {ra}s", file=sys.stderr)
                time.sleep(ra)
                continue
            r.raise_for_status()
            rem = r.headers.get("X-RateLimit-Remaining")
            reset = r.headers.get("X-RateLimit-Reset")
            if rem is not None and int(rem) <= 1 and reset:
                time.sleep(max(1, int(reset) - int(time.time()) + 1))
            return r.json()
        raise RuntimeError(f"Zenodo request failed repeatedly: {params}")

    def search(self, q):
        hits, page, total = [], 1, None
        while True:
            d = self.get({"q": q, "size": PAGE_SIZE, "page": page, "sort": "newest"})
            total = d["hits"]["total"]
            batch = d["hits"]["hits"]
            hits.extend(batch)
            if not batch or len(hits) >= total or "next" not in (d.get("links") or {}):
                break
            page += 1
        print(f"  {q}: total={total}, fetched={len(hits)}")
        return hits


def collect(cache=None):
    if cache and Path(cache).exists():
        raw = json.loads(Path(cache).read_text())
        print(f"Loaded {len(raw)} raw hits from cache {cache}")
    else:
        z = Zenodo()
        raw = []
        for q in QUERIES:
            raw.extend(z.search(q))
        print(f"Zenodo requests made: {z.requests}")
        if cache:
            Path(cache).write_text(json.dumps(raw))
    by_concept, excluded = {}, {}
    for h in raw:
        md = h.get("metadata") or {}
        key = str(h.get("conceptrecid") or h.get("id"))
        if not any(is_owner(c) for c in md.get("creators") or []):
            excluded[key] = h
            continue
        cur = by_concept.get(key)
        if cur is None or int(h["id"]) > int(cur["id"]):
            by_concept[key] = h
    # Secondary de-dup by DOI.
    seen, records = set(), []
    for h in by_concept.values():
        d = norm_doi(h.get("doi") or (h.get("metadata") or {}).get("doi"))
        if d and d in seen:
            continue
        seen.add(d)
        records.append(h)
    excluded = [h for k, h in excluded.items() if k not in by_concept]
    print(f"Raw hits: {len(raw)}; authored unique records: {len(records)}; "
          f"excluded (not a creator): {len(excluded)}")
    return records, excluded


def academia_urls(h):
    md = h.get("metadata") or {}
    urls = [ri.get("identifier", "") for ri in md.get("related_identifiers") or []]
    urls += re.findall(r'href="([^"]+)"', md.get("description") or "")
    return {html.unescape(u) for u in urls if "academia.edu" in u}


def author_names(md):
    out = []
    for c in md.get("creators") or []:
        if is_owner(c):
            out.append(OWNER_DISPLAY)
            continue
        name = (c.get("name") or "").strip()
        if "," in name:
            last, first = [x.strip() for x in name.split(",", 1)]
            name = f"{first} {last}".strip()
        out.append(name)
    return out


def section_for(md):
    rt = md.get("resource_type") or {}
    key = rt.get("type", "")
    if rt.get("subtype"):
        key2 = f"{key}-{rt['subtype']}"
        if key2 in SECTION_MAP:
            return SECTION_MAP[key2]
    return SECTION_MAP.get(key, DEFAULT_SECTION)


def merge(data, records):
    allp = data.setdefault("all_publications", [])
    by_doi, by_zurl, by_url, by_title = {}, {}, {}, {}
    for p in allp:
        if p.get("doi"):
            by_doi.setdefault(norm_doi(p["doi"]), p)
        if p.get("zenodo_url"):
            by_zurl.setdefault(p["zenodo_url"].rstrip("/"), p)
        if p.get("url"):
            by_url.setdefault(p["url"], p)
        by_title.setdefault(norm(p.get("title")), p)

    matched, enriched, new = 0, 0, []
    for h in records:
        md = h.get("metadata") or {}
        doi = h.get("doi") or md.get("doi")
        zurl = f"https://zenodo.org/records/{h['id']}"
        concept_doi = h.get("conceptdoi")
        hit = (by_doi.get(norm_doi(doi)) or by_doi.get(norm_doi(concept_doi))
               or by_zurl.get(zurl)
               or next((by_url[u] for u in academia_urls(h) if u in by_url), None)
               or by_title.get(norm(md.get("title") or h.get("title"))))
        if hit is not None:
            matched += 1
            changed = False
            if not hit.get("doi") and doi:
                hit["doi"] = doi
                changed = True
            if not hit.get("zenodo_url"):
                hit["zenodo_url"] = zurl
                changed = True
            enriched += changed
            continue
        title = html.unescape((md.get("title") or h.get("title") or "").strip())
        entry = {
            "title": title,
            "authors": author_names(md),
            "section": section_for(md),
            "url": zurl,
            "thumbnail_url": "",
            "doi": doi,
            "zenodo_url": zurl,
            "_date": md.get("publication_date") or h.get("created", "")[:10],
        }
        new.append(entry)
        # Register so duplicates inside the Zenodo set don't double-add.
        by_title.setdefault(norm(title), entry)
        if doi:
            by_doi.setdefault(norm_doi(doi), entry)

    new.sort(key=lambda e: e["_date"], reverse=True)
    for e in new:
        e.pop("_date")
    data["all_publications"] = new + allp

    # Rebuild sections view the same way scrape_publications.py does.
    sections = {}
    for p in data["all_publications"]:
        sections.setdefault(p.get("section") or DEFAULT_SECTION, []).append(p)
    data["sections"] = {}
    for name, items in sections.items():
        data["sections"][name] = [
            {k: v for k, v in (("title", p["title"]), ("authors", p["authors"]),
                               ("url", p["url"]), ("thumbnail_url", p.get("thumbnail_url", "")),
                               ("doi", p.get("doi")), ("zenodo_url", p.get("zenodo_url")))
             if v is not None or k in ("title", "authors", "url", "thumbnail_url")}
            for p in items[:SECTION_LIMIT]
        ]
    data["total_publications"] = len(data["all_publications"])
    data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    print(f"Matched existing: {matched} (enriched with DOI/Zenodo: {enriched}); new: {len(new)}")
    return data, new


def main():
    ap = argparse.ArgumentParser()
    root = Path(__file__).resolve().parent.parent
    ap.add_argument("--output", default=str(root / "_data" / "publications.yml"))
    ap.add_argument("--cache", help="JSON file to cache/reuse raw Zenodo hits")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", help="write a JSON report (new + excluded records)")
    a = ap.parse_args()
    out = Path(a.output)
    if out.name in PROTECTED_FILES:
        sys.exit(f"Refusing to write protected file {out}")

    old = yaml.safe_load(out.read_text(encoding="utf-8")) if out.exists() else {}
    old = old or {}
    before = len(old.get("all_publications") or [])
    old_keys = {(p.get("title"), p.get("url")) for p in old.get("all_publications") or []}

    records, excluded = collect(a.cache)
    if not records:
        sys.exit("No Zenodo records found; not touching the data file.")
    data = {"last_updated": old.get("last_updated"),
            "total_publications": old.get("total_publications"),
            "sections": old.get("sections") or {},
            "all_publications": old.get("all_publications") or []}
    data, new = merge(data, records)

    after = len(data["all_publications"])
    kept = {(p.get("title"), p.get("url")) for p in data["all_publications"]}
    if after < before or not old_keys <= kept:
        sys.exit(f"Safety check failed: before={before}, after={after}; not writing.")
    print(f"Total publications: {before} -> {after}")

    if a.report:
        Path(a.report).write_text(json.dumps({
            "zenodo_records": len(records),
            "new": [{"title": e["title"], "doi": e["doi"], "section": e["section"]} for e in new],
            "excluded": [{"id": h["id"], "title": (h.get("metadata") or {}).get("title"),
                          "creators": [c.get("name") for c in (h.get("metadata") or {}).get("creators") or []]}
                         for h in excluded],
        }, indent=1, ensure_ascii=False))
    if a.dry_run:
        print("Dry run: nothing written.")
        return
    with open(out, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
