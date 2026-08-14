"""Ontology catalog (E4) for the in-memory store.

``:OntoType`` / ``:OntoAttr`` rows plus hierarchy / DECLARES / RANGE_TYPE
and the Class dual-write that ADR 0013 prefers for NL membership.
"""

from __future__ import annotations

from typing import Any

from infona_client.graph.memory_store_rows import _OntoAttrRow, _OntoTypeRow
from infona_client.graph.store import GraphRecord


class MemoryOntologyMixin:
    """OntoType / OntoAttr catalog mutations."""

    def _upsert_onto_type(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        name: str,
        *,
        description: str = "",
        description_updated_at: str | None = None,
        description_provided: bool = False,
        label_token: str | None = None,
        uri: str | None = None,
    ) -> list[GraphRecord]:
        from infona_client.graph.ontology_queries import type_uri

        key = (tenant_id, kg, layer, name)
        class_id = uri or type_uri(name)
        existing = self._onto_types.get(key)
        dua = (
            str(description_updated_at)
            if description_updated_at
            else None
        )
        if existing is None:
            row = _OntoTypeRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                name=name,
                description=description or "",
                description_updated_at=dua,
                label_token=label_token,
                uri=class_id,
            )
            self._onto_types[key] = row
        else:
            if not (existing.description or "").strip() and description:
                existing.description = description
                if dua:
                    existing.description_updated_at = dua
            elif description_provided and description and description != existing.description:
                existing.description = description
                if dua:
                    existing.description_updated_at = dua
            elif existing.description_updated_at is None and dua:
                existing.description_updated_at = dua
            if label_token is not None:
                existing.label_token = label_token
            if uri is not None:
                existing.uri = uri
            elif existing.uri is None:
                existing.uri = class_id
            row = existing
            class_id = row.uri or class_id
        # ADR 0013 dual-write: Class node (id = type IRI) alongside OntoType.
        self._merge_class(tenant_id, kg, class_id, name=name, layer=layer)
        return [row.as_record()]

    def _set_subclass(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        name: str,
        parent_name: str,
        parent_label_token: str | None,
    ) -> list[GraphRecord]:
        from infona_client.graph.ontology_queries import type_uri

        child_key = (tenant_id, kg, layer, name)
        child = self._onto_types.get(child_key)
        if child is None:
            return []
        parent_key = (tenant_id, kg, layer, parent_name)
        if parent_key not in self._onto_types:
            self._onto_types[parent_key] = _OntoTypeRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                name=parent_name,
                label_token=parent_label_token,
                uri=type_uri(parent_name),
            )
        child.parent_type = parent_name
        # Dual-write Class SUBCLASS_OF (preferred NL hierarchy).
        child_id = child.uri or type_uri(name)
        parent_row = self._onto_types[parent_key]
        parent_id = parent_row.uri or type_uri(parent_name)
        self._merge_class(tenant_id, kg, child_id, name=name, layer=layer)
        self._merge_class(
            tenant_id, kg, parent_id, name=parent_name, layer=layer
        )
        self._set_class_subclass(tenant_id, kg, child_id, parent_id)
        return [
            GraphRecord(data={"name": name, "parent_type": parent_name})
        ]

    def _clear_subclass(
        self, tenant_id: str, kg: str, layer: str, name: str
    ) -> list[GraphRecord]:
        from infona_client.graph.ontology_queries import type_uri

        child = self._onto_types.get((tenant_id, kg, layer, name))
        if child is None:
            return []
        child.parent_type = None
        child_id = child.uri or type_uri(name)
        self._clear_class_subclass(tenant_id, kg, child_id)
        return [GraphRecord(data={"name": name, "parent_type": None})]

    def _list_onto_types(
        self, tenant_id: str, kg: str, layer: str | None
    ) -> list[GraphRecord]:
        rows: list[_OntoTypeRow] = []
        for (t, k, ly, _n), row in self._onto_types.items():
            if t != tenant_id or k != kg:
                continue
            if layer is not None and ly != layer:
                continue
            rows.append(row)
        rows.sort(key=lambda r: r.name)
        return [r.as_record() for r in rows]

    def _get_onto_type(
        self, tenant_id: str, kg: str, layer: str, name: str
    ) -> list[GraphRecord]:
        row = self._onto_types.get((tenant_id, kg, layer, name))
        return [row.as_record()] if row else []

    def _upsert_onto_attr(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        domain: str,
        name: str,
        *,
        kind: str,
        datatype: str | None,
        range_type: str | None,
        cardinality: str,
        description: str,
        description_updated_at: str | None = None,
        description_provided: bool = False,
        prop_key: str | None,
        domain_label_token: str | None,
        domain_description: str = "",
        domain_description_updated_at: str | None = None,
    ) -> list[GraphRecord]:
        # Ensure domain type exists (DECLARES target) with mandatory description.
        dkey = (tenant_id, kg, layer, domain)
        if dkey not in self._onto_types:
            self._onto_types[dkey] = _OntoTypeRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                name=domain,
                description=domain_description or "",
                description_updated_at=(
                    str(domain_description_updated_at)
                    if domain_description_updated_at
                    else None
                ),
                label_token=domain_label_token,
            )
        elif not (self._onto_types[dkey].description or "").strip() and domain_description:
            self._onto_types[dkey].description = domain_description
            if domain_description_updated_at:
                self._onto_types[dkey].description_updated_at = str(
                    domain_description_updated_at
                )
        akey = (tenant_id, kg, layer, domain, name)
        existing = self._onto_attrs.get(akey)
        dua = str(description_updated_at) if description_updated_at else None
        if existing is None:
            row = _OntoAttrRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                domain=domain,
                name=name,
                kind=kind,
                datatype=datatype,
                range_type=range_type,
                cardinality=cardinality or "1:1",
                description=description or "",
                description_updated_at=dua,
                prop_key=prop_key,
            )
            self._onto_attrs[akey] = row
        else:
            existing.kind = kind
            existing.datatype = datatype
            existing.range_type = range_type
            if cardinality:
                existing.cardinality = cardinality
            if not (existing.description or "").strip() and description:
                existing.description = description
                if dua:
                    existing.description_updated_at = dua
            elif description_provided and description and description != existing.description:
                existing.description = description
                if dua:
                    existing.description_updated_at = dua
            elif existing.description_updated_at is None and dua:
                existing.description_updated_at = dua
            if prop_key is not None:
                existing.prop_key = prop_key
            # text_kind is NOT touched by a general attribute upsert — only
            # set_text_kind / the dedicated template owns that field (ONTA-533).
            row = existing
        return [row.as_record()]

    def _set_onto_attr_text_kind(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        domain: str,
        name: str,
        text_kind: str,
        domain_label_token: str | None,
    ) -> list[GraphRecord]:
        """SET_TEXT_KIND: durable free-text candidacy on :OntoAttr (ONTA-533)."""
        dkey = (tenant_id, kg, layer, domain)
        if dkey not in self._onto_types:
            self._onto_types[dkey] = _OntoTypeRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                name=domain,
                label_token=domain_label_token,
            )
        akey = (tenant_id, kg, layer, domain, name)
        kind_val = (text_kind or "").strip() or None
        existing = self._onto_attrs.get(akey)
        if existing is None:
            row = _OntoAttrRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                domain=domain,
                name=name,
                kind="literal",
                datatype="string",
                cardinality="1:1",
                description="",
                text_kind=kind_val,
            )
            self._onto_attrs[akey] = row
        else:
            existing.text_kind = kind_val
            row = existing
        return [row.as_record()]

    def _set_attr_range_type(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        domain: str,
        name: str,
        range_type: str,
        range_label_token: str | None,
    ) -> list[GraphRecord]:
        akey = (tenant_id, kg, layer, domain, name)
        attr = self._onto_attrs.get(akey)
        if attr is None:
            return []
        rkey = (tenant_id, kg, layer, range_type)
        if rkey not in self._onto_types:
            self._onto_types[rkey] = _OntoTypeRow(
                tenant_id=tenant_id,
                kg=kg,
                layer=layer,
                name=range_type,
                label_token=range_label_token,
            )
        attr.range_type = range_type
        attr.kind = "relationship"
        return [GraphRecord(data={"name": name, "range_type": range_type})]

    def _list_onto_attrs(
        self,
        tenant_id: str,
        kg: str,
        domain: str | None,
        layer: str | None,
    ) -> list[GraphRecord]:
        rows: list[_OntoAttrRow] = []
        for (t, k, ly, dom, _n), row in self._onto_attrs.items():
            if t != tenant_id or k != kg:
                continue
            if layer is not None and ly != layer:
                continue
            if domain is not None and dom != domain:
                continue
            rows.append(row)
        rows.sort(key=lambda r: (r.domain, r.name))
        return [r.as_record() for r in rows]

    def _delete_onto_attr(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        domain: str,
        name: str,
    ) -> list[GraphRecord]:
        akey = (tenant_id, kg, layer, domain, name)
        if akey not in self._onto_attrs:
            return []
        del self._onto_attrs[akey]
        return [GraphRecord(data={"name": name, "domain": domain})]

    def _delete_onto_type(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        name: str,
    ) -> list[GraphRecord]:
        tkey = (tenant_id, kg, layer, name)
        if tkey not in self._onto_types:
            return []
        del self._onto_types[tkey]
        # Drop Class dual-write row when present.
        from infona_client.graph.ontology_queries import type_uri

        uri = type_uri(name)
        self._classes.pop((tenant_id, kg, uri), None)
        self._subclass_of.pop((tenant_id, kg, uri), None)
        return [GraphRecord(data={"name": name})]

    def _set_onto_attr_markers(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        domain: str,
        name: str,
        *,
        core_slot: Any = None,
        text_kind: Any = None,
        clear_text_kind: bool = False,
        deprecated_at: Any = None,
        superseded_by: Any = None,
        clear_deprecation: bool = False,
    ) -> list[GraphRecord]:
        akey = (tenant_id, kg, layer, domain, name)
        attr = self._onto_attrs.get(akey)
        if attr is None:
            return []
        if core_slot is not None:
            attr.core_slot = bool(core_slot)
        if clear_text_kind:
            attr.text_kind = None
        elif text_kind is not None:
            attr.text_kind = str(text_kind) if text_kind != "" else None
        if clear_deprecation:
            attr.deprecated_at = None
            attr.superseded_by = None
        else:
            if deprecated_at is not None:
                attr.deprecated_at = str(deprecated_at)
            if superseded_by is not None:
                attr.superseded_by = str(superseded_by) if superseded_by else None
        return [attr.as_record()]

    def _set_onto_type_markers(
        self,
        tenant_id: str,
        kg: str,
        layer: str,
        name: str,
        *,
        description: Any = None,
        deprecated_at: Any = None,
        superseded_by: Any = None,
        clear_deprecation: bool = False,
    ) -> list[GraphRecord]:
        tkey = (tenant_id, kg, layer, name)
        row = self._onto_types.get(tkey)
        if row is None:
            return []
        if description is not None:
            row.description = str(description)
        if clear_deprecation:
            row.deprecated_at = None
            row.superseded_by = None
        else:
            if deprecated_at is not None:
                row.deprecated_at = str(deprecated_at)
            if superseded_by is not None:
                row.superseded_by = str(superseded_by) if superseded_by else None
        return [row.as_record()]
