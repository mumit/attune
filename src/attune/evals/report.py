"""The published eval report (build prompt 27's acceptance criterion):
edit-burden proxy, pairwise win rate vs. gold, triage accuracy plus the
learned-adjustment direction, injection success rate, and the judge
agreement rate per domain — with each domain's ``gates`` flag making the
sub-75%-agreement-is-not-a-gate rule (task 3) a property of the report
itself, not something a caller has to remember to check separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .injection import InjectionReport
from .triage_eval import TriageEvalReport


@dataclass(frozen=True)
class DomainPairwise:
    """One domain's slice of the pairwise-judging pass."""

    domain: str
    total: int
    wins: int
    losses: int
    ties: int
    unresolved: int
    disagreement_rate: float
    agreement_rate: float | None
    gates: bool

    @property
    def win_rate(self) -> float | None:
        resolved = self.wins + self.losses + self.ties
        return (self.wins / resolved) if resolved else None

    def to_json(self) -> dict[str, Any]:
        return {
            "domain": self.domain, "total": self.total, "wins": self.wins,
            "losses": self.losses, "ties": self.ties, "unresolved": self.unresolved,
            "disagreement_rate": self.disagreement_rate,
            "agreement_rate": self.agreement_rate, "gates": self.gates,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "DomainPairwise":
        return cls(
            domain=raw["domain"], total=raw["total"], wins=raw["wins"],
            losses=raw["losses"], ties=raw["ties"], unresolved=raw["unresolved"],
            disagreement_rate=raw["disagreement_rate"],
            agreement_rate=raw.get("agreement_rate"), gates=raw["gates"],
        )


@dataclass(frozen=True)
class EvalReport:
    edit_burden_proxy: float | None
    pairwise: tuple[DomainPairwise, ...]
    triage: TriageEvalReport | None
    injection: InjectionReport | None
    generated_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "edit_burden_proxy": self.edit_burden_proxy,
            "pairwise": [p.to_json() for p in self.pairwise],
            "triage": (
                {
                    "accuracy": self.triage.accuracy,
                    "confusion": {"|".join(k): v for k, v in self.triage.confusion.items()},
                    "adjustment_correct_rate": self.triage.adjustment_correct_rate,
                    "total": self.triage.total,
                }
                if self.triage is not None else None
            ),
            "injection": (
                {
                    "success_rate": self.injection.success_rate,
                    "by_attack_type": self.injection.by_attack_type,
                }
                if self.injection is not None else None
            ),
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "EvalReport":
        triage = None
        if raw.get("triage") is not None:
            t = raw["triage"]
            triage = TriageEvalReport(
                accuracy=t["accuracy"],
                confusion={tuple(k.split("|")): v for k, v in t["confusion"].items()},
                adjustment_correct_rate=t.get("adjustment_correct_rate"),
                total=t["total"],
            )
        injection = None
        if raw.get("injection") is not None:
            i = raw["injection"]
            injection = InjectionReport(
                success_rate=i["success_rate"], by_attack_type=i["by_attack_type"], outcomes=(),
            )
        return cls(
            edit_burden_proxy=raw.get("edit_burden_proxy"),
            pairwise=tuple(DomainPairwise.from_json(p) for p in raw.get("pairwise") or ()),
            triage=triage,
            injection=injection,
            generated_at=raw.get("generated_at", ""),
        )


def render_report_text(report: EvalReport) -> str:
    lines = [f"Eval report — generated {report.generated_at}"]
    lines.append(f"edit_burden_proxy: {_fmt(report.edit_burden_proxy)}")
    lines.append("")
    lines.append("pairwise vs. gold, by domain:")
    header = (
        f"{'domain':<12} {'n':>4} {'win%':>6} {'loss%':>6} {'tie%':>6} "
        f"{'disagree%':>10} {'agreement%':>11} {'gates':>6}"
    )
    lines.append(header)
    for p in report.pairwise:
        resolved = p.wins + p.losses + p.ties
        lines.append(
            f"{p.domain:<12} {p.total:>4} "
            f"{_fmt_pct(p.wins / resolved if resolved else None):>6} "
            f"{_fmt_pct(p.losses / resolved if resolved else None):>6} "
            f"{_fmt_pct(p.ties / resolved if resolved else None):>6} "
            f"{_fmt_pct(p.disagreement_rate):>10} "
            f"{_fmt_pct(p.agreement_rate):>11} {'yes' if p.gates else 'no':>6}"
        )
    if report.triage is not None:
        lines.append("")
        lines.append(
            f"triage accuracy: {_fmt_pct(report.triage.accuracy)} "
            f"(n={report.triage.total}); adjustment-direction correct: "
            f"{_fmt_pct(report.triage.adjustment_correct_rate)}"
        )
    if report.injection is not None:
        lines.append("")
        lines.append(f"injection success rate: {_fmt_pct(report.injection.success_rate)}")
        for attack_type, rate in sorted(report.injection.by_attack_type.items()):
            lines.append(f"  {attack_type:<28} {_fmt_pct(rate)}")
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"
