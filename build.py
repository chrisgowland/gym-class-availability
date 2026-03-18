#!/usr/bin/env python3
"""
Nuffield Health Gym Class Schedule Report Builder

Fetches the next full Mon–Sun week of class data from all Nuffield Health gyms
and generates a static HTML report (index.html).

Usage:
    python build.py                 # full run (all gyms)
    python build.py --gym bristol   # test with a single gym slug
"""

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

# ── Configuration ─────────────────────────────────────────────────────────────

API_KEY = "882ee8ab406042dd9da8045dc58874a3"
API_BASE = "https://api.nuffieldhealth.com/booking/open/1.0"
BASE_URL = "https://www.nuffieldhealth.com"
UK_TZ = ZoneInfo("Europe/London")

FETCH_HEADERS = {
    "ocp-apim-subscription-key": API_KEY,
    "accept": "application/json",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}
PAGE_HEADERS = {"User-Agent": FETCH_HEADERS["user-agent"]}

# Slugs present on /gyms that are not actual gym locations
NON_GYM_SLUGS = {
    "membership",
    "classes",
    "new-member-advice-hub",
    "gyms-in-london",
    "services",
    "book-your-gym-tour",
    "new-member-advice",
}

# Title keywords that identify swim/aqua classes (case-insensitive)
SWIM_KEYWORDS = ["aqua", "swim", "hydro", "water polo", "aquafit", "swimfit"]

# Category bucket keys in display order.
# "swim" is a virtual category detected from class titles, not the API field.
CATEGORY_KEYS = [
    "swim",
    "Cycle",
    "Balance",
    "HIIT",
    "Athletic",
    "Cardio",
    "Dance",
    "Tone",
    "Junior",
]

CATEGORY_LABELS = {
    "swim":     "Swim / Aqua",
    "Cycle":    "Cycle / Spin",
    "Balance":  "Yoga / Relax",
    "HIIT":     "HIIT",
    "Athletic": "Strength",
    "Cardio":   "Cardio",
    "Dance":    "Dance",
    "Tone":     "Tone",
    "Junior":   "Junior",
}

# Subtle background colours for each category column
CATEGORY_COLOURS = {
    "swim":     "#dbeafe",  # blue-100
    "Cycle":    "#fed7aa",  # orange-200
    "Balance":  "#ede9fe",  # violet-100
    "HIIT":     "#fee2e2",  # red-100
    "Athletic": "#d1d5db",  # gray-300
    "Cardio":   "#d1fae5",  # emerald-100
    "Dance":    "#fce7f3",  # pink-100
    "Tone":     "#fef9c3",  # yellow-100
    "Junior":   "#dcfce7",  # green-100
}

# ── Data fetching ─────────────────────────────────────────────────────────────


def get_gym_slugs() -> list[str]:
    """Return sorted list of gym location slugs from the gyms listing page."""
    resp = requests.get(f"{BASE_URL}/gyms", headers=PAGE_HEADERS, timeout=30)
    resp.raise_for_status()
    slugs = set(re.findall(r'href="/gyms/([a-z][a-z0-9-]+)"', resp.text))
    return sorted(slugs - NON_GYM_SLUGS)


def get_gym_sfid(slug: str) -> str | None:
    """Extract the Salesforce facility ID from a gym's timetable page."""
    resp = requests.get(
        f"{BASE_URL}/gyms/{slug}/timetable", headers=PAGE_HEADERS, timeout=30
    )
    if not resp.ok:
        return None
    m = re.search(r"a2T[A-Za-z0-9]{15}", resp.text)
    return m.group() if m else None


def get_week_classes(sfid: str, week_start: date, week_end: date) -> list[dict]:
    """Fetch all classes for a gym for the given Mon–Sun week."""
    from_str = f"{week_start.isoformat()}T00:00:00.000+00:00"
    to_str   = f"{week_end.isoformat()}T23:59:59.999+00:00"
    url = (
        f"{API_BASE}/bookable_items/gym/"
        f"?location={sfid}"
        f"&from_date={quote(from_str, safe='')}"
        f"&to_date={quote(to_str, safe='')}"
    )
    resp = requests.get(url, headers=FETCH_HEADERS, timeout=30)
    if resp.ok:
        return resp.json().get("items", [])
    return []


# ── Classification & analysis ─────────────────────────────────────────────────


def classify_class(cls: dict) -> str:
    """Return the bucket key for a class."""
    title = cls.get("title", "").lower()
    if any(kw in title for kw in SWIM_KEYWORDS):
        return "swim"
    return cls.get("product", {}).get("class_category", "Unknown")


def analyze_classes(classes: list[dict]) -> dict:
    """Return per-gym statistics from a list of class items."""
    total = len(classes)
    off_peak_weekday = 0
    weekend = 0
    categories: dict[str, int] = defaultdict(int)

    for cls in classes:
        dt_uk = datetime.fromisoformat(cls["from_date"]).astimezone(UK_TZ)
        weekday = dt_uk.weekday()   # 0 = Mon … 6 = Sun
        hour    = dt_uk.hour

        if weekday >= 5:                        # Sat or Sun
            weekend += 1
        elif hour < 9 or hour >= 18:            # Before 09:00 or from 18:00, Mon–Fri
            off_peak_weekday += 1

        categories[classify_class(cls)] += 1

    return {
        "total":            total,
        "off_peak_weekday": off_peak_weekday,
        "weekend":          weekend,
        "categories":       dict(categories),
    }


def week_range() -> tuple[date, date]:
    """Return (Monday, Sunday) for the reporting week.

    When run on Monday (as the scheduled workflow does), returns the current
    week so we capture a full Mon–Sun window while all classes are bookable.
    When run on any other day, returns the *next* Mon–Sun week.
    """
    today = date.today()
    if today.weekday() == 0:   # Monday → use current week
        monday = today
    else:
        days_until_monday = (7 - today.weekday()) % 7
        monday = today + timedelta(days=days_until_monday)
    return monday, monday + timedelta(days=6)


# ── HTML generation ───────────────────────────────────────────────────────────


def _pct(n: int, total: int) -> str:
    if total == 0 or n == 0:
        return str(n) if n else "–"
    pct = round(n * 100 / total)
    return f'{n} <span class="pct">({pct}%)</span>'


def generate_html(gyms: list[dict], week_start: date, week_end: date) -> str:
    def fmt_date(d) -> str:
        return d.strftime("%d %b").lstrip("0")

    now = datetime.now(UK_TZ)
    updated    = now.strftime("%d %b %Y at %H:%M ") + now.strftime("%Z")
    week_label = f"{fmt_date(week_start)} \u2013 {fmt_date(week_end)} {week_end.year}"
    gyms_with_data = sum(1 for g in gyms if g["stats"]["total"] > 0)

    # ── Category header cells ──────────────────────────────────────────────────
    cat_th = "".join(
        f'<th class="num sortable" data-col="cat_{k.lower()}" '
        f'style="background:{CATEGORY_COLOURS[k]}">'
        f'{CATEGORY_LABELS[k]}</th>'
        for k in CATEGORY_KEYS
    )

    # ── Table rows ─────────────────────────────────────────────────────────────
    rows = []
    for gym in sorted(gyms, key=lambda g: g["name"]):
        s     = gym["stats"]
        total = s["total"]
        cats  = s["categories"]

        cat_tds = "".join(
            f'<td class="num" style="background:{CATEGORY_COLOURS[k]}" '
            f'data-val="{cats.get(k,0)}">{cats.get(k, 0) or "–"}</td>'
            for k in CATEGORY_KEYS
        )

        rows.append(f"""
      <tr>
        <td class="gym-name">
          <a href="{BASE_URL}/gyms/{gym['slug']}" target="_blank" rel="noopener">
            {gym['name']}
          </a>
        </td>
        <td class="num" data-val="{total}">{total or "–"}</td>
        <td class="num" data-val="{s['off_peak_weekday']}">{_pct(s['off_peak_weekday'], total)}</td>
        <td class="num" data-val="{s['weekend']}">{_pct(s['weekend'], total)}</td>
        {cat_tds}
      </tr>""")

    rows_html = "\n".join(rows)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Nuffield Health – Weekly Class Schedule Analysis</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 14px;
      color: #1f2937;
      background: #f9fafb;
      padding: 1.5rem 1rem 3rem;
    }}

    header {{
      max-width: 1600px;
      margin: 0 auto 1.5rem;
    }}
    header h1 {{
      font-size: 1.6rem;
      font-weight: 700;
      color: #005eb8;   /* Nuffield blue */
    }}
    header h2 {{
      font-size: 1.1rem;
      font-weight: 500;
      color: #374151;
      margin-top: 0.15rem;
    }}
    header p {{
      margin-top: 0.5rem;
      font-size: 0.82rem;
      color: #6b7280;
    }}

    .legend {{
      max-width: 1600px;
      margin: 0 auto 1rem;
      font-size: 0.8rem;
      color: #6b7280;
      display: flex;
      gap: 1.5rem;
      flex-wrap: wrap;
    }}
    .legend span {{ display: flex; align-items: center; gap: 0.3rem; }}
    .legend-dot {{
      width: 10px; height: 10px; border-radius: 2px; display: inline-block;
    }}

    .table-wrap {{
      max-width: 1600px;
      margin: 0 auto;
      overflow-x: auto;
      border-radius: 8px;
      box-shadow: 0 1px 3px rgba(0,0,0,.12);
    }}

    table {{
      border-collapse: collapse;
      width: 100%;
      background: #fff;
    }}

    thead th {{
      position: sticky;
      top: 0;
      background: #1e3a5f;
      color: #fff;
      font-weight: 600;
      font-size: 0.78rem;
      text-align: center;
      padding: 0.55rem 0.5rem;
      white-space: nowrap;
      cursor: pointer;
      user-select: none;
    }}
    thead th.gym-name {{ text-align: left; }}
    thead th:hover {{ background: #2d5490; }}
    thead th.sort-asc::after  {{ content: " ↑"; opacity: .8; }}
    thead th.sort-desc::after {{ content: " ↓"; opacity: .8; }}

    /* Override category header colours while keeping text readable */
    thead th[style*="background"] {{
      color: #1f2937;
    }}

    tbody tr:nth-child(even) {{ background: #f3f4f6; }}
    tbody tr:hover {{ background: #e0f0ff; }}

    td {{
      padding: 0.4rem 0.55rem;
      border-bottom: 1px solid #e5e7eb;
      vertical-align: middle;
    }}
    td.gym-name {{
      font-weight: 500;
      white-space: nowrap;
    }}
    td.gym-name a {{
      color: #1d4ed8;
      text-decoration: none;
    }}
    td.gym-name a:hover {{ text-decoration: underline; }}

    td.num {{
      text-align: right;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    /* off-peak and weekend column headers */
    th.col-offpeak {{ min-width: 90px; }}
    th.col-weekend  {{ min-width: 80px; }}

    .pct {{ font-size: 0.75em; color: #6b7280; }}

    footer {{
      max-width: 1600px;
      margin: 2rem auto 0;
      font-size: 0.78rem;
      color: #9ca3af;
    }}
    footer a {{ color: #9ca3af; }}
  </style>
</head>
<body>

<header>
  <h1>Nuffield Health</h1>
  <h2>Weekly Class Schedule Analysis</h2>
  <p>
    Week: <strong>{week_label}</strong>
    &nbsp;·&nbsp; {gyms_with_data} gyms with data
    &nbsp;·&nbsp; Updated: {updated}
  </p>
  <p style="margin-top:.4rem;font-size:.78rem;color:#6b7280">
    <em>Off-peak weekday</em> = classes starting before 09:00 or from 18:00, Mon–Fri.
    Click any column header to sort. Times are UK local (BST/GMT).
  </p>
</header>

<div class="table-wrap">
  <table id="report">
    <thead>
      <tr>
        <th class="gym-name sortable" data-col="name">Gym</th>
        <th class="num sortable" data-col="total">Total<br>classes</th>
        <th class="num sortable col-offpeak" data-col="offpeak">
          Off-peak<br>weekday
        </th>
        <th class="num sortable col-weekend" data-col="weekend">Weekend</th>
        {cat_th}
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
</div>

<footer>
  <p>
    Data sourced from
    <a href="https://www.nuffieldhealth.com/gyms" target="_blank" rel="noopener">
      nuffieldhealth.com</a>.
    Refreshed automatically every Monday via GitHub Actions.
  </p>
</footer>

<script>
(function () {{
  const table   = document.getElementById("report");
  const tbody   = table.querySelector("tbody");
  const headers = table.querySelectorAll("th.sortable");

  let sortCol = null, sortDir = 1;

  headers.forEach((th, i) => {{
    th.addEventListener("click", () => {{
      const col = th.dataset.col;
      if (sortCol === col) {{ sortDir *= -1; }}
      else {{ sortCol = col; sortDir = 1; }}

      headers.forEach(h => h.classList.remove("sort-asc", "sort-desc"));
      th.classList.add(sortDir === 1 ? "sort-asc" : "sort-desc");

      const rows = [...tbody.querySelectorAll("tr")];
      rows.sort((a, b) => {{
        let av, bv;
        if (col === "name") {{
          av = a.querySelector(".gym-name").textContent.trim().toLowerCase();
          bv = b.querySelector(".gym-name").textContent.trim().toLowerCase();
          return sortDir * av.localeCompare(bv);
        }}
        const cells = a.querySelectorAll("td");
        const idx   = [...headers].indexOf(th);
        av = parseFloat(a.querySelectorAll("td")[idx].dataset.val) || 0;
        bv = parseFloat(b.querySelectorAll("td")[idx].dataset.val) || 0;
        return sortDir * (bv - av);   // numeric: high first by default
      }});
      rows.forEach(r => tbody.appendChild(r));
    }});
  }});
}})();
</script>

</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Build Nuffield Health class report")
    parser.add_argument(
        "--gym", metavar="SLUG", help="Test with a single gym slug (skips full crawl)"
    )
    args = parser.parse_args()

    week_start, week_end = week_range()
    print(f"Week: {week_start} – {week_end}", flush=True)

    # ── Get gym slugs ──────────────────────────────────────────────────────────
    if args.gym:
        slugs = [args.gym]
    else:
        print("Fetching gym list…", flush=True)
        slugs = get_gym_slugs()
        print(f"  Found {len(slugs)} slugs", flush=True)

    # ── Scrape each gym ────────────────────────────────────────────────────────
    gyms: list[dict] = []
    for i, slug in enumerate(slugs, 1):
        print(f"  [{i:3}/{len(slugs)}] {slug}", end="  ", flush=True)

        sfid = get_gym_sfid(slug)
        if not sfid:
            print("(no timetable ID – skipped)")
            continue

        classes = get_week_classes(sfid, week_start, week_end)
        stats   = analyze_classes(classes)

        # Gym display name comes from the API response if we have classes,
        # otherwise fall back to a title-cased version of the slug.
        name = slug.replace("-", " ").title()
        if classes:
            name = (
                classes[0]
                .get("room", {})
                .get("facility", {})
                .get("name", name)
            )

        gyms.append({"slug": slug, "name": name, "sfid": sfid, "stats": stats})
        print(f"{name}  ->  {stats['total']} classes", flush=True)

        time.sleep(0.5)   # polite rate limiting

    if not gyms:
        print("No gym data collected – aborting.", file=sys.stderr)
        sys.exit(1)

    # ── Save raw data ──────────────────────────────────────────────────────────
    data_path = Path("data") / f"{week_start.isoformat()}.json"
    data_path.parent.mkdir(exist_ok=True)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(
            {"week_start": str(week_start), "week_end": str(week_end), "gyms": gyms},
            f,
            indent=2,
        )
    print(f"\nRaw data saved to {data_path}", flush=True)

    # ── Generate HTML ──────────────────────────────────────────────────────────
    html = generate_html(gyms, week_start, week_end)
    out_path = Path("index.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"Report written to {out_path}", flush=True)


if __name__ == "__main__":
    main()
