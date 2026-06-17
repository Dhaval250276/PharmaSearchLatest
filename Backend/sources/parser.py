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
