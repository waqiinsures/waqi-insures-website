from flask import Flask, request, render_template_string, redirect, url_for, session, abort
from pdf2image import convert_from_path, convert_from_bytes
import pytesseract
import re
import os
import tempfile
import subprocess
import json
import secrets
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote
from markupsafe import Markup
from functools import wraps

WAQI_BUILD = "V18.1-TOP-RIGHT-INSURED"


app = Flask(__name__)
app.secret_key = os.environ.get("WAQI_SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("WAQI_MAX_UPLOAD_MB", "25")) * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("WAQI_HTTPS", "1") == "1"

BROKER_USERNAME = os.environ.get("WAQI_BROKER_USERNAME", "waqi")
BROKER_PASSWORD = os.environ.get("WAQI_BROKER_PASSWORD", "")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("WAQI_DATA_DIR", BASE_DIR)
QUOTES_DIR = os.path.join(DATA_DIR, "quotes")
os.makedirs(QUOTES_DIR, exist_ok=True)

BROKER_WHATSAPP_NUMBER = "14373677252"
BROKER_EMAIL = "waqi.insures@gmail.com"


# =========================================================
# MONEY
# =========================================================

def to_decimal(value):
    if value is None or value == "":
        return None

    text = str(value).replace("$", "").replace(",", "").strip()
    match = re.search(r"\d+(?:\.\d{1,2})?", text)

    if not match:
        return None

    return Decimal(match.group())


def money(value):
    if value is None or value == "":
        return ""

    value = Decimal(str(value)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )

    return f"{value:,.2f}"


# =========================================================
# REGEX
# =========================================================

def first_match(patterns, text, flags=re.I | re.M):
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return match.group(1).strip()

    return ""


# =========================================================
# OCR
# =========================================================

def _ocr_header_crop(image):
    """
    OCR just the top-right customer card of an already-rendered page image
    (PIL Image), using the same proportional box as the native-PDF path.
    Used by the whole-document OCR fallback so client detection never
    silently degrades just because a PDF could not be opened with PyMuPDF.
    """
    try:
        w, h = image.size
        box = (
            int(w * 0.56),
            int(h * 0.025),
            int(w * 0.985),
            int(h * 0.215),
        )
        crop = image.crop(box)
        return pytesseract.image_to_string(crop, lang="eng", config="--psm 11") or ""
    except Exception:
        return ""


def extract_pdf_text(file):
    """
    Universal ARS PDF extraction.

    HARD RULE FOR ALL QUOTE TYPES:
    The top-right customer card on PAGE 1 is the authoritative insured block.
    It is extracted separately on EVERY PDF — Auto, Home, Tenant, Condo,
    Rented Dwelling, etc. — regardless of which extraction path (native
    PyMuPDF, per-page OCR, or whole-document OCR fallback) ends up being
    used, so client detection never silently degrades.

    Native PDFs:
      - use embedded text for the whole page;
      - ALSO extract the top-right customer card by page coordinates.

    Scanned/image PDFs (per-page OCR):
      - OCR the whole page;
      - ALSO OCR the top-right customer card separately at higher resolution.

    Total fallback (PyMuPDF missing/unable to open the file):
      - OCR every page as a whole-document image;
      - ALSO OCR the top-right customer card from the page-1 image.

    The separately extracted customer card is wrapped in:
      [[WAQI_CLIENT_HEADER_OCR]]
      ...
      [[WAQI_CLIENT_HEADER_END]]

    extract_client() always gives this block highest priority.

    A failure on any single page (e.g. one bad tesseract call) must never
    discard text or the client header already recovered from other pages —
    each page is processed independently so partial failures degrade
    gracefully instead of wiping out everything.
    """
    file.stream.seek(0)
    pdf_bytes = file.read()
    file.stream.seek(0)

    parts = []
    used_ocr = False
    client_header_text = ""
    doc = None

    try:
        import fitz
    except Exception:
        fitz = None

    if fitz is not None:
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception:
            doc = None

    if doc is not None:
        for page_index, page in enumerate(doc):
            try:
                native = page.get_text("text") or ""
            except Exception:
                native = ""

            native_clean = re.sub(r"\s+", " ", native).strip()

            # ------------------------------------------------
            # PAGE 1: ALWAYS extract the top-right customer card.
            # Same ARS placement across quote types. Wrapped in its own
            # try/except so a header-extraction problem never aborts
            # extraction of the rest of the document.
            # ------------------------------------------------
            if page_index == 0:
                try:
                    r = page.rect

                    # Broad enough to include name + phone + address,
                    # narrow enough to avoid brokerage info on the left.
                    clip = fitz.Rect(
                        r.x0 + r.width * 0.56,
                        r.y0 + r.height * 0.025,
                        r.x1 - r.width * 0.015,
                        r.y0 + r.height * 0.215
                    )

                    # Native text first.
                    header_native = (page.get_text("text", clip=clip) or "").strip()

                    if header_native:
                        client_header_text = header_native
                    else:
                        # If card has no embedded text, OCR just this card.
                        with tempfile.TemporaryDirectory() as td:
                            header_pix = page.get_pixmap(
                                matrix=fitz.Matrix(4.0, 4.0),
                                clip=clip,
                                alpha=False
                            )

                            header_path = os.path.join(td, "client_header.png")
                            header_pix.save(header_path)

                            header_result = subprocess.run(
                                [
                                    "tesseract",
                                    header_path,
                                    "stdout",
                                    "-l", "eng",
                                    "--psm", "11"
                                ],
                                capture_output=True,
                                text=True,
                                timeout=30
                            )

                            client_header_text = (header_result.stdout or "").strip()
                except Exception:
                    # Header extraction failed; leave client_header_text as-is
                    # (extract_client() has further fallbacks in the body text).
                    pass

            # ------------------------------------------------
            # Whole-page extraction
            # ------------------------------------------------
            if len(native_clean) >= 80:
                parts.append(native)
                continue

            used_ocr = True

            try:
                pix = page.get_pixmap(
                    matrix=fitz.Matrix(2.8, 2.8),
                    alpha=False
                )

                with tempfile.TemporaryDirectory() as td:
                    image_path = os.path.join(td, f"page_{page_index}.png")
                    pix.save(image_path)

                    result = subprocess.run(
                        [
                            "tesseract",
                            image_path,
                            "stdout",
                            "-l", "eng",
                            "--psm", "6"
                        ],
                        capture_output=True,
                        text=True,
                        timeout=60
                    )

                    parts.append(result.stdout or "")
            except Exception:
                # This single page's OCR failed. Keep whatever native text
                # existed (even if short) instead of dropping the page
                # entirely, and keep processing the remaining pages.
                parts.append(native)

    # ------------------------------------------------------------
    # Total fallback: PyMuPDF missing, or unable to open this PDF at all.
    # OCR every page as an image. The top-right customer card is ALSO OCR'd
    # separately here so this path never silently loses client detection.
    # ------------------------------------------------------------
    if doc is None:
        used_ocr = True
        images = convert_from_bytes(pdf_bytes, dpi=300)

        if images and not client_header_text.strip():
            client_header_text = _ocr_header_crop(images[0])

        for image in images:
            try:
                parts.append(
                    pytesseract.image_to_string(
                        image,
                        lang="eng",
                        config="--psm 6"
                    )
                )
            except Exception:
                parts.append("")

    prefix_parts = []

    if used_ocr:
        prefix_parts.append("[[WAQI_OCR_USED]]")

    if client_header_text.strip():
        prefix_parts.extend([
            "[[WAQI_CLIENT_HEADER_OCR]]",
            client_header_text.strip(),
            "[[WAQI_CLIENT_HEADER_END]]"
        ])

    prefix = "\n".join(prefix_parts)
    body = "\n".join(parts)

    return (prefix + "\n" + body).strip()



def detect_quote_type(text):
    lower = (text or "").lower()

    strong_home = [
        "primary - homeowners",
        "primary - rented dwelling",
        "primary - tenants",
        "primary - condo",
        "extended coverages",
        "guaranteed building replacement cost"
    ]
    strong_auto = [
        "private passenger",
        "bodily injury",
        "uninsured automobile",
        "#44 family protection",
        "optional accident benefits",
        "driver #"
    ]

    home_strong = sum(term in lower for term in strong_home)
    auto_strong = sum(term in lower for term in strong_auto)

    if home_strong >= 2 and home_strong > auto_strong:
        return "home"
    if auto_strong >= 2 and auto_strong > home_strong:
        return "auto"

    home_words = [
        "contents", "additional living expenses", "identity theft",
        "legal liability", "tenants", "homeowners", "dwelling",
        "residence", "sewer backup", "overland water"
    ]
    auto_words = [
        "bodily injury", "property damage", "all perils",
        "uninsured automobile", "family protection", "loss of use",
        "operator", "direct compensation", "vehicle"
    ]

    home_score = sum(item in lower for item in home_words)
    auto_score = sum(item in lower for item in auto_words)

    # Do not silently classify arbitrary/unsupported PDFs as Auto.
    # A valid quote needs several independent structural signals.
    if home_score >= 3 and home_score > auto_score:
        return "home"
    if auto_score >= 3 and auto_score > home_score:
        return "auto"
    return "unknown"

def extract_client(text):
    """
    AUTHORITATIVE CLIENT / NAMED INSURED RULE.

    Client is taken from actual insured/applicant locations only:
      1) Applicant Information
      2) carrier-detail Breakdown name
      3) explicit Named Insured wording
      4) Summary fallback

    Driver lists and brokerage contact information are never used as the client.
    """
    text = _clean_ars_text(text or "")

    # ABSOLUTE PRIORITY FOR EVERY ARS QUOTE:
    # Page-1 top-right customer card = insured/client.
    header = re.search(
        r"(?is)\[\[WAQI_CLIENT_HEADER_OCR\]\](.*?)\[\[WAQI_CLIENT_HEADER_END\]\]",
        text
    )

    if header:
        header_lines = [
            re.sub(r"\s+", " ", ln).strip()
            for ln in header.group(1).splitlines()
            if re.sub(r"\s+", " ", ln).strip()
        ]

        def plausible_header_name(candidate):
            candidate = re.sub(r"\s+", " ", candidate or "").strip()

            if not candidate or len(candidate) > 90:
                return ""

            # Never confuse brokerage/contact/address text with insured name.
            if re.search(
                r"(?i)^(?:WellCare|Insurance|Phone:|Email:|Home:|"
                r"Prepared\b|Provided\b|Applied\b|"
                r"\d{1,6}\s+|"
                r"Ajax\b|Toronto\b|North York\b|Mississauga\b|"
                r"Brampton\b|Scarborough\b|Kitchener\b)",
                candidate
            ):
                return ""

            if re.search(
                r"(?i)\b(?:Street|St\b|Ave\b|Avenue|Road|Rd\b|"
                r"Cres\b|Crescent|Suite|ON\b|Ontario|"
                r"[A-Z]\d[A-Z]\s*\d[A-Z]\d)\b",
                candidate
            ):
                return ""

            # Phone/email are never names.
            if "@" in candidate or re.search(r"\(?\d{3}\)?[-\s]\d{3}", candidate):
                return ""

            tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’\-]+", candidate)

            if 2 <= len(tokens) <= 8:
                return candidate

            return ""

        # Best pattern: name immediately before Home:
        for i, line in enumerate(header_lines):
            if re.match(r"(?i)^Home:", line):
                for candidate in reversed(header_lines[:i]):
                    name = plausible_header_name(candidate)
                    if name:
                        return name

        # Some cards may omit "Home:" or OCR it badly.
        # Then the FIRST plausible person-name line in the card wins.
        for candidate in header_lines:
            name = plausible_header_name(candidate)
            if name:
                return name

    def clean_name(candidate):
        candidate = re.sub(r"\s+", " ", candidate or "").strip()

        # OCR/PDF extraction can occasionally repeat the complete name twice.
        # Example: "John Smith John Smith" -> "John Smith".
        words = candidate.split()
        if len(words) >= 4 and len(words) % 2 == 0:
            half = len(words) // 2
            if [w.lower() for w in words[:half]] == [w.lower() for w in words[half:]]:
                candidate = " ".join(words[:half])

        if not candidate or len(candidate) > 90:
            return ""

        if re.search(
            r"(?i)^(?:WellCare Insurance Corp\.?|Phone:|Email:|Home:|"
            r"Prepared\b|Applied Ref\.|Provided by\b|Summary|Breakdown|"
            r"Policy|Company|Vehicles?|Totals?|Annual Premium|Total Premium|"
            r"New Business|Effective Date|Applicant Information|"
            r"Co-Applicant Information)$",
            candidate
        ):
            return ""

        if re.search(
            r"(?i)\b(?:Street|Ave\b|Road|Drive|Kitchener|Scarborough|"
            r"Toronto|Ajax|Territory|Premium|Coverage|Policy)\b",
            candidate
        ):
            return ""

        tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+", candidate)
        return candidate if 2 <= len(tokens) <= 8 else ""

    # 1) Applicant Information
    applicant = re.search(
        r"(?is)\bApplicant Information\b(.*?)(?:"
        r"\bCo-Applicant Information\b|"
        r"\bVehicle\s+\d+\s+of\s+\d+|"
        r"\bDrivers\b)",
        text
    )

    applicant_name = ""

    if applicant:
        lines = [
            re.sub(r"\s+", " ", ln).strip()
            for ln in applicant.group(1).splitlines()
            if ln.strip()
        ]

        first_part = ""
        last_part = ""

        for i, ln in enumerate(lines):
            if re.search(r"(?i)\bFirst Name\b", ln) and i > 0:
                first_part = lines[i - 1]

            if re.search(r"(?i)\bLast Name\b", ln) and i > 0:
                last_part = lines[i - 1]

        if first_part and last_part:
            applicant_name = clean_name(
                re.sub(
                    r"(?i)\b(?:Salutation|First Name|Middle|Last Name|Suffix)\b",
                    "",
                    f"{first_part} {last_part}"
                )
            )

    # 2) Carrier-detail Breakdown name
    breakdown_name = ""

    for bm in re.finditer(r"(?im)^\s*Breakdown\s*$", text):
        after = text[bm.end():bm.end() + 250]
        lines = [ln for ln in after.splitlines() if ln.strip()]

        for line in lines[:4]:
            candidate = clean_name(line)
            if candidate:
                breakdown_name = candidate
                break

        if breakdown_name:
            break

    # Applicant is authoritative when present.
    # If Breakdown is clearly the same person but contains more name parts,
    # use the fuller version.
    if applicant_name:
        if breakdown_name:
            a = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+", applicant_name)
            b = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+", breakdown_name)

            if (
                a and b and
                a[0].lower() == b[0].lower() and
                a[-1].lower() == b[-1].lower() and
                len(b) > len(a)
            ):
                return breakdown_name

        return applicant_name

    if breakdown_name:
        return breakdown_name

    # 3) Explicit Named Insured wording
    for pattern in [
        r"(?im)^\s*Named Insured\s+([A-Za-zÀ-ÖØ-öø-ÿ'’\- ]+?)\s+(?:is|has)\b",
        r"(?im)^\s*Driver or Named Insured\s+([A-Za-zÀ-ÖØ-öø-ÿ'’\- ]+?)\s+(?:is|has)\b",
    ]:
        mm = re.search(pattern, text)
        if mm:
            candidate = clean_name(mm.group(1))
            if candidate:
                return candidate

    # 4) Summary fallback
    for sm in re.finditer(
        r"(?im)^\s*\d+\s*Vehicle(?:s)?,\s*\d+\s*Driver(?:s)?\s*\|\s*Effective Date:",
        text
    ):
        before = text[max(0, sm.start() - 500):sm.start()]
        lines = [ln for ln in before.splitlines() if ln.strip()]

        for line in reversed(lines):
            candidate = clean_name(line)
            if candidate:
                return candidate

    # OCR/grid fallback for scanned ARS PDFs.
    grid = re.search(
        r"(?im)^\s*([A-Za-zÀ-ÖØ-öø-ÿ'’\-]+(?:\s+[A-Za-zÀ-ÖØ-öø-ÿ'’\-]+){1,7})\s*$"
        r"\n\s*Salutation\s+First Name",
        text
    )
    if grid:
        candidate = clean_name(grid.group(1))
        if candidate:
            return candidate

    return ""



def _clean_ars_text(text):
    text = (text or "").replace("\xa0", " ").replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\r\n?", "\n", text)
    return text


ARS_CARRIER_VARIANTS = [
    ("CAA Insurance Company (Future Member)", "CAA Insurance", "Future Member"),
    ("CAA Insurance Company", "CAA Insurance", ""),
    ("CAA MyPace", "CAA Insurance", ""),
    ("Aviva Traders - Aviva Journey", "Aviva Traders", "Aviva Journey"),
    ("Aviva - Aviva Journey", "Aviva", "Aviva Journey"),
    ("Aviva Traders", "Aviva Traders", ""),
    ("Aviva", "Aviva", ""),
    ("Intact Insurance - my Drive", "Intact Insurance", ""),
    ("Intact Insurance", "Intact Insurance", ""),
    ("Intact", "Intact Insurance", ""),
    ("Wawanesa Drive Change", "Wawanesa", "Drive Change"),
    ("Wawanesa Mutual", "Wawanesa", ""),
    ("Wawanesa", "Wawanesa", ""),
    ("Definity Insurance Company", "Definity", ""),
    ("Definity", "Definity", ""),
    ("Unica Insurance Inc.", "Unica", ""),
    ("Unica", "Unica", ""),
    ("Northbridge Insurance-Commercial (NGIC)", "Northbridge Insurance", "NGIC"),
    ("Northbridge Insurance", "Northbridge Insurance", ""),
    ("Dominion of Canada - Single Pay and DPD", "Dominion of Canada", "Single Pay and DPD"),
    ("Dominion of Canada - IntelliDrive", "Dominion of Canada", "IntelliDrive"),
    ("Dominion of Canada", "Dominion of Canada", ""),
    ("Travelers Essential", "Travelers Canada", "Essential"),
    ("SGI CANADA", "SGI CANADA", ""),
    ("Facility Association", "Facility Association", ""),
    ("Carrier description not available: NORD", "Nordic", ""),
    ("Gore Mutual", "Gore Mutual", ""),
    ("Gore", "Gore Mutual", ""),
    ("Coachman", "Coachman", ""),
    ("Echelon", "Echelon", ""),
    ("JEVCO", "JEVCO", ""),
    ("Pembridge", "Pembridge", ""),
    ("Pafco", "Pafco", ""),
    ("Economical Insurance", "Economical", ""),
    ("Economical", "Economical", ""),
]


def _carrier_from_variant(raw):
    raw = re.sub(r"\s+", " ", (raw or "").strip())
    for variant, carrier, product in ARS_CARRIER_VARIANTS:
        if raw.lower() == variant.lower():
            return carrier, product
    return "", ""


def extract_carrier_and_product(text):
    """
    AUTHORITATIVE CARRIER RULE

    Always take the insurer from the selected carrier-detail section:
        <CARRIER NAME>
        <carrier code> | New Business | Effective Date: ...

    That same section contains Annual Premium / Total Premium.
    The ARS comparison/summary list is NEVER used to select the insurer.

    If a carrier is new and not in our known list, preserve the exact
    carrier-detail header instead of returning blank.
    """
    text = _clean_ars_text(text or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    for i, line in enumerate(lines):
        if not re.search(r"\bNew Business\b.*\bEffective Date:", line, re.I):
            continue

        local_header_lines = []

        for back in range(1, 8):
            if i - back < 0:
                break

            candidate = re.sub(r"\s+", " ", lines[i - back]).strip()

            if not candidate:
                continue

            if re.search(
                r"(?i)^(?:WellCare Insurance Corp\.?|"
                r"\d{1,5}[- ]|Ajax,|Phone:|Email:|Home:|"
                r"Prepared\b|Applied Ref\.|Provided by\b)",
                candidate
            ):
                continue

            local_header_lines.append(candidate)

        if not local_header_lines:
            continue

        # First, find the strongest known carrier variant anywhere in the
        # local carrier header. This beats OCR logo fragments such as
        # "economical" printed next to "Definity Insurance Company".
        header_blob = " | ".join(local_header_lines)
        known_matches = []

        for variant, known_carrier, known_product in ARS_CARRIER_VARIANTS:
            pos = header_blob.lower().find(variant.lower())
            if pos >= 0:
                known_matches.append(
                    (len(variant), -pos, known_carrier, known_product, variant)
                )

        if known_matches:
            known_matches.sort(reverse=True)
            _, _, known_carrier, known_product, _ = known_matches[0]
            carrier_line = known_carrier
            preselected_product = known_product
        else:
            carrier_line = local_header_lines[0]
            preselected_product = ""

        section = "\n".join(lines[i:i + 220])

        if not (
            re.search(r"(?im)^\s*Annual Premium\b", section)
            or re.search(r"(?im)^\s*Total Premium\b", section)
            or re.search(r"(?im)^\s*Vehicles\s+TOTALS\b", section)
        ):
            continue

        if preselected_product or carrier_line in {
            carrier for _, carrier, _ in ARS_CARRIER_VARIANTS
        }:
            return carrier_line, preselected_product

        # OCR can append logo/tagline fragments to this line.
        candidates = []
        low_line = carrier_line.lower()

        for variant, known_carrier, known_product in ARS_CARRIER_VARIANTS:
            pos = low_line.find(variant.lower())
            if pos >= 0:
                candidates.append((len(variant), pos, known_carrier, known_product))

        if candidates:
            candidates.sort(reverse=True)
            _, _, carrier, product = candidates[0]
            return carrier, ""

        carrier, product = _carrier_from_variant(carrier_line)
        if carrier:
            return carrier, ""

        raw = carrier_line.strip(" -|")

        if re.search(r"[A-Za-z]", raw):
            return raw, ""

    # Strict local fallback around New Business only.
    # Never use the comparison Summary table.
    for anchor in re.finditer(r"\bNew Business\b", text, re.I):
        before = text[max(0, anchor.start() - 220):anchor.start()]

        candidates = []
        for variant, carrier, product in ARS_CARRIER_VARIANTS:
            for match in re.finditer(re.escape(variant), before, re.I):
                candidates.append((match.end(), len(variant), carrier, product))

        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]))
            _, _, carrier, product = candidates[-1]
            return carrier, ""

    return "", ""



def extract_carrier(text):
    return extract_carrier_and_product(text)[0]


def _normalize_limit(token):
    """Normalize ARS coverage-limit shorthand, never premium values."""
    raw = str(token or "").strip()
    if not raw:
        return ""

    compact = raw.replace("$", "").replace(",", "")
    compact = re.sub(r"\s+", "", compact).upper()

    m = re.fullmatch(r"([\d.]+)([MK])", compact)
    if m:
        number = Decimal(m.group(1))
        multiplier = Decimal("1000000") if m.group(2) == "M" else Decimal("1000")
        return f"${number * multiplier:,.0f}"

    if re.fullmatch(r"[\d.]+", compact):
        try:
            return f"${Decimal(compact):,.0f}"
        except Exception:
            pass

    return raw



def _coverage_from_application(text, label):
    m = re.search(rf"(?im)^\s*{re.escape(label)}\s*\(([^)\n]+)\)", text)
    return m.group(1).strip() if m else ""


def _detail_coverage_limit(text, aliases, kind="limit"):
    """
    Read ARS Carrier Detail limits/deductibles even when PDF extraction places
    every table cell on its own line. Never use premium-only columns as limits.
    """
    text = _clean_ars_text(text)
    flat = re.sub(r"\s+", " ", text)

    for alias in aliases:
        # Find all instances because UW/Application sections may occur before
        # the selected Carrier Detail section.
        for m in re.finditer(rf"(?i)(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", flat):
            rest = flat[m.end():m.end() + 100].strip()

            # Skip UW/Application syntax here; caller handles "(limit)" separately.
            if rest.startswith("("):
                continue

            if kind == "deductible":
                md = re.match(r"\$?([\d,]+)\s*Ded\.", rest, re.I)
                if md:
                    return f"${md.group(1)} deductible"

                # ARS extracted form: "0 970 970" => deductible 0, then premiums.
                md = re.match(r"([\d,]+)\s+\$?[\d,]+\s+\$?[\d,]+(?:\s|$)", rest)
                if md:
                    return f"${md.group(1)} deductible"

            # "1 M 300 300" or "50K 36 36"
            ml = re.match(r"(\d+)\s*([MK])\b", rest, re.I)
            if ml:
                return _normalize_limit(ml.group(1) + ml.group(2))

            # Explicit dollar limit, followed by premium columns.
            ml = re.match(r"(\$[\d,]+(?:\.\d+)?)", rest)
            if ml:
                dollars = re.findall(r"\$[\d,]+(?:\.\d+)?", rest[:70])
                # "$58 $58" = principal/total premium only, no limit printed.
                if len(dollars) >= 2 and dollars[0] == dollars[1]:
                    continue
                return _normalize_limit(ml.group(1))

            ml = re.match(r"([\d,]+K)\b", rest, re.I)
            if ml:
                return _normalize_limit(ml.group(1))

    return ""


def _coverage_present(text, aliases):
    alias_pattern = "|".join(re.escape(a) for a in aliases)
    return bool(re.search(rf"(?im)^\s*(?:{alias_pattern})(?:\s|$|\()", text))


def _extract_vehicle_drivers(text):
    """
    Extract every ARS vehicle-assigned driver.
    ARS marks vehicle assignments explicitly as:
      (Prn) = Primary
      (Occ) = Occasional

    These role markers do not appear on the later driver-detail pages, so
    scanning the whole PDF is safer than relying on ARS heading order.
    """
    text = _clean_ars_text(text)

    drivers = []
    seen = set()

    for match in re.finditer(
        r"(?im)^\s*([A-Za-z][A-Za-z .'\-]+?)\s*\((Prn|Occ)\)\s*$",
        text
    ):
        name = re.sub(r"\s+", " ", match.group(1)).strip()
        role = "Primary" if match.group(2).lower() == "prn" else "Occasional"

        key = (name.lower(), role)
        if key in seen:
            continue
        seen.add(key)

        drivers.append({"name": name, "role": role})

    return drivers


def _first_primary_driver(text, client=""):
    text = _clean_ars_text(text)

    m = re.search(r"(?im)^\s*([A-Za-z][A-Za-z .'\-]+?)\s*\(Prn\)\s*$", text)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()

    m = re.search(r"(?im)^\s*Driver\s+1\s+of\s+\d+\s*\|\s*([A-Za-z][A-Za-z .'\-]+?)\s*$", text)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()

    if client and re.search(r"\bNew Business\b", text, re.I):
        return client

    return ""


def _extract_vehicle(text):
    text = _clean_ars_text(text)
    m = re.search(r"Private Passenger\s*-\s*((?:19|20)\d{2}[^\n$]+)", text, re.I)
    if not m:
        m = re.search(r"\|\s*((?:19|20)\d{2}\s+[A-Z0-9][^\n$]+)", text, re.I)
    if not m:
        return ""

    vehicle = m.group(1)
    vehicle = re.sub(r"\s*\(\d{4,8}\)\s*$", "", vehicle)
    vehicle = re.sub(r"\s+", " ", vehicle).strip()
    return vehicle


def _extract_annual(text):
    text = _clean_ars_text(text)

    matches = re.findall(r"(?im)^\s*Total Premium\s+\$?([\d,]+\.\d{2})", text)
    if matches:
        return to_decimal(matches[0])

    matches = re.findall(r"(?im)^\s*Annual Premium\s+\$?([\d,]+\.\d{2})", text)
    if matches:
        return to_decimal(matches[0])

    return None


def _optional_row(text, name_patterns):
    """
    Parse an ARS Optional Accident Benefits row from either:
      - PDF text cells on separate lines, or
      - OCR text with the whole table row on one line.
    """
    text = _clean_ars_text(text)
    lines = [ln.strip() for ln in text.splitlines()]

    def normalize_limit_piece(value):
        value = value.strip()
        compact = value.replace("$", "").replace(" ", "")
        if not re.fullmatch(r"[\d,]+(?:\.\d+)?[Kk]?", compact):
            return ""
        return _normalize_limit(value)

    def normalize_compound(value):
        vals = []
        for part in value.split("/"):
            n = normalize_limit_piece(part)
            if not n:
                return ""
            vals.append(n)
        return " / ".join(vals)

    for pattern in name_patterns:
        for i, line in enumerate(lines):
            m = re.search(rf"(?i)^\s*{pattern}\s*(.*)$", line)
            if not m:
                continue

            rest = m.group(1).strip()

            # -----------------------------
            # OCR same-line row
            # -----------------------------
            if rest:
                # Death can OCR as:
                # Death $25,000 / $4 $4
                # $10,000
                # Reconstruct the visible 25,000 / 10,000 benefit.
                if re.search(r"(?i)^Death$", re.sub(r"\s+", " ", line[:m.start(1)]).strip()):
                    dm = re.match(
                        r"^\$?([\d,]+)\s*/\s*\$?([\d,]+)\s+\$?([\d,]+)\s*$",
                        rest
                    )
                    if dm:
                        first_limit = _normalize_limit(dm.group(1))
                        premium1 = dm.group(2)
                        premium2 = dm.group(3)

                        second_limit = ""
                        if i + 1 < len(lines):
                            nm = re.match(r"^\$?([\d,]+)\s*$", lines[i + 1])
                            if nm:
                                second_limit = _normalize_limit(nm.group(1))

                        limit = first_limit
                        if second_limit:
                            limit += f" / {second_limit}"

                        return limit, f"${premium2}/yr"

                # Standard OCR row:
                # $250 / $50 $1 $1
                # $185 $32 $32
                # $6,000 $1 $1
                sm = re.match(
                    r"^(\$?[\d,]+(?:\.\d+)?(?:\s*[Kk])?(?:\s*/\s*\$?[\d,]+(?:\.\d+)?(?:\s*[Kk])?)?)"
                    r"\s+\$?([\d,]+(?:\.\d+)?)\s+\$?([\d,]+(?:\.\d+)?)\s*$",
                    rest
                )
                if sm:
                    limit = normalize_compound(sm.group(1))
                    return limit or "Included", f"${sm.group(3)}/yr"

                # No-limit optional row:
                # Accident Waiver $195 $195
                nm = re.match(
                    r"^\$?([\d,]+(?:\.\d+)?)\s+\$?([\d,]+(?:\.\d+)?)\s*$",
                    rest
                )
                if nm:
                    return "Included", f"${nm.group(2)}/yr"

            # -----------------------------
            # Vertical-cell extraction
            # -----------------------------
            raw_cells = []
            for nxt in lines[i + 1:i + 10]:
                nxt = nxt.strip()
                if not nxt:
                    continue

                if re.match(
                    r"(?i)^(?:Annual Premium|Total Premium|0% Tax Applied|"
                    r"Death|Funeral|Non-Earner|Income Replacement|"
                    r"Caregiver \(|Dependant Care|Housekeeping & Home Maintenance|"
                    r"Lost Education Expenses|Expenses of Visitors|"
                    r"Damage to Personal Items|Accident Waiver|"
                    r"Optional Benefits Selected|BNDL\w+|PAK\w+|"
                    r"\d+\/\d+|#20\b|#27\b|#44\b)",
                    nxt
                ):
                    break

                raw_cells.append(nxt)

            numeric = re.compile(
                r"^\$?[\d,]+(?:\.\d+)?(?:\s*[Kk])?"
                r"(?:\s*/\s*\$?[\d,]+(?:\.\d+)?(?:\s*[Kk])?)?$"
            )

            cells = []
            started = False
            for cell in raw_cells:
                if numeric.match(cell):
                    started = True
                    cells.append(cell)
                elif started:
                    break

            if len(cells) >= 3:
                limit = normalize_compound(cells[0])
                p2 = cells[2].replace("$", "").strip()
                return limit or "Included", f"${p2}/yr"

            if len(cells) == 2:
                p2 = cells[1].replace("$", "").strip()
                return "Included", f"${p2}/yr"

            if len(cells) == 1:
                return normalize_compound(cells[0]), ""

            return "", ""

    return "", ""


def parse_auto(text):
    text = _clean_ars_text(text)

    client = extract_client(text)
    effective = first_match(
        [r"Effective Date:\s*([0-9]{2}/[0-9]{2}/[0-9]{4})"],
        text
    )
    carrier, product = extract_carrier_and_product(text)
    annual = _extract_annual(text)

    monthly = None
    monthly_interest = None
    if annual is not None:
        monthly = (
            annual * Decimal("1.013") / Decimal("12")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        monthly_interest = (
            annual * Decimal("0.013")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # -------------------------------------------------------
    # Drivers
    # -------------------------------------------------------
    drivers = _extract_vehicle_drivers(text)

    # Carrier-detail-only ARS PDFs do not always print the complete driver-name
    # list. They do, however, identify Driver #1 ... Driver #N in underwriting
    # messages. Preserve the actual count instead of pretending there is one.
    driver_numbers = [
        int(x) for x in re.findall(r"(?i)\bDriver\s*#\s*(\d+)", text)
    ]
    driver_count = max(driver_numbers) if driver_numbers else len(drivers)

    # Names that ARS explicitly prints in carrier messages.
    named_message_drivers = []
    for name in re.findall(
        r"(?im)\bDriver\s+([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,3}?)\s+is\s+not\s+eligible",
        text
    ):
        clean = re.sub(r"\s+", " ", name).strip()
        if clean and clean.lower() not in [x.lower() for x in named_message_drivers]:
            named_message_drivers.append(clean)

    # If the assignment page exists, its Prn/Occ list remains authoritative.
    # Otherwise keep the names ARS actually exposes without inventing roles.
    if not drivers and named_message_drivers:
        drivers = [{"name": n, "role": "Listed Driver"} for n in named_message_drivers]

    primary = next(
        (item["name"] for item in drivers if item.get("role") == "Primary"),
        ""
    )
    driver = primary or (drivers[0]["name"] if drivers else _first_primary_driver(text, client))

    if not driver_count and drivers:
        driver_count = len(drivers)

    # -------------------------------------------------------
    # Vehicles — preserve every vehicle and its own coverage
    # -------------------------------------------------------
    vehicle_summary = []
    summary_match = re.search(
        r"(?is)\bVehicles\s+TOTALS\s*(.*?)\bAnnual Premium\b",
        text
    )
    if summary_match:
        for m in re.finditer(
            r"(?im)^\s*Private Passenger\s*-\s*(.+?)\s+\$([\d,]+(?:\.\d{2})?)\s*$",
            summary_match.group(1)
        ):
            vehicle_summary.append({
                "vehicle": re.sub(r"\s+", " ", m.group(1)).strip(" ."),
                "annual": to_decimal(m.group(2))
            })

    # Fallback for normal one-vehicle ARS application PDFs.
    if not vehicle_summary:
        one_vehicle = _extract_vehicle(text)
        if one_vehicle:
            vehicle_summary = [{"vehicle": one_vehicle, "annual": annual}]

    # Carrier detail sections: "1 of 3 | VEHICLE", "2 of 3 | VEHICLE", ...
    section_matches = list(re.finditer(
        r"(?im)^\s*(\d+)\s+of\s+(\d+)\s*\|\s*(.+?)\s*$",
        text
    ))

    vehicle_sections = []
    for idx, m in enumerate(section_matches):
        title = re.sub(r"\s+", " ", m.group(3)).strip()

        # Skip property sections if a combined OCR text is ever supplied.
        if re.search(r"Homeowners|Tenants|Condo|Rented Dwelling", title, re.I):
            continue

        sec_start = m.start()
        sec_end = section_matches[idx + 1].start() if idx + 1 < len(section_matches) else len(text)
        block = text[sec_start:sec_end]

        # Stop before glossary if this is the final vehicle section.
        glossary = re.search(r"(?im)^\s*Glossary Of Terms\s*$", block)
        if glossary:
            block = block[:glossary.start()]

        vehicle_sections.append((int(m.group(1)), int(m.group(2)), title, block))

    if len(vehicle_sections) == 1 and vehicle_sections[0][1] == 1:
        pos, total_count, title, _old_block = vehicle_sections[0]
        carrier_anchor = re.search(
            r"(?im)^.*\bNew Business\b.*\bEffective Date:",
            text
        )
        if carrier_anchor:
            single_block = text[carrier_anchor.start():]
            glossary = re.search(r"(?im)^\s*Glossary Of Terms\s*$", single_block)
            if glossary:
                single_block = single_block[:glossary.start()]
            vehicle_sections = [(pos, total_count, title, single_block)]

    def section_coverage_limit(block, label, kind="limit"):
        """
        Read the first LIMIT/DEDUCTIBLE token after the ARS label.
        Later numbers in the row are premiums and must never be used as limits.
        """
        if kind == "deductible":
            m = re.search(
                rf"(?im)^\s*{label}\s+\$?([\d,]+(?:\.\d+)?)\s*(?:Ded\.|deductible)",
                block
            )
            if not m:
                return ""
            return f"${Decimal(m.group(1).replace(',', '')):,.0f} deductible"

        m = re.search(
            rf"(?im)^\s*{label}\s+\$?([\d,]+(?:\.\d+)?(?:\s*[MKmk])?)\b",
            block
        )
        if not m:
            return ""

        raw_value = m.group(1)
        normalized = _normalize_limit(raw_value)

        # Liability/family limits are never tiny carrier premium amounts.
        # ARS rows such as "Property Damage 58 58" contain premium columns
        # but no printed limit. In that case fall back to the UW/Application
        # coverage selection instead of presenting $58 as coverage.
        if re.search(r"Bodily Injury|Property Damage|Family Protection", label, re.I):
            compact = re.sub(r"[$,\s]", "", raw_value).upper()
            if not re.search(r"[MK]$", compact):
                try:
                    if Decimal(compact) < Decimal("10000"):
                        return ""
                except Exception:
                    return ""

        return normalized



    def included_or_na(block, label):
        m = re.search(
            rf"(?im)^\s*{label}\s+(.*)$",
            block
        )
        if not m:
            return ""
        row = m.group(1)
        if re.search(r"\bN/A\b", row, re.I):
            return ""
        if re.search(r"\bInc\.?\b|\bIncluded\b", row, re.I):
            return "Included"
        return ""

    def parse_optional_block(block):
        total = first_match(
            [r"Optional Accident Benefits\s*\(Total\s*\$?([\d,]+)\)"],
            block
        )
        if not total:
            return "", []

        opt_anchor = re.search(
            r"(?im)^\s*Optional Accident Benefits\s*\(Total\s*\$?[\d,]+\)",
            block
        )
        opt_text = block[opt_anchor.start():] if opt_anchor else block

        items = []

        def add(name, patterns, desc):
            for p in patterns:
                if re.search(rf"(?im)^\s*{p}", opt_text):
                    limit, premium = _optional_row(opt_text, patterns)
                    if not limit:
                        limit = "Included"
                    items.append({
                        "name": name,
                        "limit": limit,
                        "premium": premium,
                        "description": desc
                    })
                    return

        add("Death", [r"Death"], "Death benefit shown on the insurer quote.")
        add("Funeral", [r"Funeral"], "Funeral-expense benefit shown on the quote.")
        add("Non-Earner", [r"Non-Earner"], "Non-earner benefit shown on the quote.")
        add("Income Replacement", [r"Income Replacement"], "Income-replacement benefit shown on the quote.")
        add(
            "Medical, Rehabilitation and Attendant Care",
            [
                r"Medical,\s*Rehabilitation\s+and\s+Attendant Care",
                r"Medical\s*/\s*Rehabilitation\s*/\s*Attendant Care",
                r"Medical\s*&\s*Rehabilitation\s*&\s*Attendant Care"
            ],
            "Optional Medical, Rehabilitation and Attendant Care buy-up shown on the quote."
        )
        add("Caregiver (Catastrophic Only)", [r"Caregiver \(Catastrophic Only\)"], "Caregiver benefit shown on the quote.")
        add(
            "Caregiver (Impairment)",
            [r"Caregiver \(Impairment\)"],
            "Caregiver impairment benefit shown on the quote."
        )
        add("Dependant Care", [r"Dependant Care"], "Dependant-care benefit shown on the quote.")
        add("Housekeeping & Home Maintenance", [r"Housekeeping & Home Maintenance Expense"], "Housekeeping and home-maintenance benefit shown on the quote.")
        add("Lost Education Expenses", [r"Lost Education Expenses?"], "Lost-education expense benefit shown on the quote.")
        add("Damage to Personal Items", [r"Damage to Personal Items.*"], "Damage-to-personal-items benefit shown on the quote.")
        add("Expenses of Visitors", [r"Expenses of Visitors"], "Visitor-expense benefit shown on the quote.")
        add("Accident Waiver", [r"Accident Waiver"], "Optional accident-waiver benefit shown on the quote.")

        return total, items

    vehicles = []

    for pos, total_count, title, block in vehicle_sections:
        # Match section title to summary entry, preserving summary total premium.
        summary = vehicle_summary[pos - 1] if 0 < pos <= len(vehicle_summary) else {}
        vehicle_name = summary.get("vehicle") or title

        vannual = summary.get("annual")
        if vannual is None:
            # Last Total Premium value in this section is the section total.
            vals = re.findall(r"(?im)^\s*Total Premium\s+\$?([\d,]+\.\d{2})", block)
            if vals:
                vannual = to_decimal(vals[-1])

        cov = []

        def add_cov(name, value, desc):
            if value:
                cov.append({"name": name, "value": value, "description": desc})

        bodily_injury_limit = section_coverage_limit(block, r"Bodily Injury")
        if not bodily_injury_limit:
            bodily_injury_limit = section_coverage_limit(
                block,
                r"Bodily Injury\s*/\s*Prop\.\s*Damage"
            )
        if not bodily_injury_limit:
            bodily_injury_limit = _coverage_from_application(text, "Bodily Injury")
            if bodily_injury_limit:
                bodily_injury_limit = _normalize_limit(bodily_injury_limit)
        add_cov("Bodily Injury", bodily_injury_limit,
                "Liability protection for covered bodily injury claims.")
        property_damage_limit = section_coverage_limit(block, r"Property Damage")
        if not property_damage_limit:
            property_damage_limit = section_coverage_limit(
                block,
                r"Bodily Injury\s*/\s*Prop\.\s*Damage"
            )
        if not property_damage_limit:
            property_damage_limit = _coverage_from_application(text, "Property Damage")
            if property_damage_limit:
                property_damage_limit = _normalize_limit(property_damage_limit)
        add_cov("Property Damage", property_damage_limit,
                "Liability protection for covered damage to another person's property.")
        add_cov("Direct Compensation", section_coverage_limit(block, r"Direct Compensation", "deductible"),
                "Coverage for eligible damage to your vehicle under Ontario Direct Compensation rules.")
        add_cov(
            "Mandatory Accident Benefits",
            "Included",
            "Medical, Rehabilitation & Attendant Care — mandatory Ontario accident benefits."
        )
        add_cov("All Perils", section_coverage_limit(block, r"All Perils", "deductible"),
                "Physical damage protection combining Collision and Comprehensive coverage.")
        if re.search(r"(?im)^\s*Uninsured Automobile\b", block):
            add_cov("Uninsured Automobile", "Included",
                    "Protection for eligible losses involving an uninsured or unidentified driver.")
        add_cov("Loss of Use (OPCF 20)", section_coverage_limit(block, r"\#20 Loss of Use"),
                "Transportation replacement following an eligible covered loss.")
        nonowned = section_coverage_limit(block, r"\#27 Liab to Unowned Veh\.")
        if not nonowned:
            nonowned = included_or_na(block, r"\#27 Liab to Unowned Veh\.")
        add_cov("Legal Liability for Non-Owned Auto (OPCF 27)", nonowned,
                "Protection for eligible damage to certain non-owned vehicles.")
        add_cov("Family Protection (OPCF 44R)", section_coverage_limit(block, r"\#44 Family Protection"),
                "Additional protection when an at-fault driver has insufficient liability insurance.")

        if re.search(r"(?im)^\s*Minor Conviction Protection\b.*\bInc\.?\b", block):
            add_cov("Minor Conviction Protection", "Included",
                    "Helps protect an eligible conviction-free discount after a first minor conviction.")

        if re.search(r"(?im)^\s*Accident Waiver\b.*\bInc\.?\b", block):
            add_cov("Accident Waiver", "Included",
                    "Accident-waiver coverage shown as included by the insurer.")

        opt_total, opts = parse_optional_block(block)

        expected_optional_names = _detect_optional_benefit_names(block)

        vehicles.append({
            "number": pos,
            "vehicle": vehicle_name,
            "annual": vannual,
            "coverages": cov,
            "optional_total": opt_total,
            "optional": opts,
            "_broker_expected_optional_names": expected_optional_names,
            "_broker_expected_optional_count": len(expected_optional_names),
            "_broker_optional_bundle_premium": _optional_bundle_premium_sum(block)
        })

    # If no carrier-detail sections were detected, keep old single-vehicle logic.
    if not vehicles:
        vehicle = vehicle_summary[0]["vehicle"] if vehicle_summary else _extract_vehicle(text)

        bodily = _detail_coverage_limit(text, ["Bodily Injury"])
        prop = _detail_coverage_limit(text, ["Property Damage"])
        dcpd = _detail_coverage_limit(text, ["Direct Compensation"], kind="deductible")
        all_perils = _detail_coverage_limit(text, ["All Perils"], kind="deductible")
        loss = _detail_coverage_limit(text, ["#20 Loss of Use", "20 Loss of Use"])
        non_owned = _detail_coverage_limit(text, ["#27 Liab to Unowned Veh.", "27 Liab to Unowned Veh."])
        family = _detail_coverage_limit(text, ["#44 Family Protection", "44 Family Protection"])

        cov = []
        def add(name, value, desc):
            if value:
                cov.append({"name": name, "value": value, "description": desc})

        add("Bodily Injury", bodily, "Liability protection for covered bodily injury claims.")
        add("Property Damage", prop, "Liability protection for covered damage to another person's property.")
        add("Direct Compensation", dcpd, "Coverage for eligible damage to your vehicle under Ontario Direct Compensation rules.")
        add(
            "Mandatory Accident Benefits",
            "Included",
            "Medical, Rehabilitation & Attendant Care — mandatory Ontario accident benefits."
        )
        add("All Perils", all_perils, "Physical damage protection combining Collision and Comprehensive coverage.")
        if _coverage_present(text, ["Uninsured Automobile"]):
            add("Uninsured Automobile", "Included", "Protection for eligible losses involving an uninsured or unidentified driver.")
        add("Loss of Use (OPCF 20)", loss, "Transportation replacement following an eligible covered loss.")
        add("Legal Liability for Non-Owned Auto (OPCF 27)", non_owned, "Protection for eligible damage to certain non-owned vehicles.")
        add("Family Protection (OPCF 44R)", family, "Additional protection when an at-fault driver has insufficient liability insurance.")

        opt_total = first_match([r"Optional Accident Benefits\s*\(Total\s*\$?([\d,]+)\)"], text)
        vehicles = [{
            "number": 1,
            "vehicle": vehicle,
            "annual": annual,
            "coverages": cov,
            "optional_total": opt_total,
            "optional": []
        }]

    # Single-vehicle quotes: compare parsed Optional AB against the clean
    # ARS Summary list, independent of the carrier-detail table.
    if len(vehicles) == 1:
        summary_optional_names = _detect_summary_optional_benefit_names(text)
        if summary_optional_names:
            vehicles[0]["_broker_expected_optional_names"] = summary_optional_names
            vehicles[0]["_broker_expected_optional_count"] = len(summary_optional_names)

    vehicle = vehicles[0]["vehicle"] if vehicles else ""
    coverages = vehicles[0]["coverages"] if vehicles else []
    optional_total = vehicles[0]["optional_total"] if vehicles else ""
    optional = vehicles[0]["optional"] if vehicles else []

    return {
        "type": "auto",
        "client": client,
        "effective": effective,
        "carrier": carrier,
        "product": "",
        "vehicle": vehicle,
        "vehicles": vehicles,
        "vehicle_count": len(vehicles),
        "driver": driver,
        "drivers": drivers,
        "driver_count": driver_count,
        "annual": annual,
        "monthly": monthly,
        "monthly_interest": monthly_interest,
        "coverages": coverages,
        "optional_total": optional_total,
        "optional": optional,
        "_broker_unmapped_rows": _potential_ars_rows(text, "auto")
    }


# =========================================================
# HOME
# =========================================================

def parse_home(text):
    text = _clean_ars_text(text)

    # --------------------------------------------------------
    # CLIENT — same ARS rule as every other quote type: the top-right
    # customer card on page 1 is authoritative (handled inside
    # extract_client(), which checks that block first). "Insured
    # Information" is only a fallback for the rare case where the
    # top-right card and every other extract_client() strategy fail.
    # --------------------------------------------------------
    client = extract_client(text)

    if not client:
        insured = re.search(
            r"(?im)^\s*Insured Information\s*$\s*\n\s*([^\n]+?)\s*\n\s*Name\s*$",
            text
        )
        if insured:
            client = re.sub(r"\s+", " ", insured.group(1)).strip()

    effective = first_match(
        [r"Effective Date:\s*([0-9]{2}/[0-9]{2}/[0-9]{4})"],
        text
    )

    carrier = extract_carrier(text)

    # --------------------------------------------------------
    # TYPE / ADDRESS
    # --------------------------------------------------------
    risk_type = ""
    if re.search(r"\bRented Dwelling\b", text, re.I):
        risk_type = "Rented Dwelling"
    elif re.search(r"\bTenants\b", text, re.I):
        risk_type = "Tenants"
    elif re.search(r"\bHomeowners?\b", text, re.I):
        risk_type = "Homeowners"
    elif re.search(r"\bCondo\b", text, re.I):
        risk_type = "Condo"

    address = first_match(
        [
            r"Primary\s*-\s*Homeowners?\s*-\s*([^\n$]+)",
            r"Primary\s*-\s*Homeowners?\s+([^\n$]+)",
            r"Primary\s*-\s*Rented Dwelling\s*-\s*([^\n$]+)",
            r"Primary\s*-\s*Rented Dwelling\s+([^\n$]+)",
            r"Primary\s*-\s*Tenants\s*-\s*([^\n$]+)",
            r"Primary\s*-\s*Tenants\s+([^\n$]+)",
            r"Primary\s*-\s*Condo\s*-\s*([^\n$]+)",
            r"Primary\s*-\s*Condo\s+([^\n$]+)"
        ],
        text
    )
    address = re.sub(r"\s+\$[\d,]+(?:\.\d{2})?$", "", address).strip(" ,-")

    # --------------------------------------------------------
    # AUTHORITATIVE CARRIER DETAIL
    # Must be the code | New Business | Effective Date section,
    # not the user-entry page that merely contains "New Business".
    # --------------------------------------------------------
    detail_text = text
    anchor = re.search(
        r"(?im)^\s*[A-Z0-9][A-Z0-9\-]{3,}\s*\|\s*New Business\s*\|\s*Effective Date:",
        text
    )
    if not anchor and "[[WAQI_OCR_USED]]" in text:
        anchor = re.search(
            r"(?im)^\s*New Business\s*\|\s*Effective Date:",
            text
        )

    if anchor:
        detail_text = text[anchor.start():]

    glossary = re.search(r"(?im)^\s*Glossary Of Terms\s*$", detail_text)
    if glossary:
        detail_text = detail_text[:glossary.start()]

    # --------------------------------------------------------
    # PREMIUM TOTALS — carrier detail only
    # --------------------------------------------------------
    base_raw = first_match(
        [r"(?im)^\s*Annual Premium\s+\$?([\d,]+(?:\.\d{2})?)"],
        detail_text
    )
    base_premium = to_decimal(base_raw)

    tax_raw = first_match(
        [r"(?im)^\s*8%\s*Tax Applied\s+\$?([\d,]+(?:\.\d{2})?)"],
        detail_text
    )
    tax = to_decimal(tax_raw)

    total_raw = first_match(
        [r"(?im)^\s*Total Premium\s+\$?([\d,]+(?:\.\d{2})?)"],
        detail_text
    )
    annual = to_decimal(total_raw)

    if annual is None and base_premium is not None:
        annual = (
            base_premium + tax
            if tax is not None
            else base_premium * Decimal("1.08")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    monthly = None
    monthly_interest = None
    if annual is not None:
        monthly = (annual * Decimal("1.03") / Decimal("12")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        monthly_interest = (annual * Decimal("0.03")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    # --------------------------------------------------------
    # CARRIER TABLE CELL READER
    # PDFs visually show rows, but embedded text is often vertical:
    # Contents
    # $40,000
    # $440
    # --------------------------------------------------------
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in detail_text.splitlines()
        if re.sub(r"\s+", " ", line).strip()
    ]

    known_labels = {
        "Outbuildings",
        "Residence",
        "Contents",
        "Additional Living Expenses",
        "Voluntary Medical",
        "Voluntary Property",
        "Deductible",
        "Single Limit",
        "Guaranteed Building Replacement Cost",
        "Personal Insurance",
        "Legal Liability",
        "Liability",
        "Swimming Pool",
        "Swimming pool",
        "Sewer Backup",
        "Ground Water",
        "Overland Water",
        "Above Ground Water Damage",
        "By-Laws",
        "Identity Theft",
        "Claim Free Protection",
        "Home Warranty",
        "Service Line Coverage",
        "Discounts - Premiums may have been rounded.",
        "Claim Free",
        "Combined Policy",
        "Extended Coverages",
        "Annual Premium",
        "Breakdown",
    }

    def cells_after(label_pattern, max_cells=8):
        """
        General ARS table reader.

        Accepts:
          native vertical cells:
            Contents
            $40,000
            $440

          OCR horizontal rows:
            Contents $20,000 $435
            S Contents $20,000 $435
            2 Contents $20,000 $435

        Short leading OCR artifacts are ignored.
        """
        prefix = r"(?:[^A-Za-z#]*|[A-Za-z]{1,2}\s+)"
        horizontal_rx = re.compile(
            rf"(?i)^{prefix}{label_pattern}\s+(.+)$"
        )
        exact_rx = re.compile(
            rf"(?i)^{prefix}{label_pattern}$"
        )

        for line in lines:
            hm = horizontal_rx.match(line)
            if hm:
                cells = re.findall(
                    r"\$[\d,]+(?:\.\d+)?|Inc\.?|Included|N/A|Max|Limit",
                    hm.group(1), re.I
                )
                if cells:
                    return cells

        for i,line in enumerate(lines):
            if not exact_rx.match(line):
                continue

            cells=[]
            for nxt in lines[i+1:i+1+max_cells]:
                if re.fullmatch(r"(?i)(Coverage|Deductible|Amount|Premium)",nxt):
                    continue

                clean=re.sub(r"^(?:[^A-Za-z#]*|[A-Za-z]{1,2}\s+)","",nxt).strip()
                if nxt in known_labels or clean in known_labels:
                    break

                if (
                    re.fullmatch(r"\$[\d,]+(?:\.\d+)?",nxt)
                    or re.fullmatch(r"(?i)Inc\.?|Included|N/A|Max|Limit",nxt)
                ):
                    cells.append(nxt)
                elif cells:
                    break

            if cells:
                return cells

        return []

    def money_token(value):
        return bool(re.fullmatch(r"\$[\d,]+(?:\.\d+)?", value or ""))

    def included_token(value):
        return bool(re.fullmatch(r"(?i)Inc\.?|Included", value or ""))

    def ordinary_values(label_pattern):
        cells=cells_after(label_pattern)
        amount=premium=deductible=""

        if not cells:
            return amount,premium,deductible

        if len(cells)==1:
            x=cells[0]
            if included_token(x):
                return "Included","Included",""
            if money_token(x):
                return x,"",""
            if x.upper()=="N/A":
                return "N/A","N/A",""

        elif len(cells)>=3:
            if money_token(cells[0]) or cells[0].upper()=="N/A":
                deductible=cells[0]
            if money_token(cells[1]) or cells[1].upper() in {"N/A","MAX","LIMIT"}:
                amount=cells[1]

            x=cells[2]
            if included_token(x):
                premium="Included"
            elif money_token(x) or x.upper()=="N/A":
                premium=x

        else:
            a,b=cells
            if included_token(a):
                amount="Included"
            elif money_token(a) or a.upper()=="N/A":
                amount=a

            if included_token(b):
                premium="Included"
            elif money_token(b) or b.upper()=="N/A":
                premium=b

        return amount,premium,deductible

    def water_values(label_pattern):
        """
        Extended-water rows:
          Sewer Backup -> deductible $2,500 | premium $29
          Ground Water -> N/A | N/A
          Overland Water -> deductible $2,500 | premium $40

        Coverage Amount Max/Limit is read from the user-entry coverage page
        only if carrier detail did not return N/A.
        """
        cells = cells_after(label_pattern)

        deductible = ""
        premium = ""

        if cells:
            if money_token(cells[0]):
                deductible = cells[0]
            elif cells[0].upper() == "N/A":
                deductible = "N/A"

        if len(cells) >= 2:
            if money_token(cells[1]):
                premium = cells[1]
            elif included_token(cells[1]):
                premium = "Included"
            elif cells[1].upper() == "N/A":
                premium = "N/A"

        # Carrier result wins.
        if deductible == "N/A" and premium == "N/A":
            return "N/A", deductible, premium

        summary = re.search(
            rf"(?is)\b{label_pattern}\b.*?\b(Max|Limit)\b\s*Amount.*?\$([\d,]+)\s*Deductible",
            text
        )
        amount = summary.group(1) if summary else ""

        return amount, deductible, premium

    coverages = []

    def add(name, value, description, premium=""):
        if not value:
            return

        if included_token(premium):
            premium = "Included"

        coverages.append({
            "name": name,
            "value": value,
            "premium": premium or "",
            "description": description
        })

    # --------------------------------------------------------
    # CORE HOME COVERAGES
    # --------------------------------------------------------
    for label, name, description in [
        (r"Outbuildings", "Outbuildings",
         "Coverage for eligible detached structures and outbuildings."),
        (r"Residence", "Residence",
         "Building coverage for the insured residence."),
        (r"Contents", "Contents",
         "Coverage for eligible personal contents."),
        (r"Additional Living Expenses", "Additional Living Expenses",
         "Eligible additional living expenses following a covered loss."),
        (r"Voluntary Medical", "Voluntary Medical",
         "Voluntary medical payments shown on the insurer quote."),
        (r"Voluntary Property", "Voluntary Property",
         "Voluntary property-damage payments shown on the insurer quote."),
    ]:
        amount, premium, _row_ded = ordinary_values(label)
        if amount:
            add(name, amount, description, premium)

    # Policy Deductible: first cell is deductible, second is premium/status.
    ded_cells = cells_after(r"Deductible")
    if ded_cells:
        ded_value = ded_cells[0] if money_token(ded_cells[0]) else ""
        ded_premium = ""
        if len(ded_cells) > 1:
            if included_token(ded_cells[1]):
                ded_premium = "Included"
            elif money_token(ded_cells[1]):
                ded_premium = ded_cells[1]
        if ded_value:
            add(
                "Policy Deductible",
                ded_value,
                "The policy deductible shown on the insurer quote.",
                ded_premium
            )


    # Single Limit
    single_amount, single_premium, _row_ded = ordinary_values(r"Single Limit")
    if single_amount:
        add(
            "Single Limit",
            single_amount,
            "Combined single limit shown on the insurer quote.",
            single_premium
        )

    # Legal Liability may be labelled Personal Insurance or Liability.
    liability_amount, liability_premium, liability_ded = ordinary_values(r"Personal Insurance")
    if not liability_amount:
        liability_amount, liability_premium, liability_ded = ordinary_values(r"Liability")

    if liability_amount:
        liability_value = liability_amount
        if liability_ded:
            liability_value = f"{liability_amount} coverage / {liability_ded} deductible"

        add(
            "Legal Liability",
            liability_value,
            "Personal legal liability protection up to the limit shown.",
            liability_premium
        )

    # Guaranteed replacement cost
    grc_cells = cells_after(r"Guaranteed Building Replacement Cost")
    if grc_cells:
        first = grc_cells[0]
        if included_token(first):
            add(
                "Guaranteed Building Replacement Cost",
                "Included",
                "Guaranteed building replacement cost is included.",
                "Included"
            )

    # Swimming Pool
    pool_cells = cells_after(r"Swimming [Pp]ool")
    if pool_cells:
        value = pool_cells[0]
        add(
            "Swimming Pool",
            value,
            "Swimming-pool field as shown on the insurer quote."
        )

    # By-Laws
    bylaws_amount, bylaws_premium, _row_ded = ordinary_values(r"By-Laws")
    if bylaws_amount:
        add(
            "By-Laws",
            bylaws_amount,
            "By-law coverage up to the amount shown.",
            bylaws_premium
        )

    # Service Line Coverage may print inline with "- $..."
    service_match = re.search(
        r"(?im)^\s*Service Line Coverage\s*-\s*\$([\d,]+)",
        detail_text
    )
    if service_match:
        add(
            "Service Line Coverage",
            f"${service_match.group(1)}",
            "Service-line coverage up to the limit shown."
        )

    # Identity Theft can use Deductible | Amount | Premium.
    identity_amount, identity_premium, identity_ded = ordinary_values(r"Identity Theft")
    if identity_amount:
        identity_value = identity_amount
        if identity_ded:
            identity_value = f"{identity_amount} coverage / {identity_ded} deductible"

        add(
            "Identity Theft",
            identity_value,
            "Identity-theft coverage shown on the insurer quote.",
            identity_premium
        )

    # Telephone Legal Helpline
    helpline_amount, helpline_premium, helpline_ded = ordinary_values(
        r"Telephone Legal Helpline"
    )
    if helpline_amount:
        add(
            "Telephone Legal Helpline",
            helpline_amount,
            "Telephone legal helpline as shown on the insurer quote.",
            helpline_premium
        )


    # Claim Free Protection
    claim_cells = cells_after(r"Claim Free Protection")
    if claim_cells:
        value = claim_cells[0]
        if included_token(value):
            value = "Included"
        add(
            "Claim Free Protection",
            value,
            "Claim-free protection field as shown on the quote.",
            "Included" if value == "Included" else ""
        )

    # Home Warranty
    warranty_cells = cells_after(r"Home Warranty")
    if warranty_cells:
        deductible = ""
        premium = ""
        status = ""

        for cell in warranty_cells:
            if money_token(cell) and not deductible:
                deductible = cell
            elif included_token(cell):
                status = "Included"
                premium = "Included"
            elif cell.upper() == "N/A":
                status = "N/A"

        pieces = []
        if deductible:
            pieces.append(f"{deductible} deductible")
        if status:
            pieces.append(status)

        add(
            "Home Warranty",
            " / ".join(pieces) if pieces else "N/A",
            "Home-warranty field as shown on the insurer quote.",
            premium
        )

    # --------------------------------------------------------
    # EXTENDED WATER COVERAGES
    # --------------------------------------------------------
    for label, name in [
        (r"Sewer Backup", "Sewer Backup"),
        (r"Ground Water", "Ground Water"),
        (r"Overland Water", "Overland Water"),
        (r"Above Ground Water Damage", "Above Ground Water Damage"),
    ]:
        amount, deductible, premium = water_values(label)

        if amount == "N/A":
            value = "N/A"
        else:
            parts = []
            if amount:
                parts.append(f"{amount} coverage")
            if deductible:
                parts.append(f"{deductible} deductible")
            value = " / ".join(parts) if parts else "N/A"

        add(
            name,
            value,
            f"{name} coverage as shown on the insurer quote.",
            premium
        )

    # --------------------------------------------------------
    # DISCOUNTS
    # --------------------------------------------------------
    discounts = []
    disc_match = re.search(
        r"(?is)Discounts\s*-\s*Premiums may have been rounded\.\s*(.*?)\s*Extended Coverages",
        detail_text
    )
    if disc_match:
        for line in disc_match.group(1).splitlines():
            clean = re.sub(r"^[•\-\s]+", "", line).strip()
            if clean and clean not in discounts:
                discounts.append(clean)

    return {
        "type": "home",
        "client": client,
        "effective": effective,
        "carrier": carrier,
        "risk_type": risk_type,
        "address": address,
        "base_premium": base_premium,
        "tax": tax,
        "annual": annual,
        "monthly": monthly,
        "monthly_interest": monthly_interest,
        "coverages": coverages,
        "discounts": discounts,
        "_broker_unmapped_rows": _potential_ars_rows(text, "home")
    }



def serialize_quote(data):
    if data is None:
        return None

    if isinstance(data, Decimal):
        return str(data)

    if isinstance(data, dict):
        return {
            key: serialize_quote(value)
            for key, value in data.items()
        }

    if isinstance(data, list):
        return [
            serialize_quote(value)
            for value in data
        ]

    return data


def deserialize_quote(data):
    if data is None:
        return None

    decimal_fields = [
        "annual",
        "monthly",
        "monthly_interest",
        "base_premium",
        "tax"
    ]

    for field in decimal_fields:
        if data.get(field) not in [None, ""]:
            data[field] = Decimal(str(data[field]))

    return data


# =========================================================
# STORAGE
# =========================================================


def _strip_broker_metadata(data):
    if isinstance(data, dict):
        return {key: _strip_broker_metadata(value) for key, value in data.items() if not str(key).startswith("_broker_")}
    if isinstance(data, list):
        return [_strip_broker_metadata(value) for value in data]
    return data


def save_quote(auto, home):
    quote_id = secrets.token_urlsafe(9)

    payload = {
        "id": quote_id,
        "auto": serialize_quote(_strip_broker_metadata(auto)),
        "home": serialize_quote(_strip_broker_metadata(home))
    }

    filename = os.path.join(
        QUOTES_DIR,
        f"{quote_id}.json"
    )

    with open(filename, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)

    return quote_id


def load_quote(quote_id):
    safe_id = re.sub(
        r"[^A-Za-z0-9_-]",
        "",
        quote_id
    )

    filename = os.path.join(
        QUOTES_DIR,
        f"{safe_id}.json"
    )

    if not os.path.exists(filename):
        return None

    with open(filename, "r", encoding="utf-8") as file:
        data = json.load(file)

    data["auto"] = deserialize_quote(data.get("auto"))
    data["home"] = deserialize_quote(data.get("home"))

    return data


# =========================================================
# WHATSAPP
# =========================================================

def build_whatsapp(auto, home, quote_url=""):
    source = auto if auto else home

    client = (
        source["client"]
        if source and source["client"]
        else "there"
    )
    client = first_name(client)

    lines = [
        f"Hi {client}! Here's your insurance quote.",
        ""
    ]

    if auto:
        lines += [
            "AUTO INSURANCE",
            auto["vehicle"] or "Vehicle",
            "",
            f"${money(auto['annual'])}/yr (${money(auto['monthly'])}/mo)",
            "Policy Term: 12 Months"
        ]

        carrier_line = auto["carrier"] or ""

        if auto["product"]:
            if carrier_line:
                carrier_line += " - "

            carrier_line += auto["product"]

        if carrier_line:
            lines.append(f"Insurance: {carrier_line}")

        lines.append("")

    if home:
        lines += [
            "HOME INSURANCE",
            home["risk_type"] or "Property",
            home["address"] or "",
            "",
            f"${money(home['annual'])}/yr (${money(home['monthly'])}/mo)",
            "Policy Term: 12 Months"
        ]

        if home["carrier"]:
            lines.append(
                f"Insurance: {home['carrier']}"
            )

        lines.append("")

    if auto and home:
        total_annual = (
            (auto["annual"] or Decimal("0")) +
            (home["annual"] or Decimal("0"))
        )

        total_monthly = (
            (auto["monthly"] or Decimal("0")) +
            (home["monthly"] or Decimal("0"))
        )

        lines += [
            "AUTO + HOME TOTAL",
            f"${money(total_annual)}/yr (${money(total_monthly)}/mo)",
            ""
        ]

    if quote_url:
        lines += [
            "I've put together a clear breakdown of the quote for you here:",
            quote_url,
            ""
        ]

    lines.append(
        "If you'd like to move forward, just let me know."
    )

    return "\n".join(lines)


# =========================================================
# BROKER CONSOLE
# =========================================================

CONSOLE_HTML = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Waqi Quote Console</title>

<style>
*{box-sizing:border-box} html,body{max-width:100%;overflow-x:hidden} img{max-width:100%}
body{
margin:0;
background:#071d34;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
color:#071d34;
}
.shell{
width:min(1100px,94%);
margin:auto;
padding:28px 0 60px;
}
.card{
background:white;
border-radius:20px;
padding:28px;
margin-bottom:18px;
}
.logo{
display:block;
max-width:230px;
max-height:100px;
object-fit:contain;
margin-bottom:20px;
}
h1{margin:0;font-size:38px}
.subtitle{color:#68778b}
.drop-zone{
width:100%;
min-height:210px;
border:2px dashed #b6c0cb;
border-radius:16px;
background:#f8fafb;
display:flex;
align-items:center;
justify-content:center;
text-align:center;
cursor:pointer;
padding:30px;
margin-top:24px;
}
.drop-zone.active{
border-color:#c99a36;
background:#fff7e5;
}
.upload-icon{
width:60px;
height:60px;
margin:0 auto 14px;
border-radius:50%;
background:#071d34;
color:white;
display:flex;
align-items:center;
justify-content:center;
font-size:27px;
}
.drop-title{font-size:21px;font-weight:900}
.drop-subtitle{margin-top:7px;color:#718093;font-size:14px}
#pdfs{display:none}
#fileNames{margin-top:13px;font-weight:800}
button{
border:0;
border-radius:10px;
background:#c99a36;
color:#071d34;
padding:13px 22px;
font-weight:900;
cursor:pointer;
}
.actions{
display:flex;
justify-content:flex-end;
margin-top:16px;
}
.type{
font-size:11px;
font-weight:900;
letter-spacing:.13em;
color:#b8892d;
margin-bottom:6px;
}
.edit-grid{
display:grid;
grid-template-columns:repeat(3,1fr);
gap:12px;
margin-top:18px;
}
.field label{
display:block;
font-size:10px;
font-weight:900;
letter-spacing:.06em;
text-transform:uppercase;
color:#7c8998;
margin-bottom:5px;
}
.field input{
width:100%;
border:1px solid #d5dce4;
border-radius:9px;
padding:11px 12px;
font:inherit;
font-weight:700;
color:#071d34;
background:#fff;
}
.field.wide{grid-column:span 2}
.price-grid{
display:grid;
grid-template-columns:1fr 1fr;
gap:12px;
margin-top:12px;
}
.section-title{
margin:24px 0 10px;
font-size:15px;
font-weight:900;
}
.coverages{
display:grid;
grid-template-columns:repeat(2,1fr);
gap:10px;
}
.coverage-edit{
background:#f5f7f9;
padding:10px 12px;
border-radius:10px;
}
.coverage-edit input{
width:100%;
border:1px solid #d5dce4;
border-radius:8px;
padding:9px 10px;
font:inherit;
margin-top:7px;
}
.coverage-edit small{
display:block;
color:#071d34;
font-weight:900;
font-size:12px;
margin-bottom:8px;
}

.coverage-edit-art{
display:grid;
grid-template-columns:54px minmax(0,1fr);
gap:11px;
align-items:center;
}
.review-art{
width:54px;
height:54px;
border-radius:14px;
display:flex;
align-items:center;
justify-content:center;
background:linear-gradient(145deg,#071d34,#123b60);
border:1px solid rgba(201,154,54,.55);
color:#e0b24f;
font-size:21px;
font-weight:900;
letter-spacing:-.05em;
box-shadow:inset 0 0 0 1px rgba(255,255,255,.04);
}
.review-copy{
min-width:0;
}
.review-copy input{
margin-top:4px;
}
.mini-label{
display:block;
font-size:9px;
font-weight:900;
letter-spacing:.06em;
text-transform:uppercase;
color:#8b97a5;
margin:7px 0 3px;
}
.notice{
background:#fff7e5;
border:1px solid #ead7aa;
border-radius:12px;
padding:13px 14px;
margin-bottom:18px;
font-size:13px;
line-height:1.45;
}
.generate{
width:100%;
background:#071d34;
color:white;
font-size:16px;
}
textarea{
width:100%;
min-height:180px;
border:1px solid #d5dce4;
border-radius:10px;
padding:14px;
font:inherit;
resize:vertical;
}
.debug{
white-space:pre-wrap;
font-size:10px;
max-height:280px;
overflow:auto;
background:#f5f7f9;
padding:12px;
}
summary{
cursor:pointer;
font-weight:800;
}
.combined{
background:#071d34;
color:white;
}
.combined .type{color:#d6ad59}
.combined h2{margin:0 0 12px}
.combined strong{
font-size:28px;
display:block;
}
.combined span{color:#cbd3dc}
@media(max-width:760px){
.edit-grid,
.price-grid,
.coverages{
grid-template-columns:1fr;
}
.field.wide{grid-column:span 1}
.coverage-edit-art{
grid-template-columns:46px minmax(0,1fr);
}
.review-art{
width:46px;
height:46px;
font-size:18px;
}
}

.broker-warning-box{margin:0 0 22px;padding:16px 18px;border:1px solid #d7a43e;border-radius:16px;background:#fff9ea;color:#5c4213}
.broker-warning-box strong{display:block;font-size:16px;margin-bottom:7px}
.broker-warning-box p,.broker-warning-box li{font-size:13px;line-height:1.5}
.broker-warning-box p{margin:0 0 8px}
.broker-warning-box ul{margin:8px 0 0 20px;padding:0}
</style>
</head>

<body>
<div class="shell">

<div class="card">
<img src="{{ url_for('static', filename='waqi-logo.png') }}" class="logo" alt="Waqi Insures">
<h1>Quote Console</h1>
<p class="subtitle">Upload an Auto quote, Home quote, or both.</p>

<form method="POST" enctype="multipart/form-data">
<label class="drop-zone" id="dropZone" for="pdfs">
<div>
<div class="upload-icon">↓</div>
<div class="drop-title">Drop quote PDF here</div>
<div class="drop-subtitle">Drag & Drop or click to choose files</div>
<div id="fileNames"></div>
</div>
</label>

<input id="pdfs" type="file" name="pdfs" accept=".pdf" multiple required placeholder="Not detected — please verify">

<div class="actions">
<button type="submit">Process Quote</button>
</div>
</form>
</div>

{% if auto or home %}

{% if broker_warnings %}
<div class="broker-warning-box">
<strong>Review recommended</strong>
<p>These warnings do not block you. If you checked the original ARS quote and everything is correct, you can still generate the customer quote. Unknown fields are flagged here so they cannot disappear silently.</p>
<ul>{% for warning in broker_warnings %}<li>{{ warning }}</li>{% endfor %}</ul>
</div>
{% endif %}

<form method="POST" action="/generate">

<input type="hidden" name="quote_data" value="{{ quote_payload }}" placeholder="Not detected — please verify">

<div class="notice">
<strong>Review before sending.</strong>
Every field below can be corrected manually before the customer quote is generated.
</div>

{% if auto %}
<div class="card">

<div class="type">AUTO INSURANCE · REVIEW & EDIT</div>

<div class="edit-grid">

<div class="field wide">
<label>Client</label>
<input name="auto_client" value="{{ auto.client }}" placeholder="Not detected — please verify">
</div>

<div class="field">
<label>Effective Date</label>
<input name="auto_effective" value="{{ auto.effective }}" placeholder="Not detected — please verify">
</div>

{% if auto.vehicles %}
{% for veh in auto.vehicles %}
<div class="field wide">
<label>Vehicle {{ loop.index }}</label>
<input name="auto_vehicle_name_{{ loop.index0 }}" value="{{ veh.vehicle }}" placeholder="Not detected — please verify">
</div>
{% endfor %}
{% else %}
<div class="field wide">
<label>Vehicle</label>
<input name="auto_vehicle" value="{{ auto.vehicle }}" placeholder="Not detected — please verify">
</div>
{% endif %}

{% if auto.driver_count %}
<div class="field">
<label>Number of Drivers</label>
<input name="auto_driver_count" value="{{ auto.driver_count }}" placeholder="Not detected — please verify">
</div>
{% endif %}

{% if auto.drivers %}
{% for drv in auto.drivers %}
<div class="field">
<label>{% if drv.role == "Primary" %}Primary Driver{% else %}Occasional Driver{% endif %}</label>
<input name="auto_driver_name_{{ loop.index0 }}" value="{{ drv.name }}" placeholder="Not detected — please verify">
<input type="hidden" name="auto_driver_role_{{ loop.index0 }}" value="{{ drv.role }}" placeholder="Not detected — please verify">
</div>
{% endfor %}
{% else %}
<div class="field">
<label>Primary Driver</label>
<input name="auto_driver" value="{{ auto.driver }}" placeholder="Not detected — please verify">
</div>
{% endif %}

<div class="field">
<label>Insurance Company</label>
<input name="auto_carrier" value="{{ auto.carrier }}" placeholder="Not detected — please verify">
</div>

<div class="field">
<label>Product</label>
<input name="auto_product" value="{{ auto.product }}" placeholder="Not detected — please verify">
</div>

</div>

<div class="price-grid">

<div class="field">
<label>Annual Premium</label>
<input name="auto_annual" value="{{ money(auto.annual) }}" placeholder="Not detected — please verify">
</div>

<div class="field">
<label>Monthly Premium</label>
<input name="auto_monthly" value="{{ money(auto.monthly) }}" placeholder="Not detected — please verify">
</div>

</div>

{% if auto.vehicles and auto.vehicles|length > 1 %}
{% for veh in auto.vehicles %}
<div class="section-title">Vehicle {{ loop.index }} · {{ veh.vehicle }}{% if veh.annual %} · ${{ money(veh.annual) }}/yr{% endif %}</div>
<div class="coverages">
{% set vi = loop.index0 %}
{% for item in veh.coverages %}
<div class="coverage-edit coverage-edit-art">
<div class="review-art" aria-hidden="true">{{ coverage_art(item.name) }}</div>
<div class="review-copy">
<small>{{ item.name }}</small>
{% if item.name == "Mandatory Accident Benefits" %}
<div style="font-weight:950;font-size:16px;color:#17345c;margin-top:8px;">Included</div>
<div style="font-size:12px;line-height:1.45;color:#687487;margin-top:5px;">Medical, Rehabilitation &amp; Attendant Care</div>
{% else %}
<input name="auto_v{{ vi }}_cov_value_{{ loop.index0 }}" value="{{ item.value }}" placeholder="Not detected — please verify">
{% endif %}
</div>
</div>
{% endfor %}
</div>
{% if veh.optional %}
<div class="section-title">Vehicle {{ vi + 1 }} · Optional Accident Benefits</div>
<div style="font-size:12px;font-weight:800;color:#718094;margin:-4px 0 10px;">
ARS listed: {{ veh._broker_expected_optional_count if veh._broker_expected_optional_count is not none else veh.optional|length }}
&nbsp;·&nbsp;
Parsed: {{ veh.optional|length }}
</div>
<div class="coverages">
{% for item in veh.optional %}
<div class="coverage-edit coverage-edit-art">
<div class="review-art" aria-hidden="true">{{ coverage_art(item.name) }}</div>
<div class="review-copy">
<small>{{ item.name }}</small>
<label class="mini-label">Benefit / Limit</label>
<input name="auto_v{{ vi }}_opt_limit_{{ loop.index0 }}" value="{{ item.limit }}" placeholder="Not detected — please verify">
<label class="mini-label">Annual Premium</label>
<input name="auto_v{{ vi }}_opt_premium_{{ loop.index0 }}" value="{{ item.premium }}" placeholder="Not detected — please verify">
</div>
</div>
{% endfor %}
</div>
{% endif %}
{% endfor %}
{% else %}
<div class="section-title">Coverage</div>
<div class="coverages">
{% for item in auto.coverages %}
<div class="coverage-edit coverage-edit-art">
<div class="review-art" aria-hidden="true">{{ coverage_art(item.name) }}</div>
<div class="review-copy">
<small>{{ item.name }}</small>
{% if item.name == "Mandatory Accident Benefits" %}
<div style="font-weight:950;font-size:16px;color:#17345c;margin-top:8px;">Included</div>
<div style="font-size:12px;line-height:1.45;color:#687487;margin-top:5px;">Medical, Rehabilitation &amp; Attendant Care</div>
{% else %}
<input name="auto_cov_value_{{ loop.index0 }}" value="{{ item.value }}" placeholder="Not detected — please verify">
{% endif %}
</div>
</div>
{% endfor %}
</div>
{% endif %}

</div>
{% endif %}

{% if home %}
<div class="card">

<div class="type">HOME INSURANCE · REVIEW & EDIT</div>

<div class="edit-grid">

<div class="field wide">
<label>Client</label>
<input name="home_client" value="{{ home.client }}" placeholder="Not detected — please verify">
</div>

<div class="field">
<label>Effective Date</label>
<input name="home_effective" value="{{ home.effective }}" placeholder="Not detected — please verify">
</div>

<div class="field">
<label>Insurance Company</label>
<input name="home_carrier" value="{{ home.carrier }}" placeholder="Not detected — please verify">
</div>

<div class="field">
<label>Risk Type</label>
<input name="home_risk_type" value="{{ home.risk_type }}" placeholder="Not detected — please verify">
</div>

<div class="field wide">
<label>Property Address</label>
<input name="home_address" value="{{ home.address }}" placeholder="Not detected — please verify">
</div>

</div>

<div class="price-grid">

<div class="field">
<label>Annual Premium</label>
<input name="home_annual" value="{{ money(home.annual) }}" placeholder="Not detected — please verify">
</div>

<div class="field">
<label>Monthly Premium</label>
<input name="home_monthly" value="{{ money(home.monthly) }}" placeholder="Not detected — please verify">
</div>

</div>

<div class="section-title">Coverage</div>

<div class="coverages">
{% for item in home.coverages %}
<div class="coverage-edit coverage-edit-art">
<div class="review-art" aria-hidden="true">{{ coverage_art(item.name) }}</div>
<div class="review-copy">
<small>{{ item.name }}</small>
<span class="mini-label">Coverage / Deductible</span>
<input name="home_cov_value_{{ loop.index0 }}" value="{{ item.value }}" placeholder="Not detected — please verify">
<span class="mini-label">Premium</span>
<input name="home_cov_premium_{{ loop.index0 }}" value="{{ item.premium or '' }}" placeholder="Not detected — please verify">
</div>
</div>
{% endfor %}
</div>

{% if home.discounts %}
<div class="section-title">Discounts</div>
<div class="coverages">
{% for item in home.discounts %}
<div class="coverage-edit"><div class="review-copy"><small>Discount</small><input name="home_discount_{{ loop.index0 }}" value="{{ item }}" placeholder="Not detected — please verify"></div></div>
{% endfor %}
</div>
{% endif %}

</div>
{% endif %}

{% if auto and home %}
<div class="card combined">
<div class="type">AUTO + HOME</div>
<h2>Combined Total</h2>
<strong>${{ money(combined_monthly) }}/mo</strong>
<span>${{ money(combined_annual) }}/yr</span>
</div>
{% endif %}

<div class="card">
<button class="generate" type="submit">
Generate Customer Quote
</button>
</div>

</form>

<div class="card">
<h2>Message Preview</h2>
<textarea readonly>{{ whatsapp }}</textarea>
</div>

<div class="card">
<details>
<summary>OCR Debug</summary>
{% for result in raw_results %}
<h3>{{ result.filename }}</h3>
<div class="debug">{{ result.text }}</div>
{% endfor %}
</details>
</div>

{% endif %}

</div>

<script>
const input=document.getElementById("pdfs");
const zone=document.getElementById("dropZone");
const names=document.getElementById("fileNames");

function showFiles(files){
names.textContent=[...files].map(file=>file.name).join(" • ");
}

input.addEventListener("change",()=>showFiles(input.files));

["dragenter","dragover"].forEach(name=>{
zone.addEventListener(name,event=>{
event.preventDefault();
zone.classList.add("active");
});
});

["dragleave","drop"].forEach(name=>{
zone.addEventListener(name,event=>{
event.preventDefault();
zone.classList.remove("active");
});
});

zone.addEventListener("drop",event=>{
const dt=new DataTransfer();

[...event.dataTransfer.files]
.filter(file=>file.name.toLowerCase().endsWith(".pdf"))
.forEach(file=>dt.items.add(file));

input.files=dt.files;
showFiles(input.files);
});
</script>

</body>
</html>
"""




def first_name(full_name):
    text = re.sub(r"\\s+", " ", (full_name or "").strip())
    return text.split(" ")[0] if text else "there"


def _svg(body, viewbox="0 0 64 64", extra_class=""):
    return Markup(
        '<svg class="vector-art %s" viewBox="%s" aria-hidden="true" xmlns="http://www.w3.org/2000/svg">%s</svg>'
        % (extra_class, viewbox, body)
    )


def coverage_art(name):
    """Fictional navy/gold vector illustration for each coverage. No emoji."""
    key = (name or "").lower()

    person = (
        '<circle cx="21" cy="18" r="7" fill="currentColor"/>'
        '<path d="M10 47c1-11 6-17 11-17s10 6 11 17" fill="currentColor" opacity=".16" '
        'stroke="currentColor" stroke-width="3"/>'
    )
    shield = (
        '<path d="M42 13l12 5v10c0 10-5 17-12 22-7-5-12-12-12-22V18l12-5z" '
        'fill="none" stroke="currentColor" stroke-width="3" stroke-linejoin="round"/>'
    )
    car = (
        '<path d="M10 37l4-11c1-3 4-5 7-5h21c4 0 6 2 8 5l4 11" '
        'fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>'
        '<rect x="8" y="37" width="48" height="10" rx="3" fill="currentColor" opacity=".13" '
        'stroke="currentColor" stroke-width="3"/>'
        '<circle cx="18" cy="48" r="4" fill="currentColor"/>'
        '<circle cx="46" cy="48" r="4" fill="currentColor"/>'
    )
    house = (
        '<path d="M10 31L32 13l22 18" fill="none" stroke="currentColor" stroke-width="3" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M16 29v23h32V29" fill="currentColor" opacity=".10" stroke="currentColor" stroke-width="3"/>'
        '<path d="M28 52V38h8v14" fill="none" stroke="currentColor" stroke-width="3"/>'
    )

    if "bodily injury" in key:
        return _svg(person + shield + '<path d="M38 28h8M42 24v8" stroke="currentColor" stroke-width="3"/>')
    if "property damage" in key:
        return _svg(car + '<path d="M32 8v10M24 11l5 7M40 11l-5 7" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>')
    if "direct compensation" in key or "dcpd" in key:
        return _svg(car + '<path d="M37 16l5 5 10-11" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round"/>')
    if "accident benefits" in key or "supplementary medical" in key or "rehabilitation" in key:
        return _svg(shield + '<path d="M13 29h14M20 22v14" stroke="currentColor" stroke-width="4" stroke-linecap="round"/>')
    if "all perils" in key:
        return _svg(car + shield)
    if "uninsured automobile" in key:
        return _svg(car + '<circle cx="47" cy="15" r="9" fill="none" stroke="currentColor" stroke-width="3"/><path d="M47 10v7M47 21v1" stroke="currentColor" stroke-width="3"/>')
    if "loss of use" in key or "opcf 20" in key:
        return _svg(car + '<path d="M18 12h28M18 12l6-5M18 12l6 5M46 12l-6-5M46 12l-6 5" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "non-owned" in key or "opcf 27" in key:
        return _svg(car + '<circle cx="15" cy="14" r="6" fill="none" stroke="currentColor" stroke-width="3"/><path d="M20 18l9 9M27 25l4-4" stroke="currentColor" stroke-width="3"/>')
    if "family protection" in key or "opcf 44" in key:
        return _svg(person + '<circle cx="42" cy="22" r="5" fill="currentColor"/><path d="M35 47c1-8 4-13 8-13s7 5 8 13" fill="none" stroke="currentColor" stroke-width="3"/>' + shield)
    if "minor conviction" in key:
        return _svg('<path d="M15 18l16 16M25 8l16 16M12 21L28 5M28 37l16-16" stroke="currentColor" stroke-width="5" stroke-linecap="round"/><path d="M38 46l5 5 10-12" fill="none" stroke="currentColor" stroke-width="4"/>')
    if "income replacement" in key:
        return _svg('<rect x="10" y="20" width="44" height="30" rx="6" fill="currentColor" opacity=".10" stroke="currentColor" stroke-width="3"/><path d="M18 36h15M38 39l6-7 6 7M44 32v14" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "caregiver" in key:
        return _svg(person + '<path d="M42 23c5-7 14-3 14 4 0 7-7 11-14 16-7-5-14-9-14-16 0-7 9-11 14-4z" fill="currentColor" opacity=".14" stroke="currentColor" stroke-width="2.5"/>')
    if "dependant care" in key:
        return _svg('<circle cx="22" cy="19" r="7" fill="currentColor"/><circle cx="43" cy="23" r="5" fill="currentColor"/><path d="M10 50c1-12 6-19 12-19s11 7 12 19M35 50c1-9 4-14 8-14s8 5 9 14" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "non-earner" in key:
        return _svg(person + '<circle cx="45" cy="36" r="11" fill="none" stroke="currentColor" stroke-width="3"/><path d="M45 29v14M41 32c2-3 8-2 8 2 0 4-8 2-8 6 0 4 6 4 9 1" fill="none" stroke="currentColor" stroke-width="2"/>')
    if "lost education" in key:
        return _svg('<path d="M8 25l24-12 24 12-24 12L8 25z" fill="currentColor" opacity=".12" stroke="currentColor" stroke-width="3"/><path d="M17 31v13c8 7 22 7 30 0V31M54 26v16" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "expenses of visitors" in key:
        return _svg('<rect x="11" y="25" width="24" height="27" rx="4" fill="currentColor" opacity=".10" stroke="currentColor" stroke-width="3"/><path d="M17 25v-6c0-4 3-7 7-7s7 3 7 7v6M44 15v34M38 21h12" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "housekeeping" in key:
        return _svg(house + '<path d="M44 15l8 8M48 11l8 8M48 11l-4 4M56 19l-4 4" stroke="currentColor" stroke-width="3"/>')
    if "damage to personal" in key:
        return _svg('<rect x="10" y="18" width="26" height="34" rx="5" fill="currentColor" opacity=".10" stroke="currentColor" stroke-width="3"/><circle cx="23" cy="45" r="2" fill="currentColor"/><path d="M41 22c3-5 11-5 14 0M40 32h16M44 32v9M52 32v9" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "death" in key:
        return _svg('<path d="M32 50S10 38 10 23c0-11 14-15 22-5 8-10 22-6 22 5 0 15-22 27-22 27z" fill="currentColor" opacity=".10" stroke="currentColor" stroke-width="3"/><path d="M32 18v20M22 28h20" stroke="currentColor" stroke-width="3"/>')
    if "funeral" in key:
        return _svg('<path d="M32 51V24M32 32c-10-2-14-9-14-15 9 0 14 6 14 15zM32 35c10-2 14-9 14-15-9 0-14 6-14 15z" fill="currentColor" opacity=".13" stroke="currentColor" stroke-width="3"/><path d="M22 52h20" stroke="currentColor" stroke-width="3"/>')
    if "indexation" in key:
        return _svg('<path d="M11 49V18M11 49h44" stroke="currentColor" stroke-width="3"/><path d="M17 42l10-10 8 5 15-18" fill="none" stroke="currentColor" stroke-width="4"/><path d="M43 19h7v7" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "contents" in key:
        return _svg('<path d="M12 34h40v18H12z" fill="currentColor" opacity=".10" stroke="currentColor" stroke-width="3"/><path d="M17 34v-8c0-5 4-9 9-9h12c5 0 9 4 9 9v8M20 42h24" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "additional living" in key:
        return _svg(house + '<path d="M42 12h12v12M54 12L40 26" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "voluntary medical" in key:
        return _svg('<rect x="11" y="18" width="42" height="34" rx="6" fill="currentColor" opacity=".10" stroke="currentColor" stroke-width="3"/><path d="M32 25v20M22 35h20" stroke="currentColor" stroke-width="4"/>')
    if "voluntary property" in key:
        return _svg(house + '<path d="M39 39l5 5 9-11" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "legal liability" in key:
        return _svg('<path d="M32 12v40M15 19h34M19 19l-9 15h18L19 19zM45 19l-9 15h18L45 19zM21 52h22" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "deductible" in key:
        return _svg(shield + '<circle cx="18" cy="35" r="12" fill="currentColor" opacity=".10" stroke="currentColor" stroke-width="3"/><path d="M18 28v14M14 31c2-3 8-2 8 2 0 4-8 2-8 6 0 4 6 4 9 1" fill="none" stroke="currentColor" stroke-width="2"/>')
    if "identity theft" in key:
        return _svg('<rect x="9" y="16" width="33" height="28" rx="5" fill="currentColor" opacity=".10" stroke="currentColor" stroke-width="3"/><circle cx="19" cy="26" r="5" fill="currentColor"/><path d="M13 38c1-6 3-9 6-9s5 3 6 9M48 31v-5c0-5 8-5 8 0v5M45 31h14v16H45z" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "legal helpline" in key:
        return _svg('<path d="M17 38v-8c0-9 7-16 15-16s15 7 15 16v8" fill="none" stroke="currentColor" stroke-width="3"/><rect x="10" y="34" width="9" height="14" rx="4" fill="currentColor" opacity=".13" stroke="currentColor" stroke-width="3"/><rect x="45" y="34" width="9" height="14" rx="4" fill="currentColor" opacity=".13" stroke="currentColor" stroke-width="3"/>')

    if key == "residence":
        return _svg(house + '<path d="M21 34h7M39 34h7" stroke="currentColor" stroke-width="3"/>')
    if "outbuilding" in key:
        return _svg('<path d="M7 34L22 22l15 12v18H7zM33 31l10-8 14 11v18H33z" fill="none" stroke="currentColor" stroke-width="3" stroke-linejoin="round"/>')
    if key == "hail":
        return _svg('<path d="M17 35h30c6 0 10-4 10-9s-4-9-10-9c-2-6-7-9-13-9-8 0-14 6-14 14-5 0-9 3-9 7s3 6 6 6z" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="20" cy="47" r="3" fill="currentColor"/><circle cx="32" cy="51" r="3" fill="currentColor"/><circle cx="44" cy="47" r="3" fill="currentColor"/>')
    if key == "wind":
        return _svg('<path d="M10 20h31c8 0 8-10 1-10-4 0-6 2-7 5M10 31h41c8 0 8 11 0 11-4 0-7-2-8-5M10 43h22" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>')
    if "single limit" in key:
        return _svg('<path d="M15 17h34M15 32h34M15 47h34" stroke="currentColor" stroke-width="4" stroke-linecap="round"/><circle cx="10" cy="17" r="3" fill="currentColor"/><circle cx="10" cy="32" r="3" fill="currentColor"/><circle cx="10" cy="47" r="3" fill="currentColor"/>')
    if "guaranteed building replacement" in key:
        return _svg(house + '<path d="M47 12a18 18 0 0 1 9 16M56 28l-6-5M56 28l-5 5" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>')
    if "swimming pool" in key:
        return _svg('<path d="M12 43h40M15 35c4-4 8-4 12 0s8 4 12 0 8-4 12 0M19 30V15M19 20h10v10M29 15v15" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "sewer backup" in key:
        return _svg('<path d="M14 49h36V20H14zM22 16v16h20V16M32 45V28M26 34l6-6 6 6" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "ground water" in key:
        return _svg(house + '<path d="M9 54h46M13 48c4-4 8-4 12 0s8 4 12 0 8-4 12 0" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "overland water" in key:
        return _svg(house + '<path d="M10 44c5-5 10-5 15 0s10 5 15 0 10-5 15 0M10 52c5-5 10-5 15 0s10 5 15 0 10-5 15 0" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "above ground water" in key:
        return _svg('<path d="M17 34h30c6 0 10-4 10-9s-4-9-10-9c-2-6-7-9-13-9-8 0-14 6-14 14-5 0-9 3-9 7s3 6 6 6z" fill="none" stroke="currentColor" stroke-width="3"/><path d="M21 42l-3 9M32 42l-3 9M43 42l-3 9" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>')
    if "claim free protection" in key:
        return _svg(shield + '<path d="M36 31l5 5 9-11" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "home warranty" in key:
        return _svg(house + '<path d="M45 16c4 0 7 3 7 7 0 2-1 4-2 5l7 7-6 6-7-7c-1 1-3 2-5 2-4 0-7-3-7-7l5 5 5-5-5-5c2-2 5-3 8-2z" fill="none" stroke="currentColor" stroke-width="3" stroke-linejoin="round"/>')
    if "service line" in key:
        return _svg(house + '<path d="M8 51h18l5-8 5 8h20" fill="none" stroke="currentColor" stroke-width="3"/>')
    if "by-law" in key or "bylaw" in key:
        return _svg('<rect x="17" y="11" width="30" height="42" rx="2" fill="none" stroke="currentColor" stroke-width="3"/><path d="M23 22h18M23 31h18M23 40h11" stroke="currentColor" stroke-width="3"/>')

    return _svg(shield + '<path d="M37 32l4 4 8-9" fill="none" stroke="currentColor" stroke-width="3"/>')




def hero_art(kind):
    """Clean fictional editorial illustrations. No manufacturer logos or badges."""
    if kind == "auto":
        return Markup(
            '<svg class="hero-scene" viewBox="0 0 900 390" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
            '<defs>'
            '<linearGradient id="bgA" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#f4f7f9"/><stop offset="1" stop-color="#d9e3ea"/></linearGradient>'
            '<linearGradient id="bodyA" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#1c3d59"/><stop offset=".55" stop-color="#071d34"/><stop offset="1" stop-color="#35566f"/></linearGradient>'
            '</defs>'
            '<rect width="900" height="390" fill="url(#bgA)"/>'
            '<g opacity=".58" fill="#8196a7">'
            '<rect x="34" y="118" width="42" height="120"/><rect x="88" y="86" width="54" height="152"/><rect x="154" y="128" width="35" height="110"/>'
            '<rect x="202" y="104" width="58" height="134"/><rect x="273" y="72" width="36" height="166"/><rect x="324" y="117" width="45" height="121"/>'
            '<rect x="705" y="105" width="48" height="133"/><rect x="765" y="78" width="42" height="160"/><rect x="819" y="122" width="39" height="116"/>'
            '</g>'
            '<path d="M616 41v188M602 108h28M610 67h12" stroke="#35566f" stroke-width="6"/><path d="M616 18l9 23h-18z" fill="#c99a36"/>'
            '<path d="M0 245h900v145H0z" fill="#102b45"/><path d="M0 310C250 289 540 300 900 265" stroke="#c99a36" stroke-width="5"/>'
            '<path d="M0 337C260 315 555 326 900 290" stroke="#edf2f5" stroke-width="4" stroke-dasharray="34 28" opacity=".7"/>'
            '<g transform="translate(122 122)">'
            '<ellipse cx="326" cy="184" rx="300" ry="26" fill="#000" opacity=".18"/>'
            '<path d="M47 144l38-78c10-22 34-36 61-36h211c28 0 53 12 70 35l50 64 74 18c21 5 35 23 35 45v21H14v-28c0-21 15-37 37-41z" fill="url(#bodyA)" stroke="#c99a36" stroke-width="4"/>'
            '<path d="M151 47h194c23 0 44 10 58 29l39 50H100l32-64c4-9 10-15 19-15z" fill="#a9bbc8" opacity=".55"/>'
            '<path d="M100 130h347" stroke="#607e94" stroke-width="3" opacity=".7"/>'
            '<path d="M50 150h80l-21 25H43zM465 147h71l24 26h-83z" fill="#f3fbff"/>'
            '<rect x="244" y="164" width="122" height="34" rx="9" fill="#06131e" stroke="#5d7385" stroke-width="2"/>'
            '<rect x="276" y="172" width="58" height="18" rx="3" fill="#fafafa"/><text x="305" y="185" text-anchor="middle" font-size="9" font-weight="800" fill="#071d34">WAQI INSURES</text>'
            '<circle cx="147" cy="210" r="44" fill="#08131d" stroke="#d2a23e" stroke-width="4"/><circle cx="147" cy="210" r="21" fill="#768b9b"/>'
            '<circle cx="456" cy="210" r="44" fill="#08131d" stroke="#d2a23e" stroke-width="4"/><circle cx="456" cy="210" r="21" fill="#768b9b"/>'
            '</g>'
            '</svg>'
        )

    return Markup(
        '<svg class="hero-scene" viewBox="0 0 900 390" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
        '<defs>'
        '<linearGradient id="bgH" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#f6f5f1"/><stop offset="1" stop-color="#dce5e9"/></linearGradient>'
        '<linearGradient id="glass" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#183a59"/><stop offset="1" stop-color="#071d34"/></linearGradient>'
        '</defs>'
        '<rect width="900" height="390" fill="url(#bgH)"/>'
        '<g opacity=".42" fill="#8196a7"><rect x="34" y="126" width="45" height="116"/><rect x="92" y="94" width="50" height="148"/><rect x="158" y="138" width="35" height="104"/><rect x="726" y="91" width="54" height="151"/><rect x="796" y="126" width="43" height="116"/></g>'
        '<path d="M691 47v189M676 111h30M685 72h12" stroke="#35566f" stroke-width="6"/><path d="M691 23l9 24h-18z" fill="#c99a36"/>'
        '<path d="M0 282h900v108H0z" fill="#d8dede"/>'
        '<g transform="translate(104 54)">'
        '<path d="M25 132L342 7l317 125v176H25z" fill="#e6e1d8" stroke="#071d34" stroke-width="4"/>'
        '<path d="M116 124L342 34l226 90" fill="none" stroke="#c99a36" stroke-width="7"/>'
        '<rect x="84" y="135" width="222" height="145" fill="#d2cec7"/><rect x="323" y="111" width="266" height="169" fill="#bdbab4"/>'
        '<rect x="127" y="159" width="142" height="84" fill="url(#glass)"/><rect x="360" y="142" width="181" height="86" fill="url(#glass)"/>'
        '<g fill="#f0c36b"><rect x="139" y="170" width="54" height="61"/><rect x="205" y="170" width="52" height="61"/><rect x="374" y="154" width="70" height="62"/><rect x="458" y="154" width="70" height="62"/></g>'
        '<rect x="226" y="230" width="81" height="50" fill="#071d34"/><rect x="439" y="220" width="110" height="60" fill="#1d354b"/>'
        '<path d="M5 280h676" stroke="#8d9aa4" stroke-width="4"/>'
        '<g fill="#627b62"><circle cx="64" cy="263" r="29"/><circle cx="101" cy="269" r="21"/><circle cx="594" cy="262" r="30"/><circle cx="635" cy="268" r="21"/></g>'
        '</g>'
        '</svg>'
    )


def mascot_art():
    return _svg(
        '<path d="M31 12l8-7 7 8 9-5-1 11 8 4-8 7 4 10-12-1-6 9-7-9-11 1 4-11-8-6 9-5z" fill="#071d34"/>'
        '<circle cx="38" cy="31" r="15" fill="#d8aa7a"/>'
        '<path d="M26 29h10M41 29h10" stroke="#071d34" stroke-width="5" stroke-linecap="round"/>'
        '<path d="M18 78c3-20 10-31 20-31s18 11 21 31" fill="#071d34"/>'
        '<path d="M29 49l9 13 9-13" fill="#f8fafc"/>'
        '<path d="M38 62v15" stroke="#c99a36" stroke-width="4"/>'
        '<path d="M14 70l16-9M62 70l-16-9" stroke="#071d34" stroke-width="7" stroke-linecap="round"/>',
        "0 0 76 82",
        "mascot-vector"
    )





OFFICIAL_CARRIER_LOGOS = {
    "Wawanesa": "https://www.wawanesa.com/resources/img/Social_Media_Wawanesa_1024x512.jpg",
}

CARRIER_DOMAINS = {
    "Wawanesa": "wawanesa.com",
    "Intact Insurance": "intact.ca",
    "Intact": "intact.ca",
    "Aviva": "aviva.ca",
    "Aviva Insurance": "aviva.ca",
    "CAA Insurance": "caainsurancecompany.ca",
    "CAA": "caainsurancecompany.ca",
    "Coachman": "coachmaninsurance.ca",
    "Unica": "unicainsurance.com",
    "Pembridge": "pembridge.com",
    "Pafco": "pafco.ca",
    "Economical": "economical.com",
    "Economical Insurance": "economical.com",
    "Definity": "definityfinancial.com",
    "Gore Mutual": "goremutual.ca",
    "Echelon": "echeloninsurance.ca",
    "Facility Association": "facilityassociation.com",
    "JEVCO": "jevco.ca",
    "Northbridge Insurance": "northbridgeinsurance.ca",
    "SGI CANADA": "sgicanada.ca",
    "Travelers Canada": "travelerscanada.ca",
    "Dominion of Canada": "travelerscanada.ca",
    "Nordic": "intact.ca",
    "Aviva Traders": "aviva.ca",
}

CARRIER_FILE_SLUGS = {
    "Wawanesa": "wawanesa",
    "Intact Insurance": "intact",
    "Intact": "intact",
    "Aviva": "aviva",
    "Aviva Insurance": "aviva",
    "CAA Insurance": "caa",
    "CAA": "caa",
    "Coachman": "coachman",
    "Unica": "unica",
    "Pembridge": "pembridge",
    "Pafco": "pafco",
    "Economical": "economical",
    "Economical Insurance": "economical",
    "Definity": "definity",
    "Gore Mutual": "gore-mutual",
    "Echelon": "echelon",
    "Facility Association": "facility-association",
    "JEVCO": "jevco",
    "Northbridge Insurance": "northbridge",
    "SGI CANADA": "sgi-canada",
    "Travelers Canada": "travelers",
    "Dominion of Canada": "travelers",
    "Nordic": "nordic",
    "Aviva Traders": "aviva",
}


def normalize_carrier_name(carrier):
    return re.sub(r"\s+", " ", (carrier or "").strip())


def carrier_logo_data(carrier):
    """Automatic logo loading for all supported ARS carriers."""
    carrier = normalize_carrier_name(carrier)
    slug = CARRIER_FILE_SLUGS.get(carrier)
    domain = CARRIER_DOMAINS.get(carrier)

    candidates = []

    # 1) Local original logo, if present.
    if slug:
        for ext in ("svg", "png", "webp", "jpg", "jpeg"):
            local_path = os.path.join(app.static_folder, "carriers", f"{slug}.{ext}")
            if os.path.exists(local_path):
                candidates.append(url_for("static", filename=f"carriers/{slug}.{ext}"))
                break

    # 2) Official public asset where available.
    official = OFFICIAL_CARRIER_LOGOS.get(carrier)
    if official:
        candidates.append(official)

    # 3/4) Automatic domain-based fallbacks.
    if domain:
        candidates.append(f"https://logo.clearbit.com/{domain}?size=300")
        candidates.append(f"https://www.google.com/s2/favicons?domain={domain}&sz=256")

    unique = []
    for item in candidates:
        if item and item not in unique:
            unique.append(item)

    return {
        "name": carrier,
        "primary": unique[0] if unique else "",
        "fallbacks": unique[1:],
    }



def mascot_exists():
    return os.path.exists(
        os.path.join(app.static_folder, "waqi-mascot.png")
    )


# =========================================================
# CUSTOMER PAGE
# =========================================================

CUSTOMER_HTML = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Your Insurance Quote</title>
<style>
:root{--navy:#071d34;--navy2:#123b60;--gold:#c99a36;--ink:#0b223b;--muted:#718094;--line:#e3e8ed;--paper:#f3f5f7}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}
.page{width:min(1180px,95%);margin:auto;padding:18px 0 36px}
.top{display:flex;justify-content:space-between;align-items:center;padding:4px 3px 14px;gap:18px}
.logo{width:170px;max-height:82px;object-fit:contain}
.license{font-size:11px;font-weight:800;color:#7d8996}
.hero,.policy,.coverage-panel,.cta{background:white;border:1px solid rgba(7,29,52,.06);box-shadow:0 9px 28px rgba(7,29,52,.06)}
.hero{border-radius:23px;padding:25px 28px;display:grid;grid-template-columns:1fr auto;gap:22px;align-items:center;margin-bottom:14px}
.eyebrow{font-size:10px;letter-spacing:.15em;font-weight:950;color:#a87921}
.hero h1{font-size:clamp(30px,4.6vw,47px);line-height:1.02;letter-spacing:-.045em;margin:5px 0 8px}
.hero h1 em{font-style:normal;color:var(--gold)}
.hero p{margin:0;color:var(--muted);font-size:14px}
.hero-price{background:var(--navy);color:white;border-radius:17px;padding:17px 21px;min-width:250px}
.hero-price small{display:block;color:#aebccc;font-size:9px;font-weight:900;text-transform:uppercase;letter-spacing:.08em}
.hero-price strong{display:block;font-size:32px;margin-top:5px;letter-spacing:-.035em}
.hero-price span{display:block;color:#d9e1e8;margin-top:3px;font-size:12px;font-weight:700}
.policy-grid{display:grid;grid-template-columns:1fr;gap:18px}
.policy{border-radius:21px;overflow:hidden}
.policy-main{min-height:330px;display:grid;grid-template-columns:.78fr 1.22fr;background:linear-gradient(135deg,#fff 0%,#fff 39%,#eef3f7 39%)}
.policy-copy{padding:23px}
.policy-type{font-size:10px;font-weight:950;letter-spacing:.13em;color:#a87921}
.policy h2{font-size:23px;margin:6px 0 0;line-height:1.15}
.carrier{font-size:12px;color:var(--muted);font-weight:700;margin-top:5px}
.policy-price{margin-top:22px}
.policy-price strong{font-size:28px}
.policy-price small{font-size:11px;color:var(--muted);font-weight:800}
.policy-art{display:flex;align-items:center;justify-content:center;padding:18px;color:#d6a643;background:linear-gradient(145deg,var(--navy),#164263)}
.hero-vector{width:92%;height:auto;filter:drop-shadow(0 10px 10px rgba(0,0,0,.20))}
.quick{display:grid;grid-template-columns:repeat(3,1fr);border-top:1px solid var(--line)}
.quick div{padding:11px 14px;border-right:1px solid var(--line)}
.quick div:last-child{border-right:0}
.quick small{display:block;font-size:8px;color:#929eab;font-weight:950;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px}
.quick strong{font-size:11px}
.combined{margin:14px 0;background:var(--navy);color:white;border-radius:15px;padding:13px 18px;display:flex;justify-content:space-between;align-items:center;gap:18px}
.combined .label{font-size:10px;font-weight:950;color:#dfb552;text-transform:uppercase;letter-spacing:.08em}
.combined .numbers{display:flex;gap:28px}.combined strong{font-size:21px}
.coverage-columns{display:grid;grid-template-columns:1fr;gap:18px}
.coverage-panel{border-radius:21px;padding:19px}
.panel-title{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:13px}
.panel-title h3{margin:0;font-size:15px}.panel-title span{font-size:9px;color:var(--muted);font-weight:700}
.tile-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.tile{min-height:168px;border:1px solid var(--line);border-radius:16px;padding:16px 13px;display:flex;flex-direction:column;align-items:center;text-align:center;background:linear-gradient(180deg,#fff,#fafbfc)}
.tile-art{width:78px;height:78px;display:flex;align-items:center;justify-content:center;color:var(--navy);margin-bottom:10px}
.tile-art .vector-art{width:72px;height:72px}
.tile strong{font-size:13px;line-height:1.2}.tile b{font-size:12px;color:#a87921;margin-top:6px}.tile p{font-size:10px;color:#8793a1;line-height:1.35;margin:6px 0 0}
.coverage-subtitle{font-size:12px;font-weight:950;letter-spacing:.05em;text-transform:uppercase;color:#718094;margin:4px 0 10px}
.mandatory-ab{margin-top:14px;padding:15px;border:1px solid #d9e4ee;border-radius:16px;background:#f7fafc}
.mandatory-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:11px}
.mandatory-head strong{font-size:17px;color:var(--navy)}
.mandatory-head span{font-size:11px;color:#718094;font-weight:800}
.mandatory-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.mandatory-card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:13px;min-height:78px}
.mandatory-card strong{display:block;font-size:13px;line-height:1.25}
.mandatory-card small{display:block;font-size:11px;color:#718094;margin-top:5px;font-weight:700}
.home-premium{
margin-top:6px;padding:5px 8px;border-radius:8px;
background:#fff8e8;border:1px solid #ead6a7;
font-size:10px;font-weight:800;color:#6f531c
}
.home-premium strong{font-size:10px;color:#071d34}
.discount-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.discount-card{
display:flex;
align-items:center;
min-height:46px;
padding:10px 13px;
border:1px solid #e8c77f;
border-radius:11px;
background:#fffaf0;
font-size:12px;
font-weight:900;
line-height:1.15;
color:var(--navy);
white-space:nowrap;
overflow:hidden;
text-overflow:ellipsis;
}
.optional{margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}
.optional-head{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:8px}
.optional-head strong{font-size:17px}.optional-head span{font-size:14px;color:#a87921;font-weight:950}
.optional-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
.optional-card{display:grid;grid-template-columns:92px 1fr;align-items:center;gap:16px;border:1px solid #e6d3a6;background:#fffaf0;border-radius:16px;padding:17px;min-height:142px}
.optional-card .tile-art{width:88px;height:88px;margin:0;color:#93661c}.optional-card .vector-art{width:82px;height:82px}
.optional-card strong{display:block;font-size:13px;line-height:1.22}.optional-card small,.optional-card b{display:block;font-size:12px;margin-top:6px}.optional-card small{color:#786f61}.optional-card b{color:#a87921;font-weight:950}
.cta{margin-top:18px;border-radius:21px;padding:22px;display:grid;grid-template-columns:180px 1fr auto;align-items:center;gap:22px}
.mascot{height:180px;position:relative;display:flex;justify-content:center;align-items:flex-end;background:linear-gradient(145deg,#f5efe3,#fff);border-radius:18px;color:#c99a36}
.mascot-vector{width:155px;height:174px}
.mascot-logo{position:absolute;width:27px;height:19px;object-fit:contain;left:84px;top:72px}
.cta h3{margin:0 0 5px;font-size:20px}.cta p{margin:0;color:var(--muted);font-size:12px;line-height:1.45}
.cta-actions{display:flex;flex-direction:column;gap:8px;min-width:185px}
.btn{display:block;text-align:center;text-decoration:none;border-radius:9px;padding:11px 13px;font-size:11px;font-weight:950}.wa{background:#1fa855;color:white}.mail{background:var(--navy);color:white}
.footer{text-align:center;color:#7e8995;font-size:10px;font-weight:700;padding:19px 8px 0}
.vector-art{display:block;overflow:visible}.hero-scene{width:100%;height:100%;display:block}.carrier-brand{display:flex;align-items:center;gap:10px;margin-top:9px;min-height:36px}.carrier-brand img{max-width:165px;max-height:36px;object-fit:contain;object-position:left center}.carrier-brand .carrier-fallback{font-size:12px;color:var(--muted);font-weight:800}

.photo-art{
padding:0 !important;
background:#e8edf1 !important;
overflow:hidden;
min-height:330px;
}
.photo-art img{image-rendering:auto;
display:block;
width:100%;
height:100%;
min-height:330px;
object-fit:contain;
object-position:center;
}
.carrier-brand{
display:flex;
align-items:center;
min-height:54px;
margin-top:10px;
}
.carrier-brand img{
display:block;
max-width:190px;
max-height:50px;
width:auto;
height:auto;
object-fit:contain;
object-position:left center;
border-radius:5px;
}
.carrier-fallback{
font-size:16px;
font-weight:900;
color:#071d34;
}
.product-name{
font-size:15px !important;
margin-top:3px !important;
}
.quick div{
padding:18px 20px !important;
}
.quick small{
font-size:11px !important;
letter-spacing:.08em !important;
margin-bottom:7px !important;
}
.quick strong{
font-size:16px !important;
line-height:1.35 !important;
}
.panel-title h3{
font-size:20px !important;
}
.panel-title span{
font-size:12px !important;
}
.tile strong{
font-size:14px !important;
}
.tile b{
font-size:14px !important;
}
.tile p{
font-size:11px !important;
}
.optional-head strong{
font-size:20px !important;
}
.optional-head span{
font-size:15px !important;
}
.optional-card strong{
font-size:15px !important;
line-height:1.25 !important;
}
.optional-card small,
.optional-card b{
font-size:14px !important;
line-height:1.35 !important;
}
.policy-type{
font-size:12px !important;
}
.policy-copy h2{
font-size:27px !important;
}
.carrier{
font-size:15px !important;
}
.policy-price strong{
font-size:34px !important;
}
.policy-price small{
font-size:14px !important;
}


.approved-mascot-img{
width:100%;
height:100%;
object-fit:cover;
object-position:center top;
display:block;
border-radius:16px;
}


.carrier-name{
display:block;
font-size:15px;
font-weight:900;
color:#071d34;
line-height:1.2;
margin-left:10px;
}
@media(max-width:700px){
.carrier-brand{align-items:flex-start;flex-direction:column;gap:6px}
.carrier-name{margin-left:0}
}

@media(max-width:900px){.policy-grid,.coverage-columns{grid-template-columns:1fr}.tile-grid,.optional-grid{grid-template-columns:repeat(2,1fr)}.hero{grid-template-columns:1fr}.hero-price{min-width:0}.cta{grid-template-columns:110px 1fr}.cta-actions{grid-column:1/-1;flex-direction:row}.btn{flex:1}}
@media(max-width:620px){
.page{width:100%;padding:8px 10px 24px;overflow-x:hidden}
.top{padding:2px 4px 9px}.logo{width:122px}.license{font-size:8px}
.hero{padding:16px;border-radius:17px;gap:13px}.hero h1{font-size:30px}.hero p{font-size:12px}
.hero-price{padding:13px 15px;border-radius:13px}.hero-price strong{font-size:27px}
.policy,.coverage-panel,.cta{border-radius:16px}
.policy-main{grid-template-columns:1fr;min-height:0;background:#fff}.policy-copy{padding:17px}
.policy-copy h2{font-size:21px !important}.policy-price{margin-top:13px}.policy-price strong{font-size:28px !important}
.policy-art,.photo-art,.photo-art img{min-height:145px;height:145px}.photo-art img{object-fit:cover}
.quick{grid-template-columns:1fr}.quick div{border-right:0;border-bottom:1px solid var(--line);padding:11px 14px !important}.quick div:last-child{border-bottom:0}
.quick small{font-size:9px !important;margin-bottom:3px !important}.quick strong{font-size:13px !important}
.combined{align-items:flex-start;flex-direction:column}.combined .numbers{gap:12px;flex-wrap:wrap}
.coverage-panel{padding:13px}.panel-title{align-items:flex-start;margin-bottom:10px}.panel-title h3{font-size:17px !important}.panel-title span{font-size:10px !important;text-align:right}
.coverage-subtitle{font-size:10px;margin:2px 0 8px}
.tile-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}
.tile{min-height:0;padding:10px 7px;border-radius:11px}
.tile-art{width:44px;height:44px;margin-bottom:5px}.tile-art .vector-art{width:42px;height:42px}
.tile strong{font-size:11px !important}.tile b{font-size:11px !important;margin-top:4px}.tile p{font-size:9px !important;margin-top:4px;line-height:1.25}
.mandatory-ab{margin-top:10px;padding:11px;border-radius:12px}.mandatory-head{margin-bottom:8px}.mandatory-head strong{font-size:14px}.mandatory-head span{font-size:9px;text-align:right}
.mandatory-grid{grid-template-columns:1fr;gap:6px}.mandatory-card{min-height:0;padding:9px 10px;border-radius:9px}.mandatory-card strong{font-size:11px}.mandatory-card small{font-size:9px;margin-top:2px}
.optional{margin-top:10px;padding-top:10px}.optional-head strong{font-size:15px !important}.optional-head span{font-size:11px !important}
.optional-grid{grid-template-columns:1fr;gap:7px}
.discount-grid{grid-template-columns:1fr;gap:6px}
.discount-card{
min-height:38px;
padding:8px 10px;
font-size:11px;
line-height:1;
white-space:nowrap;
overflow:visible;
text-overflow:clip;
}.optional-card{grid-template-columns:48px minmax(0,1fr);gap:9px;min-height:0;padding:9px;border-radius:11px}
.optional-card .tile-art{width:46px;height:46px}.optional-card .vector-art{width:44px;height:44px}.optional-card strong{font-size:11px !important}.optional-card small,.optional-card b{font-size:10px !important;margin-top:3px}
.cta{grid-template-columns:1fr;text-align:center;padding:15px}.mascot{width:125px;height:125px;margin:auto}.cta-actions{flex-direction:column}
.carrier-brand img{max-width:145px;max-height:38px}.carrier-name{font-size:13px}
}
</style>
</head>
<body>
<div class="page">
<div class="top">
<img src="{{ url_for('static', filename='waqi-logo.png') }}" class="logo" alt="Waqi Insures">
<div class="license">RIBO Licensed & Registered</div>
</div>

<div class="hero">
<div>
<div class="eyebrow">YOUR PERSONALIZED QUOTE</div>
<h1>Hi <em>{{ client_first }}</em>,<br>here’s your insurance quote.</h1>
<p>Price first. Clear coverage underneath. No insurance paperwork overload.</p>
</div>
<div class="hero-price">
<small>{% if auto and home %}Auto + Home total{% elif auto %}Auto Insurance{% else %}Home Insurance{% endif %}</small>
{% if auto and home %}
<strong>${{ money(combined_monthly) }}/mo</strong><span>${{ money(combined_annual) }}/year</span>
{% elif auto %}
<strong>${{ money(auto.monthly) }}/mo</strong><span>${{ money(auto.annual) }}/year</span>
{% else %}
<strong>${{ money(home.monthly) }}/mo</strong><span>${{ money(home.annual) }}/year</span>
{% endif %}
</div>
</div>

<div class="policy-grid">
{% if auto %}
<div class="policy">
<div class="policy-main">
<div class="policy-copy">
<div class="policy-type">AUTO INSURANCE</div>
<h2>{% if auto.vehicle_count and auto.vehicle_count > 1 %}{{ auto.vehicle_count }} Vehicles{% else %}{{ auto.vehicle or "Your Vehicle" }}{% endif %}</h2>
<div class="carrier-brand">
{% set brand = carrier_logo_data(auto.carrier) %}
{% if brand.primary %}
<img
src="{{ brand.primary }}"
data-fallbacks='{{ brand.fallbacks|tojson }}'
data-index="0"
alt="{{ auto.carrier }}"
style="display:none"
onload="this.style.display='block'"
onerror="
const arr=JSON.parse(this.dataset.fallbacks || '[]');
const i=parseInt(this.dataset.index || '0');
if(i < arr.length){
this.dataset.index=String(i+1);
this.src=arr[i];
}else{
this.style.display='none';
}
"
>
{% endif %}
<span class="carrier-name">{{ auto.carrier }}</span>
</div>

<div class="policy-price"><strong>${{ money(auto.monthly) }}/mo</strong><br><small>${{ money(auto.annual) }}/year</small></div>
</div>
<div class="policy-art photo-art"><img src="{{ url_for('static', filename='auto-hero-final.png') }}" alt="Illustrated sport wagon"></div>
</div>
<div class="quick">
<div>
<small>Drivers</small>
<strong>
{% if auto.driver_count %}{{ auto.driver_count }} Drivers{% endif %}
{% if auto.drivers %}
{% if auto.driver_count %}<br>{% endif %}
{% for drv in auto.drivers %}
{{ drv.name }}{% if drv.role and drv.role != "Listed Driver" %} <span style="font-weight:700;color:#748194;">({{ drv.role }})</span>{% endif %}{% if not loop.last %}<br>{% endif %}
{% endfor %}
{% elif not auto.driver_count %}
{{ auto.driver or auto.client }}
{% endif %}
</strong>
</div>
<div><small>Effective</small><strong>{{ auto.effective }}</strong></div>
<div><small>Policy Term</small><strong>12 Months</strong></div>
</div>
</div>
{% endif %}

{% if home %}
<div class="policy">
<div class="policy-main">
<div class="policy-copy">
<div class="policy-type">HOME INSURANCE</div>
<h2>{{ home.risk_type or "Property" }} Insurance</h2>
<div class="carrier-brand">
{% set brand = carrier_logo_data(home.carrier) %}
{% if brand.primary %}
<img
src="{{ brand.primary }}"
data-fallbacks='{{ brand.fallbacks|tojson }}'
data-index="0"
alt="{{ home.carrier }}"
style="display:none"
onload="this.style.display='block'"
onerror="
const arr=JSON.parse(this.dataset.fallbacks || '[]');
const i=parseInt(this.dataset.index || '0');
if(i < arr.length){
this.dataset.index=String(i+1);
this.src=arr[i];
}else{
this.style.display='none';
}
"
>
{% endif %}
<span class="carrier-name">{{ home.carrier }}</span>
</div>
<div class="policy-price"><strong>${{ money(home.monthly) }}/mo</strong><br><small>${{ money(home.annual) }}/year</small></div>
</div>
<div class="policy-art photo-art"><img src="{{ url_for('static', filename='home-hero-final.png') }}" alt="Illustrated modern home"></div>
</div>
<div class="quick">
<div><small>Property</small><strong>{{ home.address or "Property Address" }}</strong></div>
<div><small>Effective</small><strong>{{ home.effective }}</strong></div>
<div><small>Policy Term</small><strong>12 Months</strong></div>
</div>
</div>
{% endif %}
</div>

{% if auto and home %}
<div class="combined">
<div class="label">Combined Total · Auto + Home</div>
<div class="numbers"><strong>${{ money(combined_monthly) }}/mo</strong><strong>${{ money(combined_annual) }}/yr</strong></div>
</div>
{% endif %}

<div class="coverage-columns">
{% if auto %}
{% if auto.vehicles %}
{% for veh in auto.vehicles %}
<div class="coverage-panel">
<div class="panel-title"><h3>Vehicle {{ loop.index }} · {{ veh.vehicle }}</h3><span>{% if veh.annual %}${{ money(veh.annual) }}/year · {% endif %}Only what is on your quote</span></div>

<div class="coverage-subtitle">Policy Coverage Details</div>
<div class="tile-grid">
{% for item in veh.coverages if item.name != "Mandatory Accident Benefits" %}
<div class="tile"><div class="tile-art">{{ coverage_art(item.name) }}</div><strong>{{ item.name }}</strong><b>{{ item.value }}</b><p>{{ item.description }}</p></div>
{% endfor %}
</div>

<div class="mandatory-ab">
<div class="mandatory-head"><strong>Mandatory Accident Benefits</strong><span>Included with the policy</span></div>
<div class="mandatory-grid">
<div class="mandatory-card"><strong>Medical &amp; Rehabilitation</strong><small>Non-Catastrophic</small></div>
<div class="mandatory-card"><strong>Medical &amp; Rehabilitation</strong><small>Catastrophic</small></div>
<div class="mandatory-card"><strong>Attendant Care</strong><small>Included</small></div>
</div>
</div>

{% if veh.optional %}
<div class="optional">
<div class="optional-head"><strong>Optional Accident Benefits</strong>{% if veh.optional_total %}<span>${{ veh.optional_total }}/year</span>{% endif %}</div>
<div class="optional-grid">
{% for item in veh.optional %}
<div class="optional-card"><div class="tile-art">{{ coverage_art(item.name) }}</div><div><strong>{{ item.name }}</strong>{% if item.limit %}<small>{{ item.limit }}</small>{% endif %}{% if item.premium %}<b>{{ item.premium }}</b>{% endif %}</div></div>
{% endfor %}
</div>
</div>
{% endif %}
</div>
{% endfor %}
{% else %}
<div class="coverage-panel">
<div class="panel-title"><h3>Auto Coverage</h3><span>Only what is on your quote</span></div>

<div class="coverage-subtitle">Policy Coverage Details</div>
<div class="tile-grid">
{% for item in auto.coverages if item.name != "Mandatory Accident Benefits" %}
<div class="tile"><div class="tile-art">{{ coverage_art(item.name) }}</div><strong>{{ item.name }}</strong><b>{{ item.value }}</b><p>{{ item.description }}</p></div>
{% endfor %}
</div>

<div class="mandatory-ab">
<div class="mandatory-head"><strong>Mandatory Accident Benefits</strong><span>Included with the policy</span></div>
<div class="mandatory-grid">
<div class="mandatory-card"><strong>Medical &amp; Rehabilitation</strong><small>Non-Catastrophic</small></div>
<div class="mandatory-card"><strong>Medical &amp; Rehabilitation</strong><small>Catastrophic</small></div>
<div class="mandatory-card"><strong>Attendant Care</strong><small>Included</small></div>
</div>
</div>

{% if auto.optional %}
<div class="optional">
<div class="optional-head"><strong>Optional Accident Benefits</strong>{% if auto.optional_total %}<span>${{ auto.optional_total }}/year</span>{% endif %}</div>
<div class="optional-grid">
{% for item in auto.optional %}
<div class="optional-card"><div class="tile-art">{{ coverage_art(item.name) }}</div><div><strong>{{ item.name }}</strong>{% if item.limit %}<small>{{ item.limit }}</small>{% endif %}{% if item.premium %}<b>{{ item.premium }}</b>{% endif %}</div></div>
{% endfor %}
</div>
</div>
{% endif %}
</div>
{% endif %}
{% endif %}

{% if home %}
<div class="coverage-panel">
<div class="panel-title"><h3>Home Coverage</h3><span>All fields shown on your quote</span></div>
<div class="tile-grid">
{% for item in home.coverages %}
<div class="tile">
<div class="tile-art">{{ coverage_art(item.name) }}</div>
<strong>{{ item.name }}</strong>
<b>{{ item.value }}</b>
{% if item.premium %}
<div class="home-premium">Premium: <strong>{{ item.premium }}</strong></div>
{% endif %}
<p>{{ item.description }}</p>
</div>
{% endfor %}
</div>
{% if home.discounts %}
<div class="optional home-discounts">
<div class="optional-head"><strong>Discounts shown on quote</strong></div>
<div class="discount-grid">
{% for item in home.discounts %}
<div class="discount-card">{{ item }}</div>
{% endfor %}
</div>
</div>
{% endif %}
</div>
{% endif %}
</div>

<div class="cta">
<div class="mascot">
<img src="{{ url_for('static', filename='waqi-mascot-approved.png') }}" class="approved-mascot-img" alt="Waqi Insures mascot">
</div>
<div><h3>Questions? I’m here.</h3><p>If you want to move forward or want me to explain any part of the quote, message me directly.</p></div>
<div class="cta-actions">
<a class="btn wa" href="https://wa.me/{{ whatsapp_number }}" target="_blank" rel="noopener">Reply on WhatsApp</a>
<a class="btn mail" href="mailto:{{ broker_email }}">Email Me</a>
</div>
</div>

<div class="footer">Waqi Insures · RIBO Licensed & Registered · Proudly Made in Canada 🇨🇦</div>
</div>
</body>
</html>
"""


# =========================================================
# MAIN
# =========================================================


LOGIN_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>Waqi Insures · Broker Login</title>
<style>
:root{--navy:#071d34;--gold:#c99a36;--line:#e3e8ed}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f6f8;font-family:Inter,Arial,sans-serif;color:var(--navy)}
.card{width:min(420px,calc(100% - 32px));background:#fff;border:1px solid var(--line);border-radius:22px;padding:30px;box-shadow:0 18px 55px rgba(7,29,52,.10)}
h1{margin:0 0 6px;font-size:25px}.sub{color:#6f7d8b;margin:0 0 24px;font-size:14px}
label{display:block;font-size:12px;font-weight:800;margin:14px 0 6px}
input{width:100%;padding:13px 14px;border:1px solid #cfd7df;border-radius:10px;font-size:16px}
button{width:100%;margin-top:20px;border:0;border-radius:10px;padding:14px;background:var(--navy);color:#fff;font-weight:900;font-size:15px;cursor:pointer}
.err{background:#fff2f2;color:#9d2929;border:1px solid #f1cccc;border-radius:9px;padding:10px 12px;font-size:13px;margin-bottom:14px}
.gold{height:3px;width:54px;background:var(--gold);border-radius:4px;margin:12px 0 22px}
</style>
</head>
<body>
<form class="card" method="post">
<h1>Broker Console</h1><div class="gold"></div>
<p class="sub">Private access · Waqi Insures</p>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<label>Username</label><input name="username" autocomplete="username" required autofocus>
<label>Password</label><input type="password" name="password" autocomplete="current-password" required>
<button type="submit">Sign in</button>
</form>
</body>
</html>
"""

def broker_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        remote = (request.remote_addr or "").strip()
        if remote in {"127.0.0.1", "::1", "localhost"}:
            return view(*args, **kwargs)
        if not session.get("waqi_broker"):
            return redirect(url_for("broker_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def _money_number(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace("$", "").replace(",", "").replace("/yr", "").strip())
    except Exception:
        return None


def _potential_ars_rows(text, quote_type):
    """Broker-only safety net for carrier-detail rows not mapped yet."""
    text = _clean_ars_text(text or "")
    rows = []

    # OCR text is inherently noisy. Core fields, counts and required values are
    # still validated, but generic "unknown field" guessing is disabled so OCR
    # artefacts cannot create fake warnings.
    if "[[WAQI_OCR_USED]]" in text:
        return []

    anchor = re.search(r"(?im)^.*\bNew Business\b.*\bEffective Date:", text)
    if anchor:
        text = text[anchor.start():]
    glossary = re.search(r"(?im)^\s*Glossary Of Terms\s*$", text)
    if glossary:
        text = text[:glossary.start()]

    known_common = {
        "annual premium", "total premium", "principal premium", "total",
        "coverage", "coverages", "limit", "deductible", "premium",
        "discounts", "extended coverages", "optional accident benefits",
        "insurance company", "company", "effective date", "policy term"
    }
    known_auto = {
        "bodily injury", "property damage", "direct compensation",
        "accident benefits", "mandatory accident benefits", "all perils",
        "uninsured automobile", "loss of use", "#20 loss of use",
        "#27 liab to unowned veh.", "27 liab to unowned veh.",
        "#44 family protection", "44 family protection",
        "minor conviction protection", "accident waiver",
        "death", "funeral", "non-earner", "income replacement",
        "caregiver (catastrophic only)", "caregiver (impairment)",
        "dependant care", "housekeeping & home maintenance expense",
        "housekeeping & home maintenance", "lost education expenses",
        "damage to personal items", "expenses of visitors",
        "#23a mortgage", "service fee", "body injury"
    }
    known_home = {
        "residence", "outbuildings", "contents", "additional living expense",
        "additional living expenses", "voluntary medical", "voluntary property",
        "hail", "wind", "deductible", "single limit",
        "guaranteed building replacement cost", "personal insurance",
        "legal liability", "swimming pool", "sewer backup", "ground water",
        "overland water", "above ground water damage", "identity theft",
        "claim free protection", "home warranty", "by-laws",
        "service line coverage"
    }
    known = known_common | (known_auto if quote_type == "auto" else known_home)
    heading_noise = re.compile(
        r"(?i)^(?:prepared|message|messages|underwriting|driver\s*#|"
        r"vehicle|private passenger|primary|homeowners|rented dwelling|"
        r"tenants|condo|new business|effective date)"
    )
    value_signal = re.compile(
        r"(?i)(?:\$[\d,]+|\bIncluded\b|\bInc\.\b|\bN/A\b|"
        r"\b\d+\s*(?:Ded\.|deductible)\b)"
    )

    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" \t•-")
        if not line or len(line) < 4 or len(line) > 120:
            continue
        if re.match(r"^[■●▲@©*+]", line):
            continue
        if re.search(r"(?i)\b(?:FW\d+|W\d+)\b", line):
            continue
        if re.match(r"(?i)^(?:MSG|DIS|SUR|RB\b|TE\b|PAK\w+|BNDL\w+)", line):
            continue
        if re.search(
            r"(?i)\b(?:will apply|has been included|must be added|"
            r"quote number|eligible|ineligible|confirm|review|"
            r"please refer|not offered|not available)\b",
            line
        ):
            continue
        if line.count(":") > 1:
            continue
        if not value_signal.search(line) or heading_noise.search(line):
            continue
        vm = re.search(
            r"(?i)\s+(?=\$[\d,]+|\bIncluded\b|\bInc\.\b|\bN/A\b|\d+\s*(?:Ded\.|deductible)\b)",
            line
        )
        if not vm:
            continue
        label = line[:vm.start()].strip(" :-")
        label = re.sub(r"^[^A-Za-z#]+", "", label).strip()
        label_key = re.sub(r"\s+", " ", label).lower()
        label_key = label_key.replace("non-earer", "non-earner")
        if not label or len(label) > 55:
            continue
        if len(re.findall(r"[A-Za-z]+", label)) > 5:
            continue
        if any(label_key == item or label_key.startswith(item + " ") or item.startswith(label_key + " ") for item in known):
            continue
        if re.search(r"(?i)\b(?:tax|premium|total|prepared|date)\b", label_key):
            continue
        if not re.search(r"[A-Za-z]", label):
            continue
        candidate = f"{label}: {line[vm.end():].strip()}"
        if candidate not in rows:
            rows.append(candidate)
    return rows[:12]



def _detect_summary_optional_benefit_names(text):
    """
    Independent expected-count source from the ARS user-entry Summary.
    Works even when the carrier Optional AB table is split across pages.
    """
    text = _clean_ars_text(text or "")

    for anchor in re.finditer(r"(?im)^\s*Optional Accident Benefits\s*$", text):
        section = text[anchor.end():]
        stop = re.search(
            r"(?im)^\s*(?:Prepared\b|Effective Date:|Disclaimer:|Driver\s+\d+\s+of\s+\d+)",
            section
        )
        if stop:
            section = section[:stop.start()]

        definitions = [
            ("Income Replacement", r"Income Replacement"),
            ("Non-Earner", r"Non-Earner"),
            ("Caregiver (Catastrophic Only)", r"Caregiver\s*\(Catastrophic Only\)"),
            ("Caregiver (Impairment)", r"Caregiver\s*\(Impairment\)"),
            ("Medical, Rehabilitation and Attendant Care",
             r"Medical,\s*Rehabilitation\s+and\s+Attendant Care"),
            ("Dependant Care", r"Dependant Care"),
            ("Housekeeping & Home Maintenance",
             r"Housekeeping\s*&\s*Home Maintenance(?: Expense)?(?:\s*\(Impairment\))?"),
            ("Death", r"Death"),
            ("Funeral", r"Funeral"),
            ("Lost Education Expenses", r"Lost Education Expenses?"),
            ("Expenses of Visitors", r"Expenses of Visitors"),
            ("Damage to Personal Items", r"Damage to Personal Items"),
            ("Accident Waiver", r"Accident Waiver"),
        ]

        names = []
        for display, pattern in definitions:
            if re.search(rf"(?im)^\s*[-•■]?\s*{pattern}(?:\s|\(|$)", section):
                names.append(display)

        if names:
            return names

    return []


def _detect_optional_benefit_names(text):
    """
    Independent broker-QA counter.
    Reads the carrier-detail Optional Accident Benefits table separately from
    the normal parser, so ARS count and parsed count can be compared.
    """
    text = _clean_ars_text(text or "")

    anchor = re.search(
        r"(?im)^\s*Optional Accident Benefits\s*\(Total\s*\$?[\d,]+\)\s*$",
        text
    )
    if not anchor:
        return []

    section = text[anchor.end():]

    stop = re.search(
        r"(?im)^\s*(?:Annual Premium|Total Premium|0%\s*Tax Applied|"
        r"\d+\s+of\s+\d+\s*\||Glossary Of Terms)\s*",
        section
    )
    if stop:
        section = section[:stop.start()]

    definitions = [
        ("Income Replacement", [r"Income Replacement"]),
        ("Non-Earner", [r"Non-Earner"]),
        ("Caregiver (Catastrophic Only)", [r"Caregiver\s*\(Catastrophic Only\)"]),
        ("Caregiver (Impairment)", [r"Caregiver\s*\(Impairment\)"]),
        ("Medical, Rehabilitation and Attendant Care", [
            r"Medical,\s*Rehabilitation\s+and\s+Attendant Care",
            r"Medical\s*/\s*Rehabilitation\s*/\s*Attendant Care",
            r"Medical\s*&\s*Rehabilitation\s*&\s*Attendant Care"
        ]),
        ("Dependant Care", [r"Dependant Care"]),
        ("Housekeeping & Home Maintenance", [r"Housekeeping\s*&\s*Home Maintenance(?: Expense)?"]),
        ("Death", [r"Death"]),
        ("Funeral", [r"Funeral"]),
        ("Lost Education Expenses", [r"Lost Education Expenses?"]),
        ("Expenses of Visitors", [r"Expenses of Visitors"]),
        ("Damage to Personal Items", [r"Damage to Personal Items"]),
        ("Accident Waiver", [r"Accident Waiver"]),
    ]

    found = []
    for display_name, patterns in definitions:
        for pattern in patterns:
            if re.search(rf"(?im)^\s*{pattern}(?:\s|$)", section):
                found.append(display_name)
                break

    return found


def _optional_bundle_premium_sum(text):
    """Sum BNDL-priced Optional Accident Benefit bundles for broker QA."""
    text = _clean_ars_text(text or "")

    anchor = re.search(
        r"(?im)^\s*Optional Accident Benefits\s*\(Total\s*\$?[\d,]+\)\s*$",
        text
    )
    if not anchor:
        return None

    section = text[anchor.end():]

    stop = re.search(
        r"(?im)^\s*(?:Annual Premium|Total Premium|0%\s*Tax Applied|"
        r"\d+\s+of\s+\d+\s*\||Glossary Of Terms)\s*",
        section
    )
    if stop:
        section = section[:stop.start()]

    total = Decimal("0")
    found = False

    for m in re.finditer(
        r"(?im)^\s*BNDL[\w-]*\s+\$?([\d,]+(?:\.\d+)?)\s+\$?([\d,]+(?:\.\d+)?)\s*$",
        section
    ):
        total += Decimal(m.group(2).replace(",", ""))
        found = True

    return total if found else None


def _optional_premium_sum(items):
    total = Decimal("0")
    found = False
    for item in items or []:
        premium = str(item.get("premium") or "").strip()
        m = re.search(r"\$?([\d,]+(?:\.\d+)?)", premium)
        if m:
            total += Decimal(m.group(1).replace(",", ""))
            found = True
    return total if found else None


def validate_quote_for_broker(auto=None, home=None, upload_warnings=None):
    """Broker-only advisory warnings. NOTHING here blocks generation."""
    warnings = list(upload_warnings or [])

    def warn(message):
        if message not in warnings:
            warnings.append(message)

    if auto:
        if not auto.get("carrier"):
            warn("Auto: Insurance company not detected — please verify.")
        if auto.get("annual") in (None, ""):
            warn("Auto: Total annual premium not detected — please verify.")

        vehicles = auto.get("vehicles") or []
        expected_vehicles = auto.get("vehicle_count") or len(vehicles)
        if not vehicles:
            warn("Auto: No vehicle detected — please verify.")
        elif expected_vehicles and len(vehicles) != expected_vehicles:
            warn(f"Auto: ARS indicates {expected_vehicles} vehicle(s), but {len(vehicles)} vehicle section(s) were extracted.")

        vehicle_total = Decimal("0")
        vehicle_total_found = True
        for i, veh in enumerate(vehicles, 1):
            if not veh.get("vehicle"):
                warn(f"Auto: Vehicle {i} description not detected — please verify.")
            if veh.get("annual") in (None, ""):
                warn(f"Auto: Vehicle {i} premium not detected — please verify.")
                vehicle_total_found = False
            else:
                vehicle_total += Decimal(str(veh.get("annual")))
            if not veh.get("coverages"):
                warn(f"Auto: Vehicle {i} has no detected coverages — please verify.")

            for coverage in veh.get("coverages", []):
                if coverage.get("name") in {
                    "Bodily Injury",
                    "Property Damage",
                    "Family Protection (OPCF 44R)"
                }:
                    value = str(coverage.get("value") or "")
                    mm = re.fullmatch(r"\$([\d,]+)", value)
                    if mm:
                        amount = Decimal(mm.group(1).replace(",", ""))
                        if amount and amount < Decimal("100000"):
                            warn(
                                f"Auto: Vehicle {i} {coverage.get('name')} parsed as {value}; "
                                "this looks like a premium rather than a coverage limit — please verify."
                            )
            names = {x.get("name") for x in veh.get("coverages", [])}
            if "Mandatory Accident Benefits" not in names:
                warn(f"Auto: Vehicle {i} mandatory Accident Benefits card is missing.")

            expected_opt_names = veh.get("_broker_expected_optional_names") or []
            expected_opt_count = veh.get("_broker_expected_optional_count")
            parsed_opt_names = [
                item.get("name")
                for item in (veh.get("optional") or [])
                if item.get("name")
            ]
            parsed_opt_count = len(parsed_opt_names)

            if expected_opt_count is not None and expected_opt_count != parsed_opt_count:
                missing = [name for name in expected_opt_names if name not in parsed_opt_names]
                extra = [name for name in parsed_opt_names if name not in expected_opt_names]

                detail = ""
                if missing:
                    detail += " Missing: " + ", ".join(missing) + "."
                if extra:
                    detail += " Unexpected: " + ", ".join(extra) + "."

                warn(
                    f"Auto: Vehicle {i} ARS lists {expected_opt_count} Optional Accident "
                    f"Benefit(s), but the tool parsed {parsed_opt_count}.{detail}"
                )

            # Optional Benefit COUNT is independently reconciled above.
            # Do not compare individual premiums when carriers use bundles,
            # page continuations or OCR tables. The carrier's displayed Optional
            # Accident Benefits Total remains authoritative.


        auto_annual = _money_number(auto.get("annual"))
        if auto_annual is not None and vehicle_total_found and len(vehicles) > 1 and abs(auto_annual - vehicle_total) > Decimal("1.00"):
            warn(f"Auto: Vehicle premiums add to ${vehicle_total}, but policy annual premium is ${auto_annual} — please verify.")

        drivers = auto.get("drivers") or []
        expected_drivers = auto.get("driver_count") or len(drivers)
        if expected_drivers and len(drivers) < expected_drivers:
            warn(f"Auto: ARS indicates {expected_drivers} driver(s), but only {len(drivers)} driver name(s) were available in this PDF.")

        unknown = auto.get("_broker_unmapped_rows") or []
        if unknown:
            warn("Auto: Possible ARS field(s) detected that are not yet mapped: " + " | ".join(unknown[:5]))

    if home:
        if not home.get("carrier"):
            warn("Home: Insurance company not detected — please verify.")
        if home.get("annual") in (None, ""):
            warn("Home: Total annual premium not detected — please verify.")
        if not home.get("risk_type"):
            warn("Home: Property type not detected — please verify.")
        if not home.get("address"):
            warn("Home: Property address not detected — please verify.")
        if not home.get("coverages"):
            warn("Home: No coverage fields detected — please verify.")

        base = _money_number(home.get("base_premium")); tax = _money_number(home.get("tax")); annual = _money_number(home.get("annual"))
        if base is not None and tax is not None and annual is not None:
            calc = base + tax
            if abs(calc - annual) > Decimal("0.10"):
                warn(f"Home: Annual premium is ${annual}, but base + tax equals ${calc} — please verify.")

        unknown = home.get("_broker_unmapped_rows") or []
        if unknown:
            warn("Home: Possible ARS field(s) detected that are not yet mapped: " + " | ".join(unknown[:5]))

    return warnings

@app.route("/broker/login", methods=["GET", "POST"])
def broker_login():
    remote = (request.remote_addr or "").strip()
    if remote in {"127.0.0.1", "::1", "localhost"}:
        return redirect(url_for("home"))

    if not BROKER_PASSWORD:
        return "WAQI_BROKER_PASSWORD is not configured on the server.", 503
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if secrets.compare_digest(username, BROKER_USERNAME) and secrets.compare_digest(password, BROKER_PASSWORD):
            session.clear()
            session["waqi_broker"] = True
            destination = request.args.get("next") or url_for("home")
            if not destination.startswith("/"):
                destination = url_for("home")
            return redirect(destination)
        error = "Incorrect username or password."
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/broker/logout")
def broker_logout():
    session.clear()
    return redirect(url_for("broker_login"))

@app.after_request
def waqi_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # Customer quotes and broker pages should not be indexed or cached by search engines.
    if request.path.startswith("/quote/") or request.path.startswith("/broker") or request.path == "/":
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response.headers["Cache-Control"] = "private, no-store"
    return response

@app.route("/health")
def health():
    return {"status": "ok", "build": WAQI_BUILD}, 200

@app.route("/robots.txt")
def robots_txt():
    return "User-agent: *\\nDisallow: /\\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

@app.route("/", methods=["GET", "POST"])
@broker_login_required
def home():

    auto = None
    home_quote = None
    raw_results = []
    whatsapp = ""
    upload_warnings = []
    detected_auto_files = []
    detected_home_files = []

    if request.method == "POST":

        files = request.files.getlist("pdfs")

        for file in files:

            if not file or not file.filename:
                continue

            text = extract_pdf_text(file)

            raw_results.append({
                "filename": file.filename,
                "text": text
            })

            quote_type = detect_quote_type(text)

            if quote_type == "home":
                detected_home_files.append(file.filename)
                home_quote = parse_home(text)
            elif quote_type == "auto":
                detected_auto_files.append(file.filename)
                auto = parse_auto(text)
            else:
                upload_warnings.append(
                    f"{file.filename}: This PDF was not recognized as a supported ARS Auto/Home quote. Nothing was imported from it."
                )

    if len(detected_auto_files) > 1:
        upload_warnings.append(
            f"{len(detected_auto_files)} Auto PDFs were uploaded. The current review contains the last detected Auto quote ({detected_auto_files[-1]}). Please verify that this is intentional."
        )
    if len(detected_home_files) > 1:
        upload_warnings.append(
            f"{len(detected_home_files)} Home PDFs were uploaded. The current review contains the last detected Home quote ({detected_home_files[-1]}). Please verify that this is intentional."
        )

    combined_annual = None
    combined_monthly = None

    if auto and home_quote:

        combined_annual = (
            (auto["annual"] or Decimal("0")) +
            (home_quote["annual"] or Decimal("0"))
        )

        combined_monthly = (
            (auto["monthly"] or Decimal("0")) +
            (home_quote["monthly"] or Decimal("0"))
        )

    if auto or home_quote:
        whatsapp = build_whatsapp(
            auto,
            home_quote
        )

    quote_payload = ""

    if auto or home_quote:

        quote_payload = json.dumps(
            serialize_quote({
                "auto": auto,
                "home": home_quote
            })
        )

    return render_template_string(
        CONSOLE_HTML,
        auto=auto,
        home=home_quote,
        raw_results=raw_results,
        whatsapp=whatsapp,
        quote_payload=quote_payload,
        combined_annual=combined_annual,
        combined_monthly=combined_monthly,
        money=money,
        coverage_art=coverage_art,
        broker_warnings=validate_quote_for_broker(auto, home_quote, upload_warnings)
    )



def apply_manual_edits(auto, home_quote):
    if auto:
        auto["client"] = request.form.get("auto_client", auto.get("client", "")).strip()
        auto["effective"] = request.form.get("auto_effective", auto.get("effective", "")).strip()
        if auto.get("vehicles"):
            for vi, veh in enumerate(auto.get("vehicles", [])):
                veh["vehicle"] = request.form.get(
                    f"auto_vehicle_name_{vi}",
                    veh.get("vehicle", "")
                ).strip()

                for ci, item in enumerate(veh.get("coverages", [])):
                    if item.get("name") == "Mandatory Accident Benefits":
                        item["value"] = "Included"
                        item["description"] = "Medical, Rehabilitation & Attendant Care — mandatory Ontario accident benefits."
                        continue

                    item["value"] = request.form.get(
                        f"auto_v{vi}_cov_value_{ci}",
                        item.get("value", "")
                    ).strip()

                for oi, item in enumerate(veh.get("optional", [])):
                    item["limit"] = request.form.get(
                        f"auto_v{vi}_opt_limit_{oi}",
                        item.get("limit", "")
                    ).strip()
                    item["premium"] = request.form.get(
                        f"auto_v{vi}_opt_premium_{oi}",
                        item.get("premium", "")
                    ).strip()

            if auto["vehicles"]:
                auto["vehicle"] = auto["vehicles"][0].get("vehicle", auto.get("vehicle", ""))
                auto["coverages"] = auto["vehicles"][0].get("coverages", [])
                auto["optional"] = auto["vehicles"][0].get("optional", [])
                auto["optional_total"] = auto["vehicles"][0].get("optional_total", "")
        else:
            auto["vehicle"] = request.form.get("auto_vehicle", auto.get("vehicle", "")).strip()

        if "auto_driver_count" in request.form:
            try:
                auto["driver_count"] = int(request.form.get("auto_driver_count", auto.get("driver_count", 0)))
            except (TypeError, ValueError):
                pass

        if auto.get("drivers"):
            edited_drivers = []

            for i, existing in enumerate(auto.get("drivers", [])):
                name = request.form.get(
                    f"auto_driver_name_{i}",
                    existing.get("name", "")
                ).strip()
                role = request.form.get(
                    f"auto_driver_role_{i}",
                    existing.get("role", "")
                ).strip()

                if name:
                    edited_drivers.append({
                        "name": name,
                        "role": role or existing.get("role", "")
                    })

            auto["drivers"] = edited_drivers

            primary = next(
                (d["name"] for d in edited_drivers if d.get("role") == "Primary"),
                ""
            )

            auto["driver"] = primary or (
                edited_drivers[0]["name"] if edited_drivers else auto.get("driver", "")
            )

        else:
            auto["driver"] = request.form.get(
                "auto_driver",
                auto.get("driver", "")
            ).strip()

            if auto["driver"]:
                auto["drivers"] = [{"name": auto["driver"], "role": "Primary"}]

        auto["carrier"] = request.form.get("auto_carrier", auto.get("carrier", "")).strip()
        auto["product"] = request.form.get("auto_product", auto.get("product", "")).strip()

        annual = to_decimal(request.form.get("auto_annual", ""))
        monthly = to_decimal(request.form.get("auto_monthly", ""))

        if annual is not None:
            auto["annual"] = annual
        if monthly is not None:
            auto["monthly"] = monthly

        if not auto.get("vehicles") or len(auto.get("vehicles", [])) <= 1:
            for i, item in enumerate(auto.get("coverages", [])):
                if item.get("name") == "Mandatory Accident Benefits":
                    item["value"] = "Included"
                    item["description"] = "Medical, Rehabilitation & Attendant Care — mandatory Ontario accident benefits."
                    continue

                item["value"] = request.form.get(
                    f"auto_cov_value_{i}",
                    item.get("value", "")
                ).strip()

        if "auto_optional_total" in request.form:
            auto["optional_total"] = request.form.get(
                "auto_optional_total",
                auto.get("optional_total", "")
            ).strip()

        for i, item in enumerate(auto.get("optional", [])):
            item["limit"] = request.form.get(
                f"auto_opt_limit_{i}",
                item.get("limit", "")
            ).strip()

            item["premium"] = request.form.get(
                f"auto_opt_premium_{i}",
                item.get("premium", "")
            ).strip()

    if home_quote:
        home_quote["client"] = request.form.get(
            "home_client",
            home_quote.get("client", "")
        ).strip()

        home_quote["effective"] = request.form.get(
            "home_effective",
            home_quote.get("effective", "")
        ).strip()

        home_quote["carrier"] = request.form.get(
            "home_carrier",
            home_quote.get("carrier", "")
        ).strip()

        home_quote["risk_type"] = request.form.get(
            "home_risk_type",
            home_quote.get("risk_type", "")
        ).strip()

        home_quote["address"] = request.form.get(
            "home_address",
            home_quote.get("address", "")
        ).strip()

        annual = to_decimal(request.form.get("home_annual", ""))
        monthly = to_decimal(request.form.get("home_monthly", ""))

        if annual is not None:
            home_quote["annual"] = annual
        if monthly is not None:
            home_quote["monthly"] = monthly

        for i, item in enumerate(home_quote.get("coverages", [])):
            item["value"] = request.form.get(
                f"home_cov_value_{i}",
                item.get("value", "")
            ).strip()

            item["premium"] = request.form.get(
                f"home_cov_premium_{i}",
                item.get("premium", "")
            ).strip()

        for i, item in enumerate(home_quote.get("discounts", [])):
            home_quote["discounts"][i] = request.form.get(
                f"home_discount_{i}",
                item
            ).strip()

    return auto, home_quote


# =========================================================
# GENERATE
# =========================================================

@app.route("/generate", methods=["POST"])
@broker_login_required
def generate():

    raw = request.form.get(
        "quote_data",
        ""
    )

    if not raw:
        return redirect(url_for("home"))

    data = json.loads(raw)

    auto = deserialize_quote(
        data.get("auto")
    )

    home_quote = deserialize_quote(
        data.get("home")
    )

    auto, home_quote = apply_manual_edits(
        auto,
        home_quote
    )

    quote_id = save_quote(
        auto,
        home_quote
    )

    return redirect(
        url_for(
            "generated",
            quote_id=quote_id
        )
    )


# =========================================================
# GENERATED / SEND
# =========================================================

@app.route("/generated/<quote_id>")
@broker_login_required
def generated(quote_id):

    data = load_quote(quote_id)

    if not data:
        return "Quote not found", 404

    auto = data.get("auto")
    home_quote = data.get("home")
    source = auto if auto else home_quote

    client = (source.get("client") if source else "") or "Client"
    client_first = first_name(client)

    quote_url = url_for("customer_quote", quote_id=quote_id, _external=True)
    whatsapp = build_whatsapp(auto, home_quote, quote_url)

    email_subject = "Your Waqi Insurance Quote"
    email_body = (
        f"Hi {client_first},\\n\\n"
        f"I've prepared your personalized insurance quote.\\n\\n"
        f"You can review your pricing, coverage and policy details here:\\n"
        f"{quote_url}\\n\\n"
        f"If you have any questions or would like to move forward, just reply to this email.\\n\\n"
        f"Waqi Insures\\nRIBO Licensed & Registered"
    )

    whatsapp_share_url = "https://wa.me/?text=" + quote(whatsapp)
    email_url = "mailto:?subject=" + quote(email_subject) + "&body=" + quote(email_body)

    return render_template_string(
        """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quote Ready</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#071d34;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;color:#071d34}
.wrap{width:min(900px,94%);margin:30px auto}
.box{background:#fff;border-radius:22px;padding:28px;box-shadow:0 18px 55px rgba(0,0,0,.2)}
.logo{width:190px;max-height:90px;object-fit:contain}
h1{font-size:36px;margin:18px 0 6px;letter-spacing:-.03em}.sub{color:#728094;margin:0 0 20px}
.quote-link{display:flex;justify-content:space-between;gap:12px;align-items:center;background:#f4f7f9;border:1px solid #e1e7ed;border-radius:12px;padding:13px 14px}
.quote-link span{font-weight:800;overflow-wrap:anywhere;font-size:13px}.open{background:#071d34;color:#fff;text-decoration:none;border-radius:9px;padding:10px 13px;font-weight:900;white-space:nowrap}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px}.send{border:1px solid #e2e7ec;border-radius:15px;padding:18px}.send h3{margin:0 0 5px}.send p{font-size:12px;color:#748194;line-height:1.45}
.btn{display:block;text-decoration:none;text-align:center;padding:12px;border-radius:9px;font-weight:900;margin-top:12px}.wa{background:#1fa855;color:white}.mail{background:#c99a36;color:#071d34}.back{display:inline-block;margin-top:20px;color:#071d34;font-weight:800}
@media(max-width:650px){.grid{grid-template-columns:1fr}.quote-link{align-items:flex-start;flex-direction:column}}
</style>
</head>
<body><div class="wrap"><div class="box">
<img src="{{ url_for('static', filename='waqi-logo.png') }}" class="logo" alt="Waqi Insures">
<h1>Quote is ready.</h1>
<p class="sub">Your customer link is ready to review and share.</p>
<div class="quote-link"><span>{{ quote_url }}</span><a class="open" href="{{ quote_url }}" target="_blank">Open Customer Quote</a></div>
<div class="grid">
<div class="send"><h3>WhatsApp</h3><p>Open WhatsApp with the customer message and quote link already prepared.</p><a class="btn wa" href="{{ whatsapp_share_url }}" target="_blank">Open in WhatsApp</a></div>
<div class="send"><h3>Email</h3><p>Open your email app with the subject, message and quote link already prepared.</p><a class="btn mail" href="{{ email_url }}">Open Email App</a></div>
</div>
<a class="back" href="/">← Back to Quote Console</a>
</div></div></body></html>
        """,
        quote_url=quote_url,
        whatsapp_share_url=whatsapp_share_url,
        email_url=email_url
    )


# =========================================================
# CUSTOMER QUOTE
# =========================================================

@app.route("/quote/<quote_id>")
def customer_quote(quote_id):

    data = load_quote(quote_id)

    if not data:
        return "Quote not found", 404

    auto = data.get("auto")
    home_quote = data.get("home")

    source = auto if auto else home_quote

    client = (
        source.get("client")
        if source
        else ""
    ) or "there"
    client_first = first_name(client)

    combined_annual = None
    combined_monthly = None

    if auto and home_quote:

        combined_annual = (
            (auto.get("annual") or Decimal("0")) +
            (home_quote.get("annual") or Decimal("0"))
        )

        combined_monthly = (
            (auto.get("monthly") or Decimal("0")) +
            (home_quote.get("monthly") or Decimal("0"))
        )

    return render_template_string(
        CUSTOMER_HTML,
        auto=auto,
        home=home_quote,
        client=client,
        client_first=client_first,
        combined_annual=combined_annual,
        combined_monthly=combined_monthly,
        money=money,
        coverage_art=coverage_art,
        hero_art=hero_art,
        mascot_art=mascot_art,
        carrier_logo_data=carrier_logo_data,
        mascot_available=mascot_exists(),
        whatsapp_number=BROKER_WHATSAPP_NUMBER,
        broker_email=BROKER_EMAIL
    )


# =========================================================
# START
# =========================================================


if __name__ == "__main__":
    print("WAQI Quote Tool build:", WAQI_BUILD)
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)