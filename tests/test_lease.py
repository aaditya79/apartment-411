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
    assert r["address"] == "155 East 92 Street"
    assert r["extracted"]["unit"] == "4B", r["extracted"]["unit"]
    assert r["extracted"]["term"]["start"] == "2026-11-01" and r["extracted"]["term"]["months_stated"] == 12
    checks = {
        "deposit = 2 months' rent": flag_with(r, "likely not allowed under NY law", "deposit", "2.0 months"),
        "$75 late fee after 2 days": flag_with(r, "likely not allowed under NY law", "late fee", "after 2 days", "$75"),
        "$150 application fee": flag_with(r, "likely not allowed under NY law", "$150"),
        "one-sided attorney's fees": flag_with(r, "check with landlord", "attorney"),
        "rent $2,850 vs $2,950": flag_with(r, "inconsistent", "$2,850.00", "$2,950.00"),
        "landlord name != HPD registration": flag_with(r, "check with landlord", "example realty llc",
                                                       "92nd street 6 llc"),
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


def test_fictional_label_on_every_section():
    import re
    import lease
    text = SAMPLE.read_text()
    # Split at each numbered section; every section must carry its own label.
    sections = re.split(r"\n(?=\d{1,2}\. [A-Z])", text)[1:]
    assert len(sections) == 13, len(sections)
    unlabeled = [s.split(".")[0] for s in sections if lease.SAMPLE_LABEL not in s]
    assert not unlabeled, f"sections without the label: {unlabeled}"
    lines = text.strip().splitlines()
    assert lines[0].strip("[]") == lease.SAMPLE_LABEL and lines[-1].strip("[]") == lease.SAMPLE_LABEL
    assert "Example Realty LLC" in text 


def test_label_survives_pdf():
    """If the sample is ever a PDF: the label must be on every extracted page, and the review unchanged."""
    import lease
    from pypdf import PdfReader
    import io
    from tests.make_pdf import text_to_pdf

    pdf = text_to_pdf(SAMPLE.read_text(), header=f"[{lease.SAMPLE_LABEL}]", footer=f"[{lease.SAMPLE_LABEL}]")
    pages = [p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages]
    assert len(pages) >= 2, "want a multi-page PDF to test every page"
    for i, page in enumerate(pages, start=1):
        assert page.count(lease.SAMPLE_LABEL) >= 2, f"page {i}: header/footer label lost in extraction"
    text = lease.pdf_to_text(pdf)
    assert lease.is_sample(text)
    r = json.loads(run_tool("review_lease", {}, {"lease_text": text}))
    assert r["is_sample"] and r["flag_counts"] == review_sample()["flag_counts"], r["flag_counts"]
    return len(pages)


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
    test_fictional_label_on_every_section()
    pages = test_label_survives_pdf()
    print(f"Fictional label on every section, and on all {pages} PDF pages after extraction.")
    test_no_lease()
    test_scanned_pdf_message()
    print("All lease tests passed.\n")
    print(json.dumps(result, indent=1)[:6000])
