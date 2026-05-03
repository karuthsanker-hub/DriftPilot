"""Headline classifier ported byte-for-byte from scripts/catalyst_category_spike.py.

The taxonomy rules and `_categorize` function below are an exact port of the
spike's validated rules. They produced the edge ratios (5.09x earnings/report,
2.91x analyst/target_cut, 1.42x analyst/target_raise) on full-2024 mid-cap data.
DO NOT modify the regex/keyword content — any "improvement" is a bug.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CategoryRule:
    category: str
    subcategory: str
    keywords: tuple[str, ...]  # any-of match (case-insensitive), word-boundary


# ORDER MATTERS — first match wins. More specific subcategories first within a category.
TAXONOMY_RULES: tuple[CategoryRule, ...] = (
    # ---- earnings ----
    CategoryRule("earnings", "beat",         ("beats earnings", "earnings beat", "tops estimates", "beats estimates", "beats q", "beats fourth-quarter", "beats third-quarter")),
    CategoryRule("earnings", "miss",         ("misses earnings", "earnings miss", "misses estimates", "missed estimates", "missed q", "below estimates")),
    CategoryRule("earnings", "guidance_up",  ("raises guidance", "raises outlook", "lifts forecast", "raises q1 forecast", "boosts forecast")),
    CategoryRule("earnings", "guidance_down",("cuts guidance", "lowers guidance", "lowers outlook", "warns", "guidance below")),
    CategoryRule("earnings", "preannounce",  ("preannounces", "pre-announce", "preliminary q", "preliminary results")),
    CategoryRule("earnings", "report",       ("earnings report", " q1 report", " q2 report", " q3 report", " q4 report", " eps ", "reports earnings", "earnings results", "fourth-quarter results", "third-quarter results", "first-quarter results")),
    # ---- analyst ----
    CategoryRule("analyst", "target_raise",  ("raises price target", "raises target", "boosts price target", "lifts price target", "increases price target")),
    CategoryRule("analyst", "target_cut",    ("cuts price target", "lowers price target", "reduces price target")),
    CategoryRule("analyst", "upgrade",       ("upgrades", "upgraded to ", "raised to buy", "raised to overweight", "raised to outperform")),
    CategoryRule("analyst", "downgrade",     ("downgrades", "downgraded to ", "lowered to sell", "lowered to underweight", "lowered to underperform")),
    CategoryRule("analyst", "initiates",     ("initiates coverage", "initiated coverage", "starts coverage")),
    CategoryRule("analyst", "reiterates",    ("reiterates", "maintains", "reaffirms rating")),
    # ---- M&A ----
    CategoryRule("m_and_a", "acquires",      (" acquires ", " to acquire ", " buys ", " agrees to acquire ", "acquisition of")),
    CategoryRule("m_and_a", "acquired",      ("acquired by", "to be acquired", "agrees to be acquired")),
    CategoryRule("m_and_a", "merger",        ("merger", "merge with", " merges with ")),
    CategoryRule("m_and_a", "divestiture",   ("divests", "divestiture", "spin-off", "spinoff", "to spin off")),
    # ---- product ----
    CategoryRule("product", "launch",        ("launches", "unveils", "introduces", "debut of", "rolls out", "announces new")),
    CategoryRule("product", "partnership",   ("partnership", "partners with", "joins forces", "teams up with", "strategic alliance")),
    CategoryRule("product", "contract_won",  ("wins contract", "awarded contract", "secures deal", "signs deal", "wins $")),
    # ---- regulatory ----
    CategoryRule("regulatory", "fda_approval",("fda approves", "fda approved", "fda clearance", "receives fda")),
    CategoryRule("regulatory", "fda_rejection",("fda rejects", "fda denies", "complete response letter", "crl")),
    CategoryRule("regulatory", "sec_action",  ("sec charges", "sec investigation", "sec settlement")),
    CategoryRule("regulatory", "govt_contract",("government contract", "department of defense", "pentagon awards")),
    CategoryRule("regulatory", "investigation",("under investigation", "doj investigates", "antitrust")),
    # ---- legal ----
    CategoryRule("legal", "lawsuit",         ("lawsuit", "sued", "sues", "filed suit", "class action")),
    CategoryRule("legal", "settlement",      ("settlement", "settles", "reaches deal to settle")),
    CategoryRule("legal", "fine",            ("fined", " fine of ", " imposes fine ")),
    CategoryRule("legal", "criminal",        ("criminal charges", "indicted", "pleads guilty")),
    # ---- insider ----
    CategoryRule("insider", "insider_buying",("insider buying", "insider purchased", "ceo bought", "executive buys")),
    CategoryRule("insider", "insider_selling",("insider selling", "insider sold", "ceo sold", "executive sells")),
    CategoryRule("insider", "form_4",        ("form 4", "form-4", "section 16")),
    # ---- macro ----
    CategoryRule("macro", "fomc",            ("fomc", "fed meeting", "powell")),
    CategoryRule("macro", "rate_decision",   ("rate decision", "rate hike", "rate cut", "cuts rates", "raises rates")),
    CategoryRule("macro", "cpi",             ("cpi", "consumer price")),
    CategoryRule("macro", "jobs",            ("jobs report", "nonfarm payrolls", "unemployment rate")),
    CategoryRule("macro", "gdp",             ("gdp report", "gross domestic product")),
    # ---- filing (catch-all for SEC filings without other categorization) ----
    CategoryRule("filing", "8k",             ("8-k", "form 8-k")),
    CategoryRule("filing", "10k",            ("10-k", "annual report filed")),
    CategoryRule("filing", "10q",            ("10-q", "quarterly report filed")),
    CategoryRule("filing", "13d",            ("13d", "13-d", "13g")),
    CategoryRule("filing", "8a",             ("8-a")),
)


def _categorize(headline: str) -> tuple[str, str]:
    """Return (category, subcategory) using priority-ordered keyword match.

    First rule whose keyword set hits returns. Fallback: ('other', 'generic').
    Lowercase comparison.
    """
    h = headline.lower()
    for rule in TAXONOMY_RULES:
        for kw in rule.keywords:
            if kw in h:
                return rule.category, rule.subcategory
    return "other", "generic"


class CatalystClassifier:
    def classify(self, headline: str) -> tuple[str, str, str]:
        category, subcategory = _categorize(headline)
        pillar = self._pillar_for(category)
        return category, subcategory, pillar

    @staticmethod
    def _pillar_for(category: str) -> str:
        # All currently-validated categories are micro
        if category in {"earnings", "analyst", "filing", "m_and_a", "product"}:
            return "micro"
        if category == "macro":
            return "macro"
        return "micro"  # default for "other/generic"
