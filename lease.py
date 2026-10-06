"""Lease review: pull the key terms out of a lease and check them against NY/NYC rules.

Deterministic on purpose (regex + keywords, no model call): every flag points at
the clause it came from and the official page the rule came from, so a tenant can
check both. Each rule below was verified on that page on 2026-10-05; rules I
couldn't verify on an official source were left out.

City-record cross-checks (owner name, floors, bedbug filings, violations) live in
tools.review_lease, which has the building.
"""

import io
import logging
import re
from datetime import date, datetime

from pypdf import PdfReader
from pypdf.errors import PdfReadError

# Scanned leases are often 10-30 MB; Cloud Run's HTTP/1 request limit is 32 MiB, leaving room for the form.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 60          # a lease is rarely longer; caps time and memory on a 512 MiB instance
ACCEPTED = "a PDF, a Word file (.docx) or a plain-text file (.txt)"
logging.getLogger("pypdf").setLevel(logging.ERROR)  # never echo anything about a user's file to the logs
SAMPLE_LABEL = "FICTIONAL SAMPLE — not a real lease"  # on every section of data/sample_lease.txt
LIKELY_NOT_ALLOWED, CHECK, INCONSISTENT = "likely not allowed under NY law", "check with landlord", "inconsistent"

# --- Rules (each verified on the official page in `source`) ---

RULES = {
    "deposit_cap": {
        "rule": "A security deposit can't be more than one month's rent.",
        "citation": "NY General Obligations Law § 7-108",
        "source": "https://www.nysenate.gov/legislation/laws/GOB/7-108",
    },
    "deposit_return": {
        "rule": "The landlord must return the deposit, with an itemized statement of any deductions, within 14 "
                "days after the tenant moves out.",
        "citation": "NY General Obligations Law § 7-108(1-a)",
        "source": "https://www.nysenate.gov/legislation/laws/GOB/7-108",
    },
    "application_fee": {
        "rule": "Apart from background and credit checks capped at the actual cost or $20, whichever is less, "
                "landlords can't charge application fees; the fee is waived if the tenant provides a check from "
                "the past 30 days.",
        "citation": "NY Real Property Law § 238-a(1)",
        "source": "https://www.nysenate.gov/legislation/laws/RPP/238-A",
    },
    "late_fee": {
        "rule": "A late fee is allowed only if rent is more than 5 days late, and is capped at $50 or 5% of the "
                "monthly rent, whichever is less.",
        "citation": "NY Real Property Law § 238-a(2)",
        "source": "https://www.nysenate.gov/legislation/laws/RPP/238-A",
    },
    "broker_fee": {
        "rule": "Since June 11, 2025, a broker who represents the landlord can't charge the tenant a fee; tenants "
                "must get an itemized written disclosure of all fees before signing.",
        "citation": "NYC FARE Act, Local Law 119 of 2024",
        "source": "https://www.nyc.gov/site/dca/about/FAQ-Broker-Fees.page",
    },
    "attorney_fees": {
        "rule": "If a residential lease lets the landlord recover attorney's fees, the law gives the tenant the "
                "same right; any waiver of this is void.",
        "citation": "NY Real Property Law § 234",
        "source": "https://www.nysenate.gov/legislation/laws/RPP/234",
    },
    "bedbug_disclosure": {
        "rule": "Owners must give tenants signing a vacancy (new) lease the building's bedbug history for the "
                "previous year.",
        "citation": "NYC Admin Code § 27-2018.1 (DHCR form DBB-N)",
        "source": "https://www.nyc.gov/site/hpd/services-and-information/bedbugs.page",
    },
    "window_guards": {
        "rule": "New leases in buildings with 3+ apartments must include a window guard notice.",
        "citation": "NYC Health Code § 131.15",
        "source": "https://www.nyc.gov/assets/doh/downloads/pdf/about/healthcode/health-code-article131.pdf",
    },
    "lead_paint": {
        "rule": "Leases in buildings with 3+ apartments built before 1960 must come with the lead-based paint "
                "notice.",
        "citation": "NYC Local Law 1 of 2004",
        "source": "https://www.nyc.gov/site/hpd/services-and-information/lead-based-paint.page",
    },
    "sprinkler": {
        "rule": "Every residential lease must say, in bold, whether there is a maintained and operative sprinkler "
                "system, and if so when it was last maintained and inspected.",
        "citation": "NY Real Property Law § 231-a",
        "source": "https://www.nysenate.gov/legislation/laws/RPP/231-A",
    },
    "heat_minimums": {
        "rule": "Oct 1-May 31: at least 68°F from 6am-10pm when it's below 55°F outside, and at least 62°F "
                "overnight; hot water at 120°F all year.",
        "citation": "NYC Housing Maintenance Code (as published by HPD)",
        "source": "https://www.nyc.gov/site/hpd/services-and-information/heat-and-hot-water-information.page",
    },
    "rent_stabilization_rider": {
        "rule": "Rent-stabilized leases (vacancy and renewal) must come with the DHCR lease rider.",
        "citation": "NY State DHCR, Rent Stabilization lease rider",
        "source": "https://hcr.ny.gov/leases",
    },
}

# --- Text helpers ---

WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14, "fifteen": 15, "twenty": 20, "thirty": 30}
MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")
DATE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
                  r"\s+(\d{1,2}),?\s+(\d{4})\b|\b(\d{1,2})/(\d{1,2})/(\d{4})\b|\b(\d{4})-(\d{2})-(\d{2})\b", re.IGNORECASE)


def megabytes(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def file_kind(head: bytes, filename: str) -> str:
    """What a file really is, from its first bytes (names lie), falling back to the extension."""
    name = filename.lower()
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):  # a zip: .docx, .pages, .xlsx... all look alike here
        return "docx" if name.endswith(".docx") else ("pages" if name.endswith(".pages") else "zip")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "doc"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypheis"):
        return "heic"
    if head.startswith((b"GIF8", b"II*\x00", b"MM\x00*")):
        return "image"
    if name.endswith((".txt", ".text")) or b"\x00" not in head:
        return "txt"
    return name.rsplit(".", 1)[-1] if "." in name else "unknown"


UNSUPPORTED = {
    "doc": "an old Word document (.doc)",
    "pages": "an Apple Pages document",
    "rtf": "a Rich Text (.rtf) document",
    "jpeg": "a photo (JPEG)", "png": "an image (PNG)", "heic": "an iPhone photo (HEIC)", "image": "an image",
    "zip": "a zip archive",
}


def unsupported_message(kind: str) -> str:
    what = UNSUPPORTED.get(kind, f"a .{kind} file" if kind != "unknown" else "a file type we can't read")
    tip = " Save it as a PDF or .docx (File → Export), or paste the lease text." if kind in ("doc", "pages", "rtf") \
        else " For a photo of a lease, paste the lease text instead." if kind in ("jpeg", "png", "heic", "image") \
        else " Or paste the lease text."
    return f"That's {what}. Upload {ACCEPTED}.{tip}"


def pdf_to_text(file) -> str:
    """Text of a PDF (a file object or bytes). Raises ValueError with a user-facing message.

    Reads page by page and stops after MAX_PDF_PAGES. A scan has images but no text layer: if the
    first pages yield no text, say so right away instead of walking a 30 MB file."""
    try:
        reader = PdfReader(io.BytesIO(file) if isinstance(file, bytes) else file)
        parts = []
        for i, page in enumerate(reader.pages):
            if i >= MAX_PDF_PAGES:
                break
            parts.append(page.extract_text() or "")
            if i == 2 and len("".join(parts).strip()) < 50:
                break  # three pages without text: it's a scan
        text = "\n".join(parts)
    except (PdfReadError, ValueError, KeyError, OSError) as e:
        raise ValueError("That file couldn't be read as a PDF. Paste the lease text instead.") from e
    if len(text.strip()) < 200:
        raise ValueError("This PDF is a scan with no text layer. Paste the lease text instead.")
    return text


def docx_to_text(file) -> str:
    """Text of a Word .docx (a file object or bytes), with no extra dependency: a .docx is a zip
    whose word/document.xml holds paragraphs (w:p) made of text runs (w:t)."""
    import xml.etree.ElementTree as ET
    import zipfile

    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(file) if isinstance(file, bytes) else file) as z:
            root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise ValueError("That file couldn't be read as a Word document. Save it as a PDF or paste the text.") from e
    paragraphs = ["".join(t.text or "" for t in p.iter(f"{ns}t")) for p in root.iter(f"{ns}p")]
    text = "\n\n".join(p for p in paragraphs if p.strip())
    if len(text.strip()) < 200:
        raise ValueError("That Word document has almost no text. Paste the lease text instead.")
    return text


# --- Is this a lease at all? A simple, explainable rule, not a guess. ---

LEASE_MARKERS = {
    "the word 'lease'": r"\blease\b|rental agreement",
    "a landlord (or lessor)": r"\blandlords?\b|\blessors?\b",
    "a tenant (or lessee)": r"\btenants?\b|\blessees?\b",
    "a rent amount": None,          # checked in code: "rent" near a dollar amount
    "a lease term with dates": None,            # checked in code: commence/expire/begin/end near a date
    "the premises": r"\bpremises\b|\bapartment\b|\bdemised\b|\bunit \w+",
    "a security deposit": r"security deposit",
    "a signature block": r"signature|\bsigned\b|\binitials?:|in witness whereof|executed",
}


def lease_markers(text: str) -> dict[str, bool]:
    lowered = text.lower()
    found = {name: bool(pattern and re.search(pattern, lowered)) for name, pattern in LEASE_MARKERS.items()}
    found["a rent amount"] = any(
        MONEY.search(lowered[max(0, m.start() - 80):m.end() + 80]) or
        re.search(r"dollars", lowered[max(0, m.start() - 80):m.end() + 80])
        for m in re.finditer(r"\brent\b", lowered))
    found["a lease term with dates"] = any(
        DATE.search(text[m.end():m.end() + 60])
        for m in re.finditer(r"commenc\w*|expir\w*|begin\w*|beginning|ending|ends on|term of", text, re.IGNORECASE))
    return found


def document_type(text: str) -> tuple[str, list[str]]:
    """('lease' | 'partial' | 'not_lease', markers not found).

    lease: landlord, tenant, rent with an amount and a dated term, and at least 6 of the 8 markers.
    partial: both parties, the word lease, two of premises / a security deposit / a signature block, and at
             least 5 markers (a rider, an extract): reviewed, but required disclosures can't be judged.
    not_lease: anything else; nothing is reviewed.
    """
    found = lease_markers(text)
    missing = [name for name, ok in found.items() if not ok]
    count = sum(found.values())
    parties = found["a landlord (or lessor)"] and found["a tenant (or lessee)"]
    if parties and found["a rent amount"] and found["a lease term with dates"] and count >= 6:
        return "lease", missing
    # A rider or extract still reads like a lease document: it says "lease" and has clause wording (premises,
    # a deposit, a signature block), not just the vocabulary a paper about renting would use.
    clause_like = sum(found[k] for k in ("a security deposit", "a signature block")) + bool(
        re.search(r"\bpremises\b|\bdemised\b", text, re.IGNORECASE))
    if parties and found["the word 'lease'"] and clause_like >= 2 and count >= 5:
        return "partial", missing
    return "not_lease", missing


def is_sample(text: str) -> bool:
    """The demo lease, however it arrived (text, paste or PDF: extraction can change the dash)."""
    return "fictional sample" in text.lower()


LEASE_WORDS = ("lease", "landlord", "tenant", "premises", "security deposit", "monthly rent", "rent", "term",
               "commence", "expire", "apartment", "late charge", "sublet", "renew")


def looks_like_lease(text: str) -> bool:
    """For pasted chat messages: lease wording, not length. Both parties, several lease terms, and
    the specifics a lease has (a dollar amount or a date). A ~400-character lease counts; a question
    that merely mentions a landlord doesn't."""
    lowered = text.lower()
    terms = sum(w in lowered for w in LEASE_WORDS)
    specifics = bool(MONEY.search(text) or DATE.search(text))
    return len(text) >= 200 and "landlord" in lowered and "tenant" in lowered and terms >= 5 and specifics


def clauses(text: str) -> list[str]:
    """Split a lease into sentence-sized pieces, so each flag can quote its own clause."""
    pieces = []
    for paragraph in re.split(r"\n\s*\n", text):  # blank lines separate paragraphs
        # Join wrapped lines, but keep form-style fields apart: "The rent is: $6,250.00" on one line and
        # "The amount of the security deposit is..." on the next are two statements, not one sentence.
        lines = [ln.strip() for ln in paragraph.splitlines() if ln.strip()]
        joined: list[str] = []
        for line in lines:
            ends_field = bool(joined) and re.search(r"[.:;!?)\]”\"]$|\d$", joined[-1])
            if joined and not (ends_field and re.match(r"[A-Z(“\"\[]|\d+\.\s|[a-z]\.\s", line)):
                joined[-1] = f"{joined[-1]} {line}"
            else:
                joined.append(line)
        for flat in joined:
            flat = re.sub(r"\s+", " ", flat).strip()
            if not flat or SAMPLE_LABEL in flat and len(flat) <= len(SAMPLE_LABEL) + 4:
                continue  # the sample's "[FICTIONAL SAMPLE ...]" label lines aren't clauses
            pieces += [p.strip() for p in re.split(r"(?<=[.;])\s+(?=[A-Z0-9(])", flat)]
    # "8. ATTORNEY'S FEES." is a heading, not a clause: attach it to what follows.
    merged = []
    for p in pieces:
        if len(p) <= 3:
            continue
        if merged and len(merged[-1]) <= 45 and re.fullmatch(r"[\d.\s]*[A-Z][A-Z' &/-]+\.?", merged[-1]):
            merged[-1] = f"{merged[-1]} {p}"
        else:
            merged.append(p)
    return merged


def excerpt(clause: str) -> str:
    return clause if len(clause) <= 200 else clause[:197] + "..."


def money(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in MONEY.findall(text)]


DOLLAR_WORDS = {"ten": 10, "fifteen": 15, "twenty": 20, "twenty-five": 25, "thirty": 30, "forty": 40, "fifty": 50,
                "seventy-five": 75, "one hundred": 100}
PERCENT_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                 "ten": 10}
# The lease itself applies the lower of two amounts, or a ceiling.
LESSER_OF = re.compile(r"lesser of|whichever is (less|lower|smaller)|(not|shall not|will not|never) (to )?exceed|"
                       r"no more than|not more than|up to a maximum|maximum of|capped at|"
                       r"in no event (more|greater) than", re.IGNORECASE)
# ...or defers to the law's own limit.
LEGAL_LIMIT = re.compile(r"(permitted|allowed|authori[sz]ed) (by|under) (law|statute)|legal (maximum|limit)|"
                         r"maximum (amount )?(permitted|allowed)|applicable law|\b238-a\b|\b7-108\b", re.IGNORECASE)


def dollar_amounts(text: str) -> list[float]:
    """'$50', '50 dollars', 'fifty (50) dollars', 'fifty dollars'."""
    found = money(text)
    found += [float(m.replace(",", "")) for m in re.findall(r"\(?(\d[\d,]*(?:\.\d{2})?)\)?\s*dollars", text, re.IGNORECASE)]
    for word, value in DOLLAR_WORDS.items():
        if re.search(rf"\b{word}\s+(\(\d+\)\s*)?dollars", text, re.IGNORECASE):
            found.append(float(value))
    return sorted(set(found))


def percent_values(text: str) -> list[float]:
    """'5%', '5 percent', 'five percent', 'five (5%)'."""
    found = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*(?:%|percent|per cent)", text, re.IGNORECASE)]
    for word, value in PERCENT_WORDS.items():
        if re.search(rf"\b{word}\s+(\(\d+%?\)\s*)?(?:percent|per cent)", text, re.IGNORECASE):
            found.append(float(value))
    return sorted(set(found))


MONTHS_OF_RENT = re.compile(r"(\d+(?:\.\d+)?|" + "|".join(WORD_NUMBERS) + r"|a|one and a half)\s*(?:\(\d+\)\s*)?"
                            r"months?(?:['’]s?)?\s*(?:of\s+(?:the\s+)?(?:monthly\s+)?)?rent", re.IGNORECASE)


def months_of_rent(text: str) -> float | None:
    """'one month of rent', 'one month’s rent', 'two (2) months' rent', '1 month rent' -> months."""
    m = MONTHS_OF_RENT.search(text)
    if not m:
        return None
    word = m.group(1).lower()
    return 1.5 if word == "one and a half" else 1.0 if word == "a" else (
        float(word) if word[0].isdigit() else float(WORD_NUMBERS[word]))


# "If the Rent is less than $3,000, then..." names a threshold, not the rent.
COMPARISON_BEFORE = re.compile(r"(less|more|greater|fewer) than|at (least|most)|exceeds?|over|under|below|above|"
                               r"up to|minimum|maximum", re.IGNORECASE)


def stated_amounts(text: str) -> list[float]:
    """Dollar amounts that state a figure, skipping ones introduced as a comparison or threshold."""
    out = []
    for m in MONEY.finditer(text):
        if COMPARISON_BEFORE.search(text[max(0, m.start() - 22):m.start()]):
            continue
        out.append(float(m.group(1).replace(",", "")))
    return out


def most_allowed(text: str, candidates: list[float]) -> tuple[float | None, str]:
    """The most a clause lets the landlord charge, given the amounts it names.

    "the lesser of $50 or 5%" and "5%, not to exceed $50" allow the lower amount; a clause that defers
    to the legal maximum ("one month's rent or the maximum permitted by law, whichever is less")
    can't exceed it by definition. Otherwise the highest amount named is what's allowed.
    """
    if LESSER_OF.search(text) and LEGAL_LIMIT.search(text):
        return None, "the lease caps it at the legal limit"
    if not candidates:
        return None, "no amount stated"
    if LESSER_OF.search(text):
        return min(candidates), "the lease applies the lower amount"
    return max(candidates), "as stated"


def number_of_days(text: str) -> int | None:
    """'more than two (2) days' -> 2; '5 days' -> 5."""
    m = re.search(r"\((\d+)\)\s*(?:business\s+)?days?|(\d+)\s*(?:business\s+)?days?|\b(" + "|".join(WORD_NUMBERS) +
                  r")\s+(?:business\s+)?days?", text, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1) or m.group(2)) if (m.group(1) or m.group(2)) else WORD_NUMBERS[m.group(3).lower()]


def parse_dates(text: str) -> list[date]:
    out = []
    for m in DATE.finditer(text):
        try:
            if m.group(1):
                out.append(datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date())
            elif m.group(4):
                out.append(date(int(m.group(6)), int(m.group(4)), int(m.group(5))))
            else:
                out.append(date(int(m.group(7)), int(m.group(8)), int(m.group(9))))
        except ValueError:
            continue
    return out


def months_between(start: date, end: date) -> int:
    """Nov 1 2026 -> Oct 31 2027 = 12 months (the end date is the last day of the term)."""
    return (end.year - start.year) * 12 + end.month - start.month + (1 if end.day >= start.day - 1 else 0)


def find(pieces: list[str], *patterns: str) -> list[str]:
    return [p for p in pieces if all(re.search(pat, p, re.IGNORECASE) for pat in patterns)]


def clause_around(pieces: list[str], keyword: str, need=None, prefer: str | None = None) -> str | None:
    """The text a rule should read and quote: from just before `keyword` to the end of its sentence,
    in the best piece that mentions it. Quoting the start of a paragraph showed an apartment/term
    paragraph as the "deposit clause" when that paragraph merely mentioned a deposit."""
    best = None
    for i, piece in enumerate(pieces):
        m = re.search(keyword, piece, re.IGNORECASE)
        if not m:
            continue
        start = max(0, m.start() - 60)
        start = piece.rfind(" ", 0, start) + 1 if start else 0
        stop = piece.find(". ", m.end() + 40)
        window = piece[start:(stop + 1 if stop >= 0 else min(len(piece), m.end() + 280))]
        if need and not need(window):
            continue
        score = (3 if prefer and re.search(prefer, piece[:40], re.IGNORECASE) else 0)  # the clause's heading
        score += 2 if re.search(r"\$|dollars|percent|%|months?'? rent", piece[m.end():m.end() + 140], re.IGNORECASE) else 0
        if best is None or score > best[0]:
            best = (score, i, window)
    return best[2] if best else None


STREET_TYPES = r"avenue|ave|street|st|boulevard|blvd|place|pl|road|rd|drive|dr|parkway|pkwy|terrace|ter|lane|ln|court|ct"
LEASE_ADDRESS = re.compile(
    r"\b(\d{1,5}(?:\s*[-–&/]\s*\d{1,5}|\s+\d{1,5}(?=\s+[a-z]))?\s+"      # house number, or a range "184-188" / "184 188"
    r"(?:(?:east|west|north|south|e\.?|w\.?)\s+)?[a-z0-9.' ]{1,40}?\s(?:" + STREET_TYPES + r")\b\.?)",
    re.IGNORECASE)
NYC_ZIP_PREFIXES = ("100", "101", "102", "103", "104", "111", "112", "113", "114", "116")
NYC_PLACE = re.compile(r"\b(manhattan|brooklyn|queens|bronx|staten island|new york)\b", re.IGNORECASE)
PREMISES_WORDS = re.compile(r"premises|apartment|\bapt\b|\bunit\b|leases? to|rents? to|demised|located at|dwelling",
                            re.IGNORECASE)
LANDLORD_WORDS = re.compile(r"landlord|owner|lessor|managing agent|notices?|c/o|attention|attn|business|mail|payable to|"
                            r"send (rent|payments?)", re.IGNORECASE)


def address_variants(address: str) -> list[str]:
    """'184 188 Claremont Avenue' or '184-188 ...' is a range on one building: try each end.
    ('45-17 21st St' is a Queens house number, not a range: its second part is smaller.)"""
    m = re.match(r"^(\d{1,5})\s*(?:[-–&/]|\s)\s*(\d{1,5})\s+(.*)$", address)
    if m and len(m.group(1)) == len(m.group(2)) and 0 < int(m.group(2)) - int(m.group(1)) <= 40:
        return [f"{m.group(1)} {m.group(3)}", f"{m.group(2)} {m.group(3)}"]
    return [address]


def address_candidates(text: str) -> list[dict]:
    """Every street address in the lease, best first: the premises address beats a landlord's
    business or notice address, and an NYC address beats one outside the city."""
    out = []
    for m in LEASE_ADDRESS.finditer(text):
        street = re.sub(r"\s+", " ", m.group(1)).strip(" ,.")
        after = text[m.end():m.end() + 70]
        unit = re.match(r"\s*,?\s*(?:(?:apt\.?|apartment|unit|#)\s*(?:no\.?|#)?\s*([0-9]{1,4}[A-Z]{0,2})|([0-9]{1,3}[A-Z]{1,2}))\b",
                        after, re.IGNORECASE)
        zip_match = re.search(r"\b(\d{5})\b", after)
        place = NYC_PLACE.search(after[:45])
        para_start = text.rfind("\n\n", 0, m.start())
        paragraph = text[para_start + 1 if para_start >= 0 else 0:m.start()]
        before = text[max(0, m.start() - 160):m.start()]
        score, why = 0, []
        if PREMISES_WORDS.search(paragraph[-240:]) or PREMISES_WORDS.search(after[:30]):
            score += 3; why.append("premises wording")
        if LANDLORD_WORDS.search(before[-100:]) and not PREMISES_WORDS.search(before[-60:]):
            score -= 3; why.append("next to landlord/notice wording")
        if zip_match:
            nyc = zip_match.group(1).startswith(NYC_ZIP_PREFIXES) or zip_match.group(1) in ("11004", "11005")
            score += 1 if nyc else -5
            why.append("NYC zip" if nyc else "zip outside NYC")
        address = street + (f", {place.group(1)}" if place else "") + (f" {zip_match.group(1)}" if zip_match else "")
        out.append({"address": address, "unit": ((unit.group(1) or unit.group(2)).upper() if unit else None),
                    "score": score, "why": ", ".join(why) or "no context", "position": m.start()})
    best: dict[str, dict] = {}  # the same address on several pages: keep its best-scoring mention
    for c in sorted(out, key=lambda c: (-c["score"], c["position"])):
        best.setdefault(re.sub(r"[^a-z0-9]", "", c["address"].lower()), c)
    return list(best.values())


# --- Extraction ---


def extract(text: str) -> dict:
    """The lease's key terms, each with the clause it came from."""
    pieces = clauses(text)
    facts: dict = {}

    rent_clauses = [p for p in find(pieces, r"\brent\b") if stated_amounts(p)
                    and re.search(r"monthly rent|rent of|per month|a month|each month|rent (is|shall be)", p, re.IGNORECASE)
                    and not re.search(r"deposit|late|fee", p, re.IGNORECASE)]
    facts["rent_mentions"] = [{"amount": stated_amounts(p)[0], "clause": excerpt(p)} for p in rent_clauses]
    facts["monthly_rent"] = facts["rent_mentions"][0]["amount"] if rent_clauses else None

    # The deposit comes only from a sentence that names it. A month count ("equal to one month of rent")
    # wins over any dollar figure; a sentence with neither leaves the amount unstated.
    deposit = clause_around(pieces, r"security deposit|\bdeposit\b|\bsecurity\b",
                            need=lambda t: dollar_amounts(t) or months_of_rent(t), prefer=r"security|deposit")
    if not deposit:
        deposit = clause_around(pieces, r"security deposit", prefer=r"security|deposit")
    if deposit:
        months = months_of_rent(deposit)
        amounts = stated_amounts(deposit)  # only from the deposit sentence itself; the rule prefers months
        facts["security_deposit"] = {"amount": max(amounts) if amounts else None, "months_stated": months,
                                     "_text": deposit, "clause": excerpt(deposit)}
    deposit_return = clause_around(pieces, r"\breturn", need=lambda t: re.search(r"deposit|security", t, re.IGNORECASE))
    if deposit_return:
        facts["deposit_return_days"] = {"days": number_of_days(deposit_return), "clause": excerpt(deposit_return)}

    def date_after(pattern: str) -> date | None:
        """The first date within a short distance after a keyword like 'commences on'."""
        for m in re.finditer(pattern, text, re.IGNORECASE):
            dates = parse_dates(text[m.end():m.end() + 45])
            if dates:
                return dates[0]
        return None

    term = clause_around(pieces, r"\bterm\b|commenc|beginning|begins", prefer=r"term")
    if term:
        start = date_after(r"commenc\w*(?:\s+on)?|begin\w*(?:\s+on)?|start\w*(?:\s+on)?")
        end = date_after(r"expir\w*(?:\s+on)?|end\w*(?:\s+on)?|terminat\w*(?:\s+on)?")
        years = re.search(r"(\d+|" + "|".join(WORD_NUMBERS) + r")\s*(?:\(\d+\)\s*)?[- ]?years?", term, re.IGNORECASE)
        months = re.search(r"(\d+|" + "|".join(WORD_NUMBERS) + r")\s*(?:\(\d+\)\s*)?[- ]?months?", term, re.IGNORECASE)

        def count(m):
            return (int(m.group(1)) if m.group(1).isdigit() else WORD_NUMBERS.get(m.group(1).lower(), 0)) if m else 0
        # "1 years 0 months 0 days" is 12 months, not 0.
        stated = count(years) * 12 + count(months) if (years or months) else None
        facts["term"] = {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None,
                         "months_stated": stated or None, "clause": excerpt(term)}

    # The unit: next to the premises address first ("..., 4N, New York"), then "Apartment 4N" wording in a
    # premises sentence, then anywhere. Never the sample's own "demonstrate Apartment 411" sentence.
    candidates = address_candidates(text)
    unit = next((c["unit"] for c in candidates if c["unit"] and c["score"] > 0), None)
    unit_pattern = re.compile(r"\b(?:apartment|apt\.?|unit)\s*(?:no\.?|number|#)?\s*[:#]?\s*([0-9]{1,4}[A-Z]{0,2}|[A-Z]{1,2}-?\d{1,3})\b",
                              re.IGNORECASE)
    if not unit:
        usable = [p for p in pieces if "demonstrate" not in p.lower()]
        premises = [p for p in usable if PREMISES_WORDS.search(p)]
        hit = next((m for p in premises + usable for m in [unit_pattern.search(p)] if m), None)
        unit = hit.group(1).upper() if hit else None
    facts["unit"] = unit
    facts["address_candidates"] = [{k: c[k] for k in ("address", "unit", "why")} for c in candidates]

    name = r"([A-Z0-9][^\n(]{2,80}?)"
    landlord = (re.search(r"Landlord[’']s\s+Name\s*:\s*" + name + r"\s*(?:$|\n)", text, re.IGNORECASE | re.MULTILINE)
                or re.search(r"The\s+Landlord\s+is\s*:?\s*(?:\(\s*[“\"]?The\s+Landlord[”\"]?\s*\)\s*)?\n\s*" + name + r"\s*(?:$|\n)",
                             text, re.IGNORECASE | re.MULTILINE)
                or re.search(r"between\s+" + name + r",?\s*(?:\(\s*(?:the\s+)?[\"“]?|as\s+|hereinafter\s+)(?:Landlord|Owner|Lessor)", text, re.IGNORECASE | re.DOTALL)
                or re.search(r"^\s*(?:Landlord|Owner|Lessor)(?:'s name|\s*name)?\s*[:\-–]\s*" + name + r"\s*(?:,|$|\n)", text, re.IGNORECASE | re.MULTILINE)
                or re.search(r"^\s*(?:Landlord|Owner|Lessor)\s*:?\s*\n\s*" + name + r"\s*(?:,|$)", text, re.IGNORECASE | re.MULTILINE))
    facts["landlord_name"] = re.sub(r"\s+", " ", landlord.group(1)).strip(" ,") if landlord else None

    late = clause_around(pieces, r"\blate (?:fee|charge)s?|\blate\b",
                         need=lambda t: dollar_amounts(t) or percent_values(t), prefer=r"late")
    if late:
        facts["late_fee"] = {"amounts": dollar_amounts(late), "percents": percent_values(late),
                             "after_days": number_of_days(late), "clause": excerpt(late), "_text": late}

    app_fee = clause_around(pieces, r"application|credit check|background check|processing fee",
                            need=dollar_amounts, prefer=r"application|fee")
    if app_fee:
        facts["application_fee"] = {"amounts": dollar_amounts(app_fee), "clause": excerpt(app_fee), "_text": app_fee}

    broker = clause_around(pieces, r"broker", need=lambda t: re.search(r"fee|commission", t, re.IGNORECASE),
                           prefer=r"broker")
    if broker:
        facts["broker_fee"] = {"amount": (dollar_amounts(broker) or [None])[0],
                               "tenant_pays": bool(re.search(r"tenant (shall|will|must|agrees to) pay|paid by tenant",
                                                             broker, re.IGNORECASE)),
                               "clause": excerpt(broker)}

    attorney = clause_around(pieces, r"attorney", prefer=r"attorney")
    if attorney:
        reciprocal = bool(re.search(r"prevailing party|either party|each party|landlord shall pay tenant", attorney, re.IGNORECASE))
        facts["attorney_fees"] = {"tenant_pays_landlord": bool(re.search(r"tenant shall pay|tenant will pay|tenant must pay|"
                                                                         r"tenant agrees to pay", attorney, re.IGNORECASE)),
                                  "reciprocal_in_text": reciprocal, "clause": excerpt(attorney)}

    facts["auto_renewal"] = bool(re.search(r"automatic(ally)? renew", text, re.IGNORECASE))
    facts["mentions_rent_stabilization"] = bool(re.search(r"rent[- ]stabiliz", text, re.IGNORECASE))
    heat = find(pieces, r"\bheat\b")
    facts["heat_clauses"] = [excerpt(p) for p in heat]
    facts["heat_included"] = any(re.search(r"included|landlord (shall|will) (provide|supply)", p, re.IGNORECASE) for p in heat)

    facts["disclosures_present"] = {
        "bedbug_history": bool(re.search(r"bed ?bug", text, re.IGNORECASE)),
        "window_guards": bool(re.search(r"window guard", text, re.IGNORECASE)),
        "lead_paint": bool(re.search(r"lead[- ]based paint|lead paint", text, re.IGNORECASE)),
        "sprinkler": bool(re.search(r"sprinkler", text, re.IGNORECASE)),
        "rent_stabilization_rider": bool(re.search(r"rent stabiliz\w+ (lease )?rider|RA-LR", text, re.IGNORECASE)),
    }
    return facts


# --- Checks A (consistency) and B (NY/NYC rules) ---


RULE_CATEGORY = {"deposit_cap": "money", "deposit_return": "money", "application_fee": "money", "late_fee": "money",
                 "broker_fee": "money", "attorney_fees": "money", "heat_minimums": "terms",
                 "bedbug_disclosure": "disclosures", "rent_stabilization_rider": "disclosures"}


def flag(severity: str, clause: str | None, explanation: str, rule: str | None = None,
         category: str | None = None) -> dict:
    """category: money | terms | disclosures | city_records (for the focus filter and the UI)."""
    out = {"severity": severity, "category": category or RULE_CATEGORY.get(rule, "terms"),
           "clause_excerpt": clause, "explanation": explanation}
    if rule:
        out["rule"] = RULES[rule]["rule"]
        out["citation"] = RULES[rule]["citation"]
        out["source_url"] = RULES[rule]["source"]
    else:
        out["source_url"] = None
    return out


def check_consistency(facts: dict) -> list[dict]:
    flags = []
    rents = sorted({m["amount"] for m in facts["rent_mentions"]})
    if len(rents) > 1:
        clauses_text = " | ".join(m["clause"][:95] for m in facts["rent_mentions"])
        flags.append(flag(INCONSISTENT, excerpt(clauses_text),
                          f"The lease states different monthly rents: {', '.join(f'${r:,.2f}' for r in rents)}. Get "
                          "the correct figure fixed in writing before signing.", category="money"))
    term = facts.get("term")
    if term and term["start"] and term["end"]:
        start, end = date.fromisoformat(term["start"]), date.fromisoformat(term["end"])
        if end <= start:
            flags.append(flag(INCONSISTENT, term["clause"], "The lease ends before it starts."))
        elif term["months_stated"] and months_between(start, end) != term["months_stated"]:
            flags.append(flag(INCONSISTENT, term["clause"],
                              f"The lease says {term['months_stated']} months, but {term['start']} to {term['end']} "
                              f"is {months_between(start, end)} months."))
    deposit = facts.get("security_deposit")
    rent = facts.get("monthly_rent")
    if deposit and deposit["amount"] and deposit["months_stated"] and rent:
        expected = deposit["months_stated"] * rent
        if abs(expected - deposit["amount"]) > 1:
            flags.append(flag(INCONSISTENT, deposit["clause"],
                              f"The deposit is ${deposit['amount']:,.2f}, but {deposit['months_stated']} months of the "
                              f"stated rent (${rent:,.2f}) would be ${expected:,.2f}.", category="money"))
    return flags


def check_rules(facts: dict) -> list[dict]:
    flags = []
    rent = facts.get("monthly_rent")
    rents = [m["amount"] for m in facts["rent_mentions"]]

    deposit = facts.get("security_deposit")
    if deposit:
        if deposit["months_stated"]:
            allowed, how = (None, "") if LEGAL_LIMIT.search(deposit["_text"]) and LESSER_OF.search(deposit["_text"]) \
                else (deposit["months_stated"], "months")
            months = allowed
        elif deposit["amount"] and rent:
            allowed, how = most_allowed(deposit["_text"], stated_amounts(deposit["_text"]))
            months = allowed / min(rents) if allowed else None
        else:
            months = None
            if not LEGAL_LIMIT.search(deposit["_text"]):
                flags.append(flag(CHECK, deposit["clause"], "The deposit amount isn't stated in the lease. Ask for "
                                  "it in writing; it can't be more than one month's rent.", "deposit_cap"))
        if months and months > 1.0001:
            flags.append(flag(LIKELY_NOT_ALLOWED, deposit["clause"],
                              f"The deposit is about {months:.1f} months' rent; the legal maximum is one month.",
                              "deposit_cap"))
    ret = facts.get("deposit_return_days")
    if ret and ret["days"] and ret["days"] > 14:
        flags.append(flag(LIKELY_NOT_ALLOWED, ret["clause"],
                          f"The lease allows {ret['days']} days to return the deposit; the law requires 14.", "deposit_return"))

    fee = facts.get("application_fee")
    if fee:
        allowed, _ = most_allowed(fee["_text"], fee["amounts"])
        if allowed and allowed > 20:
            flags.append(flag(LIKELY_NOT_ALLOWED, fee["clause"],
                              f"The application/credit-check fee is ${allowed:,.2f}; the cap is $20 (or the actual "
                              "cost of the check, if lower).", "application_fee"))

    late = facts.get("late_fee")
    if late:
        problems = []
        if late["after_days"] is not None and late["after_days"] < 5:
            problems.append(f"it applies after {late['after_days']} days late (the law requires more than 5)")
        if rent:
            cap = min(50.0, 0.05 * min(rents))
            candidates = late["amounts"] + [p / 100 * rent for p in late["percents"]]
            allowed, how = most_allowed(late["_text"], candidates)
            # Only flag what the lease actually permits: "the lesser of $50 or 5%" is the legal cap itself.
            if allowed is not None and allowed > cap + 0.01:
                problems.append(f"it allows ${allowed:,.2f}, over the ${cap:,.2f} cap for this rent")
        if problems:
            flags.append(flag(LIKELY_NOT_ALLOWED, late["clause"], "The late fee looks too high or too early: "
                              + "; ".join(problems) + ".", "late_fee"))

    broker = facts.get("broker_fee")
    if broker and broker["tenant_pays"]:
        flags.append(flag(CHECK, broker["clause"],
                          "The lease has the tenant paying a broker fee. If that broker represents the landlord "
                          "(for example, the listing agent), the FARE Act bars charging you. Ask whom the broker "
                          "represents and for the itemized fee disclosure.", "broker_fee"))

    attorney = facts.get("attorney_fees")
    if attorney and attorney["tenant_pays_landlord"] and not attorney["reciprocal_in_text"]:
        flags.append(flag(CHECK, attorney["clause"],
                          "This clause only mentions the landlord recovering attorney's fees. NY law makes it "
                          "mutual: if you win a dispute, you can recover yours too, even though the lease doesn't say so.",
                          "attorney_fees"))

    for clause in facts["heat_clauses"]:
        if re.search(r"waive|not (be )?required to provide heat|below \d+\s*(°|degrees)|no heat", clause, re.IGNORECASE):
            flags.append(flag(LIKELY_NOT_ALLOWED, clause, "This clause appears to waive or lower the city's heat "
                              "requirements, which a lease can't do.", "heat_minimums"))

    if facts["mentions_rent_stabilization"] and not facts["disclosures_present"]["rent_stabilization_rider"]:
        flags.append(flag(CHECK, None, "The lease mentions rent stabilization but no DHCR lease rider was found. Ask "
                          "for the rider.", "rent_stabilization_rider"))
    return flags


def missing_disclosures(facts: dict, units: int | None, year_built: int | None) -> list[dict]:
    """Required notices not found in the text. Building-type rules only apply when we know the building."""
    present = facts["disclosures_present"]
    missing = []
    if not present["bedbug_history"]:
        missing.append({"disclosure": "bedbug history (previous year)", **{k: RULES["bedbug_disclosure"][k] for k in ("citation", "source")},
                        "applies": "new (vacancy) leases"})
    if not present["sprinkler"]:
        missing.append({"disclosure": "sprinkler system notice", **{k: RULES["sprinkler"][k] for k in ("citation", "source")},
                        "applies": "every residential lease"})
    if units is not None and units >= 3 and not present["window_guards"]:
        missing.append({"disclosure": "window guard notice", **{k: RULES["window_guards"][k] for k in ("citation", "source")},
                        "applies": "buildings with 3+ apartments"})
    if units is not None and units >= 3 and year_built and year_built < 1960 and not present["lead_paint"]:
        missing.append({"disclosure": "lead-based paint notice", **{k: RULES["lead_paint"][k] for k in ("citation", "source")},
                        "applies": "buildings with 3+ apartments built before 1960"})
    return missing
