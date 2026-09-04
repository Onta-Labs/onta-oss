"""Catalog and skills must not contain SGD service_name strings."""

from sgd_binder.fixtures import fixture_catalog
from sgd_binder.llm import bind_system
from sgd_binder.schema import leak_needles
from sgd_binder.skills import write_skills


def test_catalog_and_skills_omit_service_names() -> None:
    cat = fixture_catalog()
    needles = leak_needles(cat)
    assert "Widget_1" in needles
    blob = bind_system(cat)
    for n in needles:
        assert n.lower() not in blob.lower()
    for skill in write_skills(cat, needles).values():
        for n in needles:
            assert n.lower() not in skill.body.lower()
        assert "type_" in skill.type_id
