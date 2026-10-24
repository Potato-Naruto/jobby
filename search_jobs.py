"""Search job boards and return application URLs, strictly filtered.

Every result is checked locally against YOUR filters, so boards' fuzzy matching can't leak random jobs:
  * keywords  - every word of a keyword phrase must appear in the job title (or tags)
  * location  - city/state must match the job's location (remote jobs only if you allow them)
  * posted    - within the last N days
  * salary    - minimum annual salary (jobs with no listed salary kept/dropped by choice)

Sources: Adzuna, Jooble, USAJobs (keyed); The Muse, Remotive, RemoteOK, Jobicy, We Work Remotely,
Himalayas, plus Greenhouse/Lever career pages of companies in companies.json.
Adzuna keys: ADZUNA_APP_ID / ADZUNA_APP_KEY (env vars or a .env file next to this script),
optional ADZUNA_COUNTRY (default 'us'). Free signup: https://developer.adzuna.com

Usage:
    python search_jobs.py --titles "supply chain engineer" --locations "Plano, TX" --days 7 --min-salary 70000
    python search_jobs.py --profile my_search          # saved profile (search_profiles.json)
"""
import argparse
import os
import re
from datetime import datetime, timedelta, timezone

import requests

from common import BASE_DIR, JOBS_JSON, load_json, save_json

HEADERS = {"User-Agent": "jobby/1.0"}
PROFILES_JSON = BASE_DIR / "search_profiles.json"
REMOTE_WORDS = ("remote", "anywhere", "worldwide", "work from home")

STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california", "co": "colorado",
    "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas", "ky": "kentucky", "la": "louisiana",
    "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan", "mn": "minnesota",
    "ms": "mississippi", "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina",
    "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee", "tx": "texas",
    "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}
STATE_ABBR = {v: k for k, v in STATES.items()}


# --- Config -----------------------------------------------------------------
def _load_dotenv() -> None:
    env = BASE_DIR / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_load_dotenv()


def default_filters() -> dict:
    return {"titles": [], "locations": [], "remote": None,  # None = auto | include | only | exclude
            "days": 0, "min_salary": 0, "keep_unknown_salary": True, "exclude_words": [], "levels": [], "unclear_level": True, "new_only": True}


# --- Helpers ----------------------------------------------------------------
def _get(url: str, **params):
    r = requests.get(url, params=params or None, headers=HEADERS, timeout=25)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.json()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9+#.]+", text.lower())


def parse_date(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_salary(text: str) -> tuple[float | None, float | None]:
    """Best-effort (min, max) annual USD from free text like '$90k - $120k' or '80,000-100,000'."""
    if not text:
        return None, None
    nums = []
    for m in re.finditer(r"(\d[\d,]*\.?\d*)\s*(k)?", text.lower()):
        n = float(m.group(1).replace(",", "") or 0)
        if m.group(2):
            n *= 1000
        if n >= 20000:  # ignore hourly rates / years / stray digits
            nums.append(n)
    return (min(nums), max(nums)) if nums else (None, None)


NON_US = re.compile(r"\b(india|vietnam|singapore|china|japan|korea|taiwan|philippines|indonesia|thailand|malaysia|australia|"
                    r"new zealand|uk|united kingdom|england|ireland|germany|france|spain|italy|netherlands|poland|romania|"
                    r"portugal|sweden|norway|denmark|finland|switzerland|israel|turkey|brazil|mexico|argentina|colombia|"
                    r"canada|slovakia|czechia|czech republic|hungary|ukraine|serbia|bulgaria|croatia|greece|austria|belgium|lithuania|latvia|estonia|nigeria|kenya|egypt|pakistan|bangladesh|emea|apac|latam|europe|asia)\b", re.I)


def is_remote(job: dict) -> bool:
    return any(w in (job["location"] + " " + job["title"]).lower() for w in REMOTE_WORDS) or job.get("remote", False)


def location_matches(job_loc: str, wanted: str) -> bool:
    """'Plano, TX' matches 'Plano, Texas'; 'Texas' matches any Texas city; 'US' matches US-wide."""
    loc = job_loc.lower()
    parts = [p.strip() for p in re.split(r"[,/]", wanted.lower()) if p.strip()]
    if not parts:
        return True
    for p in parts:
        full, abbr = STATES.get(p, p), STATE_ABBR.get(p, p)
        if len(p) == 2 and p in STATES:  # state abbreviation: match as a whole word
            if re.search(rf"\b({p}|{STATES[p]})\b", loc):
                continue
            return False
        if p in loc or full in loc or (abbr != p and re.search(rf"\b{abbr}\b", loc)):
            continue
        return False
    return True


def remote_mode(f: dict) -> str:
    """Explicit setting wins. Auto: only 'remote' -> only remote; remote + places -> both; places only -> no remote."""
    if f["remote"]:
        return f["remote"]
    locs = [l.lower() for l in f["locations"]]
    if not locs:
        return "include"
    remote_asked = [l in REMOTE_WORDS for l in locs]
    return "only" if all(remote_asked) else "include" if any(remote_asked) else "exclude"


LEVELS = {"intern": "Internship / co-op (0 yrs)", "entry": "Entry level (0-1 yr)", "mid": "Mid level (2-4 yrs)",
          "senior": "Senior (5-7 yrs)", "lead": "Lead / Manager / Director (8+ yrs)"}
_LEVEL_WORDS = (
    ("intern", ("intern", "internship", "co-op", "coop", "apprentice")),
    ("lead", ("lead", "principal", "staff", "manager", "director", "head", "vp", "chief", "supervisor")),
    ("senior", ("senior", "sr", "iii", "iv", "expert")),
    ("entry", ("junior", "jr", "entry", "graduate", "grad", "associate", "trainee", "i", "level 1")),
)
_YEARS = re.compile(r"(\d{1,2})\s*(?:\+|-|–|to\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?)", re.I)
_ENTRY_HINT = re.compile(r"no experience (?:is )?(?:required|necessary)|entry[- ]level|new grad|recent graduate|0\s*-\s*1 years?", re.I)


def years_required(text: str) -> int | None:
    """Smallest 'N years' requirement stated near the word 'experience' (None when not stated)."""
    if not text:
        return None
    if _ENTRY_HINT.search(text):
        return 0
    found = []
    for m in _YEARS.finditer(text):
        ctx = text[max(0, m.start() - 80): m.end() + 80].lower()
        if "experience" in ctx:
            found.append(int(m.group(1)))
    return min(found) if found else None


def job_level(job_or_title) -> tuple[str, bool]:
    """(level, confident). Title signals win; otherwise the years stated in the description; else ('mid', False)."""
    title = job_or_title if isinstance(job_or_title, str) else job_or_title["title"]
    words = {w.strip(".") for w in _words(title)}
    low = title.lower()
    for level, markers in _LEVEL_WORDS:
        if any((m in words) or (len(m) > 3 and m in low) for m in markers):
            return level, True
    yrs = years_required("" if isinstance(job_or_title, str) else job_or_title.get("desc", ""))
    if yrs is not None:
        return ("entry" if yrs <= 1 else "mid" if yrs <= 4 else "senior" if yrs <= 7 else "lead"), True
    return "mid", False


def reject_reason(job: dict, f: dict, now: datetime) -> str | None:
    """Why a job fails the filters (None = it passes). Used for filtering and for the live search log."""
    title_blob = " ".join(_words(job["title"] + " " + " ".join(job.get("tags", []))))
    title_words = set(title_blob.split())
    # keywords: ANY phrase in the list may match, but ALL words of that phrase must be present
    if f["titles"] and not any(all(w in title_words for w in _words(t)) for t in f["titles"]):
        return "keywords not in title"
    if any(w.lower() in title_blob for w in f["exclude_words"]):
        return "excluded word in title"
    if f.get("levels"):
        level, sure = job_level(job)
        if sure and level not in f["levels"]:
            return "experience level"
        if not sure and not f.get("unclear_level", True):
            return "experience level not stated"
    remote = is_remote(job)
    mode = remote_mode(f)
    if mode == "exclude" and remote:
        return "remote job (excluded)"
    if mode == "only" and not remote:
        return "not remote"
    wanted = [l for l in f["locations"] if l.lower() not in REMOTE_WORDS]
    if remote and wanted and NON_US.search(job["location"]) and not re.search(r"united states|\busa?\b", job["location"], re.I):
        return "remote, but outside the US"
    vague = re.fullmatch(r"\d+ locations?", job["location"].strip().lower())      # e.g. Workday: "3 Locations"
    if wanted and not vague and not (remote and mode != "exclude") and not any(location_matches(job["location"], w) for w in wanted):
        return "wrong location"
    if f["days"]:
        posted = job.get("posted")
        if posted is None:
            return "no posted date"
        if posted < now - timedelta(days=f["days"]):
            return "posted too long ago"
    if f["min_salary"]:
        hi = job.get("salary_max") or job.get("salary_min")
        if hi is None:
            return None if f["keep_unknown_salary"] else "salary not listed"
        if hi < f["min_salary"]:
            return "salary below minimum"
    return None


def passes(job: dict, f: dict, now: datetime) -> bool:
    return reject_reason(job, f, now) is None


def _job(title, company, location, url, source, posted=None, smin=None, smax=None, tags=None, remote=False, desc=""):
    return {"title": (title or "").strip(), "company": company or "", "location": location or "", "url": url,
            "source": source, "posted": parse_date(posted), "salary_min": smin, "salary_max": smax,
            "tags": tags or [], "remote": remote, "desc": re.sub(r"<[^>]+>", " ", desc or "")[:2500]}


# --- Sources (each takes the filters + one title/location and returns raw jobs) -----------------
def search_adzuna(f, title, location):
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return []
    params = {"app_id": app_id, "app_key": app_key, "what_and": title, "results_per_page": 50,
              "sort_by": "date", "content-type": "application/json"}
    if location and location.lower() not in REMOTE_WORDS:
        params["where"] = location
    if f["days"]:
        params["max_days_old"] = f["days"]
    if f["min_salary"]:
        params["salary_min"] = f["min_salary"]
    out = []
    for page in (1, 2, 3):
        data = _get(f"https://api.adzuna.com/v1/api/jobs/{os.getenv('ADZUNA_COUNTRY', 'us')}/search/{page}", **params)
        for j in data.get("results", []):
            out.append(_job(j.get("title"), j.get("company", {}).get("display_name"),
                            j.get("location", {}).get("display_name"), j["redirect_url"], "adzuna",
                            j.get("created"), j.get("salary_min"), j.get("salary_max"), desc=j.get("description", "")))
        if len(data.get("results", [])) < 50:
            break
    return out


def search_muse(f, title, location):
    params = {"descending": "true"}
    if location and location.lower() not in REMOTE_WORDS:
        params["location"] = location
    out = []
    for page in range(1, 6):
        data = _get("https://www.themuse.com/api/public/jobs", page=page, **params)
        for j in data.get("results", []):
            locs = ", ".join(l["name"] for l in j.get("locations", []))
            out.append(_job(j["name"], j.get("company", {}).get("name"), locs, j["refs"]["landing_page"], "themuse",
                            j.get("publication_date"), tags=[c["name"] for c in j.get("categories", [])]))
        if page >= data.get("page_count", 1):
            break
    return out


def search_remotive(f, title, location):
    data = _get("https://remotive.com/api/remote-jobs", search=title, limit=100)
    out = []
    for j in data.get("jobs", []):
        lo, hi = parse_salary(j.get("salary", ""))
        out.append(_job(j["title"], j["company_name"], j.get("candidate_required_location", ""), j["url"],
                        "remotive", j.get("publication_date"), lo, hi, j.get("tags", []), remote=True, desc=j.get("description", "")))
    return out


def search_remoteok(f, title, location):
    out = []
    for j in _get("https://remoteok.com/api")[1:]:
        out.append(_job(j.get("position"), j.get("company"), j.get("location") or "Remote", j.get("url"),
                        "remoteok", j.get("date"), j.get("salary_min") or None, j.get("salary_max") or None,
                        j.get("tags", []), remote=True))
    return out


def search_arbeitnow(f, title, location):
    out = []
    for page in (1, 2, 3):
        for j in _get("https://www.arbeitnow.com/api/job-board-api", page=page).get("data", []):
            out.append(_job(j["title"], j.get("company_name"), j.get("location"), j["url"], "arbeitnow",
                            j.get("created_at"), tags=j.get("tags", []), remote=bool(j.get("remote"))))
    return out


def search_jobicy(f, title, location):
    data = _get("https://jobicy.com/api/v2/remote-jobs", count=50, tag=title)
    return [_job(j["jobTitle"], j.get("companyName"), j.get("jobGeo"), j["url"], "jobicy", j.get("pubDate"),
                 j.get("annualSalaryMin"), j.get("annualSalaryMax"), remote=True, desc=j.get("jobExcerpt", "")) for j in data.get("jobs", [])]


def _rfc_date(text):
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


def search_wwr(f, title, location):
    """We Work Remotely public RSS feed."""
    import xml.etree.ElementTree as ET
    r = requests.get("https://weworkremotely.com/remote-jobs.rss", headers=HEADERS, timeout=25)
    r.raise_for_status()
    out = []
    for item in ET.fromstring(r.content).iter("item"):
        full = item.findtext("title") or ""
        company, _, role = full.partition(": ")
        out.append(_job(role or full, company if role else "", item.findtext("region") or "Remote",
                        item.findtext("link"), "weworkremotely", _rfc_date(item.findtext("pubDate")), remote=True))
    return out


def search_himalayas(f, title, location):
    data = _get("https://himalayas.app/jobs/api", limit=100)
    out = []
    for j in data.get("jobs", []):
        locs = ", ".join(j.get("locationRestrictions") or []) or "Worldwide"
        out.append(_job(j.get("title"), j.get("companyName"), locs, j.get("applicationLink") or j.get("guid"),
                        "himalayas", j.get("pubDate"), j.get("minSalary"), j.get("maxSalary"),
                        j.get("categories", []), remote=True))
    return out


def search_jooble(f, title, location):
    key = os.getenv("JOOBLE_API_KEY")
    body = {"keywords": title, "location": "" if location.lower() in REMOTE_WORDS else location, "page": 1}
    if f["days"]:
        body["datecreatedfrom"] = (datetime.now(timezone.utc) - timedelta(days=f["days"])).strftime("%Y-%m-%d")
    if f["min_salary"]:
        body["salary"] = f["min_salary"]
    r = requests.post(f"https://jooble.org/api/{key}", json=body, headers=HEADERS, timeout=25)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        lo, hi = parse_salary(j.get("salary", ""))
        out.append(_job(j.get("title"), j.get("company"), j.get("location"), j.get("link"), "jooble",
                        j.get("updated"), lo, hi, desc=j.get("snippet", "")))
    return out


def search_usajobs(f, title, location):
    """US federal jobs (official API; needs USAJOBS_API_KEY and USAJOBS_EMAIL)."""
    params = {"Keyword": title, "ResultsPerPage": 50, "SortField": "OpenDate", "SortDirection": "Desc"}
    if location and location.lower() not in REMOTE_WORDS:
        params["LocationName"] = location
    if f["days"]:
        params["DatePosted"] = min(f["days"], 60)
    r = requests.get("https://data.usajobs.gov/api/search", params=params, timeout=25,
                     headers={"Host": "data.usajobs.gov", "User-Agent": os.getenv("USAJOBS_EMAIL"),
                              "Authorization-Key": os.getenv("USAJOBS_API_KEY")})
    r.raise_for_status()
    out = []
    for it in r.json()["SearchResult"]["SearchResultItems"]:
        d = it["MatchedObjectDescriptor"]
        pay = (d.get("PositionRemuneration") or [{}])[0]
        out.append(_job(d["PositionTitle"], d.get("OrganizationName"), d.get("PositionLocationDisplay"),
                        d["PositionURI"], "usajobs", d.get("PublicationStartDate"),
                        float(pay["MinimumRange"]) if pay.get("MinimumRange") else None,
                        float(pay["MaximumRange"]) if pay.get("MaximumRange") else None))
    return out


def search_workingnomads(f, title, location):
    out = []
    for j in _get("https://www.workingnomads.com/api/exposed_jobs/"):
        out.append(_job(j.get("title"), j.get("company_name"), j.get("location") or "Remote", j.get("url"),
                        "workingnomads", j.get("pub_date"), tags=(j.get("tags") or "").split(","), remote=True,
                        desc=j.get("description", "")))
    return out


_SENIORITY = {"junior": "Junior", "entry": "Entry", "senior": "Senior", "lead": "Lead", "manager": "Manager"}


def search_jobscollider(f, title, location):
    """Remote First Jobs / JobsCollider API (links point back to remotefirstjobs.com as their terms ask)."""
    out = []
    for page in (0, 1):
        data = _get("https://jobscollider.com/api/search-jobs", title=title, page=page)
        for j in data.get("jobs", []):
            lvl = _SENIORITY.get(str(j.get("seniority", "")).lower(), "")
            locs = j.get("locations")
            out.append(_job(f"{lvl} {j.get('title')}".strip() if lvl and lvl.lower() not in str(j.get("title", "")).lower() else j.get("title"),
                            j.get("company_name"), ", ".join(locs) if isinstance(locs, list) else str(locs or "Remote"),
                            j.get("url"), "remotefirstjobs", j.get("published_at"),
                            float(j["salary_min"]) if j.get("salary_min") else None,
                            float(j["salary_max"]) if j.get("salary_max") else None, remote=True, desc=j.get("description", "")))
        if len(data.get("jobs", [])) < 100:
            break
    return out


def _posted_text_to_date(text: str):
    t = (text or "").lower()
    now = datetime.now(timezone.utc)
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*days?", t)
    return now - timedelta(days=int(m.group(1))) if m else None


def _workday_detail_location(base, tenant, site, path):
    try:
        d = _get(f"{base}/wday/cxs/{tenant}/{site}{path}").get("jobPostingInfo", {})
        return "; ".join(x for x in [d.get("location"), *(d.get("additionalLocations") or [])] if x)
    except (requests.RequestException, ValueError, KeyError):
        return ""


def _workday_jobs(args):
    (tenant, wd, site), title, f = args
    out = []
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    try:
        for offset in (0, 20, 40):
            r = requests.post(f"{base}/wday/cxs/{tenant}/{site}/jobs", timeout=25, headers=HEADERS,
                              json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": title})
            r.raise_for_status()
            posts = r.json().get("jobPostings", [])
            for j in posts:
                out.append(_job(j.get("title"), tenant.upper() if len(tenant) <= 4 else tenant.title(),
                                j.get("locationsText"), f"{base}/en-US/{site}{j.get('externalPath', '')}",
                                f"workday:{tenant}", _posted_text_to_date(j.get("postedOn", ""))))
            if len(posts) < 20:
                break
    except (requests.RequestException, ValueError, KeyError):
        pass
    # "3 Locations" tells us nothing about where the job is: look up the real places, but only for jobs whose
    # title, level and age already fit (keeps the number of extra requests small)
    now = datetime.now(timezone.utc)
    pending = [j for j in out if re.fullmatch(r"\d+ locations?", j["location"].strip().lower())
               and reject_reason({**j, "location": "x"}, {**f, "locations": [], "new_only": False}, now) is None][:25]
    if pending:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(6) as ex:
            places = list(ex.map(lambda j: _workday_detail_location(base, tenant, site, j["url"].split(f"/en-US/{site}", 1)[-1]), pending))
        for j, place in zip(pending, places):
            if place:
                j["location"] = place
    return out


def search_workday(f, title, location):
    """Big employers' own Workday career sites (server-side keyword search), fetched in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    tenants = load_catalog()["companies"].get("workday", [])
    with ThreadPoolExecutor(12) as ex:
        return [j for jobs in ex.map(_workday_jobs, [(t, title, f) for t in tenants]) for j in jobs]


def _smart_jobs(args):
    company, title = args
    out = []
    try:
        data = _get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings", q=title, limit=100)
        for j in data.get("content", []):
            loc = j.get("location", {})
            place = ", ".join(x for x in (loc.get("city"), loc.get("region", "").upper(), loc.get("country", "").upper()) if x)
            out.append(_job(j.get("name"), j.get("company", {}).get("name", company), place,
                            f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}", f"smartrecruiters:{company}",
                            j.get("releasedDate"), remote=bool(loc.get("remote"))))
    except (requests.RequestException, ValueError, KeyError, AttributeError):
        pass
    return out


def search_smartrecruiters(f, title, location):
    from concurrent.futures import ThreadPoolExecutor
    cos = load_catalog()["companies"].get("smartrecruiters", [])
    with ThreadPoolExecutor(8) as ex:
        return [j for jobs in ex.map(_smart_jobs, [(c, title) for c in cos]) for j in jobs]


COMPANIES_JSON = BASE_DIR / "companies.json"
CATALOG_JSON = BASE_DIR / "sources_catalog.json"


def load_catalog() -> dict:
    return load_json(CATALOG_JSON, {"companies": {}, "boards": {}})


def load_companies() -> dict:
    """Verified default company career pages plus your own additions in companies.json."""
    base, mine = load_catalog()["companies"], load_json(COMPANIES_JSON, {})
    return {k: sorted(set(base.get(k, [])) | set(mine.get(k, []))) for k in ("greenhouse", "lever", "ashby", "workable", "recruitee")}


def _company_jobs(system: str, token: str) -> list[dict]:
    out = []
    try:
        if system == "greenhouse":
            for j in _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs").get("jobs", []):
                out.append(_job(j["title"], token.title(), j.get("location", {}).get("name"), j["absolute_url"],
                                f"greenhouse:{token}", j.get("updated_at")))
        elif system == "lever":
            for j in _get(f"https://api.lever.co/v0/postings/{token}", mode="json"):
                out.append(_job(j["text"], token.title(), j.get("categories", {}).get("location"), j["hostedUrl"],
                                f"lever:{token}", (j.get("createdAt") or 0) / 1000 or None,
                                remote=j.get("workplaceType") == "remote"))
        elif system == "workable":
            for j in _get(f"https://apply.workable.com/api/v1/widget/accounts/{token}").get("jobs", []):
                out.append(_job(j["title"], token.title(), ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x),
                                j.get("url") or j.get("shortlink"), f"workable:{token}", j.get("published_on"),
                                remote=bool(j.get("telecommuting"))))
        elif system == "recruitee":
            for j in _get(f"https://{token}.recruitee.com/api/offers/").get("offers", []):
                out.append(_job(j["title"], token.title(), j.get("location"), j.get("careers_url"), f"recruitee:{token}",
                                j.get("published_at"), remote=bool(j.get("remote")), desc=j.get("description", "")))
        else:  # ashby
            for j in _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}").get("jobs", []):
                out.append(_job(j["title"], token.title(), j.get("location"), j.get("jobUrl"), f"ashby:{token}",
                                j.get("publishedAt"), remote=str(j.get("workplaceType")).lower() == "remote"))
    except (requests.RequestException, ValueError, KeyError, TypeError):
        pass
    return out


def search_company_boards(f, title, location):
    """Career pages of ~100 companies (Greenhouse, Lever, Ashby public APIs), fetched in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    tasks = [(sys_, t) for sys_, toks in load_companies().items() for t in toks]
    with ThreadPoolExecutor(16) as ex:
        return [j for jobs in ex.map(lambda a: _company_jobs(*a), tasks) for j in jobs]


# name, function, takes the keyword in the request, takes the location in the request, env vars needed
# (Arbeitnow is Europe-only so it is left out; LinkedIn/Indeed/Glassdoor have no public API and forbid scraping.)
SOURCES = [
    ("Adzuna", search_adzuna, True, True, ("ADZUNA_APP_ID", "ADZUNA_APP_KEY")),
    ("Jooble", search_jooble, True, True, ("JOOBLE_API_KEY",)),
    ("USAJobs", search_usajobs, True, True, ("USAJOBS_API_KEY", "USAJOBS_EMAIL")),
    ("The Muse", search_muse, False, True, ()),
    ("Remotive", search_remotive, True, False, ()),
    ("RemoteOK", search_remoteok, False, False, ()),
    ("Jobicy", search_jobicy, True, False, ()),
    ("We Work Remotely", search_wwr, False, False, ()),
    ("Himalayas", search_himalayas, False, False, ()),
    ("Workday employers", search_workday, True, False, ()),
    ("SmartRecruiters", search_smartrecruiters, True, False, ()),
    ("Working Nomads", search_workingnomads, False, False, ()),
    ("Remote First Jobs", search_jobscollider, True, False, ()),
    ("Company boards", search_company_boards, False, False, ()),
]


# --- Memory of jobs already shown, so repeat searches only surface NEW postings ----------------
SEEN_JSON = BASE_DIR / "seen_jobs.json"
SESSION_ID = datetime.now().strftime("%Y%m%d%H%M%S%f")   # jobs first shown in THIS run stay visible while you refine


def job_keys(job: dict) -> list[str]:
    """Stable identities: the URL without tracking params, and company|title|location (same job on two boards)."""
    from urllib.parse import urlsplit
    u = urlsplit(job["url"])
    keep = f"?{u.query}" if "gh_jid" in u.query else ""
    keys = [f"{u.netloc.lower()}{u.path.rstrip('/').lower()}{keep}"]
    keys.append("|".join(re.sub(r"[^a-z0-9]+", " ", x.lower()).strip() for x in (job["company"], job["title"], job["location"])))
    return keys


def load_seen() -> dict:
    return load_json(SEEN_JSON, {})


def reset_seen() -> None:
    save_json(SEEN_JSON, {})


# --- Aggregation ------------------------------------------------------------
def search_jobs(filters: dict, log=print) -> list[dict]:
    """Query every source, keep only jobs that satisfy ALL filters, and narrate each step through `log`.

    Log line prefixes: '>' action, '+' kept, '-' dropped, '!' warning, 'i' info/skip, ' ' plain.
    """
    from collections import Counter
    f = {**default_filters(), **filters}
    now = datetime.now(timezone.utc)
    pairs = [(t, l) for t in f["titles"] for l in (f["locations"] or [""])]
    log(f"i Looking for: {' OR '.join(repr(t) for t in f['titles'])}  (every word of a phrase must be in the title)")
    log(f"i Places: {', '.join(f['locations']) or 'anywhere'}  |  remote jobs: {remote_mode(f)}")
    sal = "$%s+" % format(f["min_salary"], ",") if f["min_salary"] else "any"
    log(f"i Posted: {'last %d day(s)' % f['days'] if f['days'] else 'any time'}  |  salary: {sal}")
    seen, results, cache = set(), [], {}
    history = load_seen()
    import phm
    record = phm.applied() if phm.available() else None
    if record:
        log(f"i please-hire-me linked: skipping postings you already applied to ({len(record['ids'])} known) and companies at the lifetime cap")
    if f.get("new_only", True):
        log(f"i Only NEW jobs: skipping the {len(history)} postings you have already been shown")
    def fetch(entry):
        """Fetch one source for every keyword/place pair (runs in a worker thread)."""
        name, fn, uses_q, uses_loc, envs = entry
        raw, used = [], set()
        for title, loc in pairs:
            key = (name, title if uses_q else "", loc if uses_loc else "")
            if key in used:
                continue
            used.add(key)
            what = f"'{title}'" if uses_q else "latest listings"
            log(f"> {name}: fetching {what}" + (f" near {loc}" if uses_loc and loc else ""))
            try:
                raw += fn(f, title, loc)
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                log(f"! {name}: failed - {str(exc)[:90]}")
        return raw

    active = []
    for entry in SOURCES:
        missing = [e for e in entry[4] if not os.getenv(e)]
        if missing:
            log(f"i {entry[0]}: skipped (needs {', '.join(missing)} - add in Settings)")
        else:
            active.append(entry)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(min(8, max(1, len(active)))) as pool:
        fetched = list(pool.map(fetch, active))             # sources are queried in parallel
    for (name, fn, uses_q, uses_loc, envs), raw in zip(active, fetched):
        if not raw:
            log(f"  {name}: nothing returned")
            continue
        reasons, kept = Counter(), []
        for job in raw:
            if not job["url"] or job["url"] in seen:
                reasons["duplicate"] += 1
                continue
            why = reject_reason(job, f, now)
            if not why and record:
                why = phm.already_applied(job, record)
            if not why and f.get("new_only", True):
                hit = [history[k] for k in job_keys(job) if k in history]
                if hit and any(h.get("session") != SESSION_ID for h in hit):
                    why = "already seen in an earlier search"
            if why:
                reasons[why] += 1
            else:
                seen.add(job["url"])
                results.append(job)
                kept.append(job)
        log(f"  {name}: {len(raw)} fetched")
        for why, n in reasons.most_common():
            log(f"- {n} dropped: {why}")
        for job in kept[:3]:
            log(f"+ kept: {job['title'][:50]} @ {job['company'][:22]}")
        if len(kept) > 3:
            log(f"+ ...and {len(kept) - 3} more from {name}")
        if not kept:
            log(f"  {name}: 0 matched")
    results.sort(key=lambda j: j["posted"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    stamp = datetime.now(timezone.utc).isoformat()
    for job in results:                                    # remember what was shown
        for k in job_keys(job):
            history.setdefault(k, {"first": stamp, "session": SESSION_ID})
    save_json(SEEN_JSON, history)
    log(f"i Done: {len(results)} job(s) match every filter")
    return results


# --- Boards without a public API: build their own pre-filled search pages (you open them yourself) ---------
CORE_LINK_BOARDS = ("Indeed", "ZipRecruiter", "Glassdoor", "LinkedIn")


def link_templates() -> dict[str, str]:
    """name -> URL template ({q} keyword, {l} place). Core four + the verified catalog (sources_catalog.json)."""
    core = {
        "Indeed": "https://www.indeed.com/jobs?q={q}&l={l}{indeed_age}{indeed_extra}",
        "ZipRecruiter": "https://www.ziprecruiter.com/jobs-search?search={q}&location={l}{zip_extra}",
        "Glassdoor": "https://www.glassdoor.com/Job/jobs.htm?sc.keyword={q}&locKeyword={l}{glass_age}",
        "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords={q}&location={l}{li_age}{li_extra}",
    }
    return {**core, **load_catalog()["boards"]}


LINK_BOARDS = tuple(link_templates())


def board_links(f: dict, only: list[str] | None = None) -> list[tuple[str, str, str]]:
    """(board, 'keyword @ place', url) for each board x keyword x place, with your filters applied where the site supports it."""
    from urllib.parse import quote_plus as q
    days, sal = f.get("days") or 0, f.get("min_salary") or 0
    out = []
    for name, tpl in link_templates().items():
        if only is not None and name not in only:
            continue
        for title in f["titles"]:
            for loc in (f["locations"] or [""]):
                remote_only = loc.lower() in REMOTE_WORDS
                place, kw = ("", f"{title} remote") if remote_only else (loc, title)
                fromage = next((d for d in (1, 3, 7, 14) if days and days <= d), 14 if days else 0)
                lv = f.get("levels") or []
                li_lv = ",".join(sorted({"intern": "1", "entry": "2", "mid": "3,4", "senior": "4", "lead": "5,6"}[x] for x in lv if x in LEVELS))
                indeed_lv = {"intern": "internship", "entry": "entry_level", "mid": "mid_level", "senior": "senior_level"}.get(lv[0]) if len(lv) == 1 else None
                url = tpl.format(
                    q=q(kw + (f" ${sal:,}" if sal and name == "Indeed" else "")), l=q(place),
                    indeed_age=f"&fromage={fromage}" if fromage else "",
                    zip_extra=(f"&days={min(days, 30)}" if days else "") + (f"&refine_by_salary={sal}" if sal else ""),
                    glass_age=f"&fromAge={days}" if days else "",
                    li_age=f"&f_TPR=r{days * 86400}" if days else "",
                    li_extra=(f"&f_E={li_lv.replace(',', '%2C')}" if li_lv else "") + ("&f_WT=2" if remote_only else ""),
                    indeed_extra=f"&explvl={indeed_lv}" if indeed_lv else "")
                out.append((name, f"{title} @ {loc or 'anywhere'}", url))
    return out


# --- Saved search profiles --------------------------------------------------
def load_profiles() -> dict:
    return load_json(PROFILES_JSON, {})


def save_profile(name: str, filters: dict) -> None:
    profiles = load_profiles()
    profiles[name] = filters
    save_json(PROFILES_JSON, profiles)


def format_job(j: dict) -> str:
    age = ""
    if j["posted"]:
        d = (datetime.now(timezone.utc) - j["posted"]).days
        age = " | today" if d <= 0 else f" | {d}d ago"
    sal = ""
    if j["salary_min"] or j["salary_max"]:
        lo, hi = j["salary_min"], j["salary_max"]
        sal = f" | ${int(lo):,}" + (f"-${int(hi):,}" if hi and hi != lo else "")
    return f"{j['title']} @ {j['company']} ({(j['location'] or 'n/a')[:70]}){age}{sal} [{j['source']}]\n  {j['url']}"


def save_jobs(jobs: list[dict]) -> None:
    save_json(JOBS_JSON, [{**j, "posted": j["posted"].isoformat() if j["posted"] else None} for j in jobs])


def run_and_save(filters: dict) -> list[dict]:
    print(f"\nSearching: {filters['titles']} in {filters['locations'] or 'anywhere'} "
          f"| posted<={filters['days'] or 'any'}d | min salary {filters['min_salary'] or 'any'} "
          f"| remote={remote_mode({**default_filters(), **filters})}")
    jobs = search_jobs(filters)
    for j in jobs:
        print(format_job(j))
    save_json(JOBS_JSON, [{**j, "posted": j["posted"].isoformat() if j["posted"] else None} for j in jobs])
    print(f"\n{len(jobs)} jobs saved to {JOBS_JSON}")
    return jobs


def main(argv=None) -> list[dict]:
    p = argparse.ArgumentParser(description="Search job boards with strict filters.")
    p.add_argument("--profile", help="Name of a saved search profile")
    p.add_argument("--titles", nargs="+", help="Keyword phrases (a job matches if any phrase's words are all in its title)")
    p.add_argument("--locations", nargs="*", help="Locations, e.g. 'Plano, TX' Texas remote")
    p.add_argument("--remote", choices=["include", "only", "exclude"])
    p.add_argument("--days", type=int, help="Posted within the last N days")
    p.add_argument("--min-salary", type=int, help="Minimum annual salary (USD)")
    p.add_argument("--drop-unknown-salary", action="store_true", help="Drop jobs that list no salary")
    p.add_argument("--exclude", nargs="*", help="Words that must NOT appear in the title")
    p.add_argument("--levels", nargs="*", choices=list(LEVELS), help="Experience levels to keep")
    p.add_argument("--all", action="store_true", help="Include jobs you were already shown in earlier searches")
    p.add_argument("--reset-seen", action="store_true", help="Forget which jobs you have already seen")
    p.add_argument("--save-as", help="Save these filters as a named profile")
    a = p.parse_args(argv)

    f = default_filters()
    if a.profile:
        saved = load_profiles().get(a.profile)
        if not saved:
            raise SystemExit(f"No saved profile {a.profile!r}. Saved: {list(load_profiles())}")
        f.update(saved)
    for key, val in (("titles", a.titles), ("locations", a.locations), ("remote", a.remote), ("days", a.days),
                     ("min_salary", a.min_salary), ("exclude_words", a.exclude), ("levels", a.levels)):
        if val is not None:
            f[key] = val
    if a.reset_seen:
        reset_seen()
        print("Seen-jobs history cleared.")
    if a.all:
        f["new_only"] = False
    if a.drop_unknown_salary:
        f["keep_unknown_salary"] = False
    if not f["titles"]:
        raise SystemExit("Give --titles or --profile.")
    if a.save_as:
        save_profile(a.save_as, f)
        print(f"Saved profile {a.save_as!r}")
    return run_and_save(f)


if __name__ == "__main__":
    main()
