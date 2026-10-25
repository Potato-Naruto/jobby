"""Jobby - terminal UI for the job application toolkit.

Run:  python jobby.py
"""
import json
import os
import subprocess
import re
import shutil
import sys
import webbrowser
from datetime import datetime, timezone

import questionary
import requests
from questionary import Choice, Style
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from common import BASE_DIR, JOBS_JSON, LLM_MODEL, PROFILE_JSON, QA_CACHE_JSON, RESUME_PDF, load_json, save_json
from app_agent import run_agent as run_fast_agent
from hermes_apply import run_agent as run_hermes_agent
from parse_resume import parse_resume
from search_jobs import (SEEN_JSON, load_seen, reset_seen, LEVELS, COMPANIES_JSON, CORE_LINK_BOARDS, board_links, link_templates, load_catalog, PROFILES_JSON, load_companies, default_filters, load_profiles, parse_date, remote_mode, save_jobs,
                         save_profile, search_jobs)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
console = Console()
ACCENT = "green"
STYLE = Style([("qmark", "fg:ansibrightgreen bold"), ("question", "bold"), ("pointer", "fg:ansibrightgreen bold"),
               ("highlighted", "fg:ansibrightgreen bold"), ("selected", "fg:ansigreen"), ("answer", "fg:ansibrightgreen bold"),
               ("instruction", "fg:#808080")])

LOGO = r"""
     _       _     _
    | | ___ | |__ | |__  _   _
 _  | |/ _ \| '_ \| '_ \| | | |
| |_| | (_) | |_) | |_) | |_| |
 \___/ \___/|_.__/|_.__/ \__, |
                         |___/   """


# --- Small helpers ----------------------------------------------------------
BACK = "__back__"  # questionary turns a Choice(value=None) into its title text, so "Back" needs a real value


def ask(question):
    """Run a questionary prompt; Ctrl-C or choosing Back returns None so every menu can 'go back'."""
    try:
        answer = question.ask()
    except KeyboardInterrupt:
        return None
    return None if answer == BACK else answer


def pause() -> None:
    ask(questionary.press_any_key_to_continue("Press any key to go back...", style=STYLE))


def header(subtitle: str = "") -> None:
    console.clear()
    logo = Text(LOGO, style="bold bright_green")
    tag = Text("AI-powered job search & application assistant", style="dim")
    body = Group(Align.center(logo), Align.center(tag))
    console.print(Panel(body, box=box.DOUBLE, border_style="bright_green", padding=(0, 2)))
    if subtitle:
        console.print(Text(f"  {subtitle}", style=f"bold {ACCENT}"))
        console.print()


_OLLAMA_CACHE: dict = {}


def busy(message: str):
    """Green spinner shown while something slow runs:  with busy('Doing X…'): ..."""
    return console.status(f"[bright_green]{message}[/]", spinner="dots12", spinner_style="bright_green")


def ollama_status() -> tuple[bool, str]:
    """Is Ollama up and the model present? Cached for 30s so redrawing menus stays instant."""
    import time
    hit = _OLLAMA_CACHE.get("v")
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    result = _ollama_status_uncached()
    _OLLAMA_CACHE["v"] = (time.time(), result)
    return result


def _ollama_status_uncached() -> tuple[bool, str]:
    try:
        r = requests.get("http://127.0.0.1:11434/api/tags", timeout=(0.5, 2))
        names = [m["name"] for m in r.json().get("models", [])]
        ok = any(n.split(":latest")[0] == LLM_MODEL for n in names)
        return ok, LLM_MODEL if ok else f"{LLM_MODEL} (model not found)"
    except Exception:
        return False, "Ollama not running"


def dashboard_panel():
    profile = load_json(PROFILE_JSON, None)
    cache = load_json(QA_CACHE_JSON, {})
    jobs = load_json(JOBS_JSON, [])
    profiles = load_profiles()
    ok, model = ollama_status()
    has_keys = bool(os.getenv("ADZUNA_APP_ID") and os.getenv("ADZUNA_APP_KEY"))

    def row(label, good, detail):
        mark = Text("●", style="green") if good else Text("●", style="red")
        return mark, Text(label, style="bold"), Text(str(detail), style="dim" if good else "yellow")

    t = Table.grid(padding=(0, 2))
    for r in (
        row("Resume PDF", RESUME_PDF.exists(), RESUME_PDF.name if RESUME_PDF.exists() else "missing - put resume.pdf here"),
        row("Profile", bool(profile), profile["contact"]["full_name"] if profile else "not parsed yet (menu: Resume)"),
        row("Local AI", ok, model),
        row("Hermes agent", bool(shutil.which("hermes")), "installed" if shutil.which("hermes") else "not found"),
        row("Adzuna keys", has_keys, "configured" if has_keys else "not set (Settings) - boosts local on-site results"),
    ):
        t.add_row(*r)
    stats = Table.grid(padding=(0, 2))
    stats.add_row(Text(str(len(jobs)), style=f"bold {ACCENT}"), Text("jobs from last search"))
    stats.add_row(Text(str(len(profiles)), style=f"bold {ACCENT}"), Text("saved search profiles"))
    stats.add_row(Text(str(len(cache)), style=f"bold {ACCENT}"), Text("remembered answers"))
    grid = Table.grid(expand=True)
    grid.add_column(ratio=3)
    grid.add_column(ratio=2)
    grid.add_row(Panel(t, title="System", border_style="dim", box=box.ROUNDED),
                 Panel(stats, title="Library", border_style="dim", box=box.ROUNDED))
    return grid


def fmt_salary(j: dict) -> str:
    lo, hi = j.get("salary_min"), j.get("salary_max")
    if not (lo or hi):
        return "-"
    k = lambda v: f"${v / 1000:.0f}k"  # noqa: E731
    return k(lo) if not hi or hi == lo else f"{k(lo or hi)}-{k(hi)}" if lo else k(hi)


def fmt_age(j: dict) -> str:
    posted = j.get("posted")
    if not posted:
        return "-"
    d = (datetime.now(timezone.utc) - posted).total_seconds()
    return "new" if d < 3600 else f"{int(d // 3600)}h" if d < 86400 else f"{int(d // 86400)}d"


def short_loc(loc: str, width: int = 26) -> str:
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if len(loc) <= width:
        return loc or "-"
    return f"{loc[:width - 1]}…"


def jobs_table(jobs: list[dict], limit: int = 40) -> Table:
    t = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ACCENT}", expand=True, row_styles=["", "dim"])
    t.add_column("#", justify="right", style="dim", width=3)
    t.add_column("Title", ratio=5, overflow="ellipsis", no_wrap=True)
    t.add_column("Company", ratio=2, overflow="ellipsis", no_wrap=True)
    t.add_column("Location", ratio=3, overflow="ellipsis", no_wrap=True)
    t.add_column("Posted", justify="right", width=6)
    t.add_column("Salary", justify="right", width=9)
    wide = console.width >= 110
    if wide:
        t.add_column("Via", width=9, style="dim")
    for i, j in enumerate(jobs[:limit], 1):
        title = Text(j["title"], style="bold", overflow="ellipsis")
        title.stylize(f"link {j['url']}")
        t.add_row(str(i), title, j["company"] or "-", short_loc(j["location"], 22), fmt_age(j), fmt_salary(j).replace("$", ""), *([j["source"]] if wide else []))
    if len(jobs) > limit:
        t.caption = f"showing {limit} of {len(jobs)} - all are available in the selection lists"
    return t


def load_last_jobs() -> list[dict]:
    jobs = load_json(JOBS_JSON, [])
    for j in jobs:
        j["posted"] = parse_date(j.get("posted"))
    return jobs


# --- Search -----------------------------------------------------------------
DAY_CHOICES = [Choice("Past 24 hours", 1), Choice("Past 3 days", 3), Choice("Past 7 days", 7),
               Choice("Past 14 days", 14), Choice("Past 30 days", 30), Choice("Any time", 0)]


def filters_summary(f: dict) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_row("Keywords", Text(", ".join(f["titles"]) or "-", style=f"bold {ACCENT}"))
    t.add_row("Locations", ", ".join(f["locations"]) or "anywhere")
    t.add_row("Remote", remote_mode(f))
    t.add_row("Posted", f"last {f['days']} day(s)" if f["days"] else "any time")
    sal = f"${f['min_salary']:,}+" + ("" if f["keep_unknown_salary"] else " (unlisted dropped)") if f["min_salary"] else "any"
    t.add_row("Salary", sal)
    t.add_row("Experience", ", ".join(LEVELS[l] for l in f["levels"]) if f.get("levels") else "any")
    if f.get("levels") and f.get("unclear_level", True):
        t.add_row("", Text("(also includes jobs that do not state a level)", style="dim"))
    t.add_row("Exclude", ", ".join(f["exclude_words"]) or "-")
    t.add_row("Results", "only NEW jobs (not shown before)" if f.get("new_only", True) else "all matching jobs, even ones seen before")
    return Panel(t, title="Search filters", border_style=ACCENT, box=box.ROUNDED)


def ask_filters(base: dict) -> dict | None:
    f = {**default_filters(), **base}
    t = ask(questionary.text("Keywords (comma-separated). All words in a phrase must be in the job title:",
                             default=", ".join(f["titles"]), style=STYLE))
    if t is None:
        return None
    f["titles"] = [x.strip() for x in t.split(",") if x.strip()]
    loc = ask(questionary.text("Locations (separate with ;)   e.g.  Plano, TX; Dallas, TX; remote",
                               default="; ".join(f["locations"]), style=STYLE))
    if loc is None:
        return None
    f["locations"] = [x.strip() for x in loc.split(";") if x.strip()]
    mode = ask(questionary.select("Remote jobs?", style=STYLE, choices=[
        Choice("Auto (include only if I searched 'remote')", "auto"), Choice("Include remote jobs", "include"),
        Choice("Only remote jobs", "only"), Choice("Exclude remote jobs", "exclude")],
        default=f["remote"] or "auto"))
    if mode is None:
        return None
    f["remote"] = None if mode == "auto" else mode
    days = ask(questionary.select("Posted within:", choices=DAY_CHOICES, style=STYLE,
                                  default=next((c for c in DAY_CHOICES if c.value == f["days"]), None)))
    if days is None:
        return None
    f["days"] = days
    sal = ask(questionary.text("Minimum annual salary in USD (0 = no minimum):", default=str(f["min_salary"]),
                               validate=lambda v: bool(re.fullmatch(r"[\d,$k ]*", v.lower())) or "numbers only",
                               style=STYLE))
    if sal is None:
        return None
    sal = sal.lower().replace(",", "").replace("$", "").strip()
    f["min_salary"] = int(float(sal[:-1]) * 1000) if sal.endswith("k") and sal[:-1] else int(sal or 0)
    if f["min_salary"]:
        keep = ask(questionary.confirm("Keep jobs that don't list a salary?", default=f["keep_unknown_salary"], style=STYLE))
        f["keep_unknown_salary"] = bool(keep)
    lv = ask(questionary.checkbox("Experience level (none selected = any):", style=STYLE,
                                  choices=[Choice(label, key, checked=key in f.get("levels", [])) for key, label in LEVELS.items()]))
    if lv is None:
        return None
    f["levels"] = lv
    if lv:
        f["unclear_level"] = bool(ask(questionary.confirm(
            "Also include jobs that do not state a level or years of experience?", default=f.get("unclear_level", True), style=STYLE)))
    f["new_only"] = bool(ask(questionary.confirm(
        "Only show NEW jobs (skip ones I have already been shown in earlier searches)?", default=f.get("new_only", True), style=STYLE)))
    ex = ask(questionary.text("Exclude titles containing (comma-separated, optional):",
                              default=", ".join(f["exclude_words"]), style=STYLE))
    f["exclude_words"] = [x.strip() for x in (ex or "").split(",") if x.strip()]
    return f


LAST_LOG: list[str] = []
LOG_STYLES = {">": ("▶", "bold bright_green"), "+": ("✔", "green"), "-": ("✖", "dim red"),
              "!": ("!", "bold yellow"), "i": ("•", "dim green"), " ": (" ", "bold")}


def render_log(lines: list[str], tail: int | None = None, title: str = "Search activity", busy: bool = False):
    shown = lines[-tail:] if tail else lines
    grid = Table.grid(padding=(0, 1))
    for line in shown:
        mark, style = LOG_STYLES.get(line[:1], (" ", "white"))
        grid.add_row(Text(mark, style=style), Text(line[2:] if line[1:2] == " " else line, style=style))
    if busy:
        grid.add_row(Text("…", style="bright_green"), Text("working", style="dim green"))
    return Panel(grid, title=title, border_style="green", box=box.ROUNDED)


def run_search(f: dict) -> list[dict]:
    LAST_LOG.clear()
    with Live(render_log(LAST_LOG, 18, busy=True), console=console, refresh_per_second=8) as live:
        def log(line: str) -> None:
            LAST_LOG.append(line)
            live.update(render_log(LAST_LOG, 18, busy=True))
        jobs = search_jobs(f, log=log)
        live.update(render_log(LAST_LOG, 18))
    save_jobs(jobs)
    return jobs


def run_agent(urls: list[str]) -> None:
    """Apply to URLs. Fast filler = Playwright + one model call. Hermes = full autonomy but very slow on a local model."""
    how = ask(questionary.select("How should I apply?", style=STYLE, choices=[
        Choice("Fast form-filler (recommended): fills from your profile, asks you about unknowns", "fast"),
        Choice("Hermes agent (experimental): visible window, 15-min cap; slow & unreliable on this PC", "hermes"),
        Choice("Cancel", BACK)]))
    if how == "fast":
        run_fast_agent(urls)
    elif how == "hermes":
        run_hermes_agent(urls)


def parse_selection(text: str, n: int) -> list[int]:
    """'1,3,5-7' / 'all' -> sorted 0-based indexes (ignores anything out of range)."""
    text = text.strip().lower()
    if text in ("a", "all", "*"):
        return list(range(n))
    picked: set[int] = set()
    for part in re.split(r"[,\s]+", text):
        if re.fullmatch(r"\d+", part):
            picked.add(int(part) - 1)
        elif re.fullmatch(r"\d+-\d+", part):
            lo, hi = map(int, part.split("-"))
            picked.update(range(lo - 1, hi))
    return sorted(i for i in picked if 0 <= i < n)


def pick_jobs(jobs: list[dict], message: str) -> list[dict]:
    """Show every job with a number; type numbers like '1,3,5-8' or 'all'. Blank cancels."""
    console.print(jobs_table(jobs, limit=len(jobs)))
    while True:
        raw = ask(questionary.text(f"{message}  (numbers like 1,3,5-8 | all | Enter = cancel):", style=STYLE))
        if not raw or not raw.strip():
            console.print("[yellow]Nothing selected.[/]")
            return []
        idx = parse_selection(raw, len(jobs))
        if idx:
            chosen = [jobs[i] for i in idx]
            console.print(f"[green]Selected {len(chosen)} job(s).[/]")
            return chosen
        console.print(f"[yellow]'{raw}' did not match any job number (1-{len(jobs)}). Try again.[/]")


def open_on_boards(f: dict) -> None:
    """Open this search, pre-filled, on job boards that have no public API (they run in your own browser)."""
    names = list(link_templates())
    mode = ask(questionary.select("Which boards?", style=STYLE, choices=[
        Choice("The big four: Indeed, ZipRecruiter, Glassdoor, LinkedIn", "core"),
        Choice(f"Let me pick from all {len(names)} boards", "pick"),
        Choice(f"All {len(names)} boards", "all"), Choice("Back", BACK)]))
    if mode is None:
        return
    if mode == "core":
        chosen = list(CORE_LINK_BOARDS)
    elif mode == "all":
        chosen = names
    else:
        chosen = ask(questionary.checkbox("Boards (space = select, a = all):", style=STYLE,
                                          choices=[Choice(n, n, checked=n in CORE_LINK_BOARDS) for n in names])) or []
    links = board_links(f, chosen)
    if not links:
        console.print("[yellow]No boards selected (in the list, press SPACE to tick a board, then Enter).[/]")
        pause()
        return
    if len(links) > 6 and not ask(questionary.confirm(f"This opens {len(links)} browser tabs. Continue?",
                                                      default=False, style=STYLE)):
        return
    with busy(f"Opening {len(links)} page(s) in your browser…"):
        for board, label, url in links:
            webbrowser.open(url)
            console.print(f"[green]▶[/] {board}: {label}")
    console.print("\n[dim]Browse them in your own browser (log in there if you want). Copy the URLs of jobs you like.[/]")
    raw = ask(questionary.text("Paste job URL(s) to apply to now, separated by spaces (Enter = later):", style=STYLE)) or ""
    urls = [u for u in raw.split() if u.startswith("http")]
    if urls:
        run_agent(urls)
    pause()


def results_menu(jobs: list[dict], f: dict) -> None:
    while True:
        header("Search results")
        console.print(filters_summary(f))
        if jobs:
            console.print(jobs_table(jobs))
        else:
            console.print(Panel("No jobs matched every filter.\nTry: a broader keyword, a wider date range, "
                                "fewer locations, or search again including jobs you have already seen.", border_style="yellow"))
        action = ask(questionary.select("What next?", style=STYLE, choices=[
            Choice("Apply to selected jobs", "apply", disabled=None if jobs else "no results"),
            Choice("Open jobs in my browser", "open", disabled=None if jobs else "no results"),
            Choice("Show the full search log (what each board returned & why jobs were dropped)", "log"),
            Choice("Also search other job boards: Indeed, Glassdoor, LinkedIn & 80+ more (opens your browser)", "boards"),
            Choice("Search again INCLUDING jobs I have already seen", "all"),
            Choice("Refine filters and search again", "refine"),
            Choice("Save these filters as a search profile", "save"),
            Choice("Back to main menu", "back")]))
        if action in (None, "back"):
            return
        if action == "apply":
            sel = pick_jobs(jobs, "Which jobs should I apply to?")
            if sel:
                run_agent([j["url"] for j in sel])
            pause()
        elif action == "open":
            sel = pick_jobs(jobs, "Which jobs should I open?")
            for j in sel:
                webbrowser.open(j["url"])
            if sel:
                console.print(f"[green]Opened {len(sel)} job(s) in your browser.[/]")
            pause()
        elif action == "boards":
            open_on_boards(f)
        elif action == "log":
            header("Search log")
            console.print(render_log(LAST_LOG, title="Everything the search did"))
            pause()
        elif action == "all":
            f = {**f, "new_only": False}
            jobs = run_search(f)
        elif action == "refine":
            new = ask_filters(f)
            if new:
                f = new
                jobs = run_search(f)
        elif action == "save":
            name = ask(questionary.text("Profile name:", style=STYLE))
            if name:
                save_profile(name.strip(), f)
                console.print(f"[green]Saved search profile '{name.strip()}'.[/]")
                pause()


def search_flow() -> None:
    header("Search jobs")
    profiles = load_profiles()
    f = None
    if profiles:
        choices = [Choice(f"{n}  -  {', '.join(p['titles'])[:30]} in {', '.join(p['locations'])[:25] or 'anywhere'}", n)
                   for n, p in profiles.items()]
        choices += [questionary.Separator(), Choice("New search", "__new__")]
        pick = ask(questionary.select("Start from a saved search profile?", choices=choices, style=STYLE))
        if pick is None:
            return
        if pick != "__new__":
            f = {**default_filters(), **profiles[pick]}
            console.print(filters_summary(f))
            if ask(questionary.confirm("Run it as-is?", default=True, style=STYLE)) is False:
                f = ask_filters(f)
    if f is None:
        f = ask_filters(default_filters())
    if not f or not f["titles"]:
        return
    jobs = run_search(f)
    pause()
    results_menu(jobs, f)


def manage_profiles() -> None:
    while True:
        header("Search profiles")
        profiles = load_profiles()
        if not profiles:
            console.print(Panel("No saved search profiles yet. Run a search, then choose 'Save these filters'.",
                                border_style="yellow"))
            pause()
            return
        for n, p in profiles.items():
            console.print(Panel(filters_summary({**default_filters(), **p}).renderable, title=n, border_style="dim"))
        pick = ask(questionary.select("Manage:", style=STYLE, choices=[
            *[Choice(f"Delete '{n}'", n) for n in profiles], questionary.Separator(), Choice("Back", BACK)]))
        if pick is None:
            return
        if ask(questionary.confirm(f"Delete '{pick}'?", default=False, style=STYLE)):
            profiles.pop(pick)
            save_json(PROFILES_JSON, profiles)


# --- Apply ------------------------------------------------------------------
def apply_flow() -> None:
    header("Apply to jobs")
    console.print(Panel("A browser opens, the form is filled from your profile, you are asked in this terminal "
                        "about anything unknown (it is remembered next time), and nothing is submitted without your OK.",
                        border_style="dim", box=box.ROUNDED))
    jobs = load_last_jobs()
    src = ask(questionary.select("Which jobs?", style=STYLE, choices=[
        Choice(f"Pick from my last search ({len(jobs)} jobs)", "last", disabled=None if jobs else "run a search first"),
        Choice("Paste job application URL(s)", "paste"), Choice("Back", BACK)]))
    urls: list[str] = []
    if src == "last":
        urls = [j["url"] for j in pick_jobs(jobs, "Which jobs should I apply to?")]
    elif src == "paste":
        raw = ask(questionary.text("URL(s), separated by spaces:", style=STYLE)) or ""
        urls = raw.split()
    if urls:
        console.print(f"\n[{ACCENT}]Starting {len(urls)} application(s).[/]\n")
        run_agent(urls)
        pause()


# --- Resume / profile -------------------------------------------------------
def show_profile() -> None:
    p = load_json(PROFILE_JSON, None)
    if not p:
        console.print(Panel("No profile yet. Choose 'Parse resume' first.", border_style="yellow"))
        return
    c = p["contact"]
    info = Table.grid(padding=(0, 2))
    for k in ("full_name", "email", "phone", "location", "linkedin", "github", "website"):
        if c.get(k):
            info.add_row(Text(k.replace("_", " ").title(), style="dim"), c[k])
    console.print(Panel(info, title="Contact", border_style=ACCENT, box=box.ROUNDED))
    exp = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ACCENT}", expand=True)
    for col in ("Company", "Title", "Dates"):
        exp.add_column(col)
    for e in p["work_experience"]:
        exp.add_row(e["company"], e["title"], f"{e['start_date']} - {e['end_date']}")
    console.print(Panel(exp, title="Experience", border_style="dim", box=box.ROUNDED))
    edu = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ACCENT}", expand=True)
    for col in ("Institution", "Degree", "Field", "Dates"):
        edu.add_column(col)
    for e in p["education"]:
        edu.add_row(e["institution"], e["degree"], e["field_of_study"], f"{e['start_date']} - {e['end_date']}")
    console.print(Panel(edu, title="Education", border_style="dim", box=box.ROUNDED))
    if p["skills"]:
        console.print(Panel(Text(" · ".join(p["skills"])), title="Skills", border_style="dim", box=box.ROUNDED))
    if p.get("certifications"):
        console.print(Panel("\n".join(f"{c['name']} {('- ' + c['issuer']) if c['issuer'] else ''}"
                                      for c in p["certifications"]), title="Certifications", border_style="dim"))


def resume_flow() -> None:
    while True:
        header("Resume profile")
        action = ask(questionary.select("Resume:", style=STYLE, choices=[
            Choice("View my parsed profile", "view"), Choice("Parse / re-parse resume.pdf (takes a few minutes)", "parse"),
            Choice("Back", BACK)]))
        if action is None:
            return
        if action == "view":
            header("Resume profile")
            show_profile()
        else:
            console.print(f"[{ACCENT}]Parsing with {LLM_MODEL}… (this can take several minutes)[/]")
            with busy("Reading your resume with the local AI (several minutes)…"):
                parse_resume()
        pause()


# --- Remembered answers -----------------------------------------------------
def answers_flow() -> None:
    while True:
        header("Remembered answers")
        cache = load_json(QA_CACHE_JSON, {})
        if not cache:
            console.print(Panel("Nothing yet. Answers you give Hermes during applications are saved here.",
                                border_style="yellow"))
            pause()
            return
        t = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ACCENT}", expand=True)
        t.add_column("Question", ratio=3)
        t.add_column("Answer", ratio=2)
        for q, a in cache.items():
            t.add_row(q, str(a))
        console.print(t)
        pick = ask(questionary.select("Manage:", style=STYLE, choices=[
            Choice("Edit an answer", "edit"), Choice("Delete an answer", "del"), Choice("Back", BACK)]))
        if pick is None:
            return
        key = ask(questionary.select("Which one?", choices=list(cache), style=STYLE))
        if key is None:
            continue
        if pick == "edit":
            new = ask(questionary.text("New answer:", default=str(cache[key]), style=STYLE))
            if new is not None:
                cache[key] = new
        else:
            cache.pop(key)
        save_json(QA_CACHE_JSON, cache)


# --- Settings ---------------------------------------------------------------
def write_env(updates: dict) -> None:
    env = BASE_DIR / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [l for l in lines if l.split("=", 1)[0].strip() not in updates]
    lines += [f"{k}={v}" for k, v in updates.items()]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ.update(updates)


KEYED_BOARDS = {
    "Adzuna (jobs everywhere, best for local on-site)": ("https://developer.adzuna.com/signup",
                                                          [("ADZUNA_APP_ID", "Adzuna app id", False),
                                                           ("ADZUNA_APP_KEY", "Adzuna app key", True)]),
    "Jooble (huge aggregator)": ("https://jooble.org/api/about", [("JOOBLE_API_KEY", "Jooble API key", True)]),
    "USAJobs (US federal jobs)": ("https://developer.usajobs.gov/APIRequest/Index",
                                  [("USAJOBS_API_KEY", "USAJobs API key", True),
                                   ("USAJOBS_EMAIL", "Email you registered with USAJobs", False)]),
}


def prompt_board_keys(pick: str) -> bool:
    """Ask for a board's credentials and save them to .env. Returns True if saved."""
    url, fields = KEYED_BOARDS[pick]
    if ask(questionary.confirm(f"Open the signup page in your browser first? ({url})", default=False, style=STYLE)):
        webbrowser.open(url)
    updates = {}
    for env, label, secret in fields:
        current = os.getenv(env, "")
        q = (questionary.password if secret else questionary.text)(
            f"{label}:" + (" (Enter keeps current)" if current else ""), **({} if secret else {"default": current}),
            style=STYLE)
        val = ask(q)
        if val is None:
            return False
        updates[env] = val.strip() or current
    write_env({k: v for k, v in updates.items() if v})
    return True


def board_status() -> Table:
    t = Table.grid(padding=(0, 2))
    for name, (_, fields) in KEYED_BOARDS.items():
        ok = all(os.getenv(k) for k, _, _ in fields)
        t.add_row(Text("●", style="green" if ok else "red"), name, Text("ready" if ok else "needs key", style="dim"))
    for name in ("The Muse", "Remotive", "RemoteOK", "Jobicy", "We Work Remotely", "Himalayas"):
        t.add_row(Text("●", style="green"), name, Text("ready (no key needed)", style="dim"))
    comps = load_companies()
    n = sum(len(v) for v in comps.values())
    t.add_row(Text("●", style="green"), "Company career pages",
              Text(f"{n} companies (Greenhouse {len(comps['greenhouse'])}, Lever {len(comps['lever'])}, "
                   f"Ashby {len(comps['ashby'])})", style="dim"))
    t.add_row(Text("●", style="yellow"), "Browser-link boards",
              Text(f"{len(link_templates())} boards open pre-filled in your browser (no API)", style="dim"))
    return t


def company_flow() -> None:
    """Watch a company's Greenhouse/Lever career page (the token is the name in its jobs URL)."""
    comps = load_json(COMPANIES_JSON, {})
    kind = ask(questionary.select("Add a company on which system?", style=STYLE, choices=[
        Choice("Greenhouse  (boards.greenhouse.io/<name>)", "greenhouse"),
        Choice("Lever  (jobs.lever.co/<name>)", "lever"), Choice("Ashby  (jobs.ashbyhq.com/<name>)", "ashby"),
        Choice("Remove one of my added companies", "remove"), Choice("Back", BACK)]))
    if kind is None:
        return
    if kind == "remove":
        opts = [Choice(f"{k}: {c}", (k, c)) for k in ("greenhouse", "lever", "ashby") for c in comps.get(k, [])]
        pick = ask(questionary.select("Remove:", choices=opts, style=STYLE)) if opts else None
        if pick:
            comps[pick[0]].remove(pick[1])
    else:
        name = ask(questionary.text("Company name as it appears in its careers URL (e.g. 'stripe'):", style=STYLE))
        if name:
            comps.setdefault(kind, [])
            if name.strip().lower() not in comps[kind]:
                comps[kind].append(name.strip().lower())
    save_json(COMPANIES_JSON, comps)


def link_phm() -> None:
    """Pull your targets and verified company boards from please-hire-me into Jobby (read-only)."""
    import phm
    from search_jobs import COMPANIES_JSON, load_profiles, save_profile
    header("Link please-hire-me")
    if not phm.available():
        console.print(Panel(f"Nothing found at {phm.PHM_DIR}. Set PHM_DIR in .env if it lives elsewhere.", border_style="yellow"))
        return
    rec = phm.applied()
    boards = phm.board_slugs()
    f = phm.search_filters()
    info = Table.grid(padding=(0, 2))
    info.add_row(Text("Folder", style="dim"), str(phm.PHM_DIR))
    info.add_row(Text("Already applied", style="dim"), f"{len(rec['ids'])} postings across {len(rec['companies'])} companies (these are skipped in searches and applies)")
    info.add_row(Text("Your targets", style="dim"), f"{', '.join(f['titles'])} | {', '.join(f['locations']) or 'anywhere'} | ${f['min_salary']:,}+ | {', '.join(f['levels'])}")
    info.add_row(Text("Verified boards", style="dim"), ", ".join(f"{len(v)} {k}" for k, v in boards.items()))
    console.print(Panel(info, title="please-hire-me", border_style=ACCENT, box=box.ROUNDED))
    if not ask(questionary.confirm("Save your targets as the search profile 'please-hire-me targets' and add its boards to the company watchlist?",
                                   default=True, style=STYLE)):
        return
    save_profile("please-hire-me targets", {**default_filters(), **f, "new_only": True})
    mine = load_json(COMPANIES_JSON, {})
    added = 0
    for system, slugs in boards.items():
        have = set(mine.get(system, []))
        added += len(set(slugs) - have)
        mine[system] = sorted(have | set(slugs))
    save_json(COMPANIES_JSON, mine)
    console.print(f"[green]Saved the profile and added {added} company boards.[/] Use it from 'Saved search profiles'.")
    pause()


def settings_flow() -> None:
    while True:
        header("Settings - job boards")
        console.print(Panel(board_status(), title="Boards", border_style="green", box=box.ROUNDED))
        console.print(Panel(f"Model: {LLM_MODEL}\nFolder: {BASE_DIR}", border_style="dim", box=box.ROUNDED))
        pick = ask(questionary.select("Settings:", style=STYLE, choices=[
            *[Choice(f"Set keys: {n}", n) for n in KEYED_BOARDS],
            Choice(f"Browser for auto-fill: {os.getenv('JOBBY_BROWSER', 'chrome')} (change)", "__br__"),
            Choice(f"Forget which jobs I have seen ({len(load_seen())} remembered)", "__seen__"),
            Choice("Link please-hire-me (targets, boards, applied history)", "__phm__"),
            Choice("Watch specific companies' career pages", "__co__"), Choice("Back", BACK)]))
        if pick is None:
            return
        if pick == "__co__":
            company_flow()
            continue
        if pick == "__phm__":
            link_phm()
            continue
        if pick == "__seen__":
            if ask(questionary.confirm("Clear the list of jobs already shown? Old postings will count as new again.",
                                       default=False, style=STYLE)):
                reset_seen()
                console.print("[green]Cleared.[/]")
                pause()
            continue
        if pick == "__br__":
            b = ask(questionary.select("Browser used to fill applications:", style=STYLE, choices=[
                Choice("Google Chrome (recommended; falls back to Chromium if missing)", "chrome"),
                Choice("Chromium (Playwright's test browser)", "chromium"), Choice("Back", BACK)]))
            if b:
                write_env({"JOBBY_BROWSER": b})
            continue
        prompt_board_keys(pick)


# --- LinkedIn outreach ------------------------------------------------------
def outreach_table(items: list[dict]) -> Table:
    import outreach
    t = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ACCENT}", expand=True)
    for col in ("#", "Name", "Company", "Archetype", "Status", "Follow up"):
        t.add_column(col)
    due = {d["id"] for d in outreach.due_followups(items)}
    for n, i in enumerate(items, 1):
        t.add_row(str(n), i["name"], i["company"], outreach.ARCHETYPES[i["archetype"]]["name"], i["status"],
                  ("[bold yellow]due now[/]" if i["id"] in due else i.get("followup_due", "")))
    return t


def review_contact(c: dict, items: list[dict]) -> None:
    """Show the drafts for one person; copy, edit, redraft, open their profile, mark progress."""
    import outreach
    while True:
        header("Outreach")
        a = outreach.ARCHETYPES[c["archetype"]]
        console.print(Panel(f"[bold]{c['name']}[/]  {c['title']} at {c['company']}\n{c['url']}\n\n"
                            f"Archetype: [bold {ACCENT}]{a['name']}[/] ({c['why']})\n[dim]{a['who']}. Tone: {a['tone']}.[/]",
                            border_style=ACCENT, box=box.ROUNDED))
        console.print(Panel(c["connection_note"], title=f"Connection note ({len(c['connection_note'])}/{outreach.NOTE_LIMIT})",
                            border_style="green", box=box.ROUNDED))
        console.print(Panel(c["follow_up"], title=f"Follow-up after they accept ({len(c['follow_up'])}/{outreach.FOLLOWUP_LIMIT})",
                            border_style="dim", box=box.ROUNDED))
        console.print(f"[dim]Status: {c['status']}  |  {c.get('how', '')}[/]")
        act = ask(questionary.select("What next?", style=STYLE, choices=[
            Choice("Copy the connection note and open their profile (you paste and send it)", "send"),
            Choice("Copy the follow-up message", "follow"),
            Choice("Edit the connection note", "edit"), Choice("Change archetype and redraft", "arch"),
            Choice("Redraft", "redo"), Choice("Mark: connection sent", "sent"), Choice("Mark: they accepted", "accepted"),
            Choice("Mark: follow-up sent", "followed_up"), Choice("Mark: they replied", "replied"),
            Choice("Skip this person", "skipped"), Choice("Back", BACK)]))
        if act is None:
            break
        if act == "send":
            ok = outreach.copy_to_clipboard(c["connection_note"])
            webbrowser.open(c["url"])
            console.print("[green]Note copied.[/] On their profile: Connect > Add a note > paste > Send."
                          if ok else "[yellow]Could not copy; select the note above by hand.[/]")
            if ask(questionary.confirm("Did you send it?", default=True, style=STYLE)):
                outreach.mark(c, "sent")
        elif act == "follow":
            console.print("[green]Copied.[/]" if outreach.copy_to_clipboard(c["follow_up"]) else "[yellow]Could not copy.[/]")
            pause()
        elif act == "edit":
            new = ask(questionary.text("Connection note:", default=c["connection_note"], style=STYLE))
            if new and len(new) <= outreach.NOTE_LIMIT + 20:
                c["connection_note"] = new.strip()
            elif new:
                console.print(f"[yellow]Too long for LinkedIn ({len(new)} characters; the limit is 300).[/]")
                pause()
        elif act in ("arch", "redo"):
            if act == "arch":
                pick = ask(questionary.select("Archetype:", style=STYLE, choices=[
                    Choice(f"{v['name']}: {v['who']}", k) for k, v in outreach.ARCHETYPES.items()] + [Choice("Back", BACK)]))
                if not pick:
                    continue
                c["archetype"], c["why"] = pick, "chosen by you"
            with busy("Writing the messages with the local AI (up to a minute)…"):
                d, c["how"] = outreach.draft(c, c["archetype"])
            c.update(d)
        else:
            outreach.mark(c, act)
        outreach.save_all(items)


def add_contact(items: list[dict]) -> None:
    import outreach
    header("Add a hiring manager")
    console.print(Panel("Open their LinkedIn profile yourself, then copy what you see. Jobby never logs in to LinkedIn "
                        "or sends anything; you paste the note into LinkedIn.", border_style="dim", box=box.ROUNDED))
    url = ask(questionary.text("LinkedIn profile URL:", style=STYLE))
    if not url:
        return
    dup = outreach.find_existing(url, items)
    if dup:
        console.print(f"[yellow]Already in your list: {dup['name']} ({dup['status']}). Opening it.[/]")
        pause()
        review_contact(dup, items)
        return
    name = ask(questionary.text("Their name:", style=STYLE))
    title = ask(questionary.text("Their headline / title (as shown under their name):", style=STYLE))
    company = ask(questionary.text("Company:", style=STYLE))
    if not (name and company):
        return
    jobs = load_last_jobs()
    role = ""
    if jobs:
        mine = [j for j in jobs if company.lower()[:6] in j["company"].lower()]
        if mine:
            role = mine[0]["title"]
    role = ask(questionary.text("Role you applied / want to apply for (Enter to leave blank):", default=role, style=STYLE)) or ""
    notes = ask(questionary.text("Anything shared or notable from their profile (school, org, past employer; optional):", style=STYLE)) or ""
    c = outreach.new_contact(name, title or "", company, url, role, notes)
    with busy("Writing the messages with the local AI (up to a minute)…"):
        d, c["how"] = outreach.draft(c, c["archetype"])
    c.update(d)
    items.append(c)
    outreach.save_all(items)
    review_contact(c, items)


def web_find_people(company: str, role: str) -> None:
    """Search the web for hiring managers at one company; show clickable profile links and a draft note each."""
    import outreach
    with busy(f"Searching the web for people at {company}…"):
        res = outreach.find_hiring_managers(company, role)
    people = res["people"]
    console.print(f"[dim]Searched via {res['via']}[/]")
    if not people:
        console.print("[yellow]No profiles found.[/] Search engines index few LinkedIn profiles. "
                      "Add SERPER_API_KEY or BRAVE_API_KEY to .env for much better results, or use LinkedIn directly:")
        console.print(f"[link={res['fallback']}]{res['fallback']}[/link]")
        pause()
        return
    for p in people:
        p["company"], p["role"] = company, role
    people_table_and_open(people)


def people_table_and_open(people: list[dict], show_company: bool = False) -> None:
    """Show ranked people with clickable links; typing a number copies the note and opens the profile in Chrome."""
    import outreach
    t = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ACCENT}", expand=True)
    for col in ("#", "Name (click)", "Title", *(("Company",) if show_company else ()), "Fit", "Type"):
        t.add_column(col)
    for n, p in enumerate(people, 1):
        t.add_row(str(n), f"[link={p['url']}]{p['name']}[/link]" + (" [dim](tracked)[/]" if p["tracked"] else ""),
                  p["title"][:55], *((p["company"][:22],) if show_company else ()), str(p["score"]),
                  outreach.ARCHETYPES[p["archetype"]]["name"])
    console.print(t)
    for n, p in enumerate(people, 1):
        console.print(f"[dim]{n}.[/] {p['url']}")
    console.print("[dim]Type a number: the connection note is copied and their profile opens in Chrome. Paste (Ctrl+V) into Connect > Add a note.[/]")
    while True:
        raw = ask(questionary.text("Open which number? (Enter = back):", style=STYLE))
        if not (raw and raw.strip().isdigit() and 1 <= int(raw) <= len(people)):
            return
        p = people[int(raw) - 1]
        ok = outreach.copy_to_clipboard(p["message"])
        open_in_chrome(p["url"])
        console.print(f"[green]Copied note for {p['name']}[/] and opened their profile." if ok
                      else f"[yellow]Opened profile but could not copy; note:[/] {p['message']}")
        console.print(f"LinkedIn: [link={p['url']}]{p['url']}[/link]")
        items = outreach.load_all()
        if not outreach.find_existing(p["url"], items):
            c = outreach.new_contact(p["name"], p["title"], p["company"], p["url"], p["role"], p["snippet"])
            c["connection_note"] = p["message"]
            outreach.save_all(items + [c])


def sift_all_companies(jobs: list[dict]) -> None:
    """Search every company in the last job search, pool the hiring managers and rank them together."""
    import outreach
    pairs = {}
    for j in jobs:
        pairs.setdefault(j["company"], j["title"])
    raw = ask(questionary.text(f"How many companies to search? ({len(pairs)} available, 1 search credit each; default 15):", style=STYLE))
    n = int(raw) if raw and raw.strip().isdigit() else 15
    pool = []
    from concurrent.futures import ThreadPoolExecutor
    def one(item):
        co, role = item
        try:
            res = outreach.find_hiring_managers(co, role, limit=3)
        except Exception:
            return []
        for p in res["people"]:
            p["company"], p["role"] = co, role
        return res["people"]
    chosen = list(pairs.items())[:n]
    with busy(f"Sifting hiring managers across {len(chosen)} companies in parallel…"):
        with ThreadPoolExecutor(max_workers=min(8, len(chosen) or 1)) as ex:
            for r in ex.map(one, chosen):
                pool += r
    seen, people = set(), []
    for p in sorted(pool, key=lambda x: -x["score"]):
        if p["url"] not in seen:
            seen.add(p["url"])
            people.append(p)
    if not people:
        console.print("[yellow]No profiles found.[/]")
        pause()
        return
    people_table_and_open(people[:40], show_company=True)


def open_in_chrome(url: str) -> None:
    """Open a URL in Chrome (your normal profile, so you're already logged in to LinkedIn); fall back to the default browser."""
    for exe in (os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")):
        if os.path.exists(exe):
            subprocess.Popen([exe, url])
            return
    webbrowser.open(url)


def find_people() -> None:
    """Open LinkedIn people searches (in your own browser) for the companies in your last job search."""
    import outreach
    header("Find hiring managers")
    jobs = load_last_jobs()
    options = {}
    for j in jobs:
        options.setdefault(j["company"], j["title"])
    choices = [Choice(f"{co}  ({t[:40]})", (co, t)) for co, t in list(options.items())[:40]]
    choices = [Choice("Sift ALL companies at once (ranked hiring managers across every role)", "all")] + choices
    choices += [Choice("Type a company myself", "typed"), Choice("Back", BACK)]
    pick = ask(questionary.select("Which company?", style=STYLE, choices=choices))
    if pick is None:
        return
    if pick == "all":
        sift_all_companies(jobs)
        return
    if pick == "typed":
        co = ask(questionary.text("Company:", style=STYLE))
        if not co:
            return
        pick = (co, "")
    mode = ask(questionary.select("How?", style=STYLE, choices=[
        Choice("Search the web for them and list clickable LinkedIn profiles", "web"),
        Choice("Just open LinkedIn people searches in my browser", "linkedin")]))
    if mode is None:
        return
    if mode == "web":
        web_find_people(*pick)
        return
    kinds = ask(questionary.checkbox("Search for:", style=STYLE, choices=[
        Choice("Hiring managers", "hiring", checked=True), Choice("University / early-career recruiters", "recruiter", checked=True),
        Choice("Team managers in my field", "team")])) or []
    for k in kinds:
        webbrowser.open(outreach.people_search_url(pick[0], pick[1], k))
        console.print(f"[green]▶[/] LinkedIn people search: {pick[0]} - {k}")
    console.print("\n[dim]Pick a few people whose role fits, copy their profile URL, then use 'Add a hiring manager'.[/]")
    pause()


def job_link_contacts() -> None:
    """Paste a job link: chart of hiring people + teammates on LinkedIn with a ready 5-minute-chat note each."""
    import job_contacts
    header("Contacts for a job link")
    url = ask(questionary.text("Job link (Enter = back):", style=STYLE))
    if not url or not url.strip():
        return
    company = role = ""
    try:
        with busy("Finding the hiring team and teammates…"):
            job_contacts.run(url.strip(), open_page=False, use_ai=False)
    except SystemExit as exc:   # company unreadable or no search key
        console.print(f"[yellow]{exc}[/]")
        company = ask(questionary.text("Company name:", style=STYLE))
        if not company:
            return
        role = ask(questionary.text("Role title:", style=STYLE)) or ""
        with busy("Finding the hiring team and teammates…"):
            job_contacts.run(url.strip(), company, role, use_ai=False, open_page=False)
    webbrowser.open(job_contacts.OUT_HTML.as_uri())
    for pdf in sorted((job_contacts.BASE_DIR / "resumes").glob("*.pdf"), key=lambda f: f.stat().st_mtime)[-1:]:
        webbrowser.open(pdf.as_uri())   # the resume just made
    console.print(f"[dim]Tailored resumes are saved in {job_contacts.BASE_DIR / 'resumes'}[/]")
    pause()


def outreach_flow() -> None:
    import outreach
    while True:
        header("LinkedIn outreach")
        items = outreach.load_all()
        due = outreach.due_followups(items)
        week = outreach.sent_this_week(items)
        console.print(Panel(f"{len(items)} people tracked  |  {week} invites sent in the last 7 days"
                            + (f"  |  [bold yellow]{len(due)} follow-up(s) due[/]" if due else "")
                            + f"\n[dim]Four archetypes: " + ", ".join(v["name"] for v in outreach.ARCHETYPES.values())
                            + ". Drafts only; you send them yourself.[/]", border_style=ACCENT, box=box.ROUNDED))
        if week >= outreach.WEEKLY_SOFT_LIMIT:
            console.print("[yellow]That is a lot of invites for one week. LinkedIn restricts accounts that send too many; slow down.[/]")
        act = ask(questionary.select("Outreach:", style=STYLE, choices=[
            Choice("Add a hiring manager / recruiter and draft messages", "add"),
            Choice("Find people to contact (opens LinkedIn searches)", "find"),
            Choice(f"My outreach list ({len(items)})", "list", disabled=None if items else "empty"),
            Choice(f"Follow-ups due ({len(due)})", "due", disabled=None if due else "none due"),
            Choice("Back", BACK)]))
        if act is None:
            return
        if act == "add":
            add_contact(items)
        elif act == "find":
            find_people()
        else:
            pool = due if act == "due" else items
            console.print(outreach_table(pool))
            raw = ask(questionary.text("Open which number? (Enter = back):", style=STYLE))
            if raw and raw.strip().isdigit() and 1 <= int(raw) <= len(pool):
                review_contact(pool[int(raw) - 1], items)


# --- Main -------------------------------------------------------------------
def stop_ollama() -> None:
    """Kill Ollama (tray app first so it can't respawn the server) when Jobby quits."""
    import subprocess
    cmds = ([["taskkill", "/F", "/T", "/IM", n] for n in ("ollama app.exe", "ollama.exe")]
            if os.name == "nt" else [["pkill", "-f", "ollama"]])
    for c in cmds:
        try:
            subprocess.run(c, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
    console.print("[dim]Stopped Ollama.[/]")


def main() -> None:
    actions = {"search": search_flow, "apply": apply_flow, "resume": resume_flow,
               "profiles": manage_profiles, "answers": answers_flow, "settings": settings_flow,
               "outreach": outreach_flow, "joblink": job_link_contacts}
    while True:
        header()
        with busy("Checking your setup…"):
            panel = dashboard_panel()
        console.print(panel)
        console.print()
        choice = ask(questionary.select("What would you like to do?", style=STYLE, choices=[
            Choice("Search jobs", "search"), Choice("Apply to jobs (auto-fill in a real browser)", "apply"),
            Choice("LinkedIn outreach (hiring-manager messages)", "outreach"),
            Choice("Job link: contacts chart + tailored resume", "joblink"),
            Choice("Resume profile", "resume"), Choice("Saved search profiles", "profiles"),
            Choice("Remembered answers", "answers"), Choice("Settings", "settings"),
            questionary.Separator(), Choice("Quit", "quit")]))
        if choice in (None, "quit"):
            console.print(f"[{ACCENT}]Good luck out there![/]")
            return
        try:
            actions[choice]()
        except KeyboardInterrupt:
            pass
        except Exception as exc:  # keep the UI alive
            console.print(Panel(f"{type(exc).__name__}: {exc}", title="Error", border_style="red"))
            pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[bright_green]Bye![/]")
    finally:
        stop_ollama()
