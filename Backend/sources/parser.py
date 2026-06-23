import re

def extract_strength_form(product):

    strength = ""
    dosage_form = ""

    mg_match = re.search(r'(\d+\s*MG(?:\s*/\s*\d+\s*ML)?)', product, re.I)

    if mg_match:
        strength = mg_match.group(1)

    forms = [
        "TABLETS",
        "FILM-COATED TABLETS",
        "CAPSULES",
        "ORAL SOLUTION",
        "ORAL SUSPENSION",
        "PROLONGED RELEASE TABLETS"
    ]

    for form in forms:
        if form in product.upper():
            dosage_form = form
            break

    return strength, dosage_form


def extract_strength(product):

    match = re.search(
        r'(\d+\s*(MG|MCG|G|ML)(?:/\d+\s*(MG|MCG|G|ML))?)',
        product,
        re.IGNORECASE
    )

    return match.group(1) if match else ""

def normalize_product(product):

    product = clean_product_name(product)

    return product

DOSAGE_FORMS = [
    "PROLONGED RELEASE TABLETS",
    "FILM-COATED TABLETS",
    "ORAL SOLUTION",
    "ORAL SUSPENSION",
    "TABLETS",
    "TABLET",
    "CAPSULES",
    "CAPSULE",
    "INJECTION",
    "CREAM",
    "OINTMENT",
    "GEL",
    "PATCH",
    "SPRAY",
    "DROPS",
    "SYRUP",
    "POWDER"
]
def extract_dosage_form(product):

    product_upper = product.upper()

    for form in DOSAGE_FORMS:

        if form in product_upper:
            return form

    return ""


def extract_pl_number(product):

    match = re.search(
        r'PL\s*\d+/\d+(?:-\d+)?',
        product,
        re.IGNORECASE
    )

    return match.group(0) if match else ""


def clean_product_name(product):

    product = " ".join(product.split())

    # Remove repeated words/phrases at beginning
    words = product.split()

    for size in range(1, len(words)//2 + 1):

        first = words[:size]
        second = words[size:size*2]

        if first == second:
            product = " ".join(words[size:])
            break

    # Remove repeated phrase before PL number
    match = re.match(
        r"^(.*?)\s+\1\s+(-?\s*PL\s+\d+/\d+.*)$",
        product,
        re.IGNORECASE
    )

    if match:
        product = match.group(1) + " " + match.group(2)

    # Remove repeated consecutive chunks
    product = re.sub(
        r'\b(.+?)\s+\1\b',
        r'\1',
        product,
        flags=re.IGNORECASE
    )

    return product.strip()
