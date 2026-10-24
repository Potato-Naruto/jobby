"""Parse resume.pdf into a structured user_profile.json using the local LLM."""
import sys

from pydantic import BaseModel, Field, ValidationError
from pypdf import PdfReader

from common import PROFILE_JSON, RESUME_PDF, ask_llm_json, save_json


# --- Schema -----------------------------------------------------------------
class Contact(BaseModel):
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""


class Experience(BaseModel):
    company: str = ""
    title: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    highlights: list[str] = Field(default_factory=list)


class Education(BaseModel):
    institution: str = ""
    degree: str = ""
    field_of_study: str = ""
    start_date: str = ""
    end_date: str = ""
    gpa: str = ""


class Project(BaseModel):
    name: str = ""
    description: str = ""
    technologies: list[str] = Field(default_factory=list)
    url: str = ""


class Certification(BaseModel):
    name: str = ""
    issuer: str = ""
    date: str = ""


class UserProfile(BaseModel):
    contact: Contact = Field(default_factory=Contact)
    work_experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)


SYSTEM_PROMPT = (
    "You extract structured data from resumes. Reply with ONLY a JSON object matching this schema: "
    + str(UserProfile.model_json_schema())
    + " Use empty strings/lists for missing data. Never invent information."
)


# --- Steps ------------------------------------------------------------------
def extract_text(pdf_path) -> str:
    reader = PdfReader(str(pdf_path))
    return "\n".join((page.extract_text() or "") for page in reader.pages).strip()


def parse_resume() -> bool:
    """Run the full pipeline. Returns True on success."""
    if not RESUME_PDF.exists():
        print(f"[error] {RESUME_PDF} not found. Place your resume there as 'resume.pdf'.")
        return False
    try:
        text = extract_text(RESUME_PDF)
    except Exception as exc:
        print(f"[error] Could not read PDF: {exc}")
        return False
    if not text:
        print("[error] No text extracted (scanned PDF?). OCR it first.")
        return False

    print("Sending resume text to local LLM...")
    try:
        raw = ask_llm_json(SYSTEM_PROMPT, f"Resume text:\n\n{text}")
        profile = UserProfile.model_validate(raw)
    except ValidationError as exc:
        print(f"[error] LLM output did not match the schema:\n{exc}")
        return False
    except Exception as exc:
        print(f"[error] {exc}")
        return False

    save_json(PROFILE_JSON, profile.model_dump())
    print(f"Saved profile to {PROFILE_JSON}")
    return True


if __name__ == "__main__":
    sys.exit(0 if parse_resume() else 1)

