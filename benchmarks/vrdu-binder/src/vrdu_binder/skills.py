"""Train-only skill writer. Valid, test, and current-doc gold are out of reach."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from vrdu_binder.constants import ADBUY_LINE_ITEM_KEYS, KEYS_FOR_TYPE
from vrdu_binder.protocol import ProtocolError, assert_skill_body_clean
from vrdu_binder.splits import RunSplit


@dataclass(frozen=True)
class Skill:
    """One type, one skill. ``type_id`` is harness-only, never in ``body``."""

    type_id: str
    keys: tuple[str, ...]
    body: str
    train_filenames: tuple[str, ...]
    seed: int


def write_skill(
    *,
    type_id: str,
    split: RunSplit,
    train_docs: Sequence[Mapping[str, Any]],
    seed: int,
    keys: Sequence[str] | None = None,
    max_few_shots: int = 4,
) -> Skill:
    """Build a skill from this seed's train filenames only.

    ``train_docs`` must be a subset of ``split.train``. A doc whose filename is
    on valid or test raises. Current-doc ``annotations`` on a held-out name
    cannot reach the body because that name is refused first.
    """
    if type_id not in KEYS_FOR_TYPE and keys is None:
        raise ProtocolError(f"unknown type_id {type_id!r} and no keys given")
    schema = tuple(keys) if keys is not None else KEYS_FOR_TYPE[type_id]
    names = [str(doc.get("filename") or "") for doc in train_docs]
    if any(not n for n in names):
        raise ProtocolError("train doc missing filename (needed for allowlist check)")
    split.assert_train_only(names, what="skill writer")
    if set(names) - set(split.train):
        raise ProtocolError("skill writer saw a filename outside train")
    examples = _few_shots(train_docs, schema, max_few_shots=max_few_shots)
    body = _procedure(schema, examples)
    assert_skill_body_clean(body)
    for name in names:
        if name and name in body:
            raise ProtocolError(f"skill body leaked train filename {name!r}")
    return Skill(
        type_id=type_id,
        keys=schema,
        body=body,
        train_filenames=tuple(names),
        seed=seed,
    )


def write_skills_for_seed(
    *,
    split_by_type: Mapping[str, RunSplit],
    docs_by_type: Mapping[str, Sequence[Mapping[str, Any]]],
    seed: int,
    keys_by_type: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for type_id, split in split_by_type.items():
        keys = None if keys_by_type is None else keys_by_type.get(type_id)
        train_docs = [
            d for d in docs_by_type[type_id] if str(d.get("filename")) in set(split.train)
        ]
        skills[type_id] = write_skill(
            type_id=type_id, split=split, train_docs=train_docs, seed=seed, keys=keys
        )
    return skills


def _few_shots(
    train_docs: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
    *,
    max_few_shots: int,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    keyset = set(keys)
    for doc in train_docs:
        values = _annotation_values(doc.get("annotations"), keyset)
        if values and _example_is_clean(values):
            out.append(values)
        if len(out) >= max_few_shots:
            break
    return out


def _example_is_clean(values: Mapping[str, str]) -> bool:
    blob = " ".join(values.values())
    try:
        assert_skill_body_clean(blob)
    except ProtocolError:
        return False
    return True


def _annotation_values(annotations: Any, keys: set[str]) -> dict[str, str]:
    if not isinstance(annotations, list):
        return {}
    values: dict[str, str] = {}
    for item in annotations:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        name = item[0]
        if name not in keys:
            continue
        cands = item[1]
        if not isinstance(cands, list) or not cands:
            continue
        first = cands[0]
        text = first[0] if isinstance(first, (list, tuple)) and first else None
        if isinstance(text, str) and text.strip():
            values[str(name)] = text.strip()
    return values


def _procedure(keys: Sequence[str], examples: Sequence[Mapping[str, str]]) -> str:
    lines = [
        "Extract values for these keys from the document tokens.",
        "",
        "Keys:",
    ]
    for key in keys:
        lines.append(f"- {key}")
    lines += [
        "",
        "Copy a value only when the tokens contain it. Do not invent values.",
        "If a key is absent, omit it.",
    ]
    line_keys = [k for k in keys if k in ADBUY_LINE_ITEM_KEYS]
    if line_keys:
        joined = ", ".join(line_keys)
        lines.append(
            f"When these keys occur together as a repeated row ({joined}), "
            "emit one group per row."
        )
    if examples:
        lines += ["", "Worked examples (values only):"]
        for i, ex in enumerate(examples, start=1):
            pairs = ", ".join(f"{k}={v}" for k, v in ex.items())
            lines.append(f"{i}. {pairs}")
    return "\n".join(lines) + "\n"


def assert_extract_keys_subset(skill: Skill, emitted_keys: Iterable[str]) -> None:
    extra = set(emitted_keys) - set(skill.keys)
    if extra:
        raise ProtocolError(
            f"extract emitted keys {sorted(extra)} not on the bound skill"
        )
