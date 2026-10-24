"""Interactive job-application form filler (Playwright + local LLM).

Usage:
    python app_agent.py URL [URL ...]
"""
import json
import os
import re
import sys
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from rich.console import Console

import phm
from common import PROFILE_JSON, QA_CACHE_JSON, RESUME_PDF, ask_llm_json, load_json, save_json

_console = Console()
class BrowserClosed(Exception):
    """The Chromium window was closed while filling."""


LONG_LIST = 40  # dropdowns with more options than this (countries, schools) are answered with plain text


def busy(message: str):
    return _console.status(f"[bright_green]{message}[/]", spinner="dots12", spinner_style="bright_green")


# --- Page inspection --------------------------------------------------------
# Runs in the browser: tags each visible control with data-jobby-id and returns its metadata.
SCAN_JS = """
() => {
  const labelOf = (el) => {
    const parts = [];
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) parts.push(l.innerText); }
    const wrap = el.closest('label'); if (wrap) parts.push(wrap.innerText);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) lb.split(/\\s+/).forEach(i => { const n = document.getElementById(i); if (n) parts.push(n.innerText); });
    parts.push(el.getAttribute('aria-label') || '');
    const fs = el.closest('fieldset'); if (fs && fs.querySelector('legend')) parts.push(fs.querySelector('legend').innerText);
    if (!parts.join('').trim()) {                       // climb a few levels looking for a nearby label
      let n = el.parentElement;
      for (let i = 0; i < 4 && n && !parts.join('').trim(); i++, n = n.parentElement) {
        const l = n.querySelector('label, legend, [class*=label]');
        if (l && !l.contains(el) && l.innerText.trim()) parts.push(l.innerText);
      }
    }
    if (!parts.join('').trim()) parts.push(el.placeholder || el.name || '');
    return parts.map(s => (s || '').replace(/\\s+/g, ' ').trim()).filter(Boolean)[0] || '';
  };
  const visible = (el) => el.type === 'file' || !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const out = [];
  let n = 0;
  document.querySelectorAll('input, select, textarea').forEach(el => {
    const combo = el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox';
    const type = combo ? 'combobox' : el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text');
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type) || el.disabled || !visible(el)) return;
    if (type !== 'file' && (/requiredInput|visually-?hidden|sr-only/i.test(String(el.className)) || el.getAttribute('aria-hidden') === 'true') || (el.tabIndex === -1 && !combo && !['checkbox','radio','file'].includes(type))) return;
    const id = String(n++);
    el.setAttribute('data-jobby-id', id);
    const label = labelOf(el);
    let value = el.value || '';
    if (combo) { const ctl = el.closest('[class*=control], [class*=select]'); const sv = ctl && ctl.querySelector('[class*=single-value]'); value = sv ? sv.innerText.trim() : ''; }
    const info = { id, type, label, name: el.name || '',
                   required: !!(el.required || el.getAttribute('aria-required') === 'true' || /\\*\\s*$/.test(label)),
                   value, checked: !!el.checked, options: [] };
    if (type === 'select') info.options = [...el.options].map(o => o.text.trim()).filter(Boolean);
    if (type === 'radio') {
      info.options = [labelOf(el) || el.value];
      const fs = el.closest('fieldset, [role=radiogroup]');
      const q = fs && (fs.querySelector('legend') || fs.querySelector('label'));
      let text = (q ? q.innerText : '').split(String.fromCharCode(10))[0].trim();
      if (!text || text.length > 200 || text === labelOf(el)) {      // look for the question in a surrounding block
        const own = labelOf(el);
        for (let n = el.parentElement, k = 0; n && k < 9; n = n.parentElement, k++) {
          const cand = n.querySelector('[class*=label]:not(:has(input)), legend');
          if (cand && !cand.contains(el)) {
            const t = cand.innerText.split(String.fromCharCode(10))[0].trim();
            if (t.length > 3 && t !== own) { text = t; break; }
          }
        }
      }
      info.label = text || el.name;
    }
    out.push(info);
  });
  return out;
}
"""


def scan_fields(page: Page) -> list[dict]:
    """Return fields on the page; radios sharing a name are merged into one multi-option field."""
    raw = page.evaluate(SCAN_JS)
    fields, radios = [], {}
    for f in raw:
        if f["type"] == "radio":
            group = radios.setdefault(f["name"] or f["id"], {**f, "ids": [], "options": [], "values": []})
            group["ids"].append(f["id"])
            group["options"].extend(f["options"])
            group["checked"] = group["checked"] or f["checked"]
            continue
        f["ids"] = [f["id"]]
        fields.append(f)
    for g in radios.values():
        g["label"] = g["label"] or g["name"]
        fields.append(g)
    return fields


# --- Answer resolution ------------------------------------------------------
def norm(label: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", label.lower().replace("*", "")).strip()


MATCH_PROMPT = (
    "You fill out job application forms using ONLY the candidate profile and previously saved answers. "
    "For each field return an answer, or null if the information is not present (never guess or invent). "
    "For select/radio/combobox fields with options the answer must be exactly one of the listed options. For checkboxes answer true/false. "
    "Hints: name/customer name/full name = contact.full_name; first/last name = split of full_name; "
    "phone/telephone/mobile = contact.phone; email = contact.email; city/state/location from contact.location; "
    "LinkedIn/GitHub/website from contact; school/degree/GPA from education; current employer/title from the latest job. "
    'Reply as JSON: {"answers": {"<field id>": <answer or null>}}.'
)


def resume_file():
    """The resume to upload: the one in please-hire-me when present, else resume.pdf here."""
    return phm.resume_path() or RESUME_PDF


def merge_phm(profile: dict) -> dict:
    """Overlay your confirmed please-hire-me profile (right email, names, work auth...) on the parsed resume."""
    if not phm.available():
        return profile
    p = phm.profile()
    edu = {**(profile.get("education") or [{}])[0]}
    edu.update(institution=p.get("school_full_name") or p.get("school") or edu.get("institution"),
               degree="Bachelor's Degree" if "bachelor" in str(p.get("degree_type_answer", "")).lower() else edu.get("degree"),
               field_of_study=p.get("major") or edu.get("field_of_study"), gpa=p.get("gpa") or edu.get("gpa"))
    contact = {**profile.get("contact", {}),
               **{k: p[k] for k in ("email", "phone", "location", "linkedin", "github", "website") if p.get(k)},
               "full_name": p.get("name_combined") or profile.get("contact", {}).get("full_name"),
               "first_name": p.get("first_name"), "last_name": p.get("last_name"),
               "legal_first_name": p.get("legal_first_name"), "preferred_first_name": p.get("preferred_first_name")}
    keep = ("work_authorized_us", "visa_needs_sponsorship", "citizenship", "earliest_start_answer", "notice_period_answer",
            "how_did_you_hear_answer", "desired_comp_answer", "willing_to_relocate", "security_clearance", "military_service",
            "gpa", "pronouns_answer", "eeo", "sat_score", "mailing_address_full", "high_school_name", "high_school_grad_year")
    return {**profile, "contact": contact, "education": [edu] + list(profile.get("education") or [])[1:],
            "confirmed_answers": {k: p[k] for k in keep if k in p}, "preset_rules": phm.presets_text()}


def llm_match(fields: list[dict], profile: dict, cache: dict, summarize=None) -> dict:
    """Ask the LLM to resolve every field from profile/cache. Returns {field_id: answer|None}."""
    summary = [summarize(f) if summarize else {"id": f["id"], "label": f["label"], "type": f["type"], "options": f["options"]}
               for f in fields]
    rules = profile.get("preset_rules")
    profile = {k: v for k, v in profile.items() if k != "preset_rules"}
    prompt = ((f"PRESET ANSWER RULES (these override everything else for legal/EEO/consent/situational questions):\n{rules}\n\n" if rules else "")
              + f"PROFILE:\n{json.dumps(profile)}\n\nSAVED ANSWERS:\n{json.dumps(cache)}\n\n"
              f"FIELDS:\n{json.dumps(summary)}")
    try:
        with busy(f"Matching {len(fields)} form field(s) to your profile with the local AI…"):
            return ask_llm_json(MATCH_PROMPT, prompt).get("answers", {})
    except Exception as exc:
        print(f"[warn] LLM matching failed ({exc}); using your saved profile for the obvious fields and asking about the rest.")
        return {}


def valid_answer(field: dict, answer) -> bool:
    if answer is None or answer == "":
        return False
    if field["type"] in ("select", "radio") or (field["type"] == "combobox" and 0 < len(field["options"]) <= LONG_LIST):
        a = str(answer).strip().lower()
        return any(a == o.lower() or a in o.lower() for o in field["options"])
    return True


def quick_answer(field: dict, profile: dict):
    """Fill the obvious fields straight from the profile, so forms still fill if the local AI is slow or down."""
    l = norm(field["label"])
    c = profile.get("contact", {})
    name = (c.get("full_name") or "").title().split()
    edu = (profile.get("education") or [{}])[0]
    job = (profile.get("work_experience") or [{}])[0]
    city = (c.get("location") or "").split(",")[0].strip()
    if field["type"] in ("checkbox", "radio", "file"):
        return None
    if "preferred" in l and "name" in l:
        return c.get("preferred_first_name") or (name[0] if name else None)
    if "legal" in l and "first" in l and c.get("legal_first_name"):
        return c["legal_first_name"]
    if l in ("first name", "legal first name", "given name") and (c.get("first_name") or name):
        return c.get("first_name") or name[0]
    if l in ("last name", "legal last name", "surname", "family name") and (c.get("last_name") or len(name) > 1):
        return c.get("last_name") or name[-1]
    ans = profile.get("confirmed_answers", {})
    if "sponsor" in l and ans.get("visa_needs_sponsorship", "").lower().startswith("no"):
        return "No"
    if ("authorized to work" in l or "legally authorized" in l or "eligible to work" in l) and ans.get("work_authorized_us", "").lower().startswith("yes"):
        return "Yes"
    if "how did you hear" in l and ans.get("how_did_you_hear_answer"):
        return ans["how_did_you_hear_answer"]
    if "gpa" in l and field.get("required") and ans.get("gpa"):
        return str(ans["gpa"])
    if l in ("name", "full name", "legal name"):
        return " ".join(name) or None
    if "email" in l:
        return c.get("email")
    if "phone" in l or "mobile" in l:
        return c.get("phone")
    if "linkedin" in l:
        return c.get("linkedin")
    if "github" in l:
        return c.get("github") or None
    if "website" in l or "portfolio" in l:
        return c.get("website") or None
    if l.startswith("location") or l in ("city", "current location"):
        return city or None
    if l in ("school", "university", "college", "school name"):
        return edu.get("institution")
    if l == "degree" or l.startswith("degree"):
        return edu.get("degree")
    if "discipline" in l or "field of study" in l or l == "major":
        return edu.get("field_of_study")
    if "current company" in l or "current employer" in l:
        return job.get("company")
    if "current title" in l or "current job title" in l:
        return job.get("title")
    return None


def ask_user(field: dict) -> str:
    """Interactive fallback: prompt in the terminal for an unknown field (pick by number when there are options)."""
    opts = field["options"][:LONG_LIST] if field["options"] else []
    print(f"\n[?] {field['label']}  ({field['type']})")
    for i, o in enumerate(opts, 1):
        print(f"     {i:>2}) {o}")
    try:
        raw = input("    Your answer" + (" (number or text" if opts else " (text") + ", Enter = skip): ").strip()
    except EOFError:
        return ""
    if opts and raw.isdigit() and 1 <= int(raw) <= len(opts):
        return opts[int(raw) - 1]
    return raw


# --- Filling ----------------------------------------------------------------
def pick_option(options: list[str], answer: str) -> str:
    a = str(answer).strip().lower()
    for o in options:
        if o.lower() == a:
            return o
    for o in options:
        if a in o.lower():
            return o
    return answer


MENU_JS = """el => {
  const id = el.getAttribute('data-jobby-id');
  document.querySelectorAll('[data-jobby-opt="' + id + '"]').forEach(n => n.removeAttribute('data-jobby-opt'));
  const r = el.getBoundingClientRect();
  let count = 0;
  document.querySelectorAll('[role=option]').forEach(o => {
    const b = o.getBoundingClientRect();
    const shown = b.width > 0 && b.height > 0 && o.getClientRects().length;
    const below = b.top >= r.bottom - 8 && b.top <= r.bottom + 600 && b.left >= r.left - 320 && b.left <= r.right + 320;
    if (shown && below) { o.setAttribute('data-jobby-opt', id); count++; }
  });
  return count;
}"""


def combo_option_locator(page: Page, field: dict):
    """Options of THIS dropdown only: visible ones sitting right under the input (stale listboxes are ignored)."""
    page.locator(f'[data-jobby-id="{field["id"]}"]').evaluate(MENU_JS)
    return page.locator(f'[data-jobby-opt="{field["id"]}"]')


def close_popups(page: Page) -> None:
    page.keyboard.press("Escape")
    page.keyboard.press("Tab")


def read_combo_options(page: Page, field: dict) -> list[str]:
    """Open a custom dropdown, read its choices, close it again."""
    loc = page.locator(f'[data-jobby-id="{field["id"]}"]')
    try:
        loc.scroll_into_view_if_needed(timeout=3000)
        loc.click(timeout=3000)
        page.wait_for_timeout(400)
        opts = [o.inner_text().strip() for o in combo_option_locator(page, field).all()[:200]]
        close_popups(page)
        return [re.sub(r"\s+", " ", o) for o in opts if o]
    except PlaywrightError:
        return []


def fill_combobox(page: Page, field: dict, answer) -> None:
    loc = page.locator(f'[data-jobby-id="{field["id"]}"]')
    loc.scroll_into_view_if_needed(timeout=3000)
    loc.click(timeout=3000)
    loc.type(str(answer), delay=30)
    page.wait_for_timeout(700)
    opts = combo_option_locator(page, field)
    want = str(answer).strip().lower()
    texts = [re.sub(r"\s+", " ", o.inner_text()).strip() for o in opts.all()]
    for i, t in enumerate(texts):
        if t.lower() == want:
            opts.nth(i).click()
            return
    for i, t in enumerate(texts):
        if want in t.lower():
            opts.nth(i).click()
            return
    if texts:
        opts.first.click()
    else:
        page.keyboard.press("Enter")
    page.wait_for_timeout(200)


def fill_field(page: Page, field: dict, answer) -> None:
    sel = lambda i: page.locator(f'[data-jobby-id="{i}"]')  # noqa: E731
    t = field["type"]
    if t == "file":
        sel(field["id"]).set_input_files(str(resume_file()))
    elif t == "combobox":
        fill_combobox(page, field, answer)
    elif t == "select":
        sel(field["id"]).select_option(label=pick_option(field["options"], answer))
    elif t == "checkbox":
        truthy = str(answer).strip().lower() in ("true", "yes", "y", "1")
        sel(field["id"]).set_checked(truthy)
    elif t == "radio":
        for i, opt in zip(field["ids"], field["options"]):
            if opt.lower() == str(answer).strip().lower():
                sel(i).check(force=True)
                return
    else:
        loc = sel(field["id"])
        try:
            loc.fill(str(answer), timeout=5000)
        except PlaywrightError:
            close_popups(page)                    # an open dropdown can block the next field
            loc.click(timeout=3000, force=True)
            loc.fill(str(answer), timeout=5000)


CONSENT = re.compile(r"acknowledg|i agree|agree to|consent|certify|i understand|privacy|terms|double-check|attest|i confirm|declare", re.I)


def fill_page(page: Page, profile: dict, cache: dict) -> list[str]:
    """Scan the current page and fill every field we can resolve (asking about required ones we can't)."""
    best: dict[str, dict] = {}
    for f in scan_fields(page):
        if not f["label"].strip():
            continue
        k = (norm(f["label"]), f["type"] == "file")      # a hidden file input may share a text field's label
        if k not in best or (f["type"] == "combobox" and best[k]["type"] != "combobox"):
            best[k] = f
    fields = list(best.values())
    todo = []
    for f in fields:
        if f["type"] in ("checkbox", "radio"):
            already = f["checked"]
        elif f["type"] == "file":
            already = False
        else:
            already = bool(f["value"])
        if not already:
            todo.append(f)
    print(f"Found {len(fields)} fields ({len(todo)} to fill).")

    combos = [f for f in todo if f["type"] == "combobox"]
    if combos:
        with busy(f"Reading {len(combos)} dropdown(s)…"):
            for f in combos:
                f["options"] = read_combo_options(page, f)

    def summarize(f):
        opts = f["options"][:LONG_LIST]
        d = {"id": f["id"], "label": f["label"], "type": f["type"], "options": opts}
        if len(f["options"]) > LONG_LIST:
            d["note"] = "long list: answer with the plain value, e.g. a country or school name"
        return d

    llm_answers = {}
    needs_llm = [f for f in todo if f["type"] != "file" and norm(f["label"]) not in cache]
    if needs_llm:
        llm_answers = llm_match(needs_llm, profile, cache, summarize)

    skipped, consent, blockers = [], [], []
    for f in todo:
        key = norm(f["label"])
        if f["type"] != "file" and CONSENT.search(f["label"]):
            consent.append(f["label"])           # legal acknowledgements are for YOU to tick
            continue
        if f["type"] == "file":
            if not resume_file().exists():
                print(f"[warn] {resume_file().name} missing; skipping file field {f['label']!r}.")
                continue
            answer = str(resume_file())
        elif key in cache:
            answer = cache[key]
        elif valid_answer(f, llm_answers.get(f["id"])):
            answer = llm_answers[f["id"]]
        elif quick_answer(f, profile) and (valid_answer(f, quick_answer(f, profile)) or norm(f["label"]) in ("school", "university", "college")):
            answer = quick_answer(f, profile)
        elif not f["required"]:
            skipped.append(f["label"])      # optional and unknown: don't interrupt you
            continue
        else:
            answer = ask_user(f)
            if not answer:
                print("    (skipped)")
                blockers.append(f"required field left empty: {f['label'][:60]}")
                continue
            cache[key] = answer                      # remember for future applications
            save_json(QA_CACHE_JSON, cache)
        try:
            fill_field(page, f, answer)
            print(f"  filled {f['label']!r}")
        except PlaywrightError as exc:
            if "has been closed" in str(exc):
                raise BrowserClosed() from exc
            print(f"  [warn] could not fill {f['label']!r}: {str(exc).splitlines()[0]}")
            if f["required"]:
                blockers.append(f"could not fill required field: {f['label'][:60]}")
    if consent:
        blockers += [f"tick yourself: {c[:60]}" for c in consent]
        print(f"  [!] {len(consent)} consent/acknowledgement box(es) left for you to tick yourself:")
        for c in consent:
            print(f"      - {c[:90]}")
    if skipped:
        print(f"  (left {len(skipped)} optional field(s) blank: {', '.join(x[:25] for x in skipped[:6])}{'…' if len(skipped) > 6 else ''})")
    return blockers


# --- Submission -------------------------------------------------------------
def click_submit(page: Page) -> bool:
    candidates = page.locator(
        "button[type=submit], input[type=submit], "
        "button:has-text('Submit'), button:has-text('Apply'), button:has-text('Send application')"
    )
    for i in range(candidates.count()):
        btn = candidates.nth(i)
        if btn.is_visible():
            btn.click()
            return True
    return False


ATS_HOSTS = re.compile(r"greenhouse|lever\.co|ashbyhq|workable|smartrecruiters|jobvite|icims|bamboohr", re.I)


AGGREGATORS = re.compile(r"adzuna\.|indeed\.|ziprecruiter\.|linkedin\.com|glassdoor\.|jooble\.|simplyhired\.|talent\.com|"
                         r"careerjet\.|jobrapido\.|learn4good\.|whatjobs\.|snagajob\.", re.I)
POPUP_WORDS = ("No, thanks", "No thanks", "Not now", "Maybe later", "Skip", "Close", "Dismiss", "Continue without",
               "Accept all", "Accept all cookies", "Accept", "I agree", "Got it", "Reject all", "Decline", "OK")
POPUP_BOX = "[role=dialog], [aria-modal=true], [class*=modal i], [class*=popup i], [class*=overlay i], [class*=cookie i], [id*=cookie i], [id*=consent i]"
APPLY_NAMES = ("Apply for this job", "Apply now", "Apply Now", "Apply on company site", "Apply on employer site",
               "Apply for this position", "Apply to this job", "Easy Apply", "Apply")


SKIP_PROMPT = re.compile(r"take me to the job|no,? thanks|no thank you|continue to (the )?(job|site|application)|continue without", re.I)


def skip_prompts(page: Page) -> bool:
    """Click 'No thanks, take me to the job' style links (email-alert prompts), wherever they appear."""
    for loc in (page.get_by_role("link", name=SKIP_PROMPT), page.get_by_role("button", name=SKIP_PROMPT),
                page.get_by_text(SKIP_PROMPT)):
        try:
            el = loc.first
            if el.is_visible(timeout=300):
                el.click(timeout=1500)
                page.wait_for_timeout(500)
                return True
        except PlaywrightError:
            continue
    return False


def dismiss_popups(page: Page) -> int:
    """Close email-alert prompts, cookie banners and similar modals that block the page. Returns how many were closed."""
    closed = 0
    for _ in range(4):
        hit = False
        if skip_prompts(page):
            closed += 1
            continue
        box = page.locator(POPUP_BOX)
        for word in POPUP_WORDS:
            pat = re.compile(rf"^\s*{re.escape(word)}\s*$", re.I)
            for loc in (box.get_by_role("button", name=pat), box.get_by_role("link", name=pat)):
                try:
                    el = loc.first
                    if el.is_visible(timeout=250):
                        el.click(timeout=1500)
                        page.wait_for_timeout(500)
                        hit = closed = closed + 1
                        break
                except PlaywrightError:
                    continue
            if hit:
                break
        if not hit:
            for sel in ("[role=dialog] [aria-label*=close i]", "[aria-modal=true] [aria-label*=close i]", "[class*=modal i] [class*=close i]"):
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=250):
                        el.click(timeout=1500)
                        page.wait_for_timeout(500)
                        hit = True
                        closed += 1
                        break
                except PlaywrightError:
                    continue
        if not hit:
            break
    return closed


def click_apply(page: Page) -> Page | None:
    """Click the Apply button/link. Returns the page to continue on (a new tab if one opened), or None if no button was found."""
    for name in APPLY_NAMES:
        pat = re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)
        btn = page.get_by_role("link", name=pat).or_(page.get_by_role("button", name=pat)).first
        try:
            if not btn.is_visible(timeout=1200):
                continue
            before = page.url
            try:
                with page.context.expect_page(timeout=9000) as new:
                    btn.click(timeout=3000)
                    page.wait_for_timeout(900)
                    for _ in range(3):                      # the click can raise an email-alert prompt: "No thanks, take me to the job"
                        if not skip_prompts(page):
                            break
                        page.wait_for_timeout(500)
                tab = new.value
                tab.wait_for_load_state("domcontentloaded", timeout=45000)
                return tab
            except PlaywrightError:
                pass                                           # no new tab: it navigated this one (or opened a modal)
            page.wait_for_timeout(3000)
            if page.url == before and skip_prompts(page):            # prompt appeared late: skip it and let the page continue
                page.wait_for_timeout(3000)
            return page
        except PlaywrightError:
            continue
    return None


def real_fields(page: Page) -> int:
    """Fields that belong to an application: ignores search boxes and email-alert sign-ups."""
    try:
        n = 0
        for f in scan_fields(page):
            lab = (f["label"] + " " + f["name"]).lower()
            if f["type"] == "search" or re.search(r"search|what\?|where\?|job.?title|alert|newsletter|subscribe|similar jobs", lab):
                continue
            n += 1
        return n
    except PlaywrightError:
        return 0


def open_application_form(page: Page, depth: int = 0) -> Page:
    """Get from a posting to the real application form: close pop-ups, click Apply (following new tabs and job-board
    redirects), or open an embedded ATS frame. Returns the page that holds the form."""
    page.wait_for_timeout(800)
    popups = dismiss_popups(page)
    if popups:
        print(f"  (closed {popups} pop-up(s))")
    on_board = bool(AGGREGATORS.search(page.url))
    if not on_board and real_fields(page) >= 4:
        return page
    if "ashbyhq.com" in page.url and not page.url.rstrip("/").endswith("/application"):
        page.goto(page.url.split("?")[0].rstrip("/") + "/application", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)
        if real_fields(page) >= 4:
            return page
    for fr in page.frames[1:]:
        if ATS_HOSTS.search(fr.url) and "recaptcha" not in fr.url:
            print(f"  (application form is embedded - opening {fr.url[:70]})")
            page.goto(fr.url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            return page
    if depth >= 3:
        return page
    nxt = click_apply(page)
    if nxt is None:
        return page
    if nxt is not page:
        print(f"  (Apply opened a new tab: {nxt.url[:70]})")
    nxt.wait_for_timeout(2500)
    if nxt.url != page.url or nxt is not page:
        print(f"  (now on {nxt.url[:80]})")
    return open_application_form(nxt, depth + 1)


HUMAN_WAIT_S = 600
HUMAN_CHECK = re.compile(r"confirm you are human|human verification|verify you are (a )?human|are you a robot|not a robot|"
                         r"security check|captcha|checking your browser|unusual traffic", re.I)
ACCOUNT_WALL = re.compile(r"/authenticate|/register|/login|/signin|/sign-in|/sign_in", re.I)


_adzuna_creds: dict = {}


def adzuna_login(page: Page) -> bool:
    """Adzuna asked you to sign in: ask for your login in the terminal (kept in memory only) and submit it."""
    import getpass
    if "adzuna." not in page.url:
        return False
    if not _adzuna_creds:
        print("\n[?] Adzuna wants you to log in. Enter your Adzuna login (blank to skip). Not saved to disk.")
        email = os.getenv("ADZUNA_EMAIL") or input("    Adzuna email: ").strip()
        if not email:
            return False
        _adzuna_creds.update(email=email, password=getpass.getpass("    Adzuna password: "))
    try:
        page.fill("input[type=email], input[name*=email i], input[id*=email i]", _adzuna_creds["email"], timeout=5000)
        page.fill("input[type=password]", _adzuna_creds["password"], timeout=5000)
        page.press("input[type=password]", "Enter")
        page.wait_for_timeout(4000)
    except PlaywrightError:
        print("[!] Could not find Adzuna's login fields; log in yourself in the browser.")
        _adzuna_creds.clear()
        return False
    if ACCOUNT_WALL.search(page.url):
        print("[!] Still on the login page - wrong password? Cleared; log in yourself or try again.")
        _adzuna_creds.clear()
        return False
    print("  Logged in to Adzuna.")
    return True


def human_check(page: Page) -> bool:
    try:
        return bool(HUMAN_CHECK.search(page.title() or "") or HUMAN_CHECK.search(page.inner_text("body", timeout=1500)[:1500]))
    except PlaywrightError:
        return False


def settle(page: Page) -> Page:
    """If a job board shows a 'confirm you are human' check, leave THAT step to you, then carry on to the employer's form."""
    for _ in range(3):
        if not human_check(page):
            break
        print("\n[!] The site is asking you to confirm you are human (a security check).")
        print("    I will not click through that. Complete it in the browser window; I'm waiting and will")
        print(f"    carry on by myself as soon as it passes (up to {HUMAN_WAIT_S // 60} min, Ctrl+C to stop waiting).")
        ctx = page.context
        deadline = time.time() + HUMAN_WAIT_S
        passed = None
        try:
            with busy("Waiting for you to complete the security check…"):
                while time.time() < deadline:
                    live = [pg for pg in ctx.pages if not pg.is_closed()]
                    if not live:
                        break
                    live[0].wait_for_timeout(1500)         # keeps Playwright's event loop running (unlike time.sleep)
                    for pg in reversed(ctx.pages):         # the check may open or replace a tab
                        if pg.is_closed():
                            continue
                        if not human_check(pg):
                            passed = pg
                            break
                    if passed:
                        break
        except KeyboardInterrupt:
            print("  (stopped waiting)")
            return page
        if passed is None:
            print("[!] Timed out waiting for the security check.")
            return page
        print("  Check passed - continuing.")
        page = passed
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PlaywrightError:
            pass
        page.wait_for_timeout(2500)
        with busy("Looking for the application form…"):
            page = open_application_form(page)
    return page


AUTO_SUBMIT = os.getenv("JOBBY_AUTO_SUBMIT", "1") != "0"
SUBMIT_DELAY_S = 8
SUCCESS = re.compile(r"thank you for (applying|your application)|application (has been |was )?(submitted|received|sent)|"
                     r"successfully (submitted|applied)|we('ve| have) received your application|you('ve| have) applied", re.I)


def auto_submit(page: Page) -> bool:
    """Everything filled and nothing needs you: click Submit after a short cancel window. True when confirmed."""
    print(f"\nAll fields filled. Submitting in {SUBMIT_DELAY_S}s - Ctrl+C to cancel and review instead.")
    try:
        page.wait_for_timeout(SUBMIT_DELAY_S * 1000)
    except KeyboardInterrupt:
        print("  (cancelled)")
        return False
    if not click_submit(page):
        print("[warn] No submit button found; click it yourself.")
        return False
    print("  clicked Submit.")
    page.wait_for_timeout(4500)
    live = [pg for pg in page.context.pages if not pg.is_closed()]
    for pg in live:
        try:
            if SUCCESS.search(pg.inner_text("body", timeout=2000)):
                print("[ok] Submitted - the site confirmed the application.")
                return True
        except PlaywrightError:
            pass
    print("[!] Clicked Submit but saw no confirmation (it may need a verification code, a CAPTCHA, or a field fixed).")
    return False


def apply_to(page: Page, url: str, profile: dict, cache: dict) -> None:
    print(f"\n=== {url}")
    try:
        with busy("Loading the application page…"):
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)  # let JS-rendered forms appear
    except PlaywrightError as exc:
        print(f"[error] Could not load page: {str(exc).splitlines()[0]}")
        return
    page.wait_for_timeout(2000)  # let JS-rendered forms appear

    with busy("Closing pop-ups and clicking Apply…"):
        page = open_application_form(page)
    page = settle(page)
    if ACCOUNT_WALL.search(page.url) and adzuna_login(page):
        with busy("Looking for the application form…"):
            page = open_application_form(page)
        page = settle(page)
    while True:
        if AGGREGATORS.search(page.url) and real_fields(page) < 4:
            if ACCOUNT_WALL.search(page.url):
                print(f"[!] {page.url.split('/')[2]} wants you to sign in or register to apply. I never create accounts or enter passwords.")
            else:
                print(f"[!] Still on a job-board page ({page.url[:70]}), not an application form. Not filling anything here.")
            print("    Get to the application form in the browser yourself, then choose [r]; or [n] to skip.")
            blockers = ["not on an application form"]
        else:
            blockers = fill_page(page, profile, cache)
        if AUTO_SUBMIT and not blockers:
            if auto_submit(page):
                return
            blockers = ["submit not confirmed"]
        elif blockers and AUTO_SUBMIT:
            print("\n[!] Not auto-submitting because:")
            for b in blockers[:8]:
                print(f"    - {b}")
        choice = input("\nReview the browser. [Enter/y] submit  [r] rescan/fill again (multi-step forms)  [n] skip: ").strip().lower()
        if choice == "":
            choice = "r" if blockers == ["not on an application form"] else "y"
        if choice == "r":
            with busy("Closing pop-ups and clicking Apply…"):
                page = open_application_form(page.context.pages[-1] if page.is_closed() else page)
            page = settle(page)
            continue
        if choice == "y":
            print("Submitted." if click_submit(page) else "[warn] No submit button found; click it manually.")
            input("Press Enter when finished with this page...")
        else:
            print("Skipped (not submitted).")
        return


def launch_browser(p):
    """Real Google Chrome when available (JOBBY_BROWSER=chrome, the default), else Playwright's test Chromium."""
    if os.getenv("JOBBY_BROWSER", "chrome").lower() == "chrome":
        try:
            return p.chromium.launch(channel="chrome", headless=False)
        except PlaywrightError:
            print("[info] Google Chrome not found - using the bundled Chromium instead.")
    return p.chromium.launch(headless=False)


def run_agent(urls: list[str]) -> None:
    profile = load_json(PROFILE_JSON, None)
    if not profile:
        print("[error] user_profile.json missing. Run the resume parser first.")
        return
    profile = merge_phm(profile)
    cache = load_json(QA_CACHE_JSON, {})
    if not isinstance(cache, dict):
        cache = {}
    with sync_playwright() as p:
        try:
            with busy("Starting the browser…"):
                browser = launch_browser(p)
        except PlaywrightError as exc:
            print(f"[error] Could not launch Chromium (run 'playwright install'): {exc}")
            return
        page = browser.new_page()
        try:
            for url in urls:
                try:
                    apply_to(page, url, profile, cache)
                except BrowserClosed:
                    print("\n[error] The browser window was closed before I finished. Re-run the apply and leave the window open.")
                    return
        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python app_agent.py URL [URL ...]")
    run_agent(sys.argv[1:])
