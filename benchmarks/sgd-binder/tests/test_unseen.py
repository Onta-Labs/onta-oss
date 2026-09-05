"""Unseen test services are marked; chance bind is 1/n not 50%."""

from sgd_binder.fixtures import fixture_catalog, fixture_instances
from sgd_binder.score import score_predictions


def test_sprocket_is_unseen() -> None:
    cat = fixture_catalog()
    assert cat.by_service["Widget_1"].seen_in_train
    assert not cat.by_service["Sprocket_1"].seen_in_train


def test_unseen_n_in_score() -> None:
    cat = fixture_catalog()
    inst = fixture_instances(cat)
    pred_t = {i.instance_id: i.type_id for i in inst}
    pred_s = {i.instance_id: dict(i.gold_slots) for i in inst}
    sc = score_predictions(inst, pred_t, pred_s)
    assert sc.unseen_n == 1
    assert sc.seen_n == 2
    assert sc.bind_hits == 3
    assert sc.slot_micro_f1 == 1.0


def test_misbind_empties_slots() -> None:
    cat = fixture_catalog()
    inst = fixture_instances(cat)
    wrong = cat.by_service["Gadget_1"].type_id
    pred_t = {i.instance_id: wrong for i in inst}
    pred_s = {i.instance_id: dict(i.gold_slots) for i in inst}
    sc = score_predictions(inst, pred_t, pred_s)
    # gadget hits and its gold slots count; others misbind so preds are ignored
    assert sc.bind_hits == 1
    assert sc.slot_tp >= 1
    assert sc.slot_fn >= 1
