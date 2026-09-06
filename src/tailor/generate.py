from pydantic import BaseModel
from src.tailor.jd_source import JobDescription
from src.tailor.cv_master import CanonicalCV

FIXED_CLOSE = (
    "I am based in Italy and working fully remotely. I would be glad to discuss "
    "how my background fits your team."
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
    eligible_proof = canonical.eligible_proof_ids()
    eligible_proof_lines = "\n".join(
        f"    {bid}: {canonical.bullet_text(bid)}" for bid in eligible_proof
    )
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
        "3. cover_letter: hook, bridge, proof_id. Free text you write (hook + bridge only). A "
        "greeting (\"Dear {company} team,\") is added structurally, do NOT write one.\n"
        "   - hook (1 to 2 sentences): why THIS company, using ONLY facts from the job "
        "description above. No flattery, no facts from your own knowledge. The FIRST clause "
        "must be about Enrico (his interest, his focus, or a capability of his), with the "
        "company fact woven in after as the reason he is writing, not handed back to them "
        "as a description. Never open with \"{company} is ...\" or \"{company} builds ...\" "
        "or any sentence whose first words are the company name followed by a verb about "
        "the company itself, that reads as a mail merge.\n"
        "   - bridge (1 sentence): map his EXISTING work to their need, containing ONE "
        "concrete specific (a number, a system name like Model Context Protocol, or a "
        "named technique), not adjectives. The bridge must never imply domain expertise "
        "that is not in the CV bullets above (e.g. do not claim prior clinical, legal, or "
        "finance-domain experience just because the job is in that domain). If the job's "
        "domain is not one of his, frame the bridge as transferability: name the real "
        "CV-backed capability (a skill, a system, a type of pipeline) and state that it "
        "transfers to their domain, rather than asserting he has worked in that domain.\n"
        "   - proof_id: the ID of the single canonical bullet that best proves the bridge. "
        "It is dropped into the letter verbatim, so it must read as a natural standalone "
        "sentence, not a pasted CV fragment. Choose proof_id ONLY from this eligible list "
        f"(imperative or subjectless bullets are excluded on purpose):\n{eligible_proof_lines}\n"
        "   First match the proof's domain to the role: for agentic, LLM, GenAI, multi-agent, "
        "or backend roles, prefer an lp.agentic.* proof (Hey-Movo); for detection, forensics, "
        "computer-vision, or classic-ML roles, prefer an internship ML proof (ResNet50/MoE); "
        "if the role is ambiguous between the two, prefer an lp.agentic.* proof. Within that "
        "domain, the proof must ADD evidence, not restate the bridge: if the bridge already "
        "names a system or technique, do not pick a bullet naming that same system or "
        "technique again, pick one that widens the picture instead. Preference order among "
        "the eligible list: (1) a bullet with a hard metric the bridge does not already cite, "
        "(2) a bullet showing a capability adjacent to but distinct from the bridge's, "
        "(3) only as a last resort, a bullet overlapping the bridge.\n"
        "   hook + bridge combined must be <= 100 words.\n"
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
        "Italy, working fully remotely), so do NOT write a close and do NOT mention Spain, "
        "relocation, or availability anywhere."
    )


def generate(
    jd: JobDescription,
    canonical: CanonicalCV,
    api_key: str,
    model: str = "openai/gpt-oss-120b",
) -> Selection:
    from groq import Groq

    client = Groq(api_key=api_key)
    prompt = (
        f"{build_prompt(jd, canonical)}\n\n"
        "Respond with ONLY a single JSON object matching this schema (no prose, no "
        f"markdown fences):\n{Selection.model_json_schema()}"
    )
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.4,
    )
    return Selection.model_validate_json(response.choices[0].message.content)
