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


def lease_with(clause: str, rent: str = "$3,000.00") -> str:
    """A minimal lease (no address, so only the rule checks run) around one clause under test."""
    return (f"RESIDENTIAL LEASE between Example Realty LLC (\"Landlord\") and Sam Tenant (\"Tenant\").\n\n"
            f"1. RENT. Tenant shall pay monthly rent of {rent}, due on the first day of each month.\n\n"
            f"2. {clause}\n\nTHERE IS NO MAINTAINED AND OPERATIVE SPRINKLER SYSTEM IN THE LEASED PREMISES.\n")


def money_flags(clause: str, rule_words: str) -> list[dict]:
    r = json.loads(run_tool("review_lease", {"focus": "money"}, {"lease_text": lease_with(clause)}))
    assert "error" not in r, r
    return [f for f in r["flags"] if f["severity"] == "likely not allowed under NY law" and rule_words in f["explanation"]]


def test_caps_written_into_the_lease_are_not_flagged():
    compliant = [
        # The real-lease false positive: the lease itself applies the legal cap.
        ("late fee", "LATE CHARGE. If rent is not paid more than five (5) days after it is due, Tenant shall pay a "
                     "reasonable late charge of the lesser of fifty (50) dollars or five percent (5%) of the monthly rent."),
        ("late fee", "LATE CHARGE. If rent is more than five (5) days late, a late fee of five percent of the monthly "
                     "rent, not to exceed $50.00, is due."),
        ("late fee", "LATE CHARGE. If rent is more than five (5) days late, Tenant shall pay $50.00 or 5% of the monthly "
                     "rent, whichever is less."),
        ("deposit", "SECURITY DEPOSIT. Tenant shall deposit one month's rent or the maximum permitted by law, whichever "
                    "is less, as security."),
        ("deposit", "SECURITY DEPOSIT. Tenant shall deposit two (2) months' rent or the maximum permitted by law, "
                    "whichever is less, as security."),
        ("application", "APPLICATION FEE. Tenant paid the actual cost of the background check or $20.00, whichever is less."),
        ("application", "APPLICATION FEE. Tenant paid a credit check fee of $50.00 or the actual cost, whichever is less, "
                        "but not to exceed $20.00."),
    ]
    for words, clause in compliant:
        found = money_flags(clause, words)
        assert not found, f"compliant clause was flagged: {clause!r} -> {found}"


def test_amounts_over_the_cap_are_still_flagged():
    over = [
        ("late fee", "LATE CHARGES. If any rent is not received more than two (2) days after it is due, Tenant shall "
                     "pay a late fee of $75.00."),
        ("late fee", "LATE CHARGE. If rent is more than five (5) days late, Tenant shall pay a late fee of 5% of the "
                     "monthly rent."),
        ("deposit", "SECURITY DEPOSIT. Upon signing, Tenant shall deposit $6,000.00, equal to two (2) months' rent."),
        ("application", "APPLICATION FEE. Tenant has paid a non-refundable application and credit check fee of $150.00."),
    ]
    for words, clause in over:
        assert money_flags(clause, words), f"over-the-cap clause was not flagged: {clause!r}"


def test_real_lease_shape():
    """Premises address in a combined apartment/term clause (a house-number range and a bare unit),
    the landlord's business address elsewhere, and a dollar-amount deposit."""
    text = (Path(__file__).parent / "fixtures" / "lease_real_shape.txt").read_text()
    r = json.loads(run_tool("review_lease", {}, {"lease_text": text}))
    assert "error" not in r, r
    # The premises address, not the landlord's office in Great Neck; and the note says which was used.
    assert r["address"] == "155 East 92 Street", r.get("building_note")
    assert "155 East 92nd Street" in r["building_note"] and "40 Harbor Road" in r["building_note"], r["building_note"]
    assert r["extracted"]["unit"] == "4N", r["extracted"]["unit"]
    assert r["extracted"]["landlord_name"] == "Sample Gardens LLC", r["extracted"]["landlord_name"]
    # The deposit flag quotes the deposit sentence, not the apartment/term paragraph.
    deposit = [f for f in r["flags"] if "deposit" in f["explanation"] and f["severity"] == "likely not allowed under NY law"]
    assert deposit, r["flags"]
    quote = deposit[0]["clause_excerpt"]
    assert "security deposit" in quote and "$6,000.00" in quote and "term" not in quote.lower(), quote
    # The late charge is capped by the lease itself: no late-fee flag.
    assert not [f for f in r["flags"] if "late fee" in f["explanation"]], r["flags"]
    # Every flag's excerpt is text from the lease.
    flat = " ".join(text.split())
    for f in r["flags"]:
        if f["clause_excerpt"] and not f["clause_excerpt"].startswith(("Landlord:", "Apartment ")):
            assert f["clause_excerpt"].rstrip(".…").split(" | ")[0][:80] in flat, f["clause_excerpt"]
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
    test_fictional_label_on_every_section()
    pages = test_label_survives_pdf()
    print(f"Fictional label on every section, and on all {pages} PDF pages after extraction.")
    test_caps_written_into_the_lease_are_not_flagged()
    test_amounts_over_the_cap_are_still_flagged()
    print("Caps written into a lease aren't flagged; amounts over the cap still are.")
    shape = test_real_lease_shape()
    print(f"Real-lease shape: {shape['building_note']}")
    test_no_lease()
    test_scanned_pdf_message()
    print("All lease tests passed.\n")
    print(json.dumps(result, indent=1)[:6000])
