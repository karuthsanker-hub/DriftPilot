from __future__ import annotations

import pytest

from driftpilot.catalyst.headline_parser import HeadlineParsed, parse_headline


@pytest.mark.parametrize(
    ("headline", "eps_actual", "eps_estimate", "eps_beat_pct"),
    [
        ("REGN Q1 Adj. EPS $9.47 Beats $8.89 Estimate", 9.47, 8.89, 6.524),
        ("PBH Q2 EPS $1.09 Beats $1.08 Estimate", 1.09, 1.08, 0.926),
        ("ACME GAAP EPS $3.59 Beats $3.39 Estimate", 3.59, 3.39, 5.900),
        ("XYZ EPS $0.12 Misses $0.15 Estimate", 0.12, 0.15, -20.000),
        ("XYZ EPS $0.12 Missed $0.15 Consensus", 0.12, 0.15, -20.000),
        ("MegaCo Adjusted Earnings Per Share $2.04 Tops $2.00 Estimate", 2.04, 2.00, 2.000),
        ("Retailer Diluted EPS ($0.10) Beats ($0.12) Estimate", -0.10, -0.12, 16.667),
        ("Bank EPS -$0.08 Below $0.02 Estimate", -0.08, 0.02, -500.000),
    ],
)
def test_eps_actual_estimate_and_beat_pct(
    headline: str,
    eps_actual: float,
    eps_estimate: float,
    eps_beat_pct: float,
) -> None:
    parsed = parse_headline(headline)

    assert parsed.eps_actual == pytest.approx(eps_actual)
    assert parsed.eps_estimate == pytest.approx(eps_estimate)
    assert parsed.eps_beat_pct == pytest.approx(eps_beat_pct, abs=0.01)


@pytest.mark.parametrize(
    ("headline", "actual_m", "estimate_m", "beat_pct"),
    [
        ("REGN Sales $3.605B Beat $3.483B Estimate", 3605.0, 3483.0, 3.503),
        ("PBH Sales $283.785M Beat $282.093M Estimate", 283.785, 282.093, 0.600),
        ("WidgetCo Revenue $1.2B Missed $1.3B Estimate", 1200.0, 1300.0, -7.692),
        ("CloudCo Revenues $950M Beats $900M Consensus", 950.0, 900.0, 5.556),
        ("TinyCo Sales $800K Beat $700K Estimate", 0.8, 0.7, 14.286),
        ("MegaCo Revenue $1.2T Above $1.1T Estimate", 1_200_000.0, 1_100_000.0, 9.091),
        ("RetailCo Revenue of $4.5B Below $4.7B Consensus", 4500.0, 4700.0, -4.255),
    ],
)
def test_revenue_actual_estimate_and_beat_pct(
    headline: str,
    actual_m: float,
    estimate_m: float,
    beat_pct: float,
) -> None:
    parsed = parse_headline(headline)

    assert parsed.revenue_actual_m == pytest.approx(actual_m)
    assert parsed.revenue_estimate_m == pytest.approx(estimate_m)
    assert parsed.revenue_beat_pct == pytest.approx(beat_pct, abs=0.01)


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("ACME Raises FY25 Revenue Growth Guidance", "up"),
        ("ACME Boosts Full-Year Outlook After Strong Q1", "up"),
        ("ACME FY2026 GAAP EPS Guidance Raised From $19.76-$20.22 To $20.08-$20.44", "up"),
        ("ACME Lowers FY Guidance", "down"),
        ("ACME Cuts Full-Year Revenue Forecast", "down"),
        ("ACME Guidance Below Prior Outlook, Shares Fall", "down"),
        ("ACME Reaffirms Full-Year Outlook", "maintained"),
        ("ACME Maintains FY2026 Guidance", "maintained"),
        ("ACME Confirms Forecast After Investor Day", "maintained"),
    ],
)
def test_guidance_direction(headline: str, expected: str) -> None:
    assert parse_headline(headline).guidance_direction == expected


def test_mixed_signal_detects_eps_beat_with_lowered_guidance() -> None:
    headline = "ACME Q1 EPS $1.10 Beats $1.00 Estimate, Lowers FY Guidance"

    parsed = parse_headline(headline)

    assert parsed.eps_beat_pct == pytest.approx(10.0)
    assert parsed.guidance_direction == "down"
    assert parsed.is_mixed_signal is True


def test_mixed_signal_false_for_beat_with_raised_guidance() -> None:
    headline = "ACME Q1 EPS $1.10 Beats $1.00 Estimate, Raises FY Guidance"

    parsed = parse_headline(headline)

    assert parsed.eps_beat_pct == pytest.approx(10.0)
    assert parsed.guidance_direction == "up"
    assert parsed.is_mixed_signal is False


@pytest.mark.parametrize(
    "headline",
    [
        "Progressive's November Surge: EPS Soars 49%",
        "ACME Announces Quarterly Dividend",
        "ACME Reports Results Without Consensus Detail",
        "Analyst Says ACME Beat Expectations But Provides No Numbers",
    ],
)
def test_unparseable_headlines_return_none_fields(headline: str) -> None:
    assert parse_headline(headline) == HeadlineParsed()


def test_eps_zero_estimate_does_not_divide_by_zero() -> None:
    parsed = parse_headline("ACME EPS $0.12 Beats $0.00 Estimate")

    assert parsed.eps_actual == pytest.approx(0.12)
    assert parsed.eps_estimate == pytest.approx(0.0)
    assert parsed.eps_beat_pct is None


def test_combined_eps_and_revenue_headline_extracts_both() -> None:
    headline = (
        "REGN Q1 Adj. EPS $9.47 Beats $8.89 Estimate, "
        "Sales $3.605B Beat $3.483B Estimate"
    )

    parsed = parse_headline(headline)

    assert parsed.eps_beat_pct == pytest.approx(6.524, abs=0.01)
    assert parsed.revenue_beat_pct == pytest.approx(3.503, abs=0.01)


# Hardcoded from data/driftpilot/catalyst_events_2024.sqlite3 so this regression
# test remains deterministic even when the local catalyst DB is absent.
REAL_CATALYST_DB_HEADLINES = [
    (
        "Accenture Q1 2025 GAAP EPS $3.59 Beats $3.39 Estimate, "
        "Sales $17.69B Beat $17.12B Estimate"
    ),
    "FedEx Q2 2025 Adj. EPS $4.05 Beats $3.91 Estimate, Sales $22B Miss $22.112B Estimate",
    "Ciena Q4 2024 Adj. EPS $0.54 Misses $0.66 Estimate, Sales $1.124B Beat $1.104B Estimate",
    "Ross Stores Q3 EPS $1.48 Beats $1.40 Estimate, Sales $5.07B Miss $5.15B Estimate",
    (
        "TransDigm Gr Q4 2024 Adj. EPS $9.83 Beats $9.32 Estimate, "
        "Sales $2.185B Beat $2.172B Estimate"
    ),
    "AutoZone Q1 2025 GAAP EPS $32.52 Misses $33.76 Estimate, Sales $4.279B Miss $4.302B Estimate",
    "Cintas Q2 2025 GAAP EPS $1.09 Beats $1.01 Estimate, Sales $2.56B Inline",
    "Nike Q2 2025 GAAP EPS $0.78 Beats $0.65 Estimate, Sales $12.354B Beat $12.133B Estimate",
    "Consolidated Edison Q3 2024 Adj. EPS $1.68 Beats $1.62 Estimate",
    "Public Service Enterprise Q3 2024 Adj. EPS $0.90 Beats $0.87 Estimate, Sales $2.642B vs $2.499B Estimate",
    "Garmin Q3 2024 Adj. EPS $1.99 Beats $1.44 Estimate, Sales $1.586B Beat $1.444B Estimate",
    "Jabil Q1 2025 Adj. EPS $2.00 Beats $1.88 Estimate, Sales $6.994B Beat $6.608B Estimate",
    "Agilent Technologies Q4 2024 Adj. EPS $1.46 Beats $1.41 Estimate, Sales $1.700B Beat $1.672B Estimate",
    "Cboe Global Markets Q3 2024 Adj. EPS $2.22 Beats $2.19 Estimate, Sales $532.000M Beat $530.762M Estimate",
    "Carnival Q4 2024 Adj. EPS $0.14 Beats $0.08 Estimate, Sales $5.938B Beat $5.932B Estimate",
    "Casey's General Stores Q2 2025 GAAP EPS $4.85 Beats $4.26 Estimate, Sales $3.95B Miss $4.02B Estimate",
    "Paychex Q2 2025 Adj EPS $1.14 Beats $1.13 Estimate, Sales $1.32B Inline",
    "Dollar Gen Q3 2024 Adj EPS $0.89 Misses $0.94 Estimate, Sales $10.20B Beat $10.14B Estimate",
    "Ulta Beauty Q3 2024 Adj EPS $5.14 Beats $4.54 Estimate, Sales $2.53B Beat $2.50B Estimate",
    "Darden Restaurants Q2 2025 Adj EPS $2.03 Beats $2.02 Estimate, Sales $2.89B Miss $2.90B Estimate",
    "NetApp Q2 2025 Adj. EPS $1.87 Beats $1.78 Estimate, Sales $1.658B Beat $1.645B Estimate",
    "Steris Q2 2025 Adj EPS $2.14 Beats $2.12 Estimate",
    "Smurfit WestRock Q3 2024 GAAP EPS $(0.30) Misses $0.76 Estimate",
    "Lennar Q4 2024 Adj EPS $4.03 Misses $4.16 Estimate, Sales $9.95B Miss $10.08B Estimate",
    "HP Q4 2024 GAAP EPS $0.93, Inline, Sales $14.05B Beat $13.99B Estimate",
    "Dollar Tree Q3 2024 Adj EPS $1.12 Beats $1.07 Estimate, Sales $7.57B Beat $7.44B Estimate",
    "Viatris Q3 2024 Adj. EPS $0.75 Beats $0.68 Estimate, Sales $3.740B Beat $3.709B Estimate",
    "Amcor Q1 2025 GAAP EPS $0.13 Misses $0.14 Estimate, Sales $3.353B Miss $3.493B Estimate",
    "Bunge Q3 2024 Adj. EPS $2.29 Beats $2.14 Estimate, Sales $12.908B Beat $12.774B Estimate",
    "Brown & Brown Q3 2024 Adj EPS $0.91 Beats $0.88 Estimate, Sales $1.186B Beat $1.17B Estimate",
    "Weyerhaeuser Q3 2024 Adj. EPS $0.04 Beats $0.01 Estimate, Sales $1.681B Miss $1.687B Estimate",
    (
        "EchoStar Q3 2024 GAAP EPS $(0.52) Misses $(0.37) Estimate, "
        "Total Revenue $3.890B Miss $3.909B Estimate"
    ),
]


@pytest.mark.parametrize("headline", REAL_CATALYST_DB_HEADLINES)
def test_real_catalyst_db_headlines_extract_structured_numbers(headline: str) -> None:
    assert len(REAL_CATALYST_DB_HEADLINES) >= 30

    parsed = parse_headline(headline)

    assert parsed != HeadlineParsed(), headline
    assert (
        parsed.eps_beat_pct is not None or parsed.revenue_beat_pct is not None
    ), headline


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        (
            "Accenture Raises FY25 Revenue Growth To 4% - 7% In Local Currency, "
            "Compared To 3% - 6% Previously; Revises GAAP EPS Outlook From "
            "$12.55 - $12.91 To $12.43 - $12.79, Est $12.74",
            "up",
        ),
        (
            "Garmin Raises FY24 Outlook: Adj EPS from $6.00 to $6.85 vs $6.08 Est; "
            "Sales from $5.95B to $6.12B vs $5.99B Est",
            "up",
        ),
        (
            "Viatris Lowers FY24 Adj EPS Guidance from $2.58-$2.73 to "
            "$2.56-$2.71 vs $2.67 Est; Affirms Sales Guidance Of "
            "$14.60B-$15.10B vs $14.80B Est",
            "down",
        ),
        ("Amcor Reaffirms 2025 Outlook Of Adj. EPS $0.72-$0.76", "maintained"),
        (
            "Centene Expects 2025 Adjusted EPS Of Greater Than $7.25 Compared To "
            "Consensus Of $6.97, Reaffirms 2024 Adjusted EPS Guidance Of Greater "
            "Than $6.80 Vs. Consensus Of $6.70",
            "maintained",
        ),
    ],
)
def test_real_catalyst_db_guidance_headlines(headline: str, expected: str) -> None:
    assert parse_headline(headline).guidance_direction == expected
