# 🧭 Jobby

**Your job-hunt sidekick.** Find roles, fill out applications, and see exactly who to say hi to on LinkedIn, with a friendly note ready to paste. It runs on your own computer with a local AI (Ollama), so your resume and answers never leave your machine.

> Jobby never logs in to LinkedIn and never sends a message for you. You click, paste, and send.

---

## ⚡ Quick start

```bash
git clone https://github.com/Potato-Naruto/jobby.git
cd jobby
pip install -r requirements.txt
playwright install chromium
python jobby.py
```

That opens the menu. Everything below is reachable from there, so you can stop reading here if you like.

**Before your first run** (about 5 minutes):

1. Put your resume at `resume.pdf` in the project folder.
2. Install [Ollama](https://ollama.com) and pull the model named in `common.py` (`LLM_MODEL`).
3. Create a file called `.env` next to `jobby.py` with the keys you have. All are optional:

   ```
   SERPER_API_KEY=your_key     # finds LinkedIn profiles (free tier at serper.dev)
   ADZUNA_APP_ID=your_id       # more job results (free at developer.adzuna.com)
   ADZUNA_APP_KEY=your_key
   ```

4. In the menu choose **Resume profile** so Jobby reads your resume once.

---

## 🗺️ The main menu

Run `python jobby.py`, then use the arrow keys and Enter.

| Menu item | What it does |
|---|---|
| **Search jobs** | Finds roles on many job boards and keeps only the ones matching your filters (title, place, remote, how recent, salary). |
| **Apply to jobs** | Opens a real browser and fills the application from your resume and saved answers. It stops and asks you for anything it shouldn't do itself (accounts, CAPTCHAs). |
| **LinkedIn outreach** | Drafts a connection note and follow-up for a person, tracks who you've contacted, and reminds you when to follow up. |
| **Job link: contacts chart + tailored resume** | Paste a job URL, get a chart of the hiring team and people on the team, plus a resume tailored to that role. See below. |
| **Resume profile** | Reads `resume.pdf` into a profile the other tools use. |
| **Saved search profiles** | Save and reuse your favourite filters. |
| **Remembered answers** | See or edit answers Jobby saved from past applications. |
| **Settings** | Search keys, browser choice, and other options. |

---

## 🔗 Job link: contacts + tailored resume (the fun one)

Paste a job posting and get a clickable chart of the people to reach out to.

**From the menu:** pick **Contacts for a job link** and paste the URL.

**From the command line:**

```bash
python job_contacts.py "https://company.wd1.myworkdayjobs.com/.../Chemical-Engineer_R123"
```

A page called `job_contacts.html` opens in your browser:

- 🔵 **Blue rows** are the hiring side: recruiters, talent teams, managers.
- 🟢 **Green rows** are people already on the team.
- ⭐ **Bold names** are the people most likely to reply (recruiters and early-career engineers rank above senior executives).
- Click a **name** or **Open** to go to their LinkedIn profile, then **Connect > Add a note**.
- Click **Copy** to copy that person's message, then paste it.
- Rows get a ✅ and dim once you open or copy them, and Jobby remembers across visits.

**Tailored resume:** at the same time, Jobby saves a one-page PDF to the `resumes/` folder, named like `Your_Name_Company_Resume.pdf`, and links it at the top of the chart. It never invents or rewords anything: it puts your bullets and skills that best match the posting first, drops the weakest bullets, and adds a one-line summary built from your real profile. Needs your resume parsed first (menu: **Resume profile**).

Each message is short and friendly, mentions something you really have in common when it can find one (same school, same field), and asks for 5 minutes on their calendar.

| Option | What it does |
|---|---|
| `--company "Name"` | Set the company yourself if the page can't be read. |
| `--role "Title"` | Set the role yourself. |
| `--no-ai` | Skip the local AI and use plain templates (faster). |
| `--no-open` | Build the chart without opening the browser. |
| `--no-resume` | Skip the tailored resume. |

Example when a page blocks Jobby:

```bash
python job_contacts.py "https://example.com/job/123" --company "Chevron" --role "Process Engineer"
```

Needs `SERPER_API_KEY` in `.env` to search for profiles.

---

## 🔍 Search jobs from the command line

```bash
python search_jobs.py --titles "chemical engineer" "process engineer" --locations "Houston, TX" --days 14
```

| Option | Meaning |
|---|---|
| `--titles` | Job title phrases. A job matches if all words of any phrase are in its title. |
| `--locations` | Places, e.g. `"Plano, TX"` or `Texas`. |
| `--remote include/only/exclude` | How to treat remote jobs. |
| `--days N` | Posted in the last N days. |
| `--min-salary N` | Minimum yearly salary in USD. |
| `--drop-unknown-salary` | Hide jobs that don't list pay. |
| `--exclude` | Words that must not appear in the title (e.g. `senior`). |
| `--levels` | Experience levels to keep. |
| `--all` | Include jobs you've already been shown. |
| `--reset-seen` | Forget which jobs you've already seen. |
| `--save-as NAME` | Save these filters as a profile. |
| `--profile NAME` | Reuse a saved profile. |

Handy combo: `python search_jobs.py --profile myroles` to repeat a saved search.

---

## 📝 Apply from the command line

```bash
python app_agent.py "https://job-link-1" "https://job-link-2"
```

Opens each link in a browser, fills the form, and pauses for anything it can't do. By default it submits when nothing is blocking; set `JOBBY_AUTO_SUBMIT=0` in `.env` if you want to review and submit by hand.

Parse your resume on its own: `python parse_resume.py`

---

## 🔧 Settings you can put in `.env`

| Setting | What for |
|---|---|
| `SERPER_API_KEY` | Finding LinkedIn profiles (recommended). |
| `BRAVE_API_KEY` | Alternative search provider. |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `ADZUNA_COUNTRY` | Adzuna job results (country defaults to `us`). |
| `JOOBLE_API_KEY`, `USAJOBS_API_KEY`, `USAJOBS_EMAIL` | More job sources. |
| `JOBBY_BROWSER` | Browser for auto-fill (`chrome` by default). |
| `JOBBY_AUTO_SUBMIT` | `0` = don't submit automatically. |

---

## 🧰 Good to know

- **Search keys cost credits.** One Serper search is one credit; a job-link lookup uses about five.
- **Local AI is optional for contacts.** If Ollama isn't running, Jobby falls back to templates.
- **Your data stays local.** `.env`, `resume.pdf`, your profile, saved answers and outreach list are git-ignored and never uploaded.
- **Weekly pace.** Jobby warns you if you send a lot of LinkedIn invites in a week, since LinkedIn restricts accounts that spam.

## 📁 What's in the box

| File | Role |
|---|---|
| `jobby.py` | The menu app. Start here. |
| `search_jobs.py` | Job search across boards. |
| `app_agent.py` | Application form filler. |
| `job_contacts.py` | Job link to hiring-team chart. |
| `resume_tailor.py` | Tailors your resume to a job and saves the PDF. |
| `outreach.py` | LinkedIn notes and follow-up tracker. |
| `parse_resume.py` | Resume to profile. |
| `common.py` | Shared settings and the local AI client. |
