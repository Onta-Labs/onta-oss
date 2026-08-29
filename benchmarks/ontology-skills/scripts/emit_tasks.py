"""One-shot emitter for fixtures/tasks.jsonl. Run from repo; do not import product."""

from __future__ import annotations

import json
from pathlib import Path

ENT = "https://graph.infona.ai/bench/ent/"
ONTO = "https://graph.infona.ai/bench/onto/"
SUP = ONTO + "SUPPLIES_TO"
EMP = ONTO + "EMPLOYS"
LOC = ONTO + "LOCATED_IN"
SUB = ONTO + "SUBSIDIARY_OF"

KNOWN = "known_ontology_unseen_instances"
UNSEEN = "unseen_ontology_branches"
ADV = "adversarial_conflicting"

NB_SUP = {
    "type_ids": ["Supplier"],
    "relation_ids": [],
    "include_ancestors": True,
    "include_incident_relations": True,
}
NB_CUST = {
    "type_ids": ["Customer"],
    "relation_ids": [],
    "include_ancestors": True,
    "include_incident_relations": True,
}
NB_LOC = {
    "type_ids": ["Location"],
    "relation_ids": ["LOCATED_IN"],
    "include_ancestors": True,
    "include_incident_relations": True,
}
NB_PERSON = {
    "type_ids": ["Person"],
    "relation_ids": ["EMPLOYS"],
    "include_ancestors": True,
    "include_incident_relations": True,
}
NB_CO = {
    "type_ids": ["Company"],
    "relation_ids": [],
    "include_ancestors": True,
    "include_incident_relations": False,
}
NB_PROD = {
    "type_ids": ["Product"],
    "relation_ids": [],
    "include_ancestors": True,
    "include_incident_relations": False,
}
NB_REL = {
    "type_ids": ["Supplier", "Customer"],
    "relation_ids": ["SUPPLIES_TO"],
    "include_ancestors": True,
    "include_incident_relations": True,
}
NB_ORG = {
    "type_ids": ["Organization"],
    "relation_ids": [],
    "include_ancestors": True,
    "include_incident_relations": True,
}


def e(slug: str) -> str:
    return ENT + slug


def task(task_id, family, split, notes, neighborhood, inp, gold) -> dict:
    return {
        "task_id": task_id,
        "family": family,
        "split": split,
        "notes": notes,
        "neighborhood": neighborhood,
        "input": inp,
        "gold": gold,
    }


def ta(entity, type_id):
    return {"entity": entity, "type_id": type_id}


def lit(entity, attr, value):
    return {"entity": entity, "attr": attr, "value": value}


def triple(s, p, o):
    return {"subject": s, "predicate": p, "object": o}


def merge(absorbed, survivor):
    return {"absorbed": absorbed, "survivor": survivor}


def ext(type_id, parent_id, label):
    return {"type_id": type_id, "parent_id": parent_id, "label": label}


def build() -> list[dict]:
    rows: list[dict] = []
    rows.extend(entity_typing())
    rows.extend(mapping())
    rows.extend(er())
    rows.extend(relations())
    rows.extend(cvr())
    rows.extend(conflict())
    rows.extend(extension())
    rows.extend(multistep())
    return rows


def entity_typing() -> list[dict]:
    acme = e("acme-components")
    globex = e("globex-manufacturing")
    leeds = e("leeds-depot")
    widget = e("widget-sku-440")
    riley = e("riley-chen")
    initech = e("initech-ltd")
    hull = e("hull-3pl")
    felix = e("felixstowe-bonded")
    jamie = e("jamie-lee")
    soylent = e("soylent-packaging")
    return [
        task(
            "et-001",
            "entity_typing",
            KNOWN,
            "Vendor row for a company not previously seen. Gold leaf type is Supplier.",
            NB_SUP,
            {
                "record": {
                    "name": "Acme Components Ltd",
                    "role": "vendor",
                    "vat": "GB-000111222",
                },
                "mention": "Acme Components Ltd",
            },
            {
                "type_assertions": [ta(acme, "Supplier")],
                "literals": [
                    lit(acme, "legalName", "Acme Components Ltd"),
                    lit(acme, "registrationId", "GB-000111222"),
                ],
            },
        ),
        task(
            "et-002",
            "entity_typing",
            KNOWN,
            "Buying organization. Leaf type is Customer, not Supplier.",
            NB_CUST,
            {
                "record": {"name": "Globex Manufacturing", "role": "buyer"},
                "mention": "Globex Manufacturing",
            },
            {
                "type_assertions": [ta(globex, "Customer")],
                "literals": [lit(globex, "legalName", "Globex Manufacturing")],
            },
        ),
        task(
            "et-003",
            "entity_typing",
            KNOWN,
            "Site name with no org cues. Leaf type is Location.",
            NB_LOC,
            {
                "record": {"name": "Leeds Depot", "kind": "site"},
                "mention": "Leeds Depot",
            },
            {
                "type_assertions": [ta(leeds, "Location")],
                "literals": [lit(leeds, "legalName", "Leeds Depot")],
            },
        ),
        task(
            "et-004",
            "entity_typing",
            KNOWN,
            "SKU row. Leaf type is Product, not Supplier.",
            NB_PROD,
            {
                "record": {"sku": "WGT-440", "name": "M8 steel widget"},
                "mention": "WGT-440",
            },
            {
                "type_assertions": [ta(widget, "Product")],
                "literals": [
                    lit(widget, "sku", "WGT-440"),
                    lit(widget, "legalName", "M8 steel widget"),
                ],
            },
        ),
        task(
            "et-005",
            "entity_typing",
            KNOWN,
            "Human contact. Leaf type is Person.",
            NB_PERSON,
            {
                "record": {"name": "Riley Chen", "title": "buyer"},
                "mention": "Riley Chen",
            },
            {
                "type_assertions": [ta(riley, "Person")],
                "literals": [lit(riley, "legalName", "Riley Chen")],
            },
        ),
        task(
            "et-006",
            "entity_typing",
            KNOWN,
            "Registered company with no vendor/buyer role. Leaf is Company.",
            NB_CO,
            {
                "record": {
                    "name": "Initech Ltd",
                    "company_number": "01234567",
                },
                "mention": "Initech Ltd",
            },
            {
                "type_assertions": [ta(initech, "Company")],
                "literals": [
                    lit(initech, "legalName", "Initech Ltd"),
                    lit(initech, "registrationId", "01234567"),
                ],
            },
        ),
        task(
            "et-007",
            "entity_typing",
            UNSEEN,
            "3PL warehouse is not in the snapshot. Extend Location, type the instance.",
            NB_LOC,
            {
                "mention": "Hull 3PL warehouse",
                "evidence": "third-party logistics site storing inbound parts",
            },
            {
                "type_extensions": [
                    ext("ThirdPartyWarehouse", "Location", "3PL warehouse")
                ],
                "type_assertions": [ta(hull, "ThirdPartyWarehouse")],
                "literals": [lit(hull, "legalName", "Hull 3PL warehouse")],
            },
        ),
        task(
            "et-008",
            "entity_typing",
            UNSEEN,
            "Bonded customs shed. New type under Location.",
            NB_LOC,
            {
                "mention": "Felixstowe bonded shed",
                "evidence": "HM Customs bonded store for inbound parts",
            },
            {
                "type_extensions": [
                    ext("BondedWarehouse", "Location", "bonded warehouse")
                ],
                "type_assertions": [ta(felix, "BondedWarehouse")],
                "literals": [lit(felix, "legalName", "Felixstowe bonded shed")],
            },
        ),
        task(
            "et-009",
            "entity_typing",
            ADV,
            "Vendor contact is a Person, not a Supplier. Do not launder the type.",
            NB_PERSON,
            {
                "record": {
                    "name": "Jamie Lee",
                    "role": "vendor",
                    "note": "accounts contact only",
                },
                "mention": "Jamie Lee",
            },
            {
                "type_assertions": [ta(jamie, "Person")],
                "literals": [lit(jamie, "legalName", "Jamie Lee")],
            },
        ),
        task(
            "et-010",
            "entity_typing",
            ADV,
            "PO footer labels the buyer as vendor. Leaf type is Customer.",
            NB_CUST,
            {
                "record": {
                    "name": "Soylent Packaging",
                    "role": "vendor",
                    "po_party": "bill-to",
                },
                "mention": "Soylent Packaging",
            },
            {
                "type_assertions": [ta(soylent, "Customer")],
                "literals": [lit(soylent, "legalName", "Soylent Packaging")],
            },
        ),
    ]


def mapping() -> list[dict]:
    nw = e("northwind-parts")
    stark = e("stark-bolts")
    wayne = e("wayne-fasteners")
    oscorp = e("oscorp-resins")
    hooli = e("hooli-components")
    massive = e("massive-dynamic")
    hull = e("hull-3pl")
    felix = e("felixstowe-bonded")
    umbrella = e("umbrella-parts")
    pied = e("pied-piper-logistics")
    return [
        task(
            "map-001",
            "property_schema_mapping",
            KNOWN,
            "VAT Number maps to registrationId, not a free-text note.",
            NB_CO,
            {
                "columns": ["Supplier Name", "VAT Number", "City"],
                "sample_rows": [
                    {
                        "Supplier Name": "Northwind Parts",
                        "VAT Number": "GB-333444555",
                        "City": "Leeds",
                    }
                ],
            },
            {
                "literals": [
                    lit(nw, "legalName", "Northwind Parts"),
                    lit(nw, "registrationId", "GB-333444555"),
                ],
                "type_assertions": [ta(nw, "Supplier")],
            },
        ),
        task(
            "map-002",
            "property_schema_mapping",
            KNOWN,
            "EIN maps to registrationId.",
            NB_CO,
            {
                "columns": ["Legal Name", "EIN"],
                "sample_rows": [
                    {"Legal Name": "Stark Bolts Inc", "EIN": "12-3456789"}
                ],
            },
            {
                "literals": [
                    lit(stark, "legalName", "Stark Bolts Inc"),
                    lit(stark, "registrationId", "12-3456789"),
                ],
                "type_assertions": [ta(stark, "Supplier")],
            },
        ),
        task(
            "map-003",
            "property_schema_mapping",
            KNOWN,
            "Company Number maps to registrationId.",
            NB_CO,
            {
                "columns": ["Name", "Company Number"],
                "sample_rows": [
                    {"Name": "Wayne Fasteners Ltd", "Company Number": "00998877"}
                ],
            },
            {
                "literals": [
                    lit(wayne, "legalName", "Wayne Fasteners Ltd"),
                    lit(wayne, "registrationId", "00998877"),
                ],
                "type_assertions": [ta(wayne, "Supplier")],
            },
        ),
        task(
            "map-004",
            "property_schema_mapping",
            KNOWN,
            "Trade name is alias; Legal name is legalName.",
            NB_ORG,
            {
                "columns": ["Legal name", "Trade name"],
                "sample_rows": [
                    {
                        "Legal name": "Oscorp Resins GmbH",
                        "Trade name": "Oscorp",
                    }
                ],
            },
            {
                "literals": [
                    lit(oscorp, "legalName", "Oscorp Resins GmbH"),
                    lit(oscorp, "alias", "Oscorp"),
                ],
                "type_assertions": [ta(oscorp, "Company")],
            },
        ),
        task(
            "map-005",
            "property_schema_mapping",
            KNOWN,
            "City is not legalName. Only name and VAT are mapped.",
            NB_SUP,
            {
                "columns": ["Vendor", "VAT", "City"],
                "sample_rows": [
                    {
                        "Vendor": "Hooli Components",
                        "VAT": "GB-777888999",
                        "City": "Reading",
                    }
                ],
            },
            {
                "literals": [
                    lit(hooli, "legalName", "Hooli Components"),
                    lit(hooli, "registrationId", "GB-777888999"),
                ],
                "type_assertions": [ta(hooli, "Supplier")],
            },
        ),
        task(
            "map-006",
            "property_schema_mapping",
            KNOWN,
            "Incoterms stay on the Supplier, not on a Product.",
            NB_SUP,
            {
                "columns": ["Supplier", "Incoterms"],
                "sample_rows": [
                    {"Supplier": "Massive Dynamic", "Incoterms": "FOB"}
                ],
            },
            {
                "literals": [
                    lit(massive, "legalName", "Massive Dynamic"),
                    lit(massive, "incoterms", "FOB"),
                ],
                "type_assertions": [ta(massive, "Supplier")],
            },
        ),
        task(
            "map-007",
            "property_schema_mapping",
            UNSEEN,
            "3PL site name maps onto an extended Location subtype.",
            NB_LOC,
            {
                "columns": ["3PL site", "Warehouse code"],
                "sample_rows": [
                    {"3PL site": "Hull 3PL warehouse", "Warehouse code": "HULL-3PL"}
                ],
            },
            {
                "type_extensions": [
                    ext("ThirdPartyWarehouse", "Location", "3PL warehouse")
                ],
                "type_assertions": [ta(hull, "ThirdPartyWarehouse")],
                "literals": [
                    lit(hull, "legalName", "Hull 3PL warehouse"),
                    lit(hull, "warehouseCode", "HULL-3PL"),
                ],
            },
        ),
        task(
            "map-008",
            "property_schema_mapping",
            UNSEEN,
            "Bonded site name maps onto BondedWarehouse.",
            NB_LOC,
            {
                "columns": ["Bonded site"],
                "sample_rows": [{"Bonded site": "Felixstowe bonded shed"}],
            },
            {
                "type_extensions": [
                    ext("BondedWarehouse", "Location", "bonded warehouse")
                ],
                "type_assertions": [ta(felix, "BondedWarehouse")],
                "literals": [lit(felix, "legalName", "Felixstowe bonded shed")],
            },
        ),
        task(
            "map-009",
            "property_schema_mapping",
            ADV,
            "A VAT-looking token in Notes is not registrationId.",
            NB_SUP,
            {
                "columns": ["Vendor", "Notes"],
                "sample_rows": [
                    {
                        "Vendor": "Umbrella Parts",
                        "Notes": "ask AP about GB-111000222",
                    }
                ],
            },
            {
                "literals": [
                    lit(umbrella, "legalName", "Umbrella Parts"),
                    lit(umbrella, "notes", "ask AP about GB-111000222"),
                ],
                "type_assertions": [ta(umbrella, "Supplier")],
            },
        ),
        task(
            "map-010",
            "property_schema_mapping",
            ADV,
            "EIN is registrationId; DUNS stays duns. Do not collapse them.",
            NB_CO,
            {
                "columns": ["Name", "EIN", "DUNS"],
                "sample_rows": [
                    {
                        "Name": "Pied Piper Logistics",
                        "EIN": "98-7654321",
                        "DUNS": "123456789",
                    }
                ],
            },
            {
                "literals": [
                    lit(pied, "legalName", "Pied Piper Logistics"),
                    lit(pied, "registrationId", "98-7654321"),
                    lit(pied, "duns", "123456789"),
                ],
                "type_assertions": [ta(pied, "Company")],
            },
        ),
    ]


def er() -> list[dict]:
    acme = e("acme-components")
    acme2 = e("acme-components-2")
    nw = e("northwind-parts")
    nw2 = e("northwind-parts-2")
    oscorp = e("oscorp-resins")
    oscorp2 = e("oscorp-resins-2")
    hooli = e("hooli-components")
    hooli2 = e("hooli-components-2")
    wayne = e("wayne-fasteners")
    wayne_b = e("wayne-fasteners-b")
    wayne_c = e("wayne-fasteners-c")
    initech = e("initech-ltd")
    initech_b = e("initech-holdings")
    hull = e("hull-3pl")
    hull2 = e("hull-3pl-2")
    felix = e("felixstowe-bonded")
    felix2 = e("felixstowe-bonded-2")
    riley = e("riley-chen")
    riley_co = e("riley-chen-supplies")
    stark = e("stark-bolts")
    stark2 = e("stark-bolts-ocr")
    return [
        task(
            "er-001",
            "entity_resolution",
            KNOWN,
            "Case and Ltd suffix: merge into the URI that already carries registrationId.",
            NB_SUP,
            {
                "left": {
                    "uri": acme,
                    "name": "Acme Components Ltd",
                    "registrationId": "GB-000111222",
                },
                "right": {
                    "uri": acme2,
                    "name": "ACME COMPONENTS",
                    "registrationId": "GB-000111222",
                },
            },
            {"merges": [merge(acme2, acme)]},
        ),
        task(
            "er-002",
            "entity_resolution",
            KNOWN,
            "Inc suffix vs bare name, same VAT.",
            NB_SUP,
            {
                "left": {
                    "uri": nw,
                    "name": "Northwind Parts Inc",
                    "registrationId": "GB-333444555",
                },
                "right": {
                    "uri": nw2,
                    "name": "Northwind Parts",
                    "registrationId": "GB-333444555",
                },
            },
            {"merges": [merge(nw2, nw)]},
        ),
        task(
            "er-003",
            "entity_resolution",
            KNOWN,
            "GmbH suffix, same registration id.",
            NB_CO,
            {
                "left": {
                    "uri": oscorp,
                    "name": "Oscorp Resins GmbH",
                    "registrationId": "HRB-44001",
                },
                "right": {
                    "uri": oscorp2,
                    "name": "Oscorp Resins",
                    "registrationId": "HRB-44001",
                },
            },
            {"merges": [merge(oscorp2, oscorp)]},
        ),
        task(
            "er-004",
            "entity_resolution",
            KNOWN,
            "Whitespace and punctuation only.",
            NB_SUP,
            {
                "left": {
                    "uri": hooli,
                    "name": "Hooli Components",
                    "registrationId": "GB-777888999",
                },
                "right": {
                    "uri": hooli2,
                    "name": "  Hooli  Components. ",
                    "registrationId": "GB-777888999",
                },
            },
            {"merges": [merge(hooli2, hooli)]},
        ),
        task(
            "er-005",
            "entity_resolution",
            KNOWN,
            "Three mentions, two are the same company. Merge both extras into the keyed survivor.",
            NB_SUP,
            {
                "mentions": [
                    {
                        "uri": wayne,
                        "name": "Wayne Fasteners Ltd",
                        "registrationId": "00998877",
                    },
                    {"uri": wayne_b, "name": "Wayne Fasteners"},
                    {"uri": wayne_c, "name": "WAYNE FASTENERS LTD"},
                ]
            },
            {"merges": [merge(wayne_b, wayne), merge(wayne_c, wayne)]},
        ),
        task(
            "er-006",
            "entity_resolution",
            KNOWN,
            "Same display name, different registration ids: keep both. Gold asserts both types.",
            NB_CO,
            {
                "left": {
                    "uri": initech,
                    "name": "Initech",
                    "registrationId": "01234567",
                },
                "right": {
                    "uri": initech_b,
                    "name": "Initech",
                    "registrationId": "07654321",
                },
            },
            {
                "type_assertions": [
                    ta(initech, "Company"),
                    ta(initech_b, "Company"),
                ]
            },
        ),
        task(
            "er-007",
            "entity_resolution",
            UNSEEN,
            "Two 3PL site strings for the same warehouse. Extend + merge.",
            NB_LOC,
            {
                "left": {"uri": hull, "name": "Hull 3PL warehouse"},
                "right": {"uri": hull2, "name": "HULL 3PL"},
            },
            {
                "type_extensions": [
                    ext("ThirdPartyWarehouse", "Location", "3PL warehouse")
                ],
                "merges": [merge(hull2, hull)],
            },
        ),
        task(
            "er-008",
            "entity_resolution",
            UNSEEN,
            "Bonded shed aliases.",
            NB_LOC,
            {
                "left": {"uri": felix, "name": "Felixstowe bonded shed"},
                "right": {"uri": felix2, "name": "Felixstowe bonded warehouse"},
            },
            {
                "type_extensions": [
                    ext("BondedWarehouse", "Location", "bonded warehouse")
                ],
                "merges": [merge(felix2, felix)],
            },
        ),
        task(
            "er-009",
            "entity_resolution",
            ADV,
            "Shared display name across Person and Supplier: do not merge.",
            NB_PERSON,
            {
                "left": {"uri": riley, "name": "Riley Chen", "type_hint": "Person"},
                "right": {
                    "uri": riley_co,
                    "name": "Riley Chen",
                    "type_hint": "Supplier",
                    "registrationId": "GB-221100000",
                },
            },
            {
                "type_assertions": [
                    ta(riley, "Person"),
                    ta(riley_co, "Supplier"),
                ]
            },
        ),
        task(
            "er-010",
            "entity_resolution",
            ADV,
            "OCR almost-collision on EIN. Different ids: keep both.",
            NB_CO,
            {
                "left": {
                    "uri": stark,
                    "name": "Stark Bolts Inc",
                    "registrationId": "12-3456789",
                },
                "right": {
                    "uri": stark2,
                    "name": "Stark Bolts Inc",
                    "registrationId": "12-3456798",
                    "note": "ocr-suspect",
                },
            },
            {
                "type_assertions": [
                    ta(stark, "Supplier"),
                    ta(stark2, "Supplier"),
                ]
            },
        ),
    ]


def relations() -> list[dict]:
    acme = e("acme-components")
    globex = e("globex-manufacturing")
    leeds = e("leeds-depot")
    riley = e("riley-chen")
    initech = e("initech-ltd")
    initech_b = e("initech-holdings")
    nw = e("northwind-parts")
    wayne = e("wayne-fasteners")
    hull = e("hull-3pl")
    felix = e("felixstowe-bonded")
    jamie = e("jamie-lee")
    soylent = e("soylent-packaging")
    return [
        task(
            "rel-001",
            "relation_inference",
            KNOWN,
            "Invoice from typed Supplier to typed Customer implies SUPPLIES_TO, not EMPLOYS.",
            NB_REL,
            {
                "record": {
                    "supplier": "Acme Components Ltd",
                    "customer": "Globex Manufacturing",
                    "invoice_id": "INV-1044",
                    "qty": 20,
                    "valid_from": "2026-01-01",
                }
            },
            {"adds": [triple(acme, SUP, globex)]},
        ),
        task(
            "rel-002",
            "relation_inference",
            KNOWN,
            "HQ city is LOCATED_IN, not SUPPLIES_TO.",
            {
                "type_ids": ["Organization", "Location"],
                "relation_ids": ["LOCATED_IN"],
                "include_ancestors": True,
                "include_incident_relations": True,
            },
            {
                "record": {
                    "org": "Globex Manufacturing",
                    "hq": "Leeds Depot",
                }
            },
            {"adds": [triple(globex, LOC, leeds)]},
        ),
        task(
            "rel-003",
            "relation_inference",
            KNOWN,
            "Payroll row is EMPLOYS.",
            {
                "type_ids": ["Organization", "Person"],
                "relation_ids": ["EMPLOYS"],
                "include_ancestors": True,
                "include_incident_relations": True,
            },
            {"record": {"employer": "Acme Components Ltd", "employee": "Riley Chen"}},
            {"adds": [triple(acme, EMP, riley)]},
        ),
        task(
            "rel-004",
            "relation_inference",
            KNOWN,
            "Parent company is SUBSIDIARY_OF.",
            {
                "type_ids": ["Company"],
                "relation_ids": ["SUBSIDIARY_OF"],
                "include_ancestors": True,
                "include_incident_relations": True,
            },
            {
                "record": {
                    "child": "Initech Ltd",
                    "parent": "Initech Holdings",
                }
            },
            {"adds": [triple(initech, SUB, initech_b)]},
        ),
        task(
            "rel-005",
            "relation_inference",
            KNOWN,
            "Two invoices in the same window are one SUPPLIES_TO edge.",
            NB_REL,
            {
                "records": [
                    {
                        "supplier": "Northwind Parts",
                        "customer": "Globex Manufacturing",
                        "invoice_id": "INV-1",
                        "valid_from": "2026-01-01",
                    },
                    {
                        "supplier": "Northwind Parts",
                        "customer": "Globex Manufacturing",
                        "invoice_id": "INV-2",
                        "valid_from": "2026-01-15",
                    },
                ]
            },
            {"adds": [triple(nw, SUP, globex)]},
        ),
        task(
            "rel-006",
            "relation_inference",
            KNOWN,
            "Purchase order still implies SUPPLIES_TO.",
            NB_REL,
            {
                "record": {
                    "from": "Wayne Fasteners Ltd",
                    "to": "Globex Manufacturing",
                    "doc": "PO-88",
                }
            },
            {"adds": [triple(wayne, SUP, globex)]},
        ),
        task(
            "rel-007",
            "relation_inference",
            UNSEEN,
            "Supplier stored at a 3PL. Extend + LOCATED_IN.",
            NB_LOC,
            {
                "record": {
                    "org": "Acme Components Ltd",
                    "site": "Hull 3PL warehouse",
                }
            },
            {
                "type_extensions": [
                    ext("ThirdPartyWarehouse", "Location", "3PL warehouse")
                ],
                "adds": [triple(acme, LOC, hull)],
            },
        ),
        task(
            "rel-008",
            "relation_inference",
            UNSEEN,
            "Bonded shed storage.",
            NB_LOC,
            {
                "record": {
                    "org": "Northwind Parts",
                    "site": "Felixstowe bonded shed",
                }
            },
            {
                "type_extensions": [
                    ext("BondedWarehouse", "Location", "bonded warehouse")
                ],
                "adds": [triple(nw, LOC, felix)],
            },
        ),
        task(
            "rel-009",
            "relation_inference",
            ADV,
            "Human named as vendor on an invoice: EMPLOYS, not Person-sourced SUPPLIES_TO.",
            {
                "type_ids": ["Person", "Supplier"],
                "relation_ids": ["EMPLOYS", "SUPPLIES_TO"],
                "include_ancestors": True,
                "include_incident_relations": True,
            },
            {
                "record": {
                    "vendor_name": "Jamie Lee",
                    "employer": "Acme Components Ltd",
                    "customer": "Globex Manufacturing",
                }
            },
            {
                "adds": [triple(acme, EMP, jamie), triple(acme, SUP, globex)],
            },
        ),
        task(
            "rel-010",
            "relation_inference",
            ADV,
            "Row swaps bill-to into the vendor column. Direction is still Supplier→Customer.",
            NB_REL,
            {
                "record": {
                    "vendor": "Soylent Packaging",
                    "vendor_role": "bill-to",
                    "true_supplier": "Acme Components Ltd",
                }
            },
            {"adds": [triple(acme, SUP, soylent)]},
        ),
    ]


def cvr() -> list[dict]:
    jamie = e("jamie-lee")
    globex = e("globex-manufacturing")
    acme = e("acme-components")
    riley = e("riley-chen")
    soylent = e("soylent-packaging")
    initech = e("initech-ltd")
    hull = e("hull-3pl")
    leeds = e("leeds-depot")
    felix = e("felixstowe-bonded")
    return [
        task(
            "cvr-001",
            "constraint_violation_repair",
            ADV,
            "A Person must not source SUPPLIES_TO. Repair deletes the illegal edge.",
            {
                "type_ids": ["Person", "Supplier"],
                "relation_ids": ["SUPPLIES_TO", "EMPLOYS"],
                "include_ancestors": True,
                "include_incident_relations": True,
            },
            {
                "constraints": ["person_not_source_supplies_to"],
                "existing": {"entity": jamie, "type_id": "Person"},
                "illegal": {
                    "subject": jamie,
                    "predicate": SUP,
                    "object": globex,
                },
                "graph": {
                    "type_assertions": [ta(jamie, "Person")],
                    "adds": [triple(jamie, SUP, globex)],
                },
            },
            {
                "deletes": [triple(jamie, SUP, globex)],
                "constraint_repairs": ["drop_supplies_to_from_person:jamie-lee"],
            },
        ),
        task(
            "cvr-002",
            "constraint_violation_repair",
            ADV,
            "Negative qty is illegal. Drop the qty literal.",
            NB_REL,
            {
                "constraints": ["qty_non_negative"],
                "graph": {
                    "type_assertions": [ta(acme, "Supplier"), ta(globex, "Customer")],
                    "adds": [triple(acme, SUP, globex)],
                    "literals": [lit(acme, "qty", "-12")],
                },
            },
            {
                "deletes": [],
                "literals": [lit(acme, "qty", "0")],
                "constraint_repairs": ["clamp_qty_non_negative:acme-components"],
            },
        ),
        task(
            "cvr-003",
            "constraint_violation_repair",
            ADV,
            "registrationId must not sit on a Person.",
            NB_PERSON,
            {
                "constraints": ["registration_id_not_on_person"],
                "graph": {
                    "type_assertions": [ta(riley, "Person")],
                    "literals": [lit(riley, "registrationId", "GB-000111222")],
                },
            },
            {
                "literals": [
                    lit(riley, "registrationId", ""),
                    lit(acme, "registrationId", "GB-000111222"),
                ],
                "constraint_repairs": ["move_registration_id_off_person:riley-chen"],
            },
        ),
        task(
            "cvr-004",
            "constraint_violation_repair",
            ADV,
            "Customer must not source SUPPLIES_TO.",
            NB_REL,
            {
                "constraints": ["supplies_to_source_is_supplier"],
                "graph": {
                    "type_assertions": [
                        ta(soylent, "Customer"),
                        ta(acme, "Supplier"),
                    ],
                    "adds": [triple(soylent, SUP, acme)],
                },
            },
            {
                "deletes": [triple(soylent, SUP, acme)],
                "adds": [triple(acme, SUP, soylent)],
                "constraint_repairs": ["flip_supplies_to_direction:soylent-packaging"],
            },
        ),
        task(
            "cvr-005",
            "constraint_violation_repair",
            ADV,
            "EMPLOYS target must be a Person, not a Company.",
            {
                "type_ids": ["Organization", "Person", "Company"],
                "relation_ids": ["EMPLOYS"],
                "include_ancestors": True,
                "include_incident_relations": True,
            },
            {
                "constraints": ["employs_target_is_person"],
                "graph": {
                    "type_assertions": [
                        ta(acme, "Supplier"),
                        ta(initech, "Company"),
                    ],
                    "adds": [triple(acme, EMP, initech)],
                },
            },
            {
                "deletes": [triple(acme, EMP, initech)],
                "constraint_repairs": ["drop_employs_non_person:initech-ltd"],
            },
        ),
        task(
            "cvr-006",
            "constraint_violation_repair",
            ADV,
            "Person-sourced edge and negative qty together.",
            {
                "type_ids": ["Person", "Supplier"],
                "relation_ids": ["SUPPLIES_TO"],
                "include_ancestors": True,
                "include_incident_relations": True,
            },
            {
                "constraints": [
                    "person_not_source_supplies_to",
                    "qty_non_negative",
                ],
                "graph": {
                    "type_assertions": [ta(jamie, "Person")],
                    "adds": [triple(jamie, SUP, globex)],
                    "literals": [lit(jamie, "qty", "-4")],
                },
            },
            {
                "deletes": [triple(jamie, SUP, globex)],
                "literals": [lit(jamie, "qty", "0")],
                "constraint_repairs": [
                    "drop_supplies_to_from_person:jamie-lee",
                    "clamp_qty_non_negative:jamie-lee",
                ],
            },
        ),
        task(
            "cvr-007",
            "constraint_violation_repair",
            UNSEEN,
            "A 3PL site typed as Supplier and sourcing SUPPLIES_TO. Retype via extension; drop the edge.",
            NB_LOC,
            {
                "constraints": ["supplies_to_source_is_supplier"],
                "graph": {
                    "type_assertions": [ta(hull, "Supplier")],
                    "adds": [triple(hull, SUP, globex)],
                },
            },
            {
                "type_extensions": [
                    ext("ThirdPartyWarehouse", "Location", "3PL warehouse")
                ],
                "type_assertions": [ta(hull, "ThirdPartyWarehouse")],
                "deletes": [triple(hull, SUP, globex)],
                "constraint_repairs": ["drop_supplies_to_from_3pl:hull-3pl"],
            },
        ),
        task(
            "cvr-008",
            "constraint_violation_repair",
            UNSEEN,
            "Location must not EMPLOYS. Drop the edge; bonded extension is the site type.",
            NB_LOC,
            {
                "constraints": ["employs_target_is_person"],
                "graph": {
                    "type_assertions": [ta(leeds, "Location"), ta(felix, "Location")],
                    "adds": [triple(leeds, EMP, felix)],
                },
            },
            {
                "type_extensions": [
                    ext("BondedWarehouse", "Location", "bonded warehouse")
                ],
                "deletes": [triple(leeds, EMP, felix)],
                "constraint_repairs": ["drop_employs_from_location:leeds-depot"],
            },
        ),
        task(
            "cvr-009",
            "constraint_violation_repair",
            ADV,
            "Do not launder a Person into Supplier to keep an illegal edge.",
            {
                "type_ids": ["Person", "Supplier"],
                "relation_ids": ["SUPPLIES_TO"],
                "include_ancestors": True,
                "include_incident_relations": True,
            },
            {
                "constraints": ["person_not_source_supplies_to"],
                "graph": {
                    "type_assertions": [ta(jamie, "Person")],
                    "adds": [triple(jamie, SUP, globex)],
                },
            },
            {
                "deletes": [triple(jamie, SUP, globex)],
                "constraint_repairs": ["drop_supplies_to_from_person:jamie-lee"],
            },
        ),
        task(
            "cvr-010",
            "constraint_violation_repair",
            KNOWN,
            "qty -1 on an otherwise legal Supplier→Customer edge. Clamp qty.",
            NB_REL,
            {
                "constraints": ["qty_non_negative", "supplies_to_source_is_supplier"],
                "graph": {
                    "type_assertions": [ta(acme, "Supplier"), ta(globex, "Customer")],
                    "adds": [triple(acme, SUP, globex)],
                    "literals": [lit(acme, "qty", "-1")],
                },
            },
            {
                "literals": [lit(acme, "qty", "0")],
                "constraint_repairs": ["clamp_qty_non_negative:acme-components"],
            },
        ),
    ]


def conflict() -> list[dict]:
    acme = e("acme-components")
    nw = e("northwind-parts")
    globex = e("globex-manufacturing")
    oscorp = e("oscorp-resins")
    massive = e("massive-dynamic")
    wayne = e("wayne-fasteners")
    hull = e("hull-3pl")
    felix = e("felixstowe-bonded")
    hooli = e("hooli-components")
    stark = e("stark-bolts")
    return [
        task(
            "conf-001",
            "conflict_resolution",
            ADV,
            "Two legal names; registry provenance wins. Nickname becomes alias.",
            NB_SUP,
            {
                "entity": acme,
                "claims": [
                    {
                        "attr": "legalName",
                        "value": "Acme Components Ltd",
                        "provenance": "companies-house",
                    },
                    {
                        "attr": "legalName",
                        "value": "Acme",
                        "provenance": "email-signature",
                    },
                ],
            },
            {
                "literals": [
                    lit(acme, "legalName", "Acme Components Ltd"),
                    lit(acme, "alias", "Acme"),
                ]
            },
        ),
        task(
            "conf-002",
            "conflict_resolution",
            ADV,
            "Two registration ids; companies-house wins.",
            NB_CO,
            {
                "entity": nw,
                "claims": [
                    {
                        "attr": "registrationId",
                        "value": "GB-333444555",
                        "provenance": "companies-house",
                    },
                    {
                        "attr": "registrationId",
                        "value": "GB-000000000",
                        "provenance": "spreadsheet",
                    },
                ],
            },
            {"literals": [lit(nw, "registrationId", "GB-333444555")]},
        ),
        task(
            "conf-003",
            "conflict_resolution",
            KNOWN,
            "HQ name from registry over a sales-sheet nickname for the site.",
            NB_ORG,
            {
                "entity": globex,
                "claims": [
                    {
                        "attr": "legalName",
                        "value": "Globex Manufacturing",
                        "provenance": "companies-house",
                    },
                    {
                        "attr": "legalName",
                        "value": "Globex Leeds",
                        "provenance": "sales-sheet",
                    },
                ],
            },
            {
                "literals": [
                    lit(globex, "legalName", "Globex Manufacturing"),
                    lit(globex, "alias", "Globex Leeds"),
                ]
            },
        ),
        task(
            "conf-004",
            "conflict_resolution",
            KNOWN,
            "Alias claim must not overwrite legalName.",
            NB_ORG,
            {
                "entity": oscorp,
                "claims": [
                    {
                        "attr": "legalName",
                        "value": "Oscorp Resins GmbH",
                        "provenance": "handelsregister",
                    },
                    {
                        "attr": "alias",
                        "value": "Oscorp",
                        "provenance": "email-signature",
                    },
                ],
            },
            {
                "literals": [
                    lit(oscorp, "legalName", "Oscorp Resins GmbH"),
                    lit(oscorp, "alias", "Oscorp"),
                ]
            },
        ),
        task(
            "conf-005",
            "conflict_resolution",
            KNOWN,
            "Contract incoterms beat an email.",
            NB_SUP,
            {
                "entity": massive,
                "claims": [
                    {
                        "attr": "incoterms",
                        "value": "FOB",
                        "provenance": "signed-contract",
                    },
                    {
                        "attr": "incoterms",
                        "value": "EXW",
                        "provenance": "email",
                    },
                ],
            },
            {"literals": [lit(massive, "incoterms", "FOB")]},
        ),
        task(
            "conf-006",
            "conflict_resolution",
            KNOWN,
            "valid_from from the invoice beats a CRM guess.",
            NB_REL,
            {
                "entity": wayne,
                "claims": [
                    {
                        "attr": "validFrom",
                        "value": "2026-02-01",
                        "provenance": "invoice",
                    },
                    {
                        "attr": "validFrom",
                        "value": "2025-01-01",
                        "provenance": "crm-guess",
                    },
                ],
            },
            {"literals": [lit(wayne, "validFrom", "2026-02-01")]},
        ),
        task(
            "conf-007",
            "conflict_resolution",
            UNSEEN,
            "3PL operator nickname vs site legal name. Site name wins; nickname is alias.",
            NB_LOC,
            {
                "entity": hull,
                "claims": [
                    {
                        "attr": "legalName",
                        "value": "Hull 3PL warehouse",
                        "provenance": "lease",
                    },
                    {
                        "attr": "legalName",
                        "value": "Hull node",
                        "provenance": "driver-app",
                    },
                ],
            },
            {
                "type_extensions": [
                    ext("ThirdPartyWarehouse", "Location", "3PL warehouse")
                ],
                "literals": [
                    lit(hull, "legalName", "Hull 3PL warehouse"),
                    lit(hull, "alias", "Hull node"),
                ],
            },
        ),
        task(
            "conf-008",
            "conflict_resolution",
            UNSEEN,
            "Bonded shed label from customs over a carrier nickname.",
            NB_LOC,
            {
                "entity": felix,
                "claims": [
                    {
                        "attr": "legalName",
                        "value": "Felixstowe bonded shed",
                        "provenance": "hmrc",
                    },
                    {
                        "attr": "legalName",
                        "value": "Felix yard",
                        "provenance": "carrier-scan",
                    },
                ],
            },
            {
                "type_extensions": [
                    ext("BondedWarehouse", "Location", "bonded warehouse")
                ],
                "literals": [
                    lit(felix, "legalName", "Felixstowe bonded shed"),
                    lit(felix, "alias", "Felix yard"),
                ],
            },
        ),
        task(
            "conf-009",
            "conflict_resolution",
            ADV,
            "Two emails agree on a nickname; registry still wins legalName.",
            NB_SUP,
            {
                "entity": hooli,
                "claims": [
                    {
                        "attr": "legalName",
                        "value": "Hooli Components",
                        "provenance": "companies-house",
                    },
                    {
                        "attr": "legalName",
                        "value": "Hooli",
                        "provenance": "email-signature",
                    },
                    {
                        "attr": "legalName",
                        "value": "Hooli",
                        "provenance": "slack",
                    },
                ],
            },
            {
                "literals": [
                    lit(hooli, "legalName", "Hooli Components"),
                    lit(hooli, "alias", "Hooli"),
                ]
            },
        ),
        task(
            "conf-010",
            "conflict_resolution",
            ADV,
            "A later email does not beat an earlier registry legalName.",
            NB_CO,
            {
                "entity": stark,
                "claims": [
                    {
                        "attr": "legalName",
                        "value": "Stark Bolts Inc",
                        "provenance": "companies-house",
                        "time": "2024-01-01",
                    },
                    {
                        "attr": "legalName",
                        "value": "Stark",
                        "provenance": "email-signature",
                        "time": "2026-08-01",
                    },
                ],
            },
            {
                "literals": [
                    lit(stark, "legalName", "Stark Bolts Inc"),
                    lit(stark, "alias", "Stark"),
                ]
            },
        ),
    ]


def extension() -> list[dict]:
    return [
        task(
            "ext-001",
            "ontology_extension",
            UNSEEN,
            "Unseen concept '3PL warehouse'. Gold extends Location; do not invent a second root.",
            NB_LOC,
            {
                "mention": "3PL warehouse",
                "evidence": "third-party logistics site storing inbound parts for a Supplier",
            },
            {
                "type_extensions": [
                    ext("ThirdPartyWarehouse", "Location", "3PL warehouse")
                ]
            },
        ),
        task(
            "ext-002",
            "ontology_extension",
            UNSEEN,
            "Bonded warehouse under Location.",
            NB_LOC,
            {
                "mention": "bonded warehouse",
                "evidence": "customs-bonded store for inbound parts",
            },
            {
                "type_extensions": [
                    ext("BondedWarehouse", "Location", "bonded warehouse")
                ]
            },
        ),
        task(
            "ext-003",
            "ontology_extension",
            UNSEEN,
            "Freight forwarder is an Organization, not a Location.",
            NB_ORG,
            {
                "mention": "freight forwarder",
                "evidence": "books ocean freight on behalf of a Supplier",
            },
            {
                "type_extensions": [
                    ext("FreightForwarder", "Organization", "freight forwarder")
                ]
            },
        ),
        task(
            "ext-004",
            "ontology_extension",
            UNSEEN,
            "Distributor is a Company, not a Customer.",
            NB_CO,
            {
                "mention": "distributor",
                "evidence": "resells components to other companies",
            },
            {
                "type_extensions": [
                    ext("Distributor", "Company", "distributor")
                ]
            },
        ),
        task(
            "ext-005",
            "ontology_extension",
            UNSEEN,
            "Product kit under Product.",
            NB_PROD,
            {
                "mention": "kitted SKU",
                "evidence": "a billed bundle of component SKUs",
            },
            {"type_extensions": [ext("ProductKit", "Product", "product kit")]},
        ),
        task(
            "ext-006",
            "ontology_extension",
            UNSEEN,
            "Factory is a Location.",
            NB_LOC,
            {
                "mention": "assembly plant",
                "evidence": "site where the Customer builds finished goods",
            },
            {"type_extensions": [ext("Factory", "Location", "factory")]},
        ),
        task(
            "ext-007",
            "ontology_extension",
            UNSEEN,
            "Carrier depot is a Location, not a Supplier.",
            NB_LOC,
            {
                "mention": "carrier depot",
                "evidence": "parcel network node, not a selling company",
            },
            {
                "type_extensions": [
                    ext("CarrierDepot", "Location", "carrier depot")
                ]
            },
        ),
        task(
            "ext-008",
            "ontology_extension",
            UNSEEN,
            "Holding company under Company.",
            NB_CO,
            {
                "mention": "holding company",
                "evidence": "parent that owns Initech Ltd and has no invoices",
            },
            {
                "type_extensions": [
                    ext("HoldingCompany", "Company", "holding company")
                ]
            },
        ),
        task(
            "ext-009",
            "ontology_extension",
            ADV,
            "Do not hang a human vendor-contact under Supplier. No new type; Person already exists. Gold is empty extension? Need a GraphDelta — attach nothing new under Person, instead a ContactRole under Person.",
            NB_PERSON,
            {
                "mention": "vendor contact",
                "evidence": "a human AP contact at a Supplier, not a company",
                "trap_parent": "Supplier",
            },
            {
                "type_extensions": [
                    ext("VendorContact", "Person", "vendor contact")
                ]
            },
        ),
        task(
            "ext-010",
            "ontology_extension",
            ADV,
            "Customer warehouse is a Location, not a Customer.",
            NB_LOC,
            {
                "mention": "customer warehouse",
                "evidence": "the buyer's receiving dock, not the buying org",
                "trap_parent": "Customer",
            },
            {
                "type_extensions": [
                    ext("CustomerWarehouse", "Location", "customer warehouse")
                ]
            },
        ),
    ]


def multistep() -> list[dict]:
    acme = e("acme-components")
    globex = e("globex-manufacturing")
    umbrella = e("umbrella-parts")
    stark = e("stark-bolts")
    riley = e("riley-chen")
    initech = e("initech-ltd")
    initech_b = e("initech-holdings")
    wayne = e("wayne-fasteners")
    hull = e("hull-3pl")
    felix = e("felixstowe-bonded")
    jamie = e("jamie-lee")
    nw = e("northwind-parts")
    return [
        task(
            "ms-001",
            "multi_step_ingest",
            KNOWN,
            "Ingest → map → resolve → normalize → mutate. Already-known survivor URI.",
            NB_SUP,
            {
                "row": {
                    "Vendor": "acme components",
                    "VAT": "GB-000111222",
                    "ShipsTo": "Globex Manufacturing",
                    "Qty": "15",
                },
                "existing_uris": {"GB-000111222": acme},
            },
            {
                "type_assertions": [ta(acme, "Supplier")],
                "literals": [
                    lit(acme, "legalName", "Acme Components Ltd"),
                    lit(acme, "registrationId", "GB-000111222"),
                ],
                "adds": [triple(acme, SUP, globex)],
            },
        ),
        task(
            "ms-002",
            "multi_step_ingest",
            KNOWN,
            "New vendor, mint URI, type Supplier, add SUPPLIES_TO.",
            NB_SUP,
            {
                "row": {
                    "Vendor": "Umbrella Parts",
                    "VAT": "GB-111000222",
                    "ShipsTo": "Globex Manufacturing",
                },
                "existing_uris": {},
            },
            {
                "type_assertions": [ta(umbrella, "Supplier")],
                "literals": [
                    lit(umbrella, "legalName", "Umbrella Parts"),
                    lit(umbrella, "registrationId", "GB-111000222"),
                ],
                "adds": [triple(umbrella, SUP, globex)],
            },
        ),
        task(
            "ms-003",
            "multi_step_ingest",
            KNOWN,
            "EIN maps to registrationId and resolves to the known survivor.",
            NB_SUP,
            {
                "row": {"Name": "stark bolts", "EIN": "12-3456789"},
                "existing_uris": {"12-3456789": stark},
            },
            {
                "type_assertions": [ta(stark, "Supplier")],
                "literals": [
                    lit(stark, "legalName", "Stark Bolts Inc"),
                    lit(stark, "registrationId", "12-3456789"),
                ],
            },
        ),
        task(
            "ms-004",
            "multi_step_ingest",
            KNOWN,
            "Contact column mints a Person and EMPLOYS from the Supplier.",
            {
                "type_ids": ["Supplier", "Person"],
                "relation_ids": ["EMPLOYS"],
                "include_ancestors": True,
                "include_incident_relations": True,
            },
            {
                "row": {
                    "Vendor": "Acme Components Ltd",
                    "VAT": "GB-000111222",
                    "Contact": "Riley Chen",
                },
                "existing_uris": {"GB-000111222": acme},
            },
            {
                "type_assertions": [ta(acme, "Supplier"), ta(riley, "Person")],
                "adds": [triple(acme, EMP, riley)],
            },
        ),
        task(
            "ms-005",
            "multi_step_ingest",
            KNOWN,
            "Subsidiary row writes SUBSIDIARY_OF.",
            {
                "type_ids": ["Company"],
                "relation_ids": ["SUBSIDIARY_OF"],
                "include_ancestors": True,
                "include_incident_relations": True,
            },
            {
                "row": {"Child": "Initech Ltd", "Parent": "Initech Holdings"},
                "existing_uris": {},
            },
            {
                "type_assertions": [
                    ta(initech, "Company"),
                    ta(initech_b, "Company"),
                ],
                "adds": [triple(initech, SUB, initech_b)],
            },
        ),
        task(
            "ms-006",
            "multi_step_ingest",
            KNOWN,
            "Dirty name + VAT merge + SUPPLIES_TO.",
            NB_SUP,
            {
                "row": {
                    "Vendor": "WAYNE fasteners",
                    "VAT": "00998877",
                    "Customer": "Globex Manufacturing",
                },
                "existing_uris": {"00998877": wayne},
            },
            {
                "type_assertions": [ta(wayne, "Supplier")],
                "literals": [
                    lit(wayne, "legalName", "Wayne Fasteners Ltd"),
                    lit(wayne, "registrationId", "00998877"),
                ],
                "adds": [triple(wayne, SUP, globex)],
            },
        ),
        task(
            "ms-007",
            "multi_step_ingest",
            UNSEEN,
            "3PL site ingest: extend, type, LOCATED_IN.",
            NB_LOC,
            {
                "row": {
                    "Vendor": "Acme Components Ltd",
                    "Site": "Hull 3PL warehouse",
                },
                "existing_uris": {"GB-000111222": acme},
            },
            {
                "type_extensions": [
                    ext("ThirdPartyWarehouse", "Location", "3PL warehouse")
                ],
                "type_assertions": [ta(hull, "ThirdPartyWarehouse")],
                "adds": [triple(acme, LOC, hull)],
            },
        ),
        task(
            "ms-008",
            "multi_step_ingest",
            UNSEEN,
            "Bonded shed ingest.",
            NB_LOC,
            {
                "row": {
                    "Vendor": "Northwind Parts",
                    "Site": "Felixstowe bonded shed",
                },
                "existing_uris": {},
            },
            {
                "type_extensions": [
                    ext("BondedWarehouse", "Location", "bonded warehouse")
                ],
                "type_assertions": [
                    ta(nw, "Supplier"),
                    ta(felix, "BondedWarehouse"),
                ],
                "adds": [triple(nw, LOC, felix)],
            },
        ),
        task(
            "ms-009",
            "multi_step_ingest",
            ADV,
            "Person in the vendor column: type Person, EMPLOYS from the company, company SUPPLIES_TO.",
            {
                "type_ids": ["Person", "Supplier"],
                "relation_ids": ["EMPLOYS", "SUPPLIES_TO"],
                "include_ancestors": True,
                "include_incident_relations": True,
            },
            {
                "row": {
                    "Vendor": "Jamie Lee",
                    "Company": "Acme Components Ltd",
                    "VAT": "GB-000111222",
                    "ShipsTo": "Globex Manufacturing",
                },
                "existing_uris": {"GB-000111222": acme},
            },
            {
                "type_assertions": [ta(jamie, "Person"), ta(acme, "Supplier")],
                "adds": [triple(acme, EMP, jamie), triple(acme, SUP, globex)],
            },
        ),
        task(
            "ms-010",
            "multi_step_ingest",
            ADV,
            "Negative qty is dropped; still resolve and type the Supplier.",
            NB_SUP,
            {
                "row": {
                    "Vendor": "acme components",
                    "VAT": "GB-000111222",
                    "ShipsTo": "Globex Manufacturing",
                    "Qty": "-9",
                },
                "existing_uris": {"GB-000111222": acme},
            },
            {
                "type_assertions": [ta(acme, "Supplier")],
                "literals": [
                    lit(acme, "legalName", "Acme Components Ltd"),
                    lit(acme, "registrationId", "GB-000111222"),
                ],
                "adds": [triple(acme, SUP, globex)],
            },
        ),
    ]


def main() -> None:
    dest = Path(__file__).resolve().parents[1] / "fixtures" / "tasks.jsonl"
    rows = build()
    dest.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(f"wrote {len(rows)} tasks to {dest}")


if __name__ == "__main__":
    main()
