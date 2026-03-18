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
    """Return (Monday, Sunday) for the current Mon–Sun week.

    Always uses the current week so that weekend classes are always within
    the API's booking window, regardless of what day the script runs.
    """
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


# ── HTML generation ───────────────────────────────────────────────────────────


def _pct(n: int, total: int) -> str:
    if total == 0 or n == 0:
        return str(n) if n else "\u2013"
    pct = round(n * 100 / total)
    return f'{pct}% <span class="pct">({n})</span>'


def generate_html(gyms: list[dict], week_start: date, week_end: date) -> str:
    def fmt_date(d) -> str:
        return d.strftime("%d %b").lstrip("0")

    now = datetime.now(UK_TZ)
    updated    = now.strftime("%d %b %Y at %H:%M ") + now.strftime("%Z")
    week_label = f"{fmt_date(week_start)} \u2013 {fmt_date(week_end)} {week_end.year}"
    gyms_with_data = sum(1 for g in gyms if g["stats"]["total"] > 0)

    # ── Category header cells ──────────────────────────────────────────────────
    cat_th = "".join(
        f'<th class="num sortable cat-header" data-col="cat_{k.lower()}" '
        f'style="--cat-bg:{CATEGORY_COLOURS[k]}">'
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
            f'data-val="{cats.get(k,0)}">{cats.get(k, 0) or "\u2013"}</td>'
            for k in CATEGORY_KEYS
        )

        rows.append(f"""
      <tr>
        <td class="gym-name">
          <a href="{BASE_URL}/gyms/{gym['slug']}" target="_blank" rel="noopener">
            {gym['name']}
          </a>
        </td>
        <td class="num total-col" data-val="{total}">{total or "\u2013"}</td>
        <td class="num offpeak-col" data-val="{round(s['off_peak_weekday']*100/total) if total else 0}">{_pct(s['off_peak_weekday'], total)}</td>
        <td class="num weekend-col" data-val="{round(s['weekend']*100/total) if total else 0}">{_pct(s['weekend'], total)}</td>
        {cat_tds}
      </tr>""")

    rows_html = "\n".join(rows)

    # ── Totals row ─────────────────────────────────────────────────────────────
    all_stats = [g["stats"] for g in gyms]
    tot_total    = sum(s["total"]            for s in all_stats)
    tot_offpeak  = sum(s["off_peak_weekday"] for s in all_stats)
    tot_weekend  = sum(s["weekend"]          for s in all_stats)
    tot_cat_tds  = "".join(
        f'<td class="num totals-row" style="background:{CATEGORY_COLOURS[k]}">'
        f'{sum(s["categories"].get(k, 0) for s in all_stats):,}</td>'
        for k in CATEGORY_KEYS
    )
    totals_row = f"""
      <tr class="totals-row">
        <td class="gym-name">All gyms ({len(gyms)})</td>
        <td class="num total-col" data-val="{tot_total}">{tot_total:,}</td>
        <td class="num offpeak-col" data-val="{round(tot_offpeak*100/tot_total) if tot_total else 0}">{_pct(tot_offpeak, tot_total)}</td>
        <td class="num weekend-col" data-val="{round(tot_weekend*100/tot_total) if tot_total else 0}">{_pct(tot_weekend, tot_total)}</td>
        {tot_cat_tds}
      </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Nuffield Health \u2013 Weekly Class Schedule Analysis</title>
  <style>
    @font-face {{
      font-family: 'NuffieldSans';
      font-weight: 300;
      font-display: swap;
      src: url('https://www.nuffieldhealth.com/assets/dist/fonts/NuffieldSans-Light-41a550a0.woff') format('woff');
    }}
    @font-face {{
      font-family: 'NuffieldSans';
      font-weight: 400;
      font-display: swap;
      src: url('https://www.nuffieldhealth.com/assets/dist/fonts/NuffieldSans-Regular-7f88adab.woff') format('woff');
    }}
    @font-face {{
      font-family: 'NuffieldSans';
      font-weight: 700;
      font-display: swap;
      src: url('https://www.nuffieldhealth.com/assets/dist/fonts/NuffieldSans-Bold-fa61a48a.woff') format('woff');
    }}

    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --green:       #00a200;
      --green-dark:  #388232;
      --green-deep:  #2f4f2d;
      --teal:        #0a5c6a;
      --text:        #282823;
      --text-mid:    #5f6062;
      --text-light:  #8f9091;
      --bg:          #f4f4f4;
      --white:       #ffffff;
      --border:      #dfdfdf;
    }}

    html {{ scroll-behavior: smooth; }}

    body {{
      font-family: 'NuffieldSans', 'Helvetica Neue', Helvetica, Arial, sans-serif;
      font-size: 14px;
      color: var(--text);
      background: var(--bg);
      min-height: 100vh;
    }}

    /* ── Top nav bar ─────────────────────────────────────────── */
    .site-nav {{
      background: var(--green);
      padding: 0 1.5rem;
      display: flex;
      align-items: center;
      height: 56px;
    }}
    .site-nav img {{
      height: 32px;
      width: auto;
    }}

    /* ── Page header ─────────────────────────────────────────── */
    .page-header {{
      background: var(--green-deep);
      color: var(--white);
      padding: 2rem 1.5rem 1.75rem;
    }}
    .page-header__inner {{
      max-width: 1600px;
      margin: 0 auto;
    }}
    .page-header h1 {{
      font-size: 1.75rem;
      font-weight: 700;
      letter-spacing: -0.01em;
      line-height: 1.2;
    }}
    .page-header__meta {{
      margin-top: 0.6rem;
      font-size: 0.88rem;
      color: rgba(255,255,255,0.78);
      display: flex;
      flex-wrap: wrap;
      gap: 0.3rem 1.25rem;
      align-items: baseline;
    }}
    .page-header__meta strong {{
      color: var(--white);
      font-weight: 700;
    }}
    .page-header__note {{
      margin-top: 0.75rem;
      font-size: 0.8rem;
      color: rgba(255,255,255,0.6);
    }}

    /* ── Main content ─────────────────────────────────────────── */
    .main {{
      max-width: 1600px;
      margin: 1.5rem auto;
      padding: 0 1rem 3rem;
    }}

    .table-wrap {{
      overflow-x: auto;
      border-radius: 6px;
      box-shadow: 0 2px 8px rgba(0,0,0,.10);
      border: 1px solid var(--border);
    }}

    table {{
      border-collapse: collapse;
      width: 100%;
      background: var(--white);
      font-size: 13px;
    }}

    /* ── Table head ──────────────────────────────────────────── */
    thead th {{
      position: sticky;
      top: 0;
      z-index: 2;
      background: var(--green);
      color: var(--white);
      font-family: 'NuffieldSans', sans-serif;
      font-weight: 700;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      text-align: center;
      padding: 0.6rem 0.6rem;
      white-space: nowrap;
      cursor: pointer;
      user-select: none;
      border-right: 1px solid rgba(255,255,255,0.15);
    }}
    thead th:last-child {{ border-right: none; }}
    thead th.gym-name {{ text-align: left; min-width: 160px; }}
    thead th:hover {{ background: var(--green-dark); }}
    thead th.sort-asc::after  {{ content: " \u25b2"; font-size: 0.65em; opacity: .9; }}
    thead th.sort-desc::after {{ content: " \u25bc"; font-size: 0.65em; opacity: .9; }}

    /* Category columns: coloured header, dark text */
    thead th.cat-header {{
      background: var(--cat-bg);
      color: var(--green-deep);
    }}
    thead th.cat-header:hover {{
      filter: brightness(0.92);
    }}

    /* Pinned core columns */
    thead th.total-col  {{ background: var(--teal); min-width: 70px; }}
    thead th.offpeak-col {{ background: #0a5c6a; min-width: 100px; }}
    thead th.weekend-col {{ background: #0a5c6a; min-width: 85px; }}

    /* ── Table body ──────────────────────────────────────────── */
    tbody tr {{ border-bottom: 1px solid var(--border); }}
    tbody tr:last-child {{ border-bottom: none; }}
    tbody tr:nth-child(even) {{ background: #fafafa; }}
    tbody tr:hover {{ background: #edf7ed; }}   /* light green tint on hover */

    td {{
      padding: 0.42rem 0.6rem;
      vertical-align: middle;
    }}
    td.gym-name {{
      font-weight: 700;
      white-space: nowrap;
      border-right: 2px solid var(--border);
    }}
    td.gym-name a {{
      color: var(--green-dark);
      text-decoration: none;
    }}
    td.gym-name a:hover {{
      color: var(--green);
      text-decoration: underline;
    }}
    td.num {{
      text-align: right;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    td.total-col  {{ font-weight: 700; color: var(--text); }}
    td.offpeak-col, td.weekend-col {{ color: var(--teal); font-weight: 600; }}

    .pct {{ font-size: 0.8em; color: var(--text-light); font-weight: 400; }}

    tfoot tr.totals-row td {{
      background: var(--green-deep) !important;
      color: var(--white);
      font-weight: 700;
      border-top: 2px solid var(--green);
    }}
    tfoot tr.totals-row td.gym-name {{
      color: var(--white);
    }}
    tfoot tr.totals-row .pct {{
      color: rgba(255,255,255,0.65);
    }}

    /* ── Footer ──────────────────────────────────────────────── */
    footer {{
      background: var(--green-deep);
      color: rgba(255,255,255,0.6);
      font-size: 0.78rem;
      padding: 1.25rem 1.5rem;
      margin-top: 2rem;
      text-align: center;
    }}
    footer a {{ color: rgba(255,255,255,0.8); }}
    footer a:hover {{ color: var(--white); }}
  </style>
</head>
<body>

<!-- Navigation bar -->
<nav class="site-nav">
  <img src="https://www.nuffieldhealth.com/assets/dist/images/logo_inverse.svg"
       alt="Nuffield Health" height="32">
</nav>

<!-- Page header -->
<div class="page-header">
  <div class="page-header__inner">
    <h1>Weekly Class Schedule Analysis</h1>
    <div class="page-header__meta">
      <span>Week: <strong>{week_label}</strong></span>
      <span>{gyms_with_data} gyms</span>
      <span>Updated: {updated}</span>
    </div>
    <p class="page-header__note">
      Weekday outside working hours = classes starting before 09:00 or from 18:00, Mon&ndash;Fri.
      Click any column header to sort. Times shown in UK local time (BST/GMT).
    </p>
  </div>
</div>

<!-- Table -->
<main class="main">
  <div class="table-wrap">
    <table id="report">
      <thead>
        <tr>
          <th class="gym-name sortable" data-col="name">Gym</th>
          <th class="num sortable total-col" data-col="total">Total<br>classes</th>
          <th class="num sortable offpeak-col" data-col="offpeak">Weekday outside<br>working hours</th>
          <th class="num sortable weekend-col" data-col="weekend">Weekend</th>
          {cat_th}
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
      <tfoot>
        {totals_row}
      </tfoot>
    </table>
  </div>
</main>

<footer>
  <p>
    Data sourced from
    <a href="https://www.nuffieldhealth.com/gyms" target="_blank" rel="noopener">nuffieldhealth.com</a>.
    Refreshed automatically every Monday via GitHub Actions.
    Not affiliated with or endorsed by Nuffield Health.
  </p>
</footer>

<script>
(function () {{
  const table   = document.getElementById("report");
  const tbody   = table.querySelector("tbody");
  const headers = [...table.querySelectorAll("th.sortable")];

  let sortCol = null, sortDir = 1;

  headers.forEach((th) => {{
    th.addEventListener("click", () => {{
      const col = th.dataset.col;
      if (sortCol === col) {{ sortDir *= -1; }}
      else {{ sortCol = col; sortDir = 1; }}

      headers.forEach(h => h.classList.remove("sort-asc", "sort-desc"));
      th.classList.add(sortDir === 1 ? "sort-asc" : "sort-desc");

      const idx  = headers.indexOf(th);
      const rows = [...tbody.querySelectorAll("tr")];

      rows.sort((a, b) => {{
        if (col === "name") {{
          const av = a.querySelector(".gym-name").textContent.trim().toLowerCase();
          const bv = b.querySelector(".gym-name").textContent.trim().toLowerCase();
          return sortDir * av.localeCompare(bv);
        }}
        const av = parseFloat(a.querySelectorAll("td")[idx].dataset.val) || 0;
        const bv = parseFloat(b.querySelectorAll("td")[idx].dataset.val) || 0;
        return sortDir * (bv - av);
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
