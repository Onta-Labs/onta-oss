"""Tiny synthetic mix: two seen services, one unseen. Not an SGD score."""

from __future__ import annotations

from sgd_binder.instances import Instance
from sgd_binder.schema import TypeCatalog, build_catalog


def fixture_schemas() -> tuple[list[dict], list[dict]]:
    train = [
        {
            "service_name": "Widget_1",
            "slots": [
                {"name": "widget_id", "description": "id of the widget"},
                {"name": "widget_name", "description": "name of the widget"},
            ],
        },
        {
            "service_name": "Gadget_1",
            "slots": [
                {"name": "gadget_id", "description": "id of the gadget"},
                {"name": "gadget_color", "description": "color of the gadget"},
            ],
        },
    ]
    test = train + [
        {
            "service_name": "Sprocket_1",
            "slots": [
                {"name": "sprocket_id", "description": "id of the sprocket"},
                {"name": "sprocket_size", "description": "size of the sprocket"},
            ],
        }
    ]
    return train, test


def fixture_catalog() -> TypeCatalog:
    train, test = fixture_schemas()
    return build_catalog(train_schemas=train, test_schemas=test)


def fixture_instances(catalog: TypeCatalog) -> list[Instance]:
    w = catalog.by_service["Widget_1"]
    g = catalog.by_service["Gadget_1"]
    s = catalog.by_service["Sprocket_1"]
    return [
        Instance(
            "d1::Widget_1",
            "Widget_1",
            w.type_id,
            True,
            "USER: I need the gasket widget please.",
            {"widget_id": "W-200", "widget_name": "gasket"},
        ),
        Instance(
            "d2::Gadget_1",
            "Gadget_1",
            g.type_id,
            True,
            "USER: invoice the blue gadget.",
            {"gadget_id": "G-9", "gadget_color": "blue"},
        ),
        Instance(
            "d3::Sprocket_1",
            "Sprocket_1",
            s.type_id,
            False,
            "USER: the large sprocket is due.",
            {"sprocket_id": "S-1", "sprocket_size": "large"},
        ),
    ]


class StubClient:
    """Fixture-only. Not a published score."""

    def complete(self, *, system: str, user: str) -> str:
        text = user.lower()
        if "Pick exactly one type id" in system or "Reply with exactly one id" in system:
            cat = fixture_catalog()
            if "gasket" in text or "widget" in text:
                return cat.by_service["Widget_1"].type_id
            if "gadget" in text or "invoice" in text:
                return cat.by_service["Gadget_1"].type_id
            return cat.by_service["Sprocket_1"].type_id
        if "widget_id" in system or "gasket" in text:
            return '{"widget_id": "W-200", "widget_name": "gasket"}'
        if "gadget_id" in system or "gadget" in text:
            return '{"gadget_id": "G-9", "gadget_color": "blue"}'
        return '{"sprocket_id": "S-1", "sprocket_size": "large"}'
