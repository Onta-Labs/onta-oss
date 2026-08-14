"""Literal equality / compare / aggregate + related-name filter.

Assertion literal values are the source of truth; the Entity property
cache is a secondary dual-write. Numeric compare strips SPARQL-era
``^^xsd`` suffixes so hermetic tests match the Neo4j templates.
"""

from __future__ import annotations

from typing import Any

from infona_client.graph.store import GraphRecord


class MemoryLiteralsMixin:
    """Literal match, numeric compare, aggregate, related-name filter."""

    @staticmethod
    def _literal_eq(store_val: Any, query_val: Any) -> bool:
        """Equality with SPARQL-era ``lexical^^xsd`` stripping + numeric coerce.

        Mirrors LITERAL_VALUES_CYPHER: normalize both sides (strip legacy
        datatype suffixes), then accept raw ==, string form of normalized
        values, or float equality when both coerce to numbers.
        """
        from infona_client.graph.assertion_model import normalize_store_literal

        left = normalize_store_literal(store_val)
        right = normalize_store_literal(query_val)
        if left == right:
            return True
        if left is None or right is None:
            return False
        if str(left).strip() == str(right).strip():
            return True
        if isinstance(left, bool) or isinstance(right, bool):
            return False
        try:
            return float(left) == float(right)
        except (TypeError, ValueError):
            return False

    def _literal_values_eq(
        self,
        tenant_id: str,
        kg: str,
        type_names: Any,
        prop_key: str,
        prop_value: Any,
        limit: int,
    ) -> list[GraphRecord]:
        # Prefer Assertion literal SoT; Entity property cache is secondary.
        matched = self._entity_ids_via_instance_of(tenant_id, kg, type_names)
        from infona_client.graph.assertion_model import property_uri

        prop_id = property_uri(prop_key) if prop_key else None
        out: list[GraphRecord] = []
        seen: set[str] = set()
        for (t, k, _), a in sorted(
            self._assertions.items(), key=lambda x: x[1].subject_id
        ):
            if t != tenant_id or k != kg:
                continue
            if a.subject_id not in matched:
                continue
            if a.literal_value is None:
                continue
            if prop_id is not None and a.property_id != prop_id:
                # Also accept Property catalog name match.
                prop_row = self._properties.get((tenant_id, kg, a.property_id))
                if prop_row is None or prop_row.name != prop_key:
                    continue
            if not self._literal_eq(a.literal_value, prop_value):
                continue
            if a.subject_id in seen:
                continue
            r = self._entities.get((tenant_id, kg, a.subject_id))
            if r is None:
                continue
            seen.add(a.subject_id)
            out.append(
                GraphRecord(
                    data={
                        "id": r.id,
                        "name": r.name,
                        "primary_type": r.primary_type,
                        "literal_value": a.literal_value,
                    }
                )
            )
            if len(out) >= limit:
                return out
        # Secondary: Entity property cache (dual-written after Assertion).
        for eid in sorted(matched):
            if eid in seen:
                continue
            r = self._entities.get((tenant_id, kg, eid))
            if r is None:
                continue
            actual = self._entity_prop_value(r, prop_key)
            if actual is None or not self._literal_eq(actual, prop_value):
                continue
            out.append(
                GraphRecord(
                    data={
                        "id": r.id,
                        "name": r.name,
                        "primary_type": r.primary_type,
                        "literal_value": actual,
                    }
                )
            )
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _to_float_legacy(raw: Any) -> float | None:
        """Coerce store values to float; strip SPARQL-era ``^^xsd`` suffixes."""
        if raw is None:
            return None
        if isinstance(raw, bool):
            return None
        if isinstance(raw, (int, float)):
            return float(raw)
        text = str(raw).strip()
        if "^^" in text:
            text = text.split("^^", 1)[0].strip()
        try:
            return float(text)
        except ValueError:
            return None

    def _literal_compare(
        self,
        tenant_id: str,
        kg: str,
        type_names: Any,
        prop_key: str,
        op: str,
        threshold: Any,
        limit: int,
    ) -> list[GraphRecord]:
        """Numeric inequality over Assertion SoT + Entity property cache."""
        from infona_client.graph.assertion_model import property_uri

        try:
            thr = float(threshold)
        except (TypeError, ValueError):
            return []
        op = (op or "lt").strip().lower()
        matched = self._entity_ids_via_instance_of(tenant_id, kg, type_names)
        prop_id = property_uri(prop_key) if prop_key else None
        rows: list[tuple[float, GraphRecord]] = []
        seen: set[str] = set()

        def _op_ok(num: float) -> bool:
            if op == "lt":
                return num < thr
            if op == "le":
                return num <= thr
            if op == "gt":
                return num > thr
            if op == "ge":
                return num >= thr
            if op == "eq":
                return num == thr
            return False

        for (t, k, _), a in sorted(
            self._assertions.items(), key=lambda x: x[1].subject_id
        ):
            if t != tenant_id or k != kg:
                continue
            if a.subject_id not in matched or a.literal_value is None:
                continue
            if prop_id is not None and a.property_id != prop_id:
                prop_row = self._properties.get((tenant_id, kg, a.property_id))
                if prop_row is None or prop_row.name != prop_key:
                    continue
            num = self._to_float_legacy(a.literal_value)
            if num is None or not _op_ok(num):
                continue
            if a.subject_id in seen:
                continue
            r = self._entities.get((tenant_id, kg, a.subject_id))
            if r is None:
                continue
            seen.add(a.subject_id)
            title = self._entity_prop_value(r, "title") or r.name
            rows.append(
                (
                    num,
                    GraphRecord(
                        data={
                            "id": r.id,
                            "name": r.name,
                            "primary_type": r.primary_type,
                            "title": title,
                            "value": num,
                        }
                    ),
                )
            )

        for eid in sorted(matched):
            if eid in seen:
                continue
            r = self._entities.get((tenant_id, kg, eid))
            if r is None:
                continue
            raw = self._entity_prop_value(r, prop_key)
            num = self._to_float_legacy(raw)
            if num is None or not _op_ok(num):
                continue
            title = self._entity_prop_value(r, "title") or r.name
            rows.append(
                (
                    num,
                    GraphRecord(
                        data={
                            "id": r.id,
                            "name": r.name,
                            "primary_type": r.primary_type,
                            "title": title,
                            "value": num,
                        }
                    ),
                )
            )

        rows.sort(key=lambda x: (x[0], str(x[1].get("id") or "")))
        return [rec for _, rec in rows[:limit]]

    def _related_entity_name_filter(
        self,
        tenant_id: str,
        kg: str,
        type_names: Any,
        rel_attr: str,
        target_name: str,
        limit: int,
    ) -> list[GraphRecord]:
        """Subjects of type(s) linked to a related entity matching display name."""
        if not rel_attr or not target_name:
            return []
        needle = target_name.strip().lower()
        matched = self._entity_ids_via_instance_of(tenant_id, kg, type_names)
        out: list[GraphRecord] = []
        seen: set[str] = set()

        def _name_hits(ent: Any) -> bool:
            display = str(getattr(ent, "display_name", None) or "").strip().lower()
            # Entity cache may store display_name as a property.
            if not display:
                display = str(self._entity_prop_value(ent, "display_name") or "").strip().lower()
            name = str(getattr(ent, "name", None) or "").strip().lower()
            spaced = name.replace("_", " ")
            if needle in (display, name, spaced):
                return True
            # Substring: "Acme" matches "Acme Corp"
            return bool(needle) and (
                needle in display or needle in name or needle in spaced
            )

        for (t, k, _), a in sorted(
            self._assertions.items(), key=lambda x: x[1].subject_id
        ):
            if t != tenant_id or k != kg:
                continue
            if not a.object_id or a.subject_id not in matched:
                continue
            prop_row = self._properties.get((tenant_id, kg, a.property_id))
            prop_name = prop_row.name if prop_row else a.property_id
            if prop_name != rel_attr and a.property_id != rel_attr:
                continue
            target = self._entities.get((tenant_id, kg, a.object_id))
            subj = self._entities.get((tenant_id, kg, a.subject_id))
            if target is None or subj is None or not _name_hits(target):
                continue
            if subj.id in seen:
                continue
            seen.add(subj.id)
            related = (
                self._entity_prop_value(target, "display_name")
                or target.name
            )
            title = self._entity_prop_value(subj, "title") or subj.name
            out.append(
                GraphRecord(
                    data={
                        "id": subj.id,
                        "title": title,
                        "primary_type": subj.primary_type,
                        "related_name": related,
                    }
                )
            )
            if len(out) >= limit:
                return out
        return out


    def _literal_aggregate(
        self,
        tenant_id: str,
        kg: str,
        type_names: Any,
        prop_key: str,
        agg_op: str,
    ) -> list[GraphRecord]:
        """SUM/AVG/MIN/MAX over Assertion SoT + Entity property cache."""
        from infona_client.graph.assertion_model import property_uri

        op = (agg_op or "sum").strip().lower()
        if op not in {"sum", "avg", "min", "max"}:
            return [GraphRecord(data={"value": None})]
        matched = self._entity_ids_via_instance_of(tenant_id, kg, type_names)
        prop_id = property_uri(prop_key) if prop_key else None
        nums: list[float] = []
        seen: set[str] = set()

        for (t, k, _), a in self._assertions.items():
            if t != tenant_id or k != kg:
                continue
            if a.subject_id not in matched or a.literal_value is None:
                continue
            if prop_id is not None and a.property_id != prop_id:
                prop_row = self._properties.get((tenant_id, kg, a.property_id))
                if prop_row is None or prop_row.name != prop_key:
                    continue
            num = self._to_float_legacy(a.literal_value)
            if num is None:
                continue
            if a.subject_id in seen:
                continue
            seen.add(a.subject_id)
            nums.append(num)

        for eid in matched:
            if eid in seen:
                continue
            r = self._entities.get((tenant_id, kg, eid))
            if r is None:
                continue
            raw = self._entity_prop_value(r, prop_key)
            num = self._to_float_legacy(raw)
            if num is None:
                continue
            seen.add(eid)
            nums.append(num)

        if not nums:
            return [GraphRecord(data={"value": None})]
        if op == "sum":
            val = float(sum(nums))
        elif op == "avg":
            val = float(sum(nums)) / len(nums)
        elif op == "min":
            val = float(min(nums))
        else:
            val = float(max(nums))
        return [GraphRecord(data={"value": val})]
