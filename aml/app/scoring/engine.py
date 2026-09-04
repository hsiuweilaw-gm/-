"""評分引擎。

設計原則：純函式、不碰資料庫、不呼叫模型。給定問卷定義與作答，
即可決定性地算出分數、等級與強制規則結果，因此可完整單元測試，
也能在金檢時逐案重現當時的判定依據。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

DEFINITIONS_DIR = Path(__file__).parent / "definitions"


@dataclass(frozen=True)
class Option:
    code: str
    label: str
    score: int
    annual_bucket: str | None = None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Factor:
    code: str
    label: str
    options: tuple[Option, ...]
    annual_key: str | None = None
    # 年度報表桶別順序。留空時沿用選項順序（即紙本表單順序）。
    annual_order: tuple[str, ...] = ()

    def option(self, code: str) -> Option | None:
        return next((o for o in self.options if o.code == code), None)


@dataclass(frozen=True)
class Category:
    code: str
    label: str
    factors: tuple[Factor, ...]


@dataclass(frozen=True)
class Check:
    code: str
    label: str


@dataclass(frozen=True)
class Questionnaire:
    id: str
    version: int
    name: str
    effective_from: str
    high_risk_threshold: int
    level_labels: dict[str, str]
    categories: tuple[Category, ...]
    refusal_checks: tuple[Check, ...]
    mandatory_high_risk: tuple[Check, ...]
    suspicious_patterns: tuple[Check, ...]

    @property
    def key(self) -> str:
        return f"{self.id}_v{self.version}"

    @property
    def factors(self) -> tuple[Factor, ...]:
        return tuple(f for c in self.categories for f in c.factors)

    def factor(self, code: str) -> Factor | None:
        return next((f for f in self.factors if f.code == code), None)

    def check_label(self, group: str, code: str) -> str:
        checks = {
            "refusal": self.refusal_checks,
            "mandatory": self.mandatory_high_risk,
            "suspicious": self.suspicious_patterns,
        }[group]
        return next((c.label for c in checks if c.code == code), code)

    @property
    def min_score(self) -> int:
        return sum(min(o.score for o in f.options) for f in self.factors)

    @property
    def max_score(self) -> int:
        return sum(max(o.score for o in f.options) for f in self.factors)


def _load(path: Path) -> Questionnaire:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    categories = tuple(
        Category(
            code=c["code"],
            label=c["label"],
            factors=tuple(
                Factor(
                    code=f["code"],
                    label=f["label"],
                    annual_key=f.get("annual_key"),
                    annual_order=tuple(f.get("annual_order", [])),
                    options=tuple(
                        Option(
                            code=o["code"],
                            label=o["label"],
                            score=int(o["score"]),
                            annual_bucket=o.get("annual_bucket"),
                            flags=tuple(o.get("flags", [])),
                        )
                        for o in f["options"]
                    ),
                )
                for f in c["factors"]
            ),
        )
        for c in raw["categories"]
    )
    def checks(key: str) -> tuple[Check, ...]:
        return tuple(Check(code=c["code"], label=c["label"]) for c in raw.get(key, []))

    return Questionnaire(
        id=raw["id"],
        version=int(raw["version"]),
        name=raw["name"],
        effective_from=str(raw["effective_from"]),
        high_risk_threshold=int(raw["high_risk_threshold"]),
        level_labels=raw.get("level_labels", {"general": "一般風險", "high": "高風險"}),
        categories=categories,
        refusal_checks=checks("refusal_checks"),
        mandatory_high_risk=checks("mandatory_high_risk"),
        suspicious_patterns=checks("suspicious_patterns"),
    )


@lru_cache
def load_questionnaire(qid: str = "life", version: int | None = None) -> Questionnaire:
    """載入問卷定義。未指定版本時取該問卷最新版。"""
    candidates = sorted(DEFINITIONS_DIR.glob(f"{qid}_v*.yaml"))
    if not candidates:
        raise FileNotFoundError(f"找不到問卷定義：{qid}")
    if version is None:
        path = candidates[-1]
    else:
        path = DEFINITIONS_DIR / f"{qid}_v{version}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"找不到問卷定義：{qid} v{version}")
    return _load(path)


@dataclass
class CategoryScore:
    code: str
    label: str
    score: int
    max_score: int


@dataclass
class ScoreResult:
    """一次評分的完整結果，含判定依據，可直接落庫與呈現。"""

    total_score: int
    max_score: int
    min_score: int
    threshold: int
    level: str                       # "general" | "high"
    level_label: str
    blocked: bool
    blocked_reasons: list[str] = field(default_factory=list)
    override_applied: bool = False
    override_reasons: list[str] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    category_scores: list[CategoryScore] = field(default_factory=list)
    answered: int = 0
    total_factors: int = 0
    missing_factors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing_factors

    @property
    def score_by_total_alone(self) -> str:
        return "high" if self.total_score >= self.threshold else "general"


def score_assessment(
    questionnaire: Questionnaire,
    answers: dict[str, str],
    *,
    refusal_checks: set[str] | None = None,
    mandatory_checks: set[str] | None = None,
    suspicious_checks: set[str] | None = None,
) -> ScoreResult:
    """依作答計算分數與風險等級。

    等級判定順序（後者可提升等級，不可降低）：
      1. 總分 >= 門檻            -> 高風險
      2. 命中強制高風險條件      -> 高風險（範本第五點）
      3. 命中疑似洗錢態樣        -> 高風險（範本附錄、第九點）
      4. 選項帶 sanctioned_geography 旗標 -> 高風險（範本第五點第二款）
      5. 命中婉拒事由            -> 擋件，並一併列為高風險（範本第四點）

    未作答的題目以 0 分計，因此未填完的草稿分數只會低估不會高估；
    是否填答完整由 ScoreResult.complete 判斷，送出前一律要求填滿。
    """
    refusal_checks = set(refusal_checks or ())
    mandatory_checks = set(mandatory_checks or ())
    suspicious_checks = set(suspicious_checks or ())

    total = 0
    flags: set[str] = set()
    category_scores: list[CategoryScore] = []
    missing: list[str] = []
    answered = 0

    for category in questionnaire.categories:
        cat_total = 0
        cat_max = 0
        for factor in category.factors:
            cat_max += max(o.score for o in factor.options)
            chosen = answers.get(factor.code)
            option = factor.option(chosen) if chosen else None
            if option is None:
                missing.append(factor.code)
                continue
            answered += 1
            cat_total += option.score
            flags.update(option.flags)
        total += cat_total
        category_scores.append(
            CategoryScore(
                code=category.code, label=category.label, score=cat_total, max_score=cat_max
            )
        )

    level = "high" if total >= questionnaire.high_risk_threshold else "general"
    override_reasons: list[str] = []

    for code in sorted(mandatory_checks):
        override_reasons.append(f"強制高風險：{questionnaire.check_label('mandatory', code)}")
    for code in sorted(suspicious_checks):
        override_reasons.append(f"疑似洗錢態樣：{questionnaire.check_label('suspicious', code)}")
    if "sanctioned_geography" in flags:
        override_reasons.append("強制高風險：客戶來自主管機關公告制裁名單之國家、地區或子（分）公司")

    blocked_reasons = [
        f"應婉拒建立業務關係：{questionnaire.check_label('refusal', code)}"
        for code in sorted(refusal_checks)
    ]

    if override_reasons or blocked_reasons:
        level = "high"

    return ScoreResult(
        total_score=total,
        max_score=questionnaire.max_score,
        min_score=questionnaire.min_score,
        threshold=questionnaire.high_risk_threshold,
        level=level,
        level_label=questionnaire.level_labels[level],
        blocked=bool(blocked_reasons),
        blocked_reasons=blocked_reasons,
        override_applied=bool(override_reasons),
        override_reasons=override_reasons,
        flags=flags,
        category_scores=category_scores,
        answered=answered,
        total_factors=len(questionnaire.factors),
        missing_factors=missing,
    )
