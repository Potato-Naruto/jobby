"""LinkedIn outreach drafts: sort a hiring manager / recruiter into one of 4 archetypes and write a custom
connection note + follow-up in your voice, using only real facts from your profile.

Nothing is sent from here. You paste the note into LinkedIn yourself (automating LinkedIn breaks its rules and
gets accounts restricted). This module classifies, drafts, checks the draft against your facts, and keeps a
tracker (outreach.json) so you never message the same person twice and know when to follow up.
"""
import re
import uuid
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import phm
from common import BASE_DIR, PROFILE_JSON, ask_llm_json, load_json, save_json

OUTREACH_JSON = BASE_DIR / "outreach.json"
NOTE_LIMIT = 280          # LinkedIn caps a connection note at 300 characters; stay under it
FOLLOWUP_LIMIT = 650
FOLLOWUP_DAYS = 7
WEEKLY_SOFT_LIMIT = 40    # LinkedIn throttles invites; a modest pace keeps the account safe

ARCHETYPES = {
    "builder": {
        "name": "The Builder",
        "who": "Hands-on engineering / operations / plant / quality manager who runs a team doing the actual work",
        "tone": "specific and technical, peer to peer; one concrete result close to their area",
        "titles": r"engineering manager|manager,? (engineering|manufacturing|process|operations|quality|production|supply)|"
                  r"(engineering|manufacturing|process|operations|quality|production|plant|supply chain|supplier) (manager|lead|supervisor)|"
                  r"team lead|principal engineer|staff engineer|senior engineer|superintendent|supervisor",
    },
    "leader": {
        "name": "The Executive",
        "who": "Director, VP, head of, or C-level who owns outcomes and has very little time",
        "tone": "two short sentences, respectful, one headline result, no ask beyond connecting",
        "titles": r"director|vice president|\bvp\b|head of|chief|\bcoo\b|\bcto\b|president|general manager|site leader|managing",
    },
    "recruiter": {
        "name": "The Gatekeeper",
        "who": "Recruiter, talent partner, early-careers or university recruiting",
        "tone": "clear and practical: target role, location, availability; make it easy to route you",
        "titles": r"recruit|talent|sourcer|sourcing|early careers?|university|campus|people partner|hr\b|human resources|staffing",
    },
    "alum": {
        "name": "The Familiar Face",
        "who": "Someone you share a real background with: same school, fraternity or org, past employer, hometown",
        "tone": "warm and short, lead with the shared thing, then a light question about their path",
        "titles": "",
    },
}


def _profile_text() -> str:
    return (phm.facts_text() or "") + " " + str(load_json(PROFILE_JSON, {}))


def shared_ground() -> list[str]:
    """Real affiliations you could genuinely share with someone (schools, orgs, employers)."""
    text = _profile_text().lower()
    cands = ["University of Texas at Austin", "UT Austin", "Theta Tau", "Hindu Students Association", "PepsiCo",
             "Delek", "Chick-fil-A", "Plano", "Frisco", "Liberty High School", "Texas", "Cockrell"]
    return [c for c in cands if c.lower() in text]


def classify(contact: dict, use_ai: bool = True) -> tuple[str, str]:
    """(archetype key, why). Shared background first, then the title; the local model only breaks ties."""
    blob = " ".join(str(contact.get(k, "")) for k in ("title", "notes", "school")).lower()
    for g in shared_ground():
        if len(g) > 4 and g.lower() in blob and g.lower() not in ("texas",):
            return "alum", f"you share: {g}"
    title = (contact.get("title") or "").lower()
    for key in ("recruiter", "leader", "builder"):
        if re.search(ARCHETYPES[key]["titles"], title):
            return key, f"title '{contact.get('title')}'"
    if not use_ai:
        return "builder", "default (no clear title)"
    try:
        opts = {k: v["who"] for k, v in ARCHETYPES.items()}
        r = ask_llm_json("Pick the best archetype for this LinkedIn contact. Reply JSON {\"archetype\": \"<key>\"}.",
                         f"ARCHETYPES: {opts}\nCONTACT: {contact}")
        if r.get("archetype") in ARCHETYPES:
            return r["archetype"], "judged by the local AI"
    except Exception:
        pass
    return "builder", "default (no clear title)"


# --- Drafting ----------------------------------------------------------------------------------------
RULES = (
    "You write LinkedIn outreach for a new graduate. Rules: first person, plain words, no em dashes, no buzzwords, no "
    "flattery, no exclamation marks. Use ONLY facts from FACTS; never invent a number, title, project or shared "
    "connection. Do not ask for a job in the connection note. Name the role or team only if given. "
    f"connection_note must be at most {NOTE_LIMIT} characters. follow_up (sent after they accept) must be at most "
    f"{FOLLOWUP_LIMIT} characters, mention one relevant fact, and ask for 15 minutes to chat or share a couple of ideas (never ask for a job). "
    'Reply as JSON: {"connection_note": "...", "follow_up": "..."}.'
)


def _first_name(contact: dict) -> str:
    return (contact.get("name") or "").split()[0] if contact.get("name") else "there"


def _numbers_ok(text: str, facts: str) -> bool:
    """Every number in the draft must appear in the fact sheet (a near-miss figure is a fabrication)."""
    return all(n in facts for n in re.findall(r"\d[\d,.]*%?", text) if len(n.rstrip(".,")) > 1)


def _trim(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "))
    return cut[:end + 1] if end > limit * 0.5 else cut.rsplit(" ", 1)[0].rstrip(",;:") + "."


def template_draft(contact: dict, archetype: str) -> dict:
    """No-AI fallback: honest templates that use only the basics."""
    n, co, role = _first_name(contact), contact.get("company") or "your company", contact.get("role") or ""
    about = f" the {role} role" if role else " early-career engineering roles"
    school = "UT Austin chemical engineering graduate (May 2026)"
    note = {
        "builder": f"Hi {n}, I'm a {school} with process and supply chain internship experience. I'm interested in{about} at {co} and would value connecting with someone on the team.",
        "leader": f"Hi {n}, I'm a {school} interested in {co}. I'd be glad to connect and follow the team's work.",
        "recruiter": f"Hi {n}, I'm a {school} based in Texas, available from October 31, 2026, and applying to{about} at {co}. I'd appreciate connecting.",
        "alum": f"Hi {n}, I'm a fellow {contact.get('shared') or 'Longhorn'} ({school}). I'd love to connect and hear how you found your path into {co}.",
    }[archetype]
    follow = (f"Thanks for connecting, {n}. I applied to{about} at {co} and wanted to put a name to the application. "
              f"Is there anything about the team or the role I should know before the interview stage?")
    return {"connection_note": _trim(note, NOTE_LIMIT), "follow_up": _trim(follow, FOLLOWUP_LIMIT)}


def draft(contact: dict, archetype: str) -> tuple[dict, str]:
    """(drafts, how) where how says whether the AI wrote it or the template did."""
    facts = phm.facts_text() or str(load_json(PROFILE_JSON, {}))
    arch = ARCHETYPES[archetype]
    prompt = (f"ARCHETYPE: {arch['name']} - {arch['who']}\nTONE: {arch['tone']}\n"
              f"CONTACT: name={contact.get('name')}, title={contact.get('title')}, company={contact.get('company')}, "
              f"role I applied/am applying for={contact.get('role') or 'not specified'}\n"
              f"THEIR PROFILE NOTES (may mention shared ground): {contact.get('notes') or 'none'}\n"
              f"SHARED GROUND I REALLY HAVE: {', '.join(shared_ground()) or 'none'}\n\nFACTS:\n{facts}")
    for attempt in range(2):
        try:
            out = ask_llm_json(RULES + (" Your last draft broke a rule (too long or an invented number). Be shorter and stricter." if attempt else ""),
                               prompt)
            note, follow = str(out.get("connection_note", "")).strip(), str(out.get("follow_up", "")).strip()
            note = note.replace("—", ",").replace("–", "-")
            follow = follow.replace("—", ",").replace("–", "-")
            if note and len(note) <= NOTE_LIMIT and len(follow) <= FOLLOWUP_LIMIT and _numbers_ok(note + " " + follow, facts):
                return {"connection_note": note, "follow_up": follow}, "written by the local AI, numbers checked against your facts"
        except Exception:
            break
    return template_draft(contact, archetype), "template (the AI draft was unavailable or broke a rule)"


# --- Tracker -----------------------------------------------------------------------------------------
def load_all() -> list[dict]:
    return load_json(OUTREACH_JSON, []) or []


def save_all(items: list[dict]) -> None:
    save_json(OUTREACH_JSON, items)


def norm_url(url: str) -> str:
    return re.sub(r"[?#].*$", "", (url or "").strip().lower()).rstrip("/")


def find_existing(url: str, items: list[dict] | None = None) -> dict | None:
    key = norm_url(url)
    return next((i for i in (items or load_all()) if key and norm_url(i.get("url", "")) == key), None)


def new_contact(name: str, title: str, company: str, url: str, role: str = "", notes: str = "") -> dict:
    c = {"id": uuid.uuid4().hex[:8], "name": name.strip(), "title": title.strip(), "company": company.strip(), "url": url.strip(),
         "role": role.strip(), "notes": notes.strip(), "status": "drafted",
         "created": datetime.now().isoformat(timespec="minutes")}
    c["archetype"], c["why"] = classify(c)
    return c


def mark(contact: dict, status: str) -> None:
    contact["status"] = status
    now = datetime.now()
    if status == "sent":
        contact["sent"] = now.isoformat(timespec="minutes")
        contact["followup_due"] = (now + timedelta(days=FOLLOWUP_DAYS)).date().isoformat()
    if status in ("replied", "skipped", "followed_up"):
        contact.pop("followup_due", None)


def due_followups(items: list[dict] | None = None) -> list[dict]:
    today = datetime.now().date().isoformat()
    return [i for i in (items or load_all()) if i.get("status") in ("sent", "accepted") and i.get("followup_due", "9") <= today]


def sent_this_week(items: list[dict] | None = None) -> int:
    since = (datetime.now() - timedelta(days=7)).isoformat()
    return sum(1 for i in (items or load_all()) if i.get("sent", "") >= since)


def people_search_url(company: str, role: str = "", kind: str = "hiring") -> str:
    """A LinkedIn people-search page you open yourself (2nd and 3rd connections first)."""
    words = {"hiring": "hiring manager", "recruiter": "university recruiter", "team": "manufacturing manager"}.get(kind, kind)
    kw = " ".join(x for x in (company, role.split(",")[0] if role else "", words) if x)
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(kw)}&origin=GLOBAL_SEARCH_HEADER"


def copy_to_clipboard(text: str) -> bool:
    import subprocess
    try:
        subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# --- Web search for hiring managers ------------------------------------------------------------------
HIRING_TITLES = (r"hiring manager|manager|director|head of|lead|supervisor|vp\b|vice president|recruit|talent|"
                 r"sourcer|early careers?|university")
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def _search_queries(company: str, role: str) -> list[str]:
    """Several phrasings; search engines index few LinkedIn profiles, so one query is rarely enough."""
    field = " ".join(re.sub(r"[^\w\s]", " ", role.split(",")[0]).split()[:4]) if role else ""
    return [f'site:linkedin.com/in "{company}" "hiring manager" {field}'.strip(),
            f'site:linkedin.com/in "{company}" manager {field}'.strip(),
            f'site:linkedin.com/in "{company}" recruiter OR "talent acquisition"']


def _search_serper(q: str, key: str) -> list[dict]:
    import requests
    r = requests.post("https://google.serper.dev/search", json={"q": q, "num": 10},
                      headers={"X-API-KEY": key, "Content-Type": "application/json"}, timeout=20)
    r.raise_for_status()
    return [{"url": o.get("link", ""), "title": o.get("title", ""), "snippet": o.get("snippet", "")}
            for o in r.json().get("organic", [])]


def _search_brave(q: str, key: str) -> list[dict]:
    import requests
    r = requests.get("https://api.search.brave.com/res/v1/web/search", params={"q": q, "count": 20},
                     headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=20)
    r.raise_for_status()
    return [{"url": o.get("url", ""), "title": o.get("title", ""), "snippet": o.get("description", "")}
            for o in r.json().get("web", {}).get("results", [])]


def _search_bing_browser(queries: list[str]) -> list[dict]:
    """Keyless fallback: drive real Chrome (headless) at Bing. Hit-or-miss, since Bing indexes few profiles."""
    import base64
    from urllib.parse import parse_qs, quote, urlparse
    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=_UA)
        for q in queries:
            try:
                page.goto("https://www.bing.com/search?q=" + quote(q), timeout=30000)
                page.wait_for_timeout(1200)
                for a in page.query_selector_all("li.b_algo"):
                    h, s = a.query_selector("h2 a"), a.query_selector("p")
                    if not h:
                        continue
                    url = parse_qs(urlparse(h.get_attribute("href") or "").query).get("u", [""])[0]
                    if url.startswith("a1"):  # Bing wraps result links in base64
                        url = base64.urlsafe_b64decode(url[2:] + "==").decode(errors="ignore")
                    out.append({"url": url, "title": h.inner_text(), "snippet": s.inner_text() if s else ""})
            except Exception:
                continue
        browser.close()
    return out


def _parse_profile(hit: dict) -> dict | None:
    """Turn a search hit into {name, title, url} if it is a LinkedIn profile ('Name - Title - Company | LinkedIn')."""
    m = re.match(r"https?://(?:[\w-]+\.)?linkedin\.com/in/([^/?#]+)", hit.get("url", ""))
    if not m:
        return None
    head = re.sub(r"\s*[|\-–]\s*LinkedIn.*$", "", hit.get("title", "")).strip()
    parts = [p.strip() for p in re.split(r"\s+[-–|]\s+", head) if p.strip()]
    name = parts[0] if parts else m.group(1).replace("-", " ").title()
    return {"name": name, "title": " - ".join(parts[1:]) or "", "url": f"https://www.linkedin.com/in/{m.group(1)}",
            "snippet": hit.get("snippet", "")}


def _parallel(fn, items) -> list:
    """Run fn over items in threads and concatenate the list results (a failed item is skipped)."""
    from concurrent.futures import ThreadPoolExecutor
    def safe(x):
        try:
            return fn(x)
        except Exception:
            return []
    with ThreadPoolExecutor(max_workers=max(1, len(items))) as ex:
        return [h for r in ex.map(safe, items) for h in r]


def find_hiring_managers(company: str, role: str = "", limit: int = 8) -> dict:
    """Search the web for likely hiring managers / recruiters at `company` for `role`.

    Returns {"people": [...], "via": str, "fallback": url}. Each person has name, title, url (a clickable LinkedIn
    profile), score (relevance, higher is better), tracked (already in outreach.json), and a ready-to-use
    foot-in-the-door `message` (connection note). Nothing is sent. Set SERPER_API_KEY or BRAVE_API_KEY in .env
    for reliable results; without one it falls back to a best-effort Bing scrape.
    """
    import os
    try:
        import search_jobs  # noqa: F401  (loads .env so API keys are visible)
    except Exception:
        pass
    queries = _search_queries(company, role)
    hits, via = [], "none"
    try:
        if os.getenv("SERPER_API_KEY"):
            via = "Serper (Google)"
            hits = _parallel(lambda q: _search_serper(q, os.environ["SERPER_API_KEY"]), queries)
        elif os.getenv("BRAVE_API_KEY"):
            via = "Brave Search"
            hits = _parallel(lambda q: _search_brave(q, os.environ["BRAVE_API_KEY"]), queries)
        else:
            via = "Bing (no API key, best effort)"
            hits = _search_bing_browser(queries)
    except Exception as exc:
        via = f"search failed: {exc}"
    role_words = {w for w in re.findall(r"[a-z]{4,}", role.lower())}
    co = company.lower()
    people, seen = [], set()
    tracked = load_all()
    for h in hits:
        p = _parse_profile(h)
        if not p or p["url"].lower() in seen:
            continue
        seen.add(p["url"].lower())
        blob = f"{p['title']} {p['snippet']}".lower()
        score = (3 if co in blob else 0) + (2 if re.search(HIRING_TITLES, p["title"].lower()) else 0) \
            + sum(1 for w in role_words if w in blob)
        if co not in blob:   # wrong-company profiles are noise
            continue
        p["score"], p["tracked"] = score, bool(find_existing(p["url"], tracked))
        arch, p["why"] = classify({**p, "company": company}, use_ai=False)
        p["archetype"] = arch
        people.append(p)
    people.sort(key=lambda x: -x["score"])
    people = people[:limit]
    for p in people:   # quick foot-in-the-door note; the full AI draft happens when you pick the person
        p["message"] = foot_in_door_note({"name": p["name"], "company": company, "role": role})
    return {"people": people, "via": via, "fallback": people_search_url(company, role)}


def foot_in_door_note(contact: dict) -> str:
    """Short ask for 15 minutes to talk through ideas; no job ask. Used for the pasted connection note."""
    n, co, role = _first_name(contact), contact.get("company") or "your team", contact.get("role") or ""
    topic = f"the {role} work at {co}" if role else f"what {co} is working on"
    return _trim(f"Hi {n}, I'm a UT Austin chemical engineering grad (May 2026) and I have a few ideas related to {topic}. "
                 f"Would you be open to a quick 15-minute chat sometime? Happy to work around your schedule.", NOTE_LIMIT)
