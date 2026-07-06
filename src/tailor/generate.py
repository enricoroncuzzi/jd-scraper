from pydantic import BaseModel
from src.tailor.jd_source import JobDescription
from src.tailor.cv_master import CanonicalCV

FIXED_CLOSE = (
    "I am based in Italy and work fully remotely. I would be glad to discuss how my "
    "background fits your team."
)


class CoverLetterParts(BaseModel):
    hook: str
    bridge: str
    proof_id: str


class Selection(BaseModel):
    included_bullet_ids: list[str]
    skill_order: list[str]
    cover_letter: CoverLetterParts
    hr_message: str


def build_prompt(jd: JobDescription, canonical: CanonicalCV) -> str:
    bullets = "\n".join(
        f"    {bid}: {canonical.bullet_text(bid)}"
        for s in canonical.sections for bid in s.bullet_ids
    )
    sections = "\n".join(
        f"  {s.id} ({s.name}): bullets {s.bullet_ids}" for s in canonical.sections
    )
    skills = "\n".join(f"    {sid}: {line}" for sid, line in canonical.skills)
    return (
        "You tailor Enrico Roncuzzi's job application. You SELECT and ORDER pre-written "
        "content and write ONLY the cover-letter hook and bridge and the LinkedIn message. "
        "You may not write, rephrase, summarize, or improve any resume text.\n\n"
        "=== TARGET JOB ===\n"
        f"Title: {jd.title}\nCompany: {jd.company}\nWork mode: {jd.work_mode}\n\n"
        f"Job description (the ONLY source of company facts):\n{jd.description}\n\n"
        "=== CANONICAL CV (verbatim, ID-addressed) ===\n"
        f"Sections (reverse chronological, keep this order):\n{sections}\n"
        f"Bullets:\n{bullets}\n"
        f"Skill groups:\n{skills}\n\n"
        "=== RETURN A SELECTION OBJECT ===\n"
        "1. included_bullet_ids: include EVERY bullet ID exactly once, in the given canonical "
        "order. Do NOT drop any bullet (the metrics live in these bullets and must all stay).\n"
        "2. skill_order: the skill group IDs reordered to surface the ones the job asks for "
        "first. Include every skill ID exactly once.\n"
        "3. cover_letter: hook, bridge, proof_id. Free text you write (hook + bridge only).\n"
        "   - hook (1 to 2 sentences): why THIS company, using ONLY facts from the job "
        "description above. No flattery, no facts from your own knowledge.\n"
        "   - bridge (1 sentence): map his work to their need, containing ONE concrete "
        "specific (a number, a system name like Model Context Protocol, or a named "
        "technique), not adjectives.\n"
        "   - proof_id: the ID of the single canonical bullet that best proves the bridge.\n"
        "   hook + bridge combined must be <= 60 words.\n"
        "4. hr_message: a short LinkedIn note written BY Enrico TO the company's recruiter or "
        "hiring manager (his outreach to them, never the reverse). First person, 3 to 4 "
        "sentences: a real hook about the role from the job description, one concrete reason he "
        "fits (a fact from the CV bullets above), and a light ask to connect. Relaxed but "
        "professional.\n\n"
        "STYLE for ALL free text (hook, bridge, hr_message): never use a dash of any kind (no em "
        "dash, en dash, or hyphen as punctuation); use commas and periods. Banned words: excited, "
        "passionate, leverage, cutting-edge, delve, seamless, fast-paced, thrilled, furthermore, "
        "moreover, resonates, 'align with your mission', 'I am writing to'. Plain declarative "
        "sentences, one idea each. The cover-letter close is fixed and added for you (based in "
        "Italy, fully remote), so do NOT write a close and do NOT mention Spain, relocation, or "
        "availability anywhere."
    )


def generate(
    jd: JobDescription,
    canonical: CanonicalCV,
    api_key: str,
    model: str = "gemini-3.5-flash",
) -> Selection:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=build_prompt(jd, canonical),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Selection,
            temperature=0.4,
        ),
    )
    return response.parsed
