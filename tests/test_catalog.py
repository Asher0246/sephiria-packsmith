import json
import re

from app.catalog import ASSETS, artifact_types, public_catalog, tablet_types
from app.solver import build_candidates

HANGUL = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")
PLACEHOLDER_OR_TAG = re.compile(r"\{[^}]+}|<[^>]+>")


def test_catalog_loads_current_wiki_data():
    artifacts = artifact_types()
    tablets = tablet_types()
    assert len(artifacts) == 268
    assert len(tablets) == 61
    assert len({item.id for item in artifacts}) == len(artifacts)
    assert len({item.id for item in tablets}) == len(tablets)
    assert all(item.base_level == 0 for item in artifacts)
    assert all(item.allow_negative for item in artifacts)
    assert all(item["baseLevel"] == 0 for item in public_catalog()["artifacts"])

    potion_lid = next(item for item in artifacts if item.id == "artifact-reinforced_potion_lid")
    assert potion_lid.name == "强化药水盖"
    assert potion_lid.image.startswith("https://img.sephiria.wiki/artifacts/")
    assert potion_lid.cap == 0
    assert next(item for item in artifacts if item.id == "artifact-eye_crystal_necklace").cap == 2
    assert next(item for item in artifacts if item.id == "artifact-giant_telescope").cap == 1
    assert next(item for item in artifacts if item.id == "artifact-sword_earring").name == "剑耳环"
    heart_burden = next(item for item in artifacts if item.id == "artifact-heart_burden")
    assert heart_burden.name == "心之重担"
    assert heart_burden.cap == 0
    assert heart_burden.allow_negative
    assert next(item for item in artifacts if item.id == "artifact-magic_carrot").criteria == ("top",)
    specials = {item.id: item.special_condition for item in artifacts if item.special_condition}
    assert specials == {
        "artifact-crystal_of_harmony": "nearby_levels",
        "artifact-multi_use_belt": "top_row_artifacts",
        "artifact-giant_telescope": "nearby_planets",
        "artifact-white_paper": "matching_side_categories",
        "artifact-unalloyed_gold_needle": "target_above",
    }
    assert {item["id"]: item["specialCondition"] for item in public_catalog()["artifacts"]
            if item["specialCondition"]} == specials

    advance = next(item for item in tablets if item.id == "tablet-advance")
    assert advance.name == "前进"
    assert advance.rotatable
    assert advance.candidates
    assert set(advance.candidates) == {f"{rows}x{cols}" for rows in range(1, 11) for cols in range(1, 7)}
    assert len(public_catalog()["tablets"]) == 61
    defender = next(item for item in tablets if item.id == "tablet-defender")
    assert defender.name == "防御招式"
    assert defender.rotatable
    assert defender.candidates is None
    candidates = build_candidates(defender, 7, 6, 39)
    rotation_zero = next(item for item in candidates if item.cell == 14 and item.rotation == 0)
    assert rotation_zero.effects == {7: 1, 9: 2, 19: 2, 21: 1, 13: -1, 15: -1}
    assert next(item for item in tablets if item.id == "tablet-triceps").name == "三头"
    assert next(item for item in tablets if item.id == "tablet-development").name == "进步"
    assert next(item for item in tablets if item.id == "tablet-honor").name == "荣誉"
    assert next(item for item in tablets if item.id == "tablet-dedication").name == "献礼"
    assert next(item for item in tablets if item.id == "tablet-last_stand").name == "破釜沉舟"
    curse = next(item for item in tablets if item.id == "tablet-curse")
    assert curse.name == "诅咒"
    assert curse.tier == "special"
    assert curse.rotatable
    shade = next(item for item in tablets if item.id == "tablet-shade")
    assert shade.constraint == "first_row"
    assert shade.candidates is None
    assert shade.directions == (("BOTTOM", 1),)


def test_official_chinese_localization_covers_the_wiki_catalog():
    wiki = json.loads((ASSETS / "wiki_artifacts.json").read_text(encoding="utf-8"))
    localized = json.loads((ASSETS / "wiki_zh_cn.json").read_text(encoding="utf-8"))

    wiki_ids = {str(item["value"]) for item in wiki["artifacts"]}
    assert set(localized["artifacts"]) == wiki_ids
    assert len(localized["tablets"]) == 61
    assert localized["source"]["kind"] == "Sephiria game localization"

    catalog = public_catalog()
    public_text = "\n".join(
        str(value)
        for collection in catalog.values()
        for item in collection
        for key, value in item.items()
        if key in {"name", "categories", "criteria"}
    )
    assert not HANGUL.search(public_text)
    assert not PLACEHOLDER_OR_TAG.search(public_text)
    assert all(item["name"] for item in localized["artifacts"].values())
    assert all(set(item) == {"sourceHash", "nameKey", "name"}
               for item in localized["artifacts"].values())
    assert all("description" not in item and "effect" not in item
               for item in catalog["artifacts"])


def test_artifact_caps_use_zero_based_effect_tiers():
    wiki = json.loads((ASSETS / "wiki_artifacts.json").read_text(encoding="utf-8"))
    expected = {}
    for row in wiki["artifacts"]:
        content = str((row.get("effect") or {}).get("content") or "")
        sequence_caps = [
            len(match.split("/")) - 1
            for match in re.findall(r"[-+]?\d+(?:\.\d+)?(?:/[-+]?\d+(?:\.\d+)?)+", content)
        ]
        expected[f"artifact-{row['value']}"] = max(
            [int(row.get("level") or 0), *sequence_caps], default=0,
        )
    actual = {item.id: item.cap for item in artifact_types()}
    assert {type_id: actual[type_id] for type_id in expected} == expected
    assert actual["artifact-heart_burden"] == 0
