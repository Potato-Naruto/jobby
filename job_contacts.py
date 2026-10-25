"""Give it a job link: find the people who hire for it and people already on the team (LinkedIn profiles via web
search), draft a personalised 5-minute-chat note for each, and show everything as a clickable chart.

Nothing is sent. Usage:  python job_contacts.py <job-url> [--company X] [--role Y] [--no-ai] [--no-open]
"""
import argparse
import html
import json
import os
import re
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor

import requests

import outreach
from common import BASE_DIR, ask_llm_json

OUT_HTML = BASE_DIR / "job_contacts.html"
_UA = outreach._UA
TEAM_TITLES = r"engineer|scientist|analyst|developer|specialist|technician|associate|architect|designer|consultant"


# --- 1. Read the job link -----------------------------------------------------------------------------
def fetch_job(url: str) -> dict:
    """{company, role, text} from a job page (JSON-LD JobPosting, then og/title tags). Empty fields if unreadable."""
    job = {"company": "", "role": "", "text": "", "url": url}
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=20)
        page = r.text
    except Exception:
        return job
    for blob in re.findall(r'<script[^>]+ld\+json[^>]*>(.*?)</script>', page, flags=re.S | re.I):
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        for d in (data if isinstance(data, list) else data.get("@graph", [data])):
            if isinstance(d, dict) and "JobPosting" in str(d.get("@type")):
                org = d.get("hiringOrganization") or {}
                job["role"] = html.unescape(str(d.get("title", "")))
                job["company"] = html.unescape(org.get("name", "") if isinstance(org, dict) else str(org))
                job["text"] = re.sub(r"<[^>]+>", " ", html.unescape(str(d.get("description", ""))))
    if not job["role"] and not re.search(r"just a moment|access denied|attention required|403|captcha", page[:3000], re.I):
        m = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.S | re.I)
        title = html.unescape(m.group(1)).strip() if m else ""
        m2 = re.match(r"(.+?)\s+\|\s+([^|]+)$", title) or re.match(r"(.+?)\s+(?:at|@|-|\|)\s+(.+?)(?:\s+[-|]\s+.*)?$", title)
        mo = re.search(r'hiringOrganization"\s+content="([^"]+)"', page)
        if mo:
            job["company"] = html.unescape(mo.group(1)).strip()
            job["role"] = re.sub(r"\s*\|.*$", "", title).strip()
        elif m2:
            job["role"], job["company"] = m2.group(1).strip(), m2.group(2).strip()
        else:
            job["role"] = title
    if not job["company"]:   # guess from the URL: /companies/<co>/, <co>.wd1.myworkdayjobs.com, boards.greenhouse.io/<co>
        m = (re.search(r"/companies?/([\w-]+)", url) or re.search(r"//([\w-]+)\.wd\d+\.myworkdayjobs", url)
             or re.search(r"(?:greenhouse\.io|lever\.co|ashbyhq\.com)/([\w-]+)", url))
        if m:
            job["company"] = m.group(1).replace("-", " ").title()
    if not job["role"]:
        m = re.search(r"/jobs?/(?:[^/]+/)*([\w-]{8,}?)(?:_[A-Z]*\d+)?(?:[/?#]|$)", url)
        if m:
            job["role"] = m.group(1).replace("-", " ").title()
    job["role"] = re.sub(r"(?i)\s*job details\s*$", "", job["role"]).strip()
    if not job["text"]:
        job["text"] = re.sub(r"\s+", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", page, flags=re.S | re.I))[:3000]
    return job


# --- 2. Find the people ------------------------------------------------------------------------------
def _queries(company: str, role: str) -> list[tuple[str, str]]:
    field = " ".join(re.sub(r"[^\w\s]", " ", role).split()[:4])
    return [("hiring", f'site:linkedin.com/in "{company}" "hiring manager" {field}'),
            ("hiring", f'site:linkedin.com/in "{company}" recruiter OR "talent acquisition" OR "university recruiter"'),
            ("hiring", f'site:linkedin.com/in "{company}" manager OR director {field}'),
            ("team", f'site:linkedin.com/in "{company}" {field}'),
            ("team", f'site:linkedin.com/in "{company}" engineer {field}')]


def short_company(name: str) -> str:
    """'Westlake Management Services, Inc.' -> 'Westlake' (legal names rarely appear on profiles)."""
    n = re.sub(r"[,.]", " ", name)
    n = re.sub(r"(?i)\b(inc|llc|corp|corporation|ltd|plc|co|company|lp|gmbh|management services|services|holdings|group|usa|us)\b", " ", n)
    return " ".join(n.split()) or name


def _response_score(p: dict) -> int:
    """How likely a cold note gets answered: recruiters and junior/early-career engineers high, senior executives low."""
    t = p["title"].lower()
    sc = 0
    if re.search(r"recruit|talent|sourc|early careers?|university|campus|people partner", t):
        sc += 5
    if re.search(r"graduate|rotation|grow\b|early career|associate|\bjunior|\bi\b|entry|intern|analyst", t):
        sc += 4
    if re.search(r"engineer|specialist|coordinator", t) and not re.search(r"senior|sr\.?|principal|staff|lead|manager|director|chief|head", t):
        sc += 3
    if re.search(r"senior manager|manager|lead|principal|staff", t):
        sc += 1
    if re.search(r"general manager|director|vp\b|vice president|president|chief|head of|\bgm\b", t):
        sc -= 4
    if "..." in t and not t.strip():
        sc -= 1
    return sc


def _is_student(p: dict) -> bool:
    """Students and job-seekers can't help with hiring, so they're never suggested."""
    head = p["title"].lower()
    return bool(re.search(r"\bstudents?\b|\bgraduated\b|\bseeking\b|\baspiring\b|looking for|open to work|\bundergrad|\bcandidate\b|"
                          r"\b(b\.?s\.?|m\.?s\.?)\s+(student|candidate)", head)
                 or re.match(r"(?i)\s*(student|undergraduate|graduate student)\b", p["snippet"]))


def find_people(company: str, role: str, max_hiring: int = 5, max_team: int = 6) -> list[dict]:
    key = os.getenv("SERPER_API_KEY")
    if not key:
        raise SystemExit("SERPER_API_KEY is not set in .env, so I can't search the web.")
    qs = _queries(company, role)

    def run(item):
        try:
            return [(item[0], h) for h in outreach._search_serper(item[1], key)]
        except Exception:
            return []
    with ThreadPoolExecutor(max_workers=len(qs)) as ex:
        hits = [x for r in ex.map(run, qs) for x in r]
    co, role_words = company.lower(), set(re.findall(r"[a-z]{4,}", role.lower()))
    seen, out = set(), []
    for kind, h in hits:
        p = outreach._parse_profile(h)
        if not p or p["url"].lower() in seen:
            continue
        blob = f"{p['title']} {p['snippet']}".lower()
        if co not in blob:
            continue
        if _is_student(p):
            continue
        seen.add(p["url"].lower())
        is_hiring = bool(re.search(outreach.HIRING_TITLES, p["title"].lower()))
        p["group"] = "Hiring" if is_hiring else "Team"
        p["score"] = 3 + (2 if is_hiring else 0) + sum(1 for w in role_words if w in blob)
        out.append(p)
    for p in out:
        p["resp"] = _response_score(p)
    hiring = sorted((p for p in out if p["group"] == "Hiring"), key=lambda p: -p["score"])[:max_hiring]
    team = sorted((p for p in out if p["group"] == "Team" and re.search(TEAM_TITLES, p["title"].lower())),
                  key=lambda p: -p["score"])[:max_team]
    res = hiring + team
    top = sorted(res, key=lambda p: -p["resp"])[:5]
    for p in res:
        p["likely"] = p in top and p["resp"] >= 3
    return res


# --- 3. Draft personalised messages ------------------------------------------------------------------
INTRO = "UT Austin chemical engineering grad (May 2026)"
LIMIT = 280


_NUM = r"(?:\d+\+?|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen|twenty)"


def _heuristic_hook(p: dict) -> tuple[str, str]:
    """(quoted profile phrase, sentence about it) from their search snippet, with no AI."""
    s = p["snippet"].replace("�", " ")
    for pat, fmt in ((rf"(?i)\b{_NUM}\s+years\s+(?:of\s+)?(?:extensive\s+)?experience\s+in\s+([^.,;]{{4,60}})", "I noticed your background in {0}."),
                     (r"\(\s*([A-Z][\w+# ]+(?:,\s*[A-Z][\w+# ]+){1,3})\s*\)", "I noticed your work on {0}."),
                     (r"(?i)(?:focus|specializ\w+|background)\s+(?:on|in)\s+([^.,;]{4,60})", "I noticed your focus on {0}."),
                     (r"(?i)(?:expertise|experience)\s+in\s+([^.,;]{4,60})", "I noticed your experience in {0}.")):
        m = re.search(pat, s)
        if m:
            return m.group(0).strip(), fmt.format(m.group(1).strip())
    return "", ""


COMMON = (  # (pattern in their title/snippet, what to say) - only things that are really true of me
    (r"university of texas at austin|ut austin|\bUT\b.*austin|longhorn", "we're both UT Austin alums"),
    (r"theta tau", "we're both in Theta Tau"),
    (r"pepsico|frito", "we both have PepsiCo experience"),
    (r"delek", "we both have Delek experience"),
    (r"chick-fil-a", "we both worked at Chick-fil-A"),
    (r"chemical engineer", "we're both chemical engineers"),
)


def _common(p: dict) -> str:
    text = f"{p['title']} {p['snippet']}".lower()
    for pat, say in COMMON:
        if re.search(pat, p["title"].lower() if pat == "chemical engineer" else text, re.I):   # snippets may just mention it
            return say
    return ""


def _fallback(p: dict, company: str, role: str) -> dict:
    n = outreach._first_name(p)
    short_role = re.sub(r"\s*\(.*?\)", "", role).strip()
    what = f"the {short_role} role" if short_role else "the team"
    common = _common(p)
    line = f"I saw {common} and wanted to reach out." if common else "I wanted to reach out."
    if p["group"] == "Hiring":
        msg = (f"Hi {n}! I'm a recent UT Austin chemical engineering grad, excited about {what} at {company}. "
               f"{line} Would you have 5 minutes on your calendar for a quick chat? Thank you!")
    else:
        msg = (f"Hi {n}! I'm a recent UT Austin chemical engineering grad looking at {what} at {company}. {line} "
               f"Would you have 5 minutes on your calendar to tell me what the team is like? Thanks!")
    return {"hook": common, "message": outreach._trim(msg, LIMIT)}


def draft_messages(people: list[dict], company: str, role: str, use_ai: bool = True) -> None:
    """Adds p['hook'] (a phrase quoted from their profile text) and p['message']. One local-AI call for everyone."""
    for p in people:
        p.update(_fallback(p, company, role))
    if not use_ai or not people:
        return
    listing = [{"id": i, "name": p["name"], "group": p["group"], "headline": p["title"], "profile_text": p["snippet"]}
               for i, p in enumerate(people)]
    system = (
        "You write LinkedIn connection notes for a new graduate. For each person, find ONE specific thing in their "
        "headline or profile_text worth complimenting (a project, product, team, topic, school, past employer) and quote it "
        "exactly as 'hook' (copy the words verbatim from their text; use '' if there is nothing specific). Then write "
        f"'message' (max {LIMIT} characters): 'Hi <first name>,' then a short genuine line about the hook (e.g. 'Love the work on X'), "
        f"then one line: I'm a {INTRO} interested in {role or 'the team'} at {company}, then ask if they could spare 5 minutes "
        "on their calendar to talk. Rules: plain words, no em dashes, no exclamation marks, no flattery, never invent facts "
        "about them or me, do not ask for a job. If hook is '' skip the compliment line. "
        'Reply JSON: {"results": [{"id": 0, "hook": "...", "message": "..."}]}')
    try:
        res = ask_llm_json(system, f"COMPANY: {company}\nROLE: {role}\nPEOPLE:\n{json.dumps(listing)}", retries=0)
    except Exception as exc:
        print(f"[warn] local AI unavailable ({str(exc)[:80]}); using plain templates.", file=sys.stderr)
        return
    for r in (res.get("results", []) if isinstance(res, dict) else []):
        try:
            p = people[int(r["id"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        msg = " ".join(str(r.get("message", "")).replace("—", ",").replace("–", "-").split())
        hook = str(r.get("hook", "")).strip()
        source = f"{p['title']} {p['snippet']}".lower()
        if hook and hook.lower() not in source:   # quoted text must really be on their profile
            continue
        if msg and len(msg) <= LIMIT and "5 minutes" in msg and outreach._numbers_ok(msg, source + " 5 2026 may"):
            p["hook"], p["message"] = hook, msg


# --- 4. Chart ------------------------------------------------------------------------------------------
def write_html(job: dict, people: list[dict]) -> None:
    rows = []
    for i, p in enumerate(people):
        rows.append(
            f'<tr class="{p["group"].lower()}{" likely" if p.get("likely") else ""}" data-k="{html.escape(p["url"])}"><td><input type="checkbox" class="ck" title="Done" onchange="mark(this,this.checked)"> <span class="tag">{p["group"]}</span></td>'
            f'<td><a href="{html.escape(p["url"])}" target="_blank" rel="noopener" onclick="mark(this,true)">{"<b>" if p.get("likely") else ""}{html.escape(p["name"])}{"</b> ⭐ <span class=lk>most likely to reply</span>" if p.get("likely") else ""}</a>'
            f'<div class="t">{html.escape(p["title"])}</div></td>'
            f'<td class="hook">{html.escape(p["hook"] or "-")}</td>'
            f'<td><div class="msg" id="m{i}">{html.escape(p["message"])}</div></td>'
            f'<td><button onclick="cp({i},this)">Copy</button><br>'
            f'<a class="go" href="{html.escape(p["url"])}" target="_blank" rel="noopener" onclick="mark(this,true)">Open ↗</a></td></tr>')
    page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Contacts for {html.escape(job['role'] or 'job')}</title>
<style>
:root{{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e2e2e2;--acc:#0a66c2;--h:#e8f3ff;--t:#eef8ee}}
@media(prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#e8e8e8;--mut:#9aa;--line:#2c3036;--acc:#6db3ff;--h:#1b2a3a;--t:#1c2c1f}}}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:var(--mut);margin-bottom:16px}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;min-width:760px}}
th,td{{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid var(--line)}}
th{{font-size:12px;text-transform:uppercase;color:var(--mut)}}
tr.hiring{{background:var(--h)}} tr.team{{background:var(--t)}}
.tag{{font-size:12px;font-weight:600}} .t{{color:var(--mut);font-size:13px}} .hook{{font-style:italic;max-width:200px}}
.msg{{max-width:460px}} a{{color:var(--acc);font-weight:600}} .go{{font-size:13px}}
tr.likely .t{{font-weight:600;color:var(--fg)}} .lk{{font-size:12px;font-weight:700;color:#b26a00}} tr.likely td{{font-weight:600}}
tr.done{{opacity:.5}} tr.done .msg,tr.done a:not(.go){{text-decoration:line-through}} .ck{{width:18px;height:18px;vertical-align:middle;accent-color:#2a9d3f}}
button{{cursor:pointer;padding:6px 12px;border:1px solid var(--acc);background:transparent;color:var(--acc);border-radius:6px;font-weight:600}}
</style>
<h1>{html.escape(job['role'] or 'Job')} at {html.escape(job['company'])}</h1>
<div class="sub">Blue = hiring side, green = already on the team. Click Open, then Connect &gt; Add a note, paste.
<a href="{html.escape(job['url'])}" target="_blank" rel="noopener">Job posting</a>
{('&middot; <a href="' + html.escape(job['resume']) + '" target="_blank">&#128196; Tailored resume (PDF)</a>') if job.get('resume') else ''}</div>
<div class="wrap"><table><tr><th>Who</th><th>Name (LinkedIn)</th><th>Profile detail used</th><th>Message</th><th></th></tr>
{''.join(rows)}</table></div>
<script>
function store(){{try{{return JSON.parse(localStorage.getItem('jc_done')||'{{}}')}}catch(e){{return {{}}}}}}
function mark(el,on){{const tr=el.closest('tr');tr.classList.toggle('done',on);tr.querySelector('.ck').checked=on;
 try{{const d=store();if(on)d[tr.dataset.k]=1;else delete d[tr.dataset.k];localStorage.setItem('jc_done',JSON.stringify(d))}}catch(e){{}}}}
function cp(i,b){{const tr=b.closest('tr');navigator.clipboard.writeText(document.getElementById('m'+i).innerText).then(()=>{{b.textContent='Copied';setTimeout(()=>b.textContent='Copy',1500)}});mark(tr,true)}}
const d0=store();document.querySelectorAll('tr[data-k]').forEach(tr=>{{if(d0[tr.dataset.k])mark(tr,true)}});
</script>"""
    OUT_HTML.write_text(page, encoding="utf-8")


def print_chart(job: dict, people: list[dict]) -> None:
    from rich import box
    from rich.console import Console
    from rich.table import Table
    t = Table(title=f"{job['role']} at {job['company']}", box=box.SIMPLE_HEAD, expand=True)
    for c in ("#", "Who", "Name (click)", "Title", "Detail used"):
        t.add_column(c, overflow="fold")
    for i, p in enumerate(people, 1):
        t.add_row(str(i), p["group"], f"[link={p['url']}]{p['name']}[/link]\n[dim]{p['url']}[/]", p["title"][:50], p["hook"] or "-")
    Console().print(t)


def run(url: str, company: str = "", role: str = "", use_ai: bool = True, open_page: bool = True, resume: bool = True) -> list[dict]:
    try:
        import search_jobs  # noqa: F401  (loads .env)
    except Exception:
        pass
    job = fetch_job(url) if url else {"company": "", "role": "", "text": "", "url": ""}
    job["company"], job["role"] = short_company(company or job["company"]), role or job["role"]
    if not job["company"]:
        raise SystemExit("Couldn't read the company from that page. Re-run with --company \"Name\" --role \"Title\".")
    print(f"Job: {job['role'] or '(unknown role)'} at {job['company']}")
    people = find_people(job["company"], job["role"])
    if not people:
        print("No LinkedIn profiles found for that company.")
        return []
    draft_messages(people, job["company"], job["role"], use_ai)
    job["resume"] = ""
    if resume:
        try:
            import resume_tailor
            made = resume_tailor.tailor_for_job(job["company"], job["role"], job.get("text", ""))
            if made:
                job["resume"] = made[0].relative_to(BASE_DIR).as_posix()
                print(f"Tailored resume: {made[0]}  (matched: {', '.join(made[1][:8]) or 'none'})")
            else:
                print("No resume profile yet, so no tailored resume. Run 'Resume profile' in the menu first.")
        except Exception as exc:
            print(f"[warn] couldn't build the tailored resume: {exc}")
    write_html(job, people)
    print_chart(job, people)
    print(f"\nClickable chart with Copy buttons: {OUT_HTML}")
    if open_page:
        webbrowser.open(OUT_HTML.as_uri())
        if job.get("resume"):
            webbrowser.open((BASE_DIR / job["resume"]).as_uri())
    return people


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default="")
    ap.add_argument("--company", default="")
    ap.add_argument("--role", default="")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--no-resume", action="store_true")
    a = ap.parse_args()
    run(a.url, a.company, a.role, not a.no_ai, not a.no_open, not a.no_resume)
