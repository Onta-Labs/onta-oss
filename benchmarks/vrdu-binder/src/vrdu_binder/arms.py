"""Four-arm claim experiment. Same seed, same test lists. No invented scores."""

from __future__ import annotations

from dataclasses import dataclass

from vrdu_binder.bare import BareBinder, BareExtractor
from vrdu_binder.constants import MODEL_08B, MODEL_27B
from vrdu_binder.llm import ChatClient, LlmBinder, LlmExtractor
from vrdu_binder.protocol import ProtocolError

ARM_27B_BARE = "27b_bare"
ARM_08B_BARE = "0.8b_bare"
ARM_08B_VANILLA_FT = "0.8b_vanilla_ft"
ARM_08B_FT_INFONA = "0.8b_ft_infona"
ARM_IDS = (ARM_27B_BARE, ARM_08B_BARE, ARM_08B_VANILLA_FT, ARM_08B_FT_INFONA)


@dataclass(frozen=True)
class Arm:
    arm_id: str
    model_id: str
    uses_infona_router: bool
    lora_recipe: str | None
    title: str


ARMS: dict[str, Arm] = {
    ARM_27B_BARE: Arm(
        arm_id=ARM_27B_BARE,
        model_id=MODEL_27B,
        uses_infona_router=False,
        lora_recipe=None,
        title="27B bare",
    ),
    ARM_08B_BARE: Arm(
        arm_id=ARM_08B_BARE,
        model_id=MODEL_08B,
        uses_infona_router=False,
        lora_recipe=None,
        title="0.8B bare",
    ),
    ARM_08B_VANILLA_FT: Arm(
        arm_id=ARM_08B_VANILLA_FT,
        model_id=MODEL_08B,
        uses_infona_router=False,
        lora_recipe="vanilla",
        title="0.8B vanilla-FT",
    ),
    ARM_08B_FT_INFONA: Arm(
        arm_id=ARM_08B_FT_INFONA,
        model_id=MODEL_08B,
        uses_infona_router=True,
        lora_recipe="infona",
        title="0.8B FT+Infona",
    ),
}


def get_arm(arm_id: str) -> Arm:
    if arm_id not in ARMS:
        raise ProtocolError(f"unknown arm {arm_id!r}; expected one of {list(ARM_IDS)}")
    return ARMS[arm_id]


def adapters_for_arm(arm: Arm, client: ChatClient | None = None) -> tuple[object, object]:
    """Inference adapters. Bare arms never see catalog keys or skill bodies."""
    if arm.uses_infona_router:
        return LlmBinder(client=client), LlmExtractor(client=client)
    return BareBinder(client=client), BareExtractor(client=client)
