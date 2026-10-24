# Jobby

A Python command-line tool that runs a job search end to end: find roles, auto-fill applications, and reach the right people on LinkedIn. It uses a local LLM (Ollama), so your resume and answers never leave your machine.

## What it does
- **Search jobs** across several boards (Adzuna, Himalayas and more), filter by role, location and keywords, and remember what you've already seen.
- **Apply in a real browser** with Playwright: it fills forms from your resume profile and remembered answers, and stops for anything it shouldn't do for you (accounts, CAPTCHAs).
- **Resume profile** parsed from your PDF into structured facts that drafts and answers are checked against.
- **LinkedIn outreach**: sorts contacts into four archetypes, drafts a connection note and follow-up, and tracks who you've contacted and when to follow up. Nothing is sent automatically.
- **Contacts for a job link**: paste a job URL and get a chart of the hiring team and people already on the team, each with a clickable LinkedIn link, a short friendly note asking for 5 minutes, and a Copy button. Rows are checked off as you open or copy them, and the people most likely to reply are bolded.

## Setup
1. `pip install -r requirements.txt` and `playwright install chromium`
2. Install [Ollama](https://ollama.com) and pull the model set in `common.py`.
3. Put your resume at `resume.pdf`, and create a `.env` with any keys you have (all optional):
   ```
   SERPER_API_KEY=...   # web search for LinkedIn profiles
   BRAVE_API_KEY=...    # alternative search provider
   ADZUNA_APP_ID=...
   ADZUNA_APP_KEY=...
   ```
4. Run `python jobby.py`, or for one job link: `python job_contacts.py "<job url>"`.

## Notes
- Jobby never logs in to LinkedIn and never sends messages; you paste each note yourself.
- Personal data (resume, profile, answers, `.env`, outreach tracker) is git-ignored.
