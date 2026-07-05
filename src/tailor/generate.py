from pydantic import BaseModel
from src.tailor.jd_source import JobDescription
from src.tailor.cv_master import MasterCV


class RoleBullets(BaseModel):
    role_index: int
    bullets: list[str]


class TailoredOutput(BaseModel):
    summary: str
    experience: list[RoleBullets]
    cover_letter: str
    hr_message: str


_PERSONA = (
    "You help Enrico Roncuzzi, a junior AI/ML engineer, tailor his job application to a specific "
    "role. You get his master CV (the ONLY source of truth) and a job description. A recruiter and "
    "hiring manager will read the result, so it must feel written by a real person, not by AI.\n\n"
    "GROUNDING (hard): never invent experience, metrics, tools, employers, dates, or personal "
    "facts. Every claim must trace to the master CV. When unsure, cut it. Do NOT invent where he "
    "lives, relocation, office visits, availability, or visa status: he is based in Italy and open "
    "to fully remote roles in the EU, so never claim he lives in or will visit the company's city "
    "or country. If location comes up at all, the only true framing is 'based in Italy, working "
    "fully remotely'. Respect the exact length budgets given.\n\n"
    "VOICE (matters as much as the content):\n"
    "* Never use a dash of any kind. No em dash, no en dash, no '-'. Not as punctuation, not to "
    "join words. Use commas and periods, and write compound terms as separate words (end to end, "
    "real time, cross distribution, two stage, sub pixel).\n"
    "* Ban corporate/AI filler and anything like it: leverage, spearheaded, passionate, driven, "
    "esteemed, cutting edge, synergy, delve, furthermore, moreover, robust, seamless, 'proven "
    "track record', 'I am writing to', 'align with your goals', 'fast paced', 'at your earliest "
    "convenience', 'esteemed company'.\n"
    "* No rule of three lists, no 'not only X but also Y', no symmetrical or balanced sentences. "
    "Vary the rhythm, keep it plain and specific. Contractions are welcome.\n"
    "* Warm, direct, a little understated. Confident about what he actually built, honest about "
    "the rest. Concrete beats grand. Never flatter the company."
)


def build_prompt(jd: JobDescription, master: MasterCV) -> str:
    lc = master.length_contract
    role_specs = []
    for role in lc.roles:
        originals = "\n".join(
            f"    - (<= {budget} chars) original: {master.bullets_by_role[role.index][i]}"
            for i, budget in enumerate(role.bullet_budgets)
        )
        role_specs.append(
            f"  Role {role.index} ({role.name}): rewrite exactly "
            f"{len(role.bullet_budgets)} bullets.\n{originals}"
        )
    roles_block = "\n".join(role_specs)
    return (
        f"{_PERSONA}\n\n"
        f"=== TARGET JOB ===\n"
        f"Title: {jd.title}\nCompany: {jd.company}\nLocation: {jd.location}\n"
        f"Work mode: {jd.work_mode}\n\nDescription:\n{jd.description}\n\n"
        f"=== MASTER CV (source of truth) ===\n{master.raw}\n\n"
        f"=== YOUR TASK (obey the VOICE rules above in every field, especially no dashes) ===\n"
        f"1. Summary: rewrite it for this job in <= {lc.summary_budget} characters. Sharp and "
        f"specific, CV register.\n"
        f"2. Experience bullets: keep the exact count per role, each within its char budget, "
        f"start with a real verb, keep the numbers:\n{roles_block}\n"
        f"3. Cover letter: write it in Enrico's own first person voice, about 150 to 200 words, "
        f"as a greeting plus three short paragraphs (NO header, NO signature, those get added "
        f"around it). Open with 'Dear {jd.company} team,'. Paragraph 1: one genuine, specific "
        f"reason this role interests him, tied to what the company actually builds (no flattery). "
        f"Paragraph 2: one real thing he built from the CV, with its number, and why it maps to "
        f"this job. Paragraph 3: a short warm close that invites a conversation, no groveling. It "
        f"should sound like a smart, motivated person wrote it in ten minutes, not a template.\n"
        f"4. HR message: a short LinkedIn note written BY Enrico TO the company's recruiter or "
        f"hiring manager (his outreach to them, never the reverse). First person, 3 to 4 "
        f"sentences: a real hook about the role, one concrete reason he'd fit (from the CV), and a "
        f"light ask to connect or chat. Relaxed but professional, the way a real person opens a "
        f"conversation on LinkedIn.\n"
        f"Return summary, experience (role_index + bullets per role), cover_letter, hr_message."
    )


def generate(
    jd: JobDescription,
    master: MasterCV,
    api_key: str,
    model: str = "gemini-3.5-flash",
) -> TailoredOutput:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=build_prompt(jd, master),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=TailoredOutput,
            temperature=0.4,
        ),
    )
    return response.parsed
