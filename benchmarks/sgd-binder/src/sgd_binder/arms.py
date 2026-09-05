"""Four arms. Same models as the VRDU mix. No invented scores."""

from __future__ import annotations

from dataclasses import dataclass

from sgd_binder.constants import ARM_IDS, MODEL_08B, MODEL_27B
from sgd_binder.llm import (
    BareBinder,
    BareExtractor,
    ChatClient,
    InfonaBinder,
    InfonaExtractor,
)
from sgd_binder.protocol import ProtocolError
from sgd_binder.schema import TypeCatalog
from sgd_binder.skills import Skill


@dataclass(frozen=True)
class Arm:
    arm_id: str
    model_id: str
    uses_infona_router: bool
    lora_recipe: str | None


ARMS: dict[str, Arm] = {
    "27b_bare": Arm("27b_bare", MODEL_27B, False, None),
    "0.8b_bare": Arm("0.8b_bare", MODEL_08B, False, None),
    "0.8b_vanilla_ft": Arm("0.8b_vanilla_ft", MODEL_08B, False, "vanilla"),
    "0.8b_ft_infona": Arm("0.8b_ft_infona", MODEL_08B, True, "infona"),
}


def get_arm(arm_id: str) -> Arm:
    if arm_id not in ARMS:
        raise ProtocolError(f"unknown arm {arm_id!r}; expected one of {list(ARM_IDS)}")
    return ARMS[arm_id]


def adapters_for_arm(
    arm: Arm,
    *,
    client: ChatClient,
    catalog: TypeCatalog,
    skills: dict[str, Skill],
    needles: tuple[str, ...],
):
    if arm.uses_infona_router:
        return InfonaBinder(client, catalog, needles), InfonaExtractor(client, skills, needles)
    return BareBinder(client, catalog, needles), BareExtractor(client, skills, needles)
