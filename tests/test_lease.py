"""The sample lease must trigger every planted issue. Run: uv run python -m tests.test_lease"""

import json
from pathlib import Path

from tools import run_tool

SAMPLE = Path(__file__).parent.parent / "data" / "sample_lease.txt"


def review_sample() -> dict:
    state = {"lease_text": SAMPLE.read_text()}
    return json.loads(run_tool("review_lease", {}, state))


def flag_with(result: dict, severity: str, *words: str) -> dict | None:
    for f in result["flags"]:
        text = (f["explanation"] + " " + (f["clause_excerpt"] or "")).lower()
        if f["severity"] == severity and all(w.lower() in text for w in words):
            return f
    return None


def test_planted_issues():
    r = review_sample()
    assert "error" not in r, r
    assert r["is_sample"]
    assert r["address"] == "184 Claremont Avenue"
    assert r["extracted"]["unit"] == "4B", r["extracted"]["unit"]
    assert r["extracted"]["term"]["start"] == "2026-11-01" and r["extracted"]["term"]["months_stated"] == 12
    checks = {
        "deposit = 2 months' rent": flag_with(r, "likely not allowed under NY law", "deposit", "2.0 months"),
        "$75 late fee after 2 days": flag_with(r, "likely not allowed under NY law", "late fee", "after 2 days", "$75"),
        "$150 application fee": flag_with(r, "likely not allowed under NY law", "$150"),
        "one-sided attorney's fees": flag_with(r, "check with landlord", "attorney"),
        "rent $2,850 vs $2,950": flag_with(r, "inconsistent", "$2,850.00", "$2,950.00"),
        "landlord name != HPD registration": flag_with(r, "check with landlord", "claremont gardens realty",
                                                       "184-188 claremont investors"),
    }
    missing = {m["disclosure"] for m in r["missing_disclosures"]}
    checks["no bedbug disclosure"] = "bedbug history (previous year)" in missing or None
    failed = [name for name, found in checks.items() if not found]
    assert not failed, f"Planted issues not found: {failed}"
    # Disclosures that ARE in the sample must not be reported missing.
    assert not missing & {"window guard notice", "lead-based paint notice", "sprinkler system notice"}, missing
    # Every rule-based flag links its official source.
    for f in r["flags"]:
        if f["severity"] == "likely not allowed under NY law":
            assert f["source_url"] and f["source_url"].startswith("https://"), f
    return r


def test_no_lease():
    r = json.loads(run_tool("review_lease", {}, {}))
    assert r["error"].startswith("No lease"), r


def test_scanned_pdf_message():
    import lease
    try:
        lease.pdf_to_text(b"not a pdf")
    except ValueError as e:
        assert "Paste the lease text" in str(e)
    else:
        raise AssertionError("expected ValueError")


if __name__ == "__main__":
    result = test_planted_issues()
    test_no_lease()
    test_scanned_pdf_message()
    print("All lease tests passed.\n")
    print(json.dumps(result, indent=1)[:6000])
