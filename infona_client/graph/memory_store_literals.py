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

    def _current_denorm_literal(
        self,
        tenant_id: str,
        kg: str,
        subject: str,
        prop_key: str,
        value: Any,
    ) -> Any:
        """Entity-cache literal with closed validity terms removed."""
        from infona_client.graph.current_facts import drop_closed_value

        return drop_closed_value(
            value, self._closed_terms_for_prop(tenant_id, kg, subject, prop_key)
        )

    def _literal_values_eq(
        self,
        tenant_id: str,
        kg: str,
        type_names: Any,
        prop_key: str,
        prop_value: Any,
        limit: int | None,
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
            if not self._value_is_current(
                tenant_id, kg, a.subject_id, prop_key, a.literal_value
            ):
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
            if limit is not None and len(out) >= limit:
                return out
        # Secondary: Entity property cache (dual-written after Assertion).
        for eid in sorted(matched):
            if eid in seen:
                continue
            r = self._entities.get((tenant_id, kg, eid))
            if r is None:
                continue
            actual = self._current_denorm_literal(
                tenant_id, kg, eid, prop_key, self._entity_prop_value(r, prop_key)
            )
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
            if limit is not None and len(out) >= limit:
                break
        return out

    def _literal_values_count(
        self,
        tenant_id: str,
        kg: str,
        type_names: Any,
        prop_key: str,
        prop_value: Any,
    ) -> list[GraphRecord]:
        """Uncapped equality count — never use the list helper's default 25."""
        rows = self._literal_values_eq(
            tenant_id, kg, type_names, prop_key, prop_value, limit=None
        )
        return [GraphRecord(data={"n": len(rows)})]

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
            if not self._value_is_current(
                tenant_id, kg, a.subject_id, prop_key, a.literal_value
            ):
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
            raw = self._current_denorm_literal(
                tenant_id, kg, eid, prop_key, self._entity_prop_value(r, prop_key)
            )
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
            if not self._value_is_current(
                tenant_id, kg, a.subject_id, prop_key, a.literal_value
            ):
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
            raw = self._current_denorm_literal(
                tenant_id, kg, eid, prop_key, self._entity_prop_value(r, prop_key)
            )
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

    def _literal_argmax_by_dim(
        self,
        tenant_id: str,
        kg: str,
        type_names: Any,
        group_key: str,
        prop_key: str,
    ) -> list[GraphRecord]:
        """Uncapped group-by SUM, then top-1 dim value."""
        from infona_client.graph.assertion_model import property_uri

        if not group_key or not prop_key:
            return []
        matched = self._entity_ids_via_instance_of(tenant_id, kg, type_names)
        meas_id = property_uri(prop_key)
        totals: dict[str, float] = {}
        seen: set[str] = set()

        def _grp_of(eid: str) -> str:
            r = self._entities.get((tenant_id, kg, eid))
            if r is None:
                return ""
            raw = self._current_denorm_literal(
                tenant_id, kg, eid, group_key, self._entity_prop_value(r, group_key)
            )
            if raw is None:
                return ""
            return str(raw).strip()

        for (t, k, _), a in self._assertions.items():
            if t != tenant_id or k != kg:
                continue
            if a.subject_id not in matched or a.literal_value is None:
                continue
            if a.property_id != meas_id:
                prop_row = self._properties.get((tenant_id, kg, a.property_id))
                if prop_row is None or prop_row.name != prop_key:
                    continue
            if not self._value_is_current(
                tenant_id, kg, a.subject_id, prop_key, a.literal_value
            ):
                continue
            num = self._to_float_legacy(a.literal_value)
            if num is None or a.subject_id in seen:
                continue
            grp = _grp_of(a.subject_id)
            if not grp:
                continue
            seen.add(a.subject_id)
            totals[grp] = totals.get(grp, 0.0) + num

        for eid in matched:
            if eid in seen:
                continue
            r = self._entities.get((tenant_id, kg, eid))
            if r is None:
                continue
            num = self._to_float_legacy(
                self._current_denorm_literal(
                    tenant_id, kg, eid, prop_key, self._entity_prop_value(r, prop_key)
                )
            )
            if num is None:
                continue
            grp = _grp_of(eid)
            if not grp:
                continue
            seen.add(eid)
            totals[grp] = totals.get(grp, 0.0) + num

        if not totals:
            return []
        winner = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        return [GraphRecord(data={"name": winner[0], "value": winner[1]})]

    def _literal_distinct_count(
        self,
        tenant_id: str,
        kg: str,
        type_names: Any,
        prop_key: str,
    ) -> list[GraphRecord]:
        """Uncapped count of distinct non-empty values of a datatype leaf."""
        from infona_client.graph.assertion_model import property_uri

        if not prop_key:
            return [GraphRecord(data={"n": 0})]
        matched = self._entity_ids_via_instance_of(tenant_id, kg, type_names)
        prop_id = property_uri(prop_key)
        per_entity: dict[str, str] = {}

        def _norm(raw: Any) -> str:
            if raw is None:
                return ""
            text = str(raw).strip()
            if "^^" in text:
                text = text.split("^^", 1)[0].strip()
            return text

        for (t, k, _), a in self._assertions.items():
            if t != tenant_id or k != kg:
                continue
            if a.subject_id not in matched or a.literal_value is None:
                continue
            if a.property_id != prop_id:
                prop_row = self._properties.get((tenant_id, kg, a.property_id))
                if prop_row is None or prop_row.name != prop_key:
                    continue
            if not self._value_is_current(
                tenant_id, kg, a.subject_id, prop_key, a.literal_value
            ):
                continue
            val = _norm(a.literal_value)
            if not val or a.subject_id in per_entity:
                continue
            per_entity[a.subject_id] = val

        for eid in matched:
            if eid in per_entity:
                continue
            r = self._entities.get((tenant_id, kg, eid))
            if r is None:
                continue
            val = _norm(
                self._current_denorm_literal(
                    tenant_id, kg, eid, prop_key, self._entity_prop_value(r, prop_key)
                )
            )
            if val:
                per_entity[eid] = val

        return [GraphRecord(data={"n": len(set(per_entity.values()))})]
