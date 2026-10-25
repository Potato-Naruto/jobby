"""Tailor your resume to a job posting and save it as a PDF in resumes/.

Honest by design: nothing is invented or reworded. For each job it
  * puts the bullets and skills that best match the posting first (and trims the weakest bullets),
  * writes a one-line summary built only from your profile facts and the target role/company,
  * saves resumes/<Your_Name>_<Company>_Resume.pdf.
Your facts come from user_profile.json (made by parse_resume.py from resume.pdf).
"""
import re
from collections import Counter
from pathlib import Path

from common import BASE_DIR, PROFILE_JSON, load_json

RESUMES_DIR = BASE_DIR / "resumes"
MAX_BULLETS = 3
STOP = set("""about above after also and any are been being both but can for from have has into its more most not our out over
per such than that the their them then these they this those through very was were what when where which while who will with
within would you your able across ability work working team teams including include includes role roles job jobs position
experience years year strong skills required preferred responsibilities qualifications company candidates candidate apply
opportunity opportunities employees employee equal employer benefits within etc using use used well new full time part""".split())


def _stem(w: str) -> str:
    return re.sub(r"(ing|ed|es|s|ions?|ment)$", "", w) if len(w) > 5 else w.rstrip("s")


def keywords(role: str, text: str) -> Counter:
    """Weighted word counts: words in the job title count triple."""
    c = Counter()
    for weight, src in ((3, role), (1, text[:6000])):
        for w in re.findall(r"[a-z][a-z+#.\-]{2,}", src.lower()):
            w = w.strip(".-")
            if w not in STOP and len(w) > 2:
                c[_stem(w)] += weight
    return c


def _score(text: str, kw: Counter) -> int:
    return sum(min(kw.get(_stem(w), 0), 6) for w in set(re.findall(r"[a-z][a-z+#.\-]{2,}", text.lower())))


def tailor(profile: dict, role: str, company: str, job_text: str = "") -> dict:
    """Return the tailored resume as a dict (same shape as the profile plus summary and matched keywords)."""
    role = re.sub(r"(?i)\s*job details\s*$", "", role).strip()
    kw = keywords(role, job_text)
    exp = []
    for job in profile.get("work_experience", []):
        bullets = sorted(job.get("highlights", []), key=lambda b: -_score(b, kw))[:MAX_BULLETS]
        exp.append({**job, "highlights": bullets, "_rel": sum(_score(b, kw) for b in bullets)})
    skills = sorted(profile.get("skills", []), key=lambda s: -_score(s, kw))   # stable: ties keep your order
    matched = [s for s in skills if _score(s, kw) and not re.search(r"native|english", s, re.I)]
    edu = (profile.get("education") or [{}])[0]
    titles = [j["title"] for j in profile.get("work_experience", []) if j.get("title") and "team member" not in j["title"].lower()]
    summary = f"{edu.get('field_of_study', 'Engineering')} graduate ({edu.get('institution', '')}, {edu.get('end_date', '')})"
    if titles:
        summary += f" with internship experience as {' and '.join(t.lower() for t in titles[:2])}"
    summary += f", applying for the {role} role at {company}." if role else f", interested in {company}."
    return {**profile, "work_experience": exp, "skills": skills, "summary": summary, "matched": matched}


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


def pdf_path(profile: dict, company: str) -> Path:
    name = _safe(profile.get("contact", {}).get("full_name", "").title()) or "Resume"
    return RESUMES_DIR / f"{name}_{_safe(company)}_Resume.pdf"


def render_pdf(r: dict, path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape as esc

    c = r.get("contact", {})
    for size in (10.5, 10, 9.5, 9):    # shrink until it fits on one page
        base = ParagraphStyle("b", fontName="Helvetica", fontSize=size, leading=size * 1.25)
        name = ParagraphStyle("n", parent=base, fontName="Helvetica-Bold", fontSize=size + 8, leading=size + 11, alignment=TA_CENTER)
        center = ParagraphStyle("c", parent=base, alignment=TA_CENTER, textColor=colors.HexColor("#333333"))
        head = ParagraphStyle("h", parent=base, fontName="Helvetica-Bold", fontSize=size + 1, spaceBefore=7, spaceAfter=1)
        bullet = ParagraphStyle("bu", parent=base, leftIndent=12, bulletIndent=2)
        bold = ParagraphStyle("bo", parent=base, fontName="Helvetica-Bold")
        S = []
        S.append(Paragraph(esc(c.get("full_name", "").title()), name))
        contact = " | ".join(x for x in (c.get("location"), c.get("phone"), c.get("email"), c.get("linkedin")) if x)
        S.append(Paragraph(esc(contact), center))
        S.append(Spacer(1, 4))

        def section(title):
            S.append(Paragraph(title.upper(), head))
            S.append(HRFlowable(width="100%", thickness=0.6, color=colors.black, spaceAfter=3))
        section("Summary")
        S.append(Paragraph(esc(r["summary"]), base))
        section("Education")
        for e in r.get("education", []):
            if not e.get("institution"):
                continue
            deg = ", ".join(x for x in (e.get("degree"), e.get("field_of_study")) if x)
            S.append(Paragraph(f"<b>{esc(e['institution'])}</b>"
                               f"{' - ' + esc(deg) if deg else ''} ({esc(e.get('start_date', ''))} - {esc(e.get('end_date', ''))})"
                               f"{', GPA ' + esc(e['gpa']) if e.get('gpa') else ''}", base))
        section("Experience")
        for j in r.get("work_experience", []):
            S.append(Paragraph(f"<b>{esc(j.get('title', ''))}</b>, {esc(j.get('company', ''))}"
                               f"{', ' + esc(j['location']) if j.get('location') else ''} "
                               f"({esc(j.get('start_date', ''))} - {esc(j.get('end_date', ''))})", bold if False else base))
            for b in j.get("highlights", []):
                S.append(Paragraph(esc(b), bullet, bulletText="•"))
            S.append(Spacer(1, 3))
        for key, title in (("projects", "Projects"), ("certifications", "Certifications")):
            items = r.get(key) or []
            if items:
                section(title)
                for it in items:
                    S.append(Paragraph(esc(it if isinstance(it, str) else it.get("name", str(it))), bullet, bulletText="•"))
        section("Skills")
        S.append(Paragraph(esc(", ".join(r.get("skills", []))), base))
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = SimpleDocTemplate(str(path), pagesize=letter, leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                                topMargin=0.55 * inch, bottomMargin=0.55 * inch, title=f"{c.get('full_name', '')} Resume")
        doc.build(S)
        from pypdf import PdfReader
        if len(PdfReader(str(path)).pages) == 1:
            return


def tailor_for_job(company: str, role: str, job_text: str = "") -> tuple[Path, list[str]] | None:
    """Build and save the tailored PDF. Returns (path, matched keywords), or None if there is no parsed profile."""
    profile = load_json(PROFILE_JSON, {})
    if not profile.get("work_experience") and not profile.get("education"):
        return None
    r = tailor(profile, role, company, job_text)
    path = pdf_path(profile, company)
    render_pdf(r, path)
    return path, r["matched"]


if __name__ == "__main__":
    import sys
    co, ro = (sys.argv[1:3] + ["", ""])[:2]
    print(tailor_for_job(co, ro))
