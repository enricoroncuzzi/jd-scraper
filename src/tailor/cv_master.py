import re
from dataclasses import dataclass


@dataclass
class RoleContract:
    index: int
    name: str
    bullet_budgets: list[int]


@dataclass
class LengthContract:
    summary_budget: int
    roles: list[RoleContract]


@dataclass
class MasterCV:
    raw: str
    summary: str
    role_names: list[str]
    bullets_by_role: list[list[str]]
    length_contract: LengthContract

    def reassemble(self, summary: str, bullets_by_role: list[list[str]]) -> str:
        text = self.raw.replace(self.summary, summary, 1)
        if len(bullets_by_role) != len(self.bullets_by_role):
            raise ValueError(
                f"role count mismatch: master has {len(self.bullets_by_role)}, "
                f"got {len(bullets_by_role)}"
            )
        for i, (old_role, new_role) in enumerate(zip(self.bullets_by_role, bullets_by_role)):
            if len(old_role) != len(new_role):
                raise ValueError(
                    f"bullet count mismatch in role {i}: "
                    f"master has {len(old_role)}, got {len(new_role)}"
                )
        old_flat = [b for role in self.bullets_by_role for b in role]
        new_flat = [b for role in bullets_by_role for b in role]
        for old, new in zip(old_flat, new_flat):
            text = text.replace(f"- {old}", f"- {new}", 1)
        return text


def _section(text: str, header: str) -> str:
    m = re.search(rf"## {header}\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _parse_experience(exp: str) -> tuple[list[str], list[list[str]]]:
    role_names: list[str] = []
    bullets_by_role: list[list[str]] = []
    current: list[str] | None = None
    for line in exp.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            if current is not None:
                current.append(stripped[2:])
        elif re.match(r"^\*\*.+\*\*$", stripped) and not line.startswith(" "):
            # a new role header (bold, not an indented "  :" continuation)
            role_names.append(stripped.strip("*").strip())
            current = []
            bullets_by_role.append(current)
    return role_names, bullets_by_role


def load_master(path: str) -> MasterCV:
    with open(path) as f:
        raw = f.read()
    summary = _section(raw, "Summary")
    role_names, bullets_by_role = _parse_experience(_section(raw, "Experience"))
    roles = [
        RoleContract(index=i, name=name, bullet_budgets=[len(b) for b in bullets])
        for i, (name, bullets) in enumerate(zip(role_names, bullets_by_role))
    ]
    contract = LengthContract(summary_budget=len(summary), roles=roles)
    return MasterCV(
        raw=raw,
        summary=summary,
        role_names=role_names,
        bullets_by_role=bullets_by_role,
        length_contract=contract,
    )


_SKILL_RE = re.compile(r"^\*\*(?P<label>[^:]+):\*\*")


def _slug(label: str) -> str:
    return "skill." + re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


@dataclass
class Section:
    id: str
    name: str
    bullet_ids: list[str]
    bullets: dict[str, str]


@dataclass
class CanonicalCV:
    raw: str
    summary: str
    skills: list[tuple[str, str]]          # [(id, verbatim line), ...]
    sections: list["Section"]

    def bullet_text(self, bullet_id: str) -> str:
        for s in self.sections:
            if bullet_id in s.bullets:
                return s.bullets[bullet_id]
        raise KeyError(bullet_id)

    def all_bullet_texts(self) -> set[str]:
        return {t for s in self.sections for t in s.bullets.values()}

    def skill_line(self, skill_id: str) -> str:
        for sid, line in self.skills:
            if sid == skill_id:
                return line
        raise KeyError(skill_id)

    def section_order(self) -> list[str]:
        return [s.id for s in self.sections]

    def assemble(self, included_bullet_ids: list[str], skill_order: list[str]) -> str:
        text = self.raw
        # 1) reorder the Skills block, verbatim lines only
        skills_block = "\n\n".join(self.skill_line(sid) for sid in skill_order)
        orig_skills_block = "\n\n".join(line for _, line in self.skills)
        text = text.replace(orig_skills_block, skills_block, 1)
        # 2) within each section, keep only included bullets in the given order
        included = set(included_bullet_ids)
        order = {bid: i for i, bid in enumerate(included_bullet_ids)}
        for s in self.sections:
            orig_block = "\n\n".join(f"- {s.bullets[bid]}" for bid in s.bullet_ids)
            kept = [bid for bid in s.bullet_ids if bid in included]
            kept.sort(key=lambda bid: order[bid])
            new_block = "\n\n".join(f"- {s.bullets[bid]}" for bid in kept)
            text = text.replace(orig_block, new_block, 1)
        return text


def load_canonical(path: str) -> CanonicalCV:
    with open(path) as f:
        raw = f.read()
    summary = _section(raw, "Summary")
    skills = []
    for line in _section(raw, "Skills").splitlines():
        line = line.strip()
        m = _SKILL_RE.match(line)
        if m:
            skills.append((_slug(m.group("label")), line))
    role_names, bullets_by_role = _parse_experience(_section(raw, "Experience"))
    sections = []
    for r, (name, bullets) in enumerate(zip(role_names, bullets_by_role)):
        bullet_ids = [f"exp.{r}.b{i}" for i in range(len(bullets))]
        sections.append(
            Section(
                id=f"exp.{r}",
                name=name,
                bullet_ids=bullet_ids,
                bullets=dict(zip(bullet_ids, bullets)),
            )
        )
    return CanonicalCV(raw=raw, summary=summary, skills=skills, sections=sections)
