"""ONTA-384 enumeration + scoped-schema graph-profile bar.

Implementation sibling of :mod:`infona_client.qc.tier3_grade`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from infona_client.pipeline.find_metrics import normalize_key
from infona_client.qc.tier3_fixture import Tier3Fixture

# =========================================================================== #
# ONTA-384 — enumeration + scoped-schema graph-profile bar
# =========================================================================== #
#
# Pass thresholds designed so:
#   * today's broken BC-universities profile (~5/40 entities, ~49 attrs,
#     ~17 types with junk Colour/Asset/Online/InstructionMode) FAILS all three
#     headline metrics and their anti-gaming counters;
#   * a post-P1/P2/P5 clean profile (≥50% coverage, attrs ⊆ requested±structural,
#     ≤6 types with no forbidden junk) PASSES.
#
# These numbers are the contract — do not loosen them to green a broken pipeline.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GraphProfileSnapshot:
    """Offline snapshot of what the pipeline produced for an enumeration goal.

    Pure data — the live harness (283-B) or a unit test constructs one from the
    graph / discovery rows. No I/O here.

    * ``entity_keys`` — the identity values of produced entities (the
      ``key_attribute``, typically ``name``). Near-duplicates and off-roster
      noise are kept so the anti-gaming counters can see them.
    * ``attributes`` — distinct attribute *leaves* observed on the produced
      ontology / instance data (not full URIs — ``website``, not
      ``attrs/website``).
    * ``types`` — distinct type labels / local names observed (``University``,
      not the full type URI).
    """

    entity_keys: tuple[str, ...]
    attributes: tuple[str, ...]
    types: tuple[str, ...]

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "GraphProfileSnapshot":
        def _as_str_tuple(key: str) -> tuple[str, ...]:
            raw = d.get(key, ())
            if raw is None:
                return ()
            if isinstance(raw, (str, bytes)):
                return (str(raw),)
            if not isinstance(raw, Sequence):
                return ()
            return tuple(str(x) for x in raw if str(x).strip() or str(x) == "0")

        return cls(
            entity_keys=_as_str_tuple("entity_keys"),
            attributes=_as_str_tuple("attributes"),
            types=_as_str_tuple("types"),
        )


@dataclass(frozen=True)
class ProfileThresholds:
    """Pass/fail bar for the enumeration + scoped-schema profile metrics.

    Floors are lower bounds (≥), ceilings upper bounds (≤). Defaults are the
    **post-P1/P2/P5** pass contract (see module docstring +
    ``POST_FIX_PROFILE_THRESHOLDS``); they deliberately fail the broken profile
    numbers documented as ``BROKEN_BC_PROFILE``.
    """

    # Coverage (P1)
    coverage_floor: float = 0.50
    # Anti-gaming (coverage): off-roster share of produced entities.
    off_roster_ceiling: float = 0.30
    # Anti-gaming (coverage): near-dup collapse rate among produced entity keys.
    near_dup_ceiling: float = 0.15

    # Scope-adherence (P2)
    scope_adherence_floor: float = 0.80
    # Anti-gaming (scope): fraction of requested attrs that must appear at least
    # once — blocks "emit nothing → perfect scope" gaming.
    requested_attr_coverage_floor: float = 0.67
    # Anti-gaming (scope): absolute out-of-scope attr count ceiling.
    max_out_of_scope_attrs: int = 5

    # Fragmentation (P5)
    max_types: int = 6
    # Anti-gaming (fragmentation): forbidden junk types must be absent.
    max_forbidden_types: int = 0
    # Anti-gaming (fragmentation): at least this many allowed types must appear
    # — blocks "emit zero types → type_count=0 passes ceiling" gaming.
    min_allowed_types_present: int = 1

    @classmethod
    def from_dict(cls, d: Optional[Mapping[str, Any]]) -> "ProfileThresholds":
        if not d:
            return cls()
        fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        coerced: dict[str, Any] = {}
        for k, v in d.items():
            if k not in fields:
                continue
            if k in ("max_out_of_scope_attrs", "max_types", "max_forbidden_types",
                     "min_allowed_types_present"):
                coerced[k] = int(v)
            else:
                coerced[k] = float(v)
        return cls(**coerced)


# Documented post-fix pass contract (alias of the defaults for test import).
POST_FIX_PROFILE_THRESHOLDS = ProfileThresholds()

# Today's broken BC-universities profile numbers (regression control). A scorer
# that does not fail this snapshot is broken. Counts are approximate midpoints
# of the live job that motivated ONTA-379/380/382/383/384.
BROKEN_BC_PROFILE = GraphProfileSnapshot(
    # ~5 of ~40 expected institutions.
    entity_keys=(
        "University of British Columbia",
        "Simon Fraser University",
        "University of Victoria",
        "British Columbia Institute of Technology",
        "Langara College",
    ),
    # User asked only name/website/type; pipeline emitted ~49 leaves.
    attributes=(
        "name",
        "website",
        "type",
        "label",
        # 45 fabricated / out-of-scope leaves (attr explosion + invention).
        "online_activity_percentage_of_summer_instruction",
        "affordability_ranking",
        "colour",
        "asset_value",
        "instruction_mode",
        "campus_size_hectares",
        "founded_year_approximate",
        "student_body_diversity_index",
        "parking_spaces",
        "dormitory_capacity",
        "endowment_usd_estimate",
        "research_output_score",
        "alumni_network_size",
        "international_student_pct",
        "acceptance_rate_guess",
        "average_class_size",
        "library_volumes",
        "sports_teams_count",
        "mascot_name",
        "school_colors",
        "motto_latin",
        "president_name",
        "board_chair",
        "accreditation_body",
        "qs_rank_world",
        "times_rank_world",
        "maclean_rank_canada",
        "tuition_domestic_cad",
        "tuition_international_cad",
        "application_fee_cad",
        "sat_required",
        "toefl_min",
        "ielts_min",
        "housing_guarantee",
        "meal_plan_available",
        "wifi_coverage_pct",
        "lab_count",
        "patent_count_annual",
        "startup_spinouts",
        "nobel_affiliates",
        "olympic_athletes",
        "climate_action_score",
        "transit_score",
        "bike_racks",
        "cafeteria_rating",
    ),
    # 17 types incl. junk Colour / Asset / Online / InstructionMode.
    types=(
        "University",
        "College",
        "PublicInstitution",
        "PrivateInstitution",
        "Polytechnic",
        "Institute",
        "CommunityCollege",
        "ArtSchool",
        "ResearchUniversity",
        "TeachingUniversity",
        "Colour",
        "Asset",
        "Online",
        "InstructionMode",
        "Campus",
        "Faculty",
        "Program",
    ),
)


@dataclass
class EnumerationProfileScore:
    """Scored bundle for one enumeration + scoped-schema graph profile."""

    fixture_id: str

    # --- Coverage (P1) ---
    coverage: float
    gold_total: int
    found_gold_entities: int
    produced_entity_total: int
    distinct_produced_entities: int
    off_roster_entities: int
    off_roster_rate: float
    near_dup_collapsed_rows: int
    near_dup_collapse_rate: float
    coverage_ok: bool
    off_roster_ok: bool
    near_dup_ok: bool

    # --- Scope-adherence (P2) ---
    scope_adherence: float
    attribute_total: int
    in_scope_attributes: int
    out_of_scope_attributes: int
    out_of_scope_attr_names: tuple[str, ...]
    requested_attr_coverage: float
    requested_present: int
    requested_total: int
    scope_adherence_ok: bool
    requested_attr_coverage_ok: bool
    out_of_scope_count_ok: bool

    # --- Fragmentation (P5) ---
    type_count: int
    allowed_types_present: int
    forbidden_types_present: int
    forbidden_type_names: tuple[str, ...]
    extra_type_names: tuple[str, ...]
    type_count_ok: bool
    forbidden_types_ok: bool
    allowed_presence_ok: bool

    # Aggregate
    passed: bool
    thresholds: ProfileThresholds

    def failures(self) -> list[str]:
        """Names of the gates this profile violated (empty ⇒ passed)."""
        out: list[str] = []
        if not self.coverage_ok:
            out.append("coverage")
        if not self.off_roster_ok:
            out.append("off_roster")
        if not self.near_dup_ok:
            out.append("near_dup_collapse")
        if not self.scope_adherence_ok:
            out.append("scope_adherence")
        if not self.requested_attr_coverage_ok:
            out.append("requested_attr_coverage")
        if not self.out_of_scope_count_ok:
            out.append("out_of_scope_count")
        if not self.type_count_ok:
            out.append("type_count")
        if not self.forbidden_types_ok:
            out.append("forbidden_types")
        if not self.allowed_presence_ok:
            out.append("allowed_type_presence")
        return out


def _build_alias_map(alias_table: Sequence[tuple[str, str]]) -> dict[str, str]:
    """Normalize both sides of variant→canonical so lookups are case/ws free."""
    return {normalize_key(k): normalize_key(v) for k, v in alias_table}


def _canonical(value: Any, alias_map: Mapping[str, str]) -> str:
    nk = normalize_key(value)
    return alias_map.get(nk, nk)


def grade_enumeration_profile(
    *,
    fixture: Tier3Fixture,
    profile: GraphProfileSnapshot,
    thresholds: Optional[ProfileThresholds] = None,
) -> EnumerationProfileScore:
    """Score a produced graph profile against a fixture's ``enumeration_scope``.

    Pure — no I/O. Raises ``ValueError`` if the fixture has no
    ``enumeration_scope`` (the profile bar is only meaningful for enumeration +
    scoped-schema goals).
    """
    scope = fixture.enumeration_scope
    if scope is None:
        raise ValueError(
            f"fixture {fixture.id!r} has no enumeration_scope; cannot grade a profile"
        )
    th = thresholds or ProfileThresholds()
    alias_map = _build_alias_map(scope.alias_table)

    # ------------------------------------------------------------------ #
    # Coverage (P1)
    # ------------------------------------------------------------------ #
    gold_canon = {
        _canonical(g, alias_map) for g in scope.expected_entities if normalize_key(g)
    }
    gold_total = len(gold_canon)

    produced_keys: list[str] = []
    for raw in profile.entity_keys:
        ckey = _canonical(raw, alias_map)
        if ckey:
            produced_keys.append(ckey)

    produced_distinct = set(produced_keys)
    found_gold = len(gold_canon & produced_distinct)
    coverage = (found_gold / gold_total) if gold_total else 0.0

    # Anti-gaming (a): off-roster rate — entities produced that are NOT in gold.
    # Padding the result with noise to look "busy" without covering the roster.
    off_roster = len(produced_distinct - gold_canon)
    produced_entity_total = len(profile.entity_keys)
    distinct_produced = len(produced_distinct)
    off_roster_rate = (off_roster / distinct_produced) if distinct_produced else 0.0

    # Anti-gaming (b): near-dup collapse — restating the same entity under
    # key-normalization to pad raw counts (coverage itself is set-based so this
    # does not inflate the headline; it flags the gaming attempt).
    collapsed = len(produced_keys) - len(produced_distinct)
    near_dup_rate = (collapsed / produced_entity_total) if produced_entity_total else 0.0

    coverage_ok = coverage >= th.coverage_floor
    off_roster_ok = off_roster_rate <= th.off_roster_ceiling
    near_dup_ok = near_dup_rate <= th.near_dup_ceiling

    # ------------------------------------------------------------------ #
    # Scope-adherence (P2)
    # ------------------------------------------------------------------ #
    def _attr_key(a: str) -> str:
        # Accept either a bare leaf ("website") or a trailing URI segment.
        s = str(a).strip()
        if "/" in s:
            s = s.rstrip("/").rsplit("/", 1)[-1]
        return normalize_key(s)

    requested = {_attr_key(a) for a in scope.requested_attributes}
    structural = {_attr_key(a) for a in scope.structural_attributes}
    # The key attribute is always in-scope even if the author forgot it.
    structural.add(_attr_key(scope.key_attribute))
    allowed_attrs = requested | structural

    produced_attrs = [_attr_key(a) for a in profile.attributes if str(a).strip()]
    # Distinct by normalized leaf.
    produced_attr_set = {a for a in produced_attrs if a}
    attribute_total = len(produced_attr_set)
    in_scope = produced_attr_set & allowed_attrs
    out_of_scope = produced_attr_set - allowed_attrs
    # Preserve a stable-ish order for reporting (sorted).
    out_of_scope_names = tuple(sorted(out_of_scope))

    scope_adherence = (len(in_scope) / attribute_total) if attribute_total else 0.0
    # Anti-gaming (a): requested-attr coverage — must surface the fields the
    # user asked for. Emitting zero attrs would otherwise score scope=0 which
    # fails the floor, but emitting only structural attrs (name/label/type)
    # would score high scope while ignoring the goal's field set.
    requested_present = len(requested & produced_attr_set)
    requested_total = len(requested)
    requested_attr_coverage = (
        (requested_present / requested_total) if requested_total else 1.0
    )

    scope_adherence_ok = scope_adherence >= th.scope_adherence_floor
    requested_attr_coverage_ok = (
        requested_attr_coverage >= th.requested_attr_coverage_floor
    )
    out_of_scope_count_ok = len(out_of_scope) <= th.max_out_of_scope_attrs

    # ------------------------------------------------------------------ #
    # Fragmentation (P5)
    # ------------------------------------------------------------------ #
    def _type_key(t: str) -> str:
        s = str(t).strip()
        if "/" in s:
            s = s.rstrip("/").rsplit("/", 1)[-1]
        # Strip a trailing fragment if present.
        if "#" in s:
            s = s.rsplit("#", 1)[-1]
        return normalize_key(s)

    produced_types = {_type_key(t) for t in profile.types if str(t).strip()}
    type_count = len(produced_types)

    allowed_types = {_type_key(t) for t in scope.allowed_types if str(t).strip()}
    forbidden_types = {_type_key(t) for t in scope.forbidden_types if str(t).strip()}

    forbidden_hit = produced_types & forbidden_types
    # Extra = produced types neither allowed nor (if allowed is empty) anything.
    # When the fixture declares no allowed_types, we only gate on count + forbidden.
    if allowed_types:
        extra = produced_types - allowed_types
        allowed_present = len(produced_types & allowed_types)
    else:
        extra = set()
        # No allow-list declared → presence gate is vacuously satisfied once
        # *some* type exists (or min is 0).
        allowed_present = type_count if th.min_allowed_types_present == 0 else (
            type_count if type_count >= th.min_allowed_types_present else type_count
        )
        # When no allow-list, the presence gate only requires type_count >= min
        # if min > 0; we treat "any types present" as the signal.
        if th.min_allowed_types_present > 0:
            allowed_present = type_count  # compared below against min

    type_count_ok = type_count <= th.max_types
    forbidden_types_ok = len(forbidden_hit) <= th.max_forbidden_types
    if allowed_types:
        allowed_presence_ok = allowed_present >= th.min_allowed_types_present
    else:
        # No allow-list: require at least min types present (still blocks empty).
        allowed_presence_ok = type_count >= th.min_allowed_types_present

    passed = (
        coverage_ok
        and off_roster_ok
        and near_dup_ok
        and scope_adherence_ok
        and requested_attr_coverage_ok
        and out_of_scope_count_ok
        and type_count_ok
        and forbidden_types_ok
        and allowed_presence_ok
    )

    return EnumerationProfileScore(
        fixture_id=fixture.id,
        coverage=coverage,
        gold_total=gold_total,
        found_gold_entities=found_gold,
        produced_entity_total=produced_entity_total,
        distinct_produced_entities=distinct_produced,
        off_roster_entities=off_roster,
        off_roster_rate=off_roster_rate,
        near_dup_collapsed_rows=collapsed,
        near_dup_collapse_rate=near_dup_rate,
        coverage_ok=coverage_ok,
        off_roster_ok=off_roster_ok,
        near_dup_ok=near_dup_ok,
        scope_adherence=scope_adherence,
        attribute_total=attribute_total,
        in_scope_attributes=len(in_scope),
        out_of_scope_attributes=len(out_of_scope),
        out_of_scope_attr_names=out_of_scope_names,
        requested_attr_coverage=requested_attr_coverage,
        requested_present=requested_present,
        requested_total=requested_total,
        scope_adherence_ok=scope_adherence_ok,
        requested_attr_coverage_ok=requested_attr_coverage_ok,
        out_of_scope_count_ok=out_of_scope_count_ok,
        type_count=type_count,
        allowed_types_present=allowed_present,
        forbidden_types_present=len(forbidden_hit),
        forbidden_type_names=tuple(sorted(forbidden_hit)),
        extra_type_names=tuple(sorted(extra)),
        type_count_ok=type_count_ok,
        forbidden_types_ok=forbidden_types_ok,
        allowed_presence_ok=allowed_presence_ok,
        passed=passed,
        thresholds=th,
    )
