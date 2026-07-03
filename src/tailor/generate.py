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
    "You are a senior technical recruiter who hires AI/ML engineers for EU remote roles. "
    "You are given a candidate's master CV and a target job description. Make the candidate's "
    "material as strong and role-specific as possible: rewrite bullets to lead with the most "
    "relevant experience, surface matching skills, and mirror the job description's language "
    "WHERE IT IS TRUTHFULLY SUPPORTED BY THE MASTER CV. "
    "Absolute rule: never invent experience, metrics, tools, employers, or dates. Every claim "
    "must be traceable to the master CV. When in doubt, cut rather than fabricate. "
    "Respect the exact length budgets given for each section."
)


def build_prompt(jd: JobDescription, master: MasterCV) -> str:
    lc = master.length_contract
    role_specs = []
    for role in lc.roles:
        originals = "\n".join(
            f"    - (≤{budget} chars) original: {master.bullets_by_role[role.index][i]}"
            for i, budget in enumerate(role.bullet_budgets)
        )
        role_specs.append(
            f"  Role {role.index} — {role.name}: rewrite exactly "
            f"{len(role.bullet_budgets)} bullets.\n{originals}"
        )
    roles_block = "\n".join(role_specs)
    return (
        f"{_PERSONA}\n\n"
        f"=== TARGET JOB ===\n"
        f"Title: {jd.title}\nCompany: {jd.company}\nLocation: {jd.location}\n"
        f"Work mode: {jd.work_mode}\n\nDescription:\n{jd.description}\n\n"
        f"=== MASTER CV (source of truth) ===\n{master.raw}\n\n"
        f"=== YOUR TASK ===\n"
        f"1. Rewrite the Summary for this job in <= {lc.summary_budget} characters.\n"
        f"2. Rewrite the Experience bullets, preserving the exact count per role and each "
        f"bullet within its char budget:\n{roles_block}\n"
        f"3. Write a cover letter of <= 200 words: 1-sentence hook, 2-3 evidence sentences from "
        f"the CV, 1-sentence close. Precise, direct, no fluff.\n"
        f"4. Write a 3-4 sentence LinkedIn/HR opener referencing this specific role and one "
        f"concrete match between the job and the candidate's profile.\n"
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
