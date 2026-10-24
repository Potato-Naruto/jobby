"""Bridge to the please-hire-me project (read-only): your confirmed profile, preset answers, targets,
verified job boards, and the record of every application already sent.

Point PHM_DIR at the folder if it lives somewhere else. Nothing in that folder is ever modified.
"""
import json
import os
import re
from pathlib import Path

try:                                               # pick up .env even when search_jobs has not been imported yet
    for _l in (Path(__file__).parent / ".env").read_text(encoding="utf-8").splitlines():
        _k, _, _v = _l.partition("=")
        if _k.strip() and _v.strip():
            os.environ.setdefault(_k.strip(), _v.strip())
except OSError:
    pass

PHM_DIR = Path(os.getenv("PHM_DIR", r"C:\Users\Aravi\please-hire-me"))
JOBBY_APPLICATIONS = Path(__file__).parent / "applications"
JOBBY_LOG = Path(__file__).parent / "logs" / "applications-log.md"


def available() -> bool:
    return (PHM_DIR / "config" / "profile.json").exists()


def _json(rel: str) -> dict:
    try:
        return json.loads((PHM_DIR / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def profile() -> dict:
    """profile.json without its comment keys (those start with an underscore)."""
    p = {k: v for k, v in _json("config/profile.json").items() if not k.startswith("_")}
    if os.getenv("JOBBY_EMAIL"):                       # your override, set in .env, wins over please-hire-me's file
        p["email"] = os.getenv("JOBBY_EMAIL")
    return p


def settings() -> dict:
    return _json("config/settings.json")


def resume_path() -> Path | None:
    rel = profile().get("resume")
    p = (PHM_DIR / rel) if rel else None
    return p if p and p.exists() else None


# --- answers.md: preset legal / EEO / situational answers and the fact sheet ---------------------------
def section(heading: str) -> str:
    """Body of the '## <heading...>' block of answers.md (prefix match)."""
    md = _text(PHM_DIR / "config" / "answers.md")
    m = re.search(r"^## " + re.escape(heading) + r".*?\n(.*?)(?=^## |\Z)", md, re.S | re.M)
    return m.group(1).strip() if m else ""


def presets_text() -> str:
    """The rulebook for legal, demographic, consent and 'asked more than once' questions."""
    parts = [("PRESET LEGAL / EEO ANSWERS", "Preset legal"),
             ("CONSENT AND WILLINGNESS (default YES)", "Consent and willingness"),
             ("SITUATIONAL ANSWERS", "Situational answers"),
             ("THINGS TO LEAVE FOR THE HUMAN", "Things the agent must refuse")]
    return "\n\n".join(f"## {t}\n{section(h)}" for t, h in parts if section(h))


def facts_text() -> str:
    return section("Fact sheet")


def writing_style() -> str:
    return section("Writing style")


# --- Search targets -> Jobby search filters ------------------------------------------------------------
def search_filters() -> dict:
    t = settings().get("targets", {})
    titles = [r for r in t.get("roles", []) if len(r.split()) <= 4]   # drop the sentence-long catch-all roles
    locs = [re.sub(r"\s*\(.*?\)", "", x).strip() for x in t.get("locations", [])]
    places = [x for x in locs if x and not x.lower().startswith(("anywhere", "remote"))]
    remote = any(x.lower().startswith("remote") for x in locs)
    seniority = " ".join(t.get("seniority", [])).lower()
    levels = ["entry"] + (["intern"] if "intern" in seniority else [])
    return {"titles": titles or ["engineer"], "locations": places, "remote": "include" if remote else None,
            "min_salary": int(t.get("min_annual_comp_usd") or 0), "levels": levels}


# --- Verified company boards ---------------------------------------------------------------------------
def board_slugs() -> dict[str, list[str]]:
    """{'ashby': [...], 'greenhouse': [...], 'lever': [...]} from data/boards.md (only boards with US postings)."""
    md = _text(PHM_DIR / "data" / "boards.md")
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"^## (Ashby|Greenhouse|Lever)[^\n]*\n(.*?)(?=^## |\Z)", md, re.S | re.M):
        system = m.group(1).lower()
        for row in re.finditer(r"^\|\s*`([^`]+)`\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", m.group(2), re.M):
            if int(row.group(3)) > 0:
                out.setdefault(system, []).append(row.group(1))
    return out


# --- What has already been applied to ------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{6,}", re.I)
_URL = re.compile(r"https?://[^\s)>\]|`]+")


def _record_files() -> list[Path]:
    files = []
    for base in (PHM_DIR, JOBBY_LOG.parent.parent):
        files += [base / "logs" / "applications-log.md", base / "data" / "queue.md"]
        files += sorted((base / "applications").glob("*.md")) if (base / "applications").is_dir() else []
    return [f for f in files if f.exists()]


def applied() -> dict:
    """{'companies': {norm company: count}, 'ids': {job ids seen in a URL}} across both projects' records."""
    companies: dict[str, int] = {}
    ids: set[str] = set()
    for f in _record_files():
        text = _text(f)
        if f.parent.name == "applications":
            if re.search(r"Status:\*\*\s*(SUBMITTED|NEEDS HUMAN)", text, re.I):
                key = _norm(f.stem.split("-")[0])
                companies[key] = companies.get(key, 0) + 1
        elif f.name == "queue.md":
            for line in text.splitlines():
                if line.startswith("- [x]"):
                    key = _norm(line[5:].split("|")[0])
                    if key:
                        companies.setdefault(key, 0)
        for u in _URL.findall(text):
            ids.update(i.lower() for i in _ID.findall(u))
    return {"companies": companies, "ids": ids}


def company_count(company: str, record: dict | None = None) -> int:
    record = record or applied()
    key = _norm(company)
    if not key:
        return 0
    return max((n for c, n in record["companies"].items() if c and (c == key or c.startswith(key) or key.startswith(c))), default=0)


def already_applied(job: dict, record: dict | None = None) -> str | None:
    """A reason string when this exact posting was already sent, or the company is at the lifetime cap."""
    record = record or applied()
    if any(i.lower() in record["ids"] for i in _ID.findall(job.get("url", ""))):
        return "you already applied to this exact posting"
    cap = int(settings().get("run", {}).get("max_applications_per_company_lifetime") or 0)
    n = company_count(job.get("company", ""), record)
    if cap and n >= cap:
        return f"already {n} applications to {job.get('company')} (lifetime cap {cap})"
    return None


def log_application(company: str, role: str, url: str, status: str, note: str = "") -> None:
    """Append one entry to Jobby's own log (same '### ... STATUS' format please-hire-me uses)."""
    from datetime import datetime
    JOBBY_LOG.parent.mkdir(exist_ok=True)
    JOBBY_APPLICATIONS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with JOBBY_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"\n### [{stamp}] {company} — {role} — {status}\n- URL: {url}\n" + (f"- Note: {note}\n" if note else ""))
    doc = JOBBY_APPLICATIONS / f"{_norm(company) or 'company'}-{_norm(role)[:40] or 'role'}.md"
    if not doc.exists():
        doc.write_text(f"# {company} — {role}\n\n- **Status:** {status} {stamp}\n- **URL:** {url}\n" + (f"- **Note:** {note}\n" if note else ""),
                       encoding="utf-8")
