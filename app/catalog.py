from __future__ import annotations

import gzip
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from .models import SPECIAL_CONDITION_BY_ID, ArtifactType, TabletType

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

TIER_RARITY = {"common": 0, "advanced": 1, "rare": 2, "legend": 3, "solid": 4}
CRITERIA_PATTERNS = (
    ("both_side_artifacts", "양쪽 칸에 아티팩트"),
    ("side_free", "양쪽 칸이 모두 비어"),
    ("top", "최상단"),
    ("bottom", "가장 아래 칸"),
    ("bottom", "최하단"),
    ("inner", "인벤토리 안쪽"),
    ("edge", "인벤토리 가장자리"),
)


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _source_hash(record: dict) -> str:
    effect = record.get("effect") or {}
    raw = json.dumps(
        [record.get("label_kor", ""), record.get("description", ""), effect.get("content", "")],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _localization() -> dict:
    payload = _load_json(ASSETS / "wiki_zh_cn.json")
    if payload.get("locale") != "zh-CN":
        raise RuntimeError("Wiki catalog localization must be zh-CN")
    return payload


def _effect_cap(content: str, level_hint: int) -> int:
    sequence_caps = [
        len(match.split("/")) - 1
        for match in re.findall(r"[-+]?\d+(?:\.\d+)?(?:/[-+]?\d+(?:\.\d+)?)+", content)
    ]
    return max([level_hint, *sequence_caps], default=level_hint)


def _criteria(content: str) -> tuple[str, ...]:
    constraint = content.split("\n", 1)[0] if content.startswith("<제약>") else ""
    return tuple(kind for kind, phrase in CRITERIA_PATTERNS if phrase in constraint)


@lru_cache(maxsize=1)
def artifact_types() -> tuple[ArtifactType, ...]:
    payload = _load_json(ASSETS / "wiki_artifacts.json")
    localization = _localization()
    records = payload.get("artifacts", [])
    if payload.get("count") != len(records):
        raise RuntimeError("Wiki artifact catalog count does not match its metadata")
    result = []
    for row in records:
        localized = localization.get("artifacts", {}).get(str(row["value"]))
        if not localized:
            raise RuntimeError(f"Missing Chinese localization for artifact {row['value']}")
        if localized.get("sourceHash") != _source_hash(row):
            raise RuntimeError(
                f"Chinese localization for artifact {row['value']} is stale; "
                "run tools/update_zh_cn.py again"
            )
        effect = row.get("effect") or {}
        content = str(effect.get("content") or "")
        level_hint = int(row.get("level") or 0)
        result.append(ArtifactType(
            id=f"artifact-{row['value']}", name=str(localized["name"]),
            cap=_effect_cap(content, level_hint), rarity=TIER_RARITY[str(row["tier"])],
            categories=tuple(str(localization.get("sets", {}).get(value, value)) for value in effect.get("sets", [])),
            criteria=_criteria(content), allow_negative=True, base_level=0, image=row.get("image"),
            special_condition=SPECIAL_CONDITION_BY_ID.get(f"artifact-{row['value']}"),
        ))
    # This hidden hardship-mode artifact is present in the game data but omitted
    # from the public Wiki catalog. It has no effect tiers and may stay negative.
    result.append(ArtifactType(
        id="artifact-heart_burden", name="心之重担", cap=0, rarity=0,
        allow_negative=True, base_level=0,
    ))
    return tuple(result)


@lru_cache(maxsize=1)
def tablet_types() -> tuple[TabletType, ...]:
    with gzip.open(ASSETS / "wiki_tablets.json.gz", "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    localization = _localization()
    records = payload.get("tablets", [])
    rules = payload.get("candidates", {})
    if payload.get("count") != len(records) or set(rules) != {row.get("value") for row in records}:
        raise RuntimeError("Wiki tablet catalog and rule set do not match")
    missing_names = {str(row["value"]) for row in records} - set(localization.get("tablets", {}))
    if missing_names:
        raise RuntimeError(f"Missing Chinese localization for tablets: {sorted(missing_names)}")
    result = []
    for row in records:
        value = str(row["value"])
        if value == "defender":
            # Current game data calls this DefensiveMove and allows rotation;
            # the Wiki snapshot still exposes the older fixed-direction rule.
            result.append(TabletType(
                id="tablet-defender", name=str(localization["tablets"][value]),
                tier=str(row["tier"]), rotatable=True, constraint=None,
                image=f"https://img.sephiria.wiki{row['image']}",
                directions=(("DIAUPLEFT", 1), ("DIAUPRIGHT", 2),
                            ("DIADOWNLEFT", 2), ("DIADOWNRIGHT", 1),
                            ("LEFT", -1), ("RIGHT", -1)),
            ))
            continue
        if value == "shade":
            result.append(TabletType(
                id="tablet-shade", name=str(localization["tablets"][value]),
                tier=str(row["tier"]), rotatable=False, constraint="first_row",
                image=f"https://img.sephiria.wiki{row['image']}",
                directions=(("BOTTOM", 1),),
            ))
            continue
        result.append(TabletType(
            id=f"tablet-{value}", name=str(localization["tablets"][value]), tier=str(row["tier"]),
            rotatable=bool(row.get("rotate")), constraint=None,
            image=f"https://img.sephiria.wiki{row['image']}", candidates=rules[value],
        ))
    result.append(TabletType(
        id="tablet-curse", name=str(localization["tablets"]["curse"]),
        tier="special", rotatable=True, constraint=None, image=None,
        directions=(("CHECKERBOARD2", 1), ("CHECKERBOARD", -1)),
    ))
    return tuple(result)


def public_catalog() -> dict:
    return {
        "artifacts": [{
            "id": item.id, "name": item.name, "cap": item.cap, "baseLevel": item.base_level,
            "rarity": item.rarity, "categories": item.categories, "criteria": item.criteria,
            "allowNegative": item.allow_negative, "image": item.image,
            "specialCondition": item.special_condition,
        } for item in artifact_types()],
        "tablets": [{
            "id": item.id, "name": item.name, "tier": item.tier, "rotatable": item.rotatable,
            "constraint": item.constraint, "image": item.image,
        } for item in tablet_types()],
    }
