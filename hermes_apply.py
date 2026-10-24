"""Let Hermes Agent drive the browser to fill out (and, after your OK, submit) a job application.

Hermes gets a brief built from your please-hire-me setup: the playbook (HERMES_PLAYBOOK.md), your confirmed
profile, the preset legal/EEO/situational answers and your fact sheet. Before it starts, postings you already
applied to are skipped. When it finishes, the result is logged in logs/applications-log.md and applications/.

Usage:
    python hermes_apply.py URL [URL ...]
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import phm
from common import BASE_DIR, JOBS_JSON, PROFILE_JSON, QA_CACHE_JSON, RESUME_PDF, load_json, save_json

TOOLSETS = "browser,terminal,file,clarify,todo"
RUN_BUDGET_S = 900   # hard stop after 15 minutes - a local model can otherwise thrash for a very long time
MAX_TURNS = 60
PLAYBOOK = BASE_DIR / "HERMES_PLAYBOOK.md"
RESULTS_DIR = BASE_DIR / "state" / "hermes_results"
BRIEFS_DIR = BASE_DIR / "state" / "briefs"

TASK = """You are my job-application agent. Use your browser tools (the window is visible to me) to fully
control the browser and complete the application at this URL:

  {url}
{meta}
FILES (absolute paths): resume to upload = {resume}   |   saved-answers cache (JSON, key = lowercase question) = {cache}
When I answer a question you asked, add it to the cache file so it is remembered.
Write your final result JSON to: {result}

{playbook}

# PROFILE (verbatim values, my confirmed facts)
{profile}

{presets}

# FACT SHEET (the only facts allowed in free-text answers)
{facts}
"""


def _job_for(url: str) -> dict:
    for j in load_json(JOBS_JSON, []) or []:
        if isinstance(j, dict) and j.get("url") == url:
            return j
    return {}


def build_task(url: str, job: dict, result: Path) -> str:
    if phm.available():
        profile = json.dumps(phm.profile(), indent=0)
        presets, facts = phm.presets_text(), phm.facts_text()
        resume = phm.resume_path() or RESUME_PDF
    else:
        profile = json.dumps(load_json(PROFILE_JSON, {}), indent=0)
        presets = facts = ""
        resume = RESUME_PDF
    meta = f"  (posting: {job.get('title')} at {job.get('company')}, {job.get('location')})\n" if job else ""
    return TASK.format(url=url, meta=meta, resume=resume, cache=QA_CACHE_JSON, result=result,
                       playbook=PLAYBOOK.read_text(encoding="utf-8") if PLAYBOOK.exists() else "",
                       profile=profile, presets=presets, facts=facts)


def record_result(url: str, job: dict, result: Path) -> None:
    data = {}
    try:
        data = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if not data.get("status"):
        print("\nHermes did not write its result file. What happened?")
        pick = input("  [s] submitted  [h] needs me / left open  [k] skipped  [n] not submitted (Enter = don't log): ").strip().lower()
        status = {"s": "SUBMITTED", "h": "NEEDS_HUMAN", "k": "SKIPPED", "n": "NOT_SUBMITTED"}.get(pick)
        if not status:
            return
        data["status"] = status
    company = data.get("company") or job.get("company") or re.sub(r"^www\.", "", url.split("/")[2])
    role = data.get("role") or job.get("title") or "application"
    phm.log_application(company, role, url, data["status"].replace("_", " "), data.get("reason", ""))
    qa = data.get("qa")
    if qa:
        doc = phm.JOBBY_APPLICATIONS / f"{re.sub('[^a-z0-9]+', '', company.lower())}-{re.sub('[^a-z0-9]+', '', role.lower())[:40]}.md"
        with doc.open("a", encoding="utf-8") as fh:
            fh.write("\n## Questions and answers\n" + "\n".join(f"- {q}: {a}" for q, a in qa) + "\n")
    print(f"Logged: {company} - {role} - {data['status']}")


def run_agent(urls: list[str]) -> None:
    if not shutil.which("hermes"):
        print("[error] 'hermes' not found on PATH.")
        return
    if not phm.available() and not load_json(PROFILE_JSON, None):
        print("[error] No profile found. Parse your resume first (menu option 1) or set PHM_DIR to please-hire-me.")
        return
    if not QA_CACHE_JSON.exists():
        save_json(QA_CACHE_JSON, {})
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    record = phm.applied() if phm.available() else None

    for url in urls:
        job = _job_for(url)
        if record and (why := phm.already_applied({"url": url, "company": job.get("company", "")}, record)):
            print(f"\n=== Skipping {url[:80]}: {why}")
            continue
        result = RESULTS_DIR / f"{uuid.uuid4().hex[:8]}.json"
        BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
        brief = BRIEFS_DIR / f"{result.stem}.md"
        brief.write_text(build_task(url, job, result), encoding="utf-8")      # long context lives in a file, not the chat
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(f"Read the file {brief} with your file tools, then do exactly what it says: complete the job application "
                    f"at {url} in the visible browser. Follow its rules strictly.")
            qfile = f.name
        print(f"\n=== Handing {url} to Hermes (type /exit in Hermes to move on)")
        try:
            env = {**os.environ, "AGENT_BROWSER_HEADED": "true", "BROWSER_INACTIVITY_TIMEOUT": "1800"}  # visible window, never auto-closed
            subprocess.run(["hermes", "chat", "-t", TOOLSETS, "--query-file", qfile, "--in", str(BASE_DIR),
                            "--run-budget", str(RUN_BUDGET_S), "--max-turns", str(MAX_TURNS)], env=env)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return
        finally:
            Path(qfile).unlink(missing_ok=True)
        brief.unlink(missing_ok=True)
        record_result(url, job, result)
        record = phm.applied() if phm.available() else None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python hermes_apply.py URL [URL ...]")
    run_agent(sys.argv[1:])
