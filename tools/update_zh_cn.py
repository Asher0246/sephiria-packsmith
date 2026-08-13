from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
OUTPUT = ASSETS / "wiki_zh_cn.json"
GAME_APP_ID = "2436940"
HANGUL = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")
PLACEHOLDER = re.compile(r"\{([^{}]+)}")
TAG = re.compile(r"<tag=([^>]+)>")
RICH_TEXT = re.compile(r"<[^>]+>")
NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d+)?|\?)(?:/(?:[-+]?\d+(?:\.\d+)?|\?))*%?")

# These names do not exactly match the current Korean localization because the
# Wiki has spacing, spelling, or punctuation differences. Values are game keys.
NAME_KEY_OVERRIDES = {
    "sword_earring": "Item_SwordDamage_Name",
    "lightning_bolt": "Item_MagicCharm_LightningBolt_Name",
    "endodits_questionnaire": "Item_MovingCast_Name",
    "chakram": "Item_Chakram_Name",
    "bronze_mirror": "Item_PerfectBlockBuff_Name",
    "rabbit_village_guard_helm": "Item_RabbitVillageGuardHelmet_Name",
    "heart_shaped_carrot": "Item_HeartShapedCarrot_Name",
    "brass_mirror_fragments": "Item_BrassMirrorShard_Name",
    "overall_armband": "Item_SummonMole_Chief_Name",
    "plasma_helmet": "Item_PlasmaActive_Name",
    "tome_of_transmutation_lightning": "Item_PhysicalToLightning_Name",
    "tome_of_transmutation_ice": "Item_PhysicalToIce_Name",
    "tome_of_transmutation_fire": "Item_PhysicalToFire_Name",
}

EFFECT_KEY_OVERRIDES = {
    "lightning_bolt": (),
    "glass_hammer": ("Status_FinalWeaponDamage_Name", "Status_MaxHP_Name"),
    "agma_projection_sword": ("Status_PhysicalDamage_Name",),
    "rabbit_village_guard_helm": ("Status_FinalWeaponDamage_Name", "Status_Evasion_Name"),
    "six_leaf_clover": (
        "Status_Evasion_Name", "Status_Luck_Name",
        "Status_CriticalChance_Name", "Status_TrueDamage_Name",
    ),
    "iridescent_feathers": (
        "Status_SpecialAttackDamage_Name", "Status_DashRecoverySpeed_Name",
    ),
    "meditation_books": ("Status_MagicQuickCast_Name", "Status_MagicDamageBonus_Name"),
    "mysterious_weight": ("Status_BasicAttackDamage_Name", "Status_AttackSpeed_Name"),
    "shock_amplifier": ("Status_TrueDamage_Name",),
    "yakumo_kodachi": ("Status_DashAttackDamage_Name", "Status_DashRecoverySpeed_Name"),
    "tome_of_transmutation_lightning": ("Status_PhysicalToLightning_Name",),
    "tome_of_transmutation_ice": ("Status_PhysicalToIce_Name",),
    "tome_of_transmutation_fire": ("Status_PhysicalToFire_Name",),
}

EFFECT_TEXT_OVERRIDES = {
    "lightning_bolt": "获得闪电魔弹",
    "iridescent_feathers": "获得专注时额外获得 0/0/1 层",
    "overall_armband": "召唤鼹鼠队长",
}

SET_NAMES = {
    "yinggalbul": "余烬", "ice_weapon": "冰霜武装", "glacier": "冰川",
    "magic_engineering": "魔法工程", "shadow": "影子", "guardian": "守护",
    "spring_song": "风之歌", "mystery": "神秘", "planet": "行星",
    "colleague": "同伴", "precision": "精密", "extrium": "乌云",
    "firmness": "坚固", "lake": "湖泊", "sun_sword": "太阳剑",
    "academy": "学院", "curse": "诅咒", "bargaining": "交涉",
    "element": "元素", "alchemy": "炼金术",
}

TABLET_NAMES = {
    "chivalry": "骑士道", "dry": "干燥", "approximation": "近似",
    "advent": "到来", "linear": "善意", "sight": "凝视",
    "handshake": "握手", "fate": "命运", "wit": "才智",
    "exploitation": "剥削", "unity": "团结", "cheer": "欢呼",
    "hope": "希望", "compete": "竞争", "beating": "鼓动",
    "home_town": "激昂", "past": "过去", "future": "未来",
    "distribution": "分配", "triceps": "三头", "harvesting": "收获",
    "binary_star": "双星", "nurture": "滋养", "yearning": "渴望",
    "agglutination": "聚集", "entrance": "入口", "joke": "恶作剧",
    "load": "堆叠", "transition": "转移", "advance": "前进",
    "justice": "正义", "preparation": "准备", "exit": "出口",
    "tide": "波浪", "dedication": "献礼", "honor": "荣誉",
    "rally": "集结", "development": "进步", "base": "基础",
    "warrant": "权能", "disconnection": "断绝", "concurrency": "同时性",
    "vow": "誓言", "rebellion": "反抗", "connection": "连接",
    "junction": "接合", "last_stand": "破釜沉舟", "flag": "旗帜",
    "defender": "防御招式", "shade": "遮阳", "thorn": "刺",
    "boundary": "边界", "sheen": "光辉", "miracle": "奇迹",
    "daydream": "白日梦", "compression": "压缩", "certitude": "确信",
    "hospitality": "款待", "courage": "勇气", "peace": "和平",
}

GAME_ONLY_TABLET_NAMES = {
    "curse": "诅咒",
}

CONSTRAINT_TRANSLATIONS = (
    ("양쪽 칸에 아티팩트", "两侧格子均有神器时生效"),
    ("양쪽 칸이 모두 비어", "两侧格子均为空时生效"),
    ("최상단", "位于背包最上方时生效"),
    ("가장 아래 칸", "位于背包最下方时生效"),
    ("최하단", "位于背包最下方时生效"),
    ("인벤토리 안쪽", "位于背包内侧时生效"),
    ("인벤토리 가장자리", "位于背包边缘时生效"),
)

STATUS_KEY_OVERRIDES = {
    "CRITICAL": "Status_CriticalChance_Name",
    "FINAL_DAMAGE": "Status_FinalDamage_Name",
    "FLAME_SWORD_FAST_FALL": "Status_FlameSwordFastFall_Name",
}

TAG_TEXT = {
    "Artifact": "神器", "Charm": "神器", "DarkCloud": "乌云",
    "Follower": "同伴", "Item": "物品", "Magic": "魔法",
    "Magic_NoIcon": "魔法", "Skill": "技能", "Slot": "格子",
    "Weapon": "武器",
}

STATUS_WIKI_LABELS = {
    "Status_FinalWeaponDamage_Name": "최종 무기 공격력",
}


def source_hash(record: dict) -> str:
    effect = record.get("effect") or {}
    raw = json.dumps(
        [record.get("label_kor", ""), record.get("description", ""), effect.get("content", "")],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _game_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("SEPHIRIA_GAME_DIR")
    if configured:
        candidates.append(Path(configured))
    for drive in "CDEFGHIJ":
        candidates.extend((
            Path(f"{drive}:/SteamLibrary/steamapps/common/Sephiria"),
            Path(f"{drive}:/Program Files (x86)/Steam/steamapps/common/Sephiria"),
        ))
    return candidates


def resolve_game_dir(configured: Path | None) -> Path:
    candidates = [configured] if configured else _game_candidates()
    for candidate in candidates:
        if candidate and (candidate / "Sephiria_Data/StreamingAssets/Localization/zh-CN.json").is_file():
            return candidate.resolve()
    checked = "\n".join(f"  - {path}" for path in candidates)
    raise RuntimeError(
        "Sephiria localization files were not found. Pass --game-dir or set "
        f"SEPHIRIA_GAME_DIR. Checked:\n{checked}"
    )


def load_locale_pair(game_dir: Path) -> tuple[dict[str, str], dict[str, str], Path, Path]:
    localization = game_dir / "Sephiria_Data/StreamingAssets/Localization"
    ko_path = localization / "ko-KR.json"
    zh_path = localization / "zh-CN.json"
    ko = json.loads(ko_path.read_text(encoding="utf-8-sig"))
    zh = json.loads(zh_path.read_text(encoding="utf-8-sig"))
    if set(ko) != set(zh):
        raise RuntimeError("ko-KR.json and zh-CN.json do not contain the same localization keys")
    return ko, zh, ko_path, zh_path


def load_sources() -> tuple[list[dict], list[dict]]:
    with (ASSETS / "wiki_artifacts.json").open(encoding="utf-8") as handle:
        artifacts = json.load(handle)["artifacts"]
    with gzip.open(ASSETS / "wiki_tablets.json.gz", "rt", encoding="utf-8") as handle:
        tablets = json.load(handle)["tablets"]
    return artifacts, tablets


def load_artifact_export(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _reverse(values: dict[str, str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for key, value in values.items():
        if value:
            result[value].append(key)
    return result


def _name_rank(key: str) -> tuple[int, int, int, str]:
    prefixes = ("Item_", "Skill_", "Buff_", "EffectHUD_")
    prefix_rank = next((index for index, prefix in enumerate(prefixes) if key.startswith(prefix)), len(prefixes))
    return (0 if key.endswith("_Name") else 1, prefix_rank, len(key), key)


def resolve_name_key(
    record: dict, reverse_ko: dict[str, list[str]], locale_keys: set[str],
) -> str | None:
    artifact_id = str(record["value"])
    if artifact_id in NAME_KEY_OVERRIDES:
        return NAME_KEY_OVERRIDES[artifact_id]
    keys = sorted(reverse_ko.get(str(record.get("label_kor") or ""), []), key=_name_rank)
    name_keys = [key for key in keys if key.endswith("_Name")]
    description_keys = [
        key for key in reverse_ko.get(str(record.get("description") or ""), [])
        if key.endswith("_FlavorText")
    ]
    for description_key in description_keys:
        corresponding = description_key.removesuffix("_FlavorText") + "_Name"
        if corresponding in locale_keys:
            return corresponding
    return (name_keys or keys or [None])[0]


def item_stem(name_key: str | None) -> str | None:
    if not name_key or not name_key.endswith("_Name"):
        return None
    return re.sub(r"^(?:Item|Skill|Buff|EffectHUD)_", "", name_key[:-5])


def _old_record(name: str, stem: str | None, rows: list[dict[str, str]]) -> dict[str, str] | None:
    by_name = [row for row in rows if row["name"] == name and not "(X)" in row["internal_name"]]
    if len(by_name) == 1:
        return by_name[0]
    if stem:
        by_stem = [
            row for row in rows
            if row["internal_name"].split("_", 1)[-1].strip().casefold() == stem.strip().casefold()
            and "(X)" not in row["internal_name"]
        ]
        if len(by_stem) == 1:
            return by_stem[0]
    return by_name[0] if by_name else None


def _status_key(status_id: str, ko: dict[str, str]) -> str | None:
    if status_id in STATUS_KEY_OVERRIDES and STATUS_KEY_OVERRIDES[status_id] in ko:
        return STATUS_KEY_OVERRIDES[status_id]
    camel = "".join(part.capitalize() for part in status_id.lower().split("_"))
    expected = f"Status_{camel}_Name".casefold()
    return next((key for key in ko if key.casefold() == expected), None)


def _tag_text(tag: str, zh: dict[str, str]) -> str:
    kind, separator, target = tag.partition(":")
    base = target if separator and kind == "TEXT" else kind
    if base in {"HP", "MP"}:
        return base
    if zh.get(base):
        return RICH_TEXT.sub("", zh[base])
    if base in TAG_TEXT:
        return TAG_TEXT[base]
    candidates = (
        f"{base}_Name", f"Status_{base}_Name", f"Buff_{base}_Name",
        f"Skill_{base}_Name", f"Common_{base}_Name",
    )
    for key in candidates:
        value = zh.get(key)
        if value and not PLACEHOLDER.search(value):
            return TAG.sub("", value)
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", base).replace("_", " ")
    return words


def _clean_template(text: str, zh: dict[str, str]) -> str:
    text = TAG.sub(lambda match: _tag_text(match.group(1), zh), text)
    text = RICH_TEXT.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _numbers(content: str) -> list[str]:
    return NUMBER.findall(content)


def _fill_template(
    template: str, numbers: list[str], zh: dict[str, str], named: dict[str, str] | None = None,
) -> str:
    values: dict[str, str] = dict(named or {})
    position = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal position
        name = match.group(1)
        if name not in values:
            if position >= len(numbers):
                return ""
            values[name] = numbers[position]
            position += 1
        value = values[name]
        if match.end() < len(template) and template[match.end()] == "%":
            value = value.removesuffix("%")
        return value

    return _clean_template(PLACEHOLDER.sub(replace, template), zh)


def _localized_entries(raw: str) -> list[dict]:
    if not raw:
        return []
    value = json.loads(raw)
    return value if isinstance(value, list) else []


def _direct_templates(stem: str | None, zh: dict[str, str]) -> list[tuple[str, str]]:
    if not stem:
        return []
    prefixes = (f"Charm_{stem}_Effect", f"Charm_{stem}_Criteria", f"Status_{stem}_Name")
    return [(key, zh[key]) for key in zh if any(key.startswith(prefix) for prefix in prefixes) and zh[key]]


def _matching_numbers(key: str, content: str, ko: dict[str, str]) -> list[str]:
    source = ko.get(key, "")
    literal_parts = [
        part.strip() for part in PLACEHOLDER.split(TAG.sub("", source))[::2]
        if len(part.strip()) >= 2
    ]
    candidates = []
    aliases = [*literal_parts, STATUS_WIKI_LABELS.get(key, "")]
    for line in content.splitlines():
        score = sum(len(part) for part in aliases if part and part in line)
        if score:
            candidates.append((score, line))
    if candidates:
        return _numbers(max(candidates, key=lambda item: item[0])[1])
    return _numbers(content)


def _constraint_line(content: str) -> str | None:
    first = content.splitlines()[0] if content else ""
    if not first.startswith("<제약>"):
        return None
    return next((translated for phrase, translated in CONSTRAINT_TRANSLATIONS if phrase in first), None)


def _stat_line(stat: dict, ko: dict[str, str], zh: dict[str, str], wiki_content: str) -> tuple[str, str] | None:
    key = _status_key(str(stat.get("status_id") or ""), ko)
    if not key or not zh.get(key):
        return None
    old_values = stat.get("values_by_level") or []
    token = "/".join(str(value) for value in old_values)
    ko_label = _clean_template(ko[key], ko)
    candidates = [line for line in wiki_content.splitlines() if ko_label and ko_label in TAG.sub("", line)]
    matching_line = next(
        (line for line in candidates if line.replace("[고유]", "").strip().startswith(ko_label)),
        candidates[0] if candidates else None,
    )
    if matching_line:
        matching_numbers = _numbers(matching_line)
        if matching_numbers:
            token = matching_numbers[-1]
    label = _clean_template(zh[key], zh)
    if PLACEHOLDER.search(zh[key]):
        rendered = _fill_template(zh[key], [token], zh)
    else:
        rendered = f"{label} {token}".strip()
    return key, rendered


def render_effect(
    record: dict, old: dict[str, str] | None, stem: str | None,
    ko: dict[str, str], zh: dict[str, str], artifact_name: str,
) -> tuple[str, list[str]]:
    content = str((record.get("effect") or {}).get("content") or "")
    numbers = _numbers(content)
    lines: list[str] = []
    keys: list[str] = []

    constraint = _constraint_line(content)
    if constraint:
        lines.append(f"【约束】{constraint}")

    entries: list[tuple[str, str]] = []
    override_keys = EFFECT_KEY_OVERRIDES.get(str(record["value"]), ())
    entries.extend((key, zh.get(key, "")) for key in override_keys)
    has_explicit_effect = str(record["value"]) in EFFECT_KEY_OVERRIDES or str(record["value"]) in EFFECT_TEXT_OVERRIDES
    if old and not has_explicit_effect:
        for field in ("criteria_keys", "effect_keys"):
            entries.extend((str(item.get("key") or ""), str(item.get("zh_cn") or ""))
                           for item in _localized_entries(old.get(field, "")))
    if not entries:
        entries = _direct_templates(stem, zh)

    seen_entries: set[tuple[str, str]] = set()
    for key, template in entries:
        identity = (key, template)
        if not template or identity in seen_entries:
            continue
        seen_entries.add(identity)
        entry_numbers = _matching_numbers(key, content, ko)
        if key.startswith("Status_") and PLACEHOLDER.search(template):
            placeholder_count = len(set(PLACEHOLDER.findall(template)))
            entry_numbers = entry_numbers[-placeholder_count:]
        rendered = _fill_template(
            template, entry_numbers or numbers, zh,
            {"MAGICNAME": artifact_name, "MAGIC_NAME": artifact_name},
        )
        if key.startswith("Status_") and not PLACEHOLDER.search(template) and entry_numbers:
            rendered = f"{rendered} {entry_numbers[-1]}"
        if rendered and rendered not in lines:
            lines.append(rendered)
            keys.append(key)

    text_override = EFFECT_TEXT_OVERRIDES.get(str(record["value"]))
    if text_override and text_override not in lines:
        lines.insert(0, text_override)

    if old and not has_explicit_effect:
        for stat in _localized_entries(old.get("stats", "")):
            localized = _stat_line(stat, ko, zh, content)
            if localized and localized[1] not in lines:
                keys.append(localized[0])
                lines.append(localized[1])

    if lines and "[고유]" in content and not lines[0].startswith("【约束】"):
        lines[0] = f"【固有】{lines[0]}"
    return "\n".join(lines), list(dict.fromkeys(key for key in keys if key))


def _build_id(game_dir: Path) -> str | None:
    manifest = game_dir.parents[1] / f"appmanifest_{GAME_APP_ID}.acf"
    if not manifest.is_file():
        return None
    match = re.search(r'"buildid"\s+"(\d+)"', manifest.read_text(encoding="utf-8-sig", errors="replace"))
    return match.group(1) if match else None


def build_localization(game_dir: Path) -> tuple[dict, dict]:
    artifacts, tablets = load_sources()
    ko, zh, ko_path, zh_path = load_locale_pair(game_dir)
    reverse_ko = _reverse(ko)

    tablet_ids = {str(row["value"]) for row in tablets}
    if set(TABLET_NAMES) != tablet_ids:
        raise RuntimeError("Curated tablet names do not match the current Wiki tablet IDs")

    localized: dict[str, dict] = {}
    for record in artifacts:
        artifact_id = str(record["value"])
        name_key = resolve_name_key(record, reverse_ko, set(zh))
        name = _clean_template(zh.get(name_key, ""), zh) if name_key else ""
        if not name or HANGUL.search(name):
            raise RuntimeError(f"No official Simplified Chinese name found for {artifact_id}")
        localized[artifact_id] = {
            "sourceHash": source_hash(record),
            "nameKey": name_key,
            "name": name,
        }

    payload = {
        "locale": "zh-CN",
        "generatedAt": datetime.now(UTC).isoformat(),
        "source": {
            "kind": "Sephiria game localization",
            "buildId": _build_id(game_dir),
            "ko": str(ko_path),
            "zh": str(zh_path),
        },
        "sets": SET_NAMES,
        "tablets": {**TABLET_NAMES, **GAME_ONLY_TABLET_NAMES},
        "artifacts": localized,
    }
    problems = []
    for artifact_id, item in localized.items():
        for field in ("name",):
            if HANGUL.search(str(item[field])):
                problems.append(f"{artifact_id}.{field}")
    if problems:
        raise RuntimeError(f"Hangul remains in generated fields: {problems[:20]}")
    stats = {
        "artifacts": len(localized), "tablets": len(TABLET_NAMES) + len(GAME_ONLY_TABLET_NAMES),
    }
    return payload, stats


def update_localization(game_dir: Path | None = None) -> dict:
    resolved_game_dir = resolve_game_dir(game_dir)
    payload, stats = build_localization(resolved_game_dir)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Export official Simplified Chinese Wiki catalog text from Sephiria")
    parser.add_argument("--game-dir", type=Path, help="Sephiria installation directory")
    args = parser.parse_args()
    stats = update_localization(args.game_dir)
    print(
        "Official Chinese localization ready: "
        f"{stats['artifacts']} artifacts and {stats['tablets']} tablets."
    )


if __name__ == "__main__":
    main()
