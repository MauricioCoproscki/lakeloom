"""Model-graph builders shared by the publish tests."""

B = "https://example.org/m/"


def cls(name: str, label: str | None = None) -> dict:
    return {"iri": B + name, "conceptType": "OWL_CLASS", "displayName": label or name}


def data(name: str, domain: list[str], range_: str, **extra) -> dict:
    return {
        "iri": B + name,
        "kind": "DATA",
        "domain": [B + d for d in domain],
        "range": [range_],
        "displayName": name,
        **extra,
    }


def rel(name: str, domain: str, range_: str) -> dict:
    return {
        "iri": B + name,
        "kind": "OBJECT",
        "domain": [B + domain],
        "range": [B + range_],
        "displayName": name,
    }


def restriction(subject: str, prop: str, kind: str, n: int) -> dict:
    return {
        "subjectIri": B + subject,
        "axiomType": "SUBCLASS_OF",
        "expressions": [
            {
                "kind": "RESTRICTION",
                "propertyIri": B + prop,
                "restrictionKind": kind,
                "cardinality": n,
            }
        ],
    }
