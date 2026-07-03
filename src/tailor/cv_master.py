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
