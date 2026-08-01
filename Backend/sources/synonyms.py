SUBSTANCE_SYNONYMS = {
    "paracetamol": ["acetaminophen"],
    "acetaminophen": ["paracetamol"],
    "lidocaine prilocaine": [
        "prilocaine lidocaine",
        "lidocaine+prilocaine",
        "prilocaine+lidocaine",
        "lidocaine and prilocaine",
        "lidocaine",
        "prilocaine",
    ],
    "prilocaine lidocaine": [
        "lidocaine prilocaine",
        "lidocaine+prilocaine",
        "prilocaine+lidocaine",
        "lidocaine and prilocaine",
        "lidocaine",
        "prilocaine",
    ],
    "lidocaine+prilocaine": [
        "lidocaine prilocaine",
        "prilocaine lidocaine",
        "prilocaine+lidocaine",
        "lidocaine and prilocaine",
        "lidocaine",
        "prilocaine",
    ],
    "prilocaine+lidocaine": [
        "prilocaine lidocaine",
        "lidocaine prilocaine",
        "lidocaine+prilocaine",
        "lidocaine and prilocaine",
        "lidocaine",
        "prilocaine",
    ],
}


def get_substance_search_terms(substance):
    normalized_parts = [
        part
        for part in substance.strip().lower().replace("+", " ").split()
        if part not in {"and", "&"}
    ]
    normalized = " ".join(normalized_parts)
    terms = [substance]
    synonyms = list(SUBSTANCE_SYNONYMS.get(normalized, []))
    synonyms.extend(SUBSTANCE_SYNONYMS.get(substance.strip().lower(), []))
    for synonym in synonyms:
        if synonym.lower() not in {term.lower() for term in terms}:
            terms.append(synonym)
    return terms
