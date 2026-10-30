"""采集桥把 GBK 字节当 UTF-8 解码时写进账本的规范名，按快照还原。

`entity.canonical_name` 里的 U+FFFD 不是来源写错，是通道错了：Windows 冻结运行时把
采集桥的 stdout 当 cp936 发，父进程按 UTF-8 收，日文字全落成替换字符。修复发生在
`tools/amane-bridge/bridge.py` 与 `metadata_amane.py`，这里只管它之前落进账本的那些。

判据是**逐字节可逆**：把快照里的干净名字按 GBK 编码再按 UTF-8 解码，得到的正是账本
里那个损坏名，才认它是原名。这一步不能退化成「猜哪个名字像」——U+FFFD 吃掉所有非法
字节，编码是天然的降维，一个损坏名可能对应多个原名。

证据只取**挂在同一番号上**的快照：同一个番号就是同一部作品，作品的人与厂牌就是那条。
这也是它不能靠全库比对的原因——没挂番号的实体（孤儿）只能全库找唯一图像，找不到就
留着交人。

``collect`` 只出计划，``apply_rows`` 才写。改名与合并都撤不回来：改名靠审计 CSV 里的
旧值反向，合并根本撤不回，先取得授权并备份。
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from .entities import merge_entity, normalize_entity_name, rewrite_flat_projection

#: 解码失败的替换字符。它出现在名字里就说明这个名字不是任何来源写的原文。
DAMAGE = "\ufffd"

#: 各类实体的名字在哪几个快照字段里。厂牌是字符串，女优是对象列表——来源写法不同，
#: 这里只描述位置，值怎么取交给 ``_harvest``。
NAME_FIELDS = {
    "performer": ("actresses", "actors", "performers"),
    "studio": ("maker", "studio", "label", "publisher"),
    "series": ("series",),
}

FIELDS = (
    "entity_id", "kind", "current", "target", "assets", "codes", "snapshots", "action", "reason",
    "conflict_entity_id",
)

RENAME, MERGE, SKIP = "rename", "merge", "skip"
#: 旧名不是原文、是通道故障的产物：它不留别名（按它搜不到，留着只污染检索），旧值只留在
#: 审计 CSV 与备份里。
RENAME_ALIAS_SOURCE = "script:repair_garbled_entity_names"
MERGE_ALIAS_SOURCE = "merge:repair-garbled-entity-names"


def _key(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def mangled(name: str) -> str | None:
    """这个名字若被按 GBK 写出去、再按 UTF-8 读回来，会变成什么。

    编不进 GBK 的名字（emoji、生僻字）不可能走过那条通道，返回 ``None``：它不可能是
    账本里那个损坏名的来源。
    """
    try:
        return name.encode("gbk").decode("utf-8", errors="replace")
    except (UnicodeEncodeError, LookupError):
        return None


def _harvest(container: object, keys: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    if not isinstance(container, dict):
        return out
    for key in keys:
        value = container.get(key)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, str):
                    out.append(entry)
                elif isinstance(entry, dict):
                    for field in ("japanese_name", "name"):
                        if isinstance(entry.get(field), str):
                            out.append(entry[field])
    return out


def _snapshot(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    # Javinizer-Go 的快照把作品包在 ``result`` 里，Peach 自己的直接是作品。
    inner = payload.get("result")
    return inner if isinstance(inner, dict) else payload


def names_in(payload: dict, kind: str) -> set[str]:
    """这份快照里全部能当证据的名字：不含 U+FFFD 的才算，坏掉的名字不是证据。"""
    keys = NAME_FIELDS.get(kind, ())
    found = _harvest(payload, keys)
    found += _harvest(payload.get("raw"), keys)
    return {_text(name) for name in found if _text(name) and DAMAGE not in name}


def snapshot_index(root: Path) -> dict[str, list[Path]]:
    """按番号索引快照目录。同一番号的多份来源都留着——它们互为旁证，不互相取代。"""
    grouped: dict[str, list[Path]] = defaultdict(list)
    for path in root.glob("*.json"):
        code, separator, _source = path.name.rpartition("-")
        if separator and code:
            grouped[_key(code)].append(path)
    return dict(grouped)


def _clash(connection: sqlite3.Connection, kind: str, entity_id: int, name: str) -> int | None:
    """这个名字已经被另一条同类实体占着时返回那一条的 id：规范名优先，别名次之。"""
    key = normalize_entity_name(name)
    taken = connection.execute(
        "SELECT id FROM entity WHERE kind=? AND normalized_name=? AND id<>?",
        (kind, key, entity_id)).fetchone()
    if taken:
        return int(taken[0])
    alias = connection.execute(
        "SELECT a.entity_id FROM entity_alias a JOIN entity e ON e.id=a.entity_id"
        " WHERE e.kind=? AND a.normalized_alias=? AND a.entity_id<>? ORDER BY a.entity_id LIMIT 1",
        (kind, key, entity_id)).fetchone()
    return int(alias[0]) if alias else None


def _dropped_aliases(connection: sqlite3.Connection, entity_id: int) -> int:
    """损坏名不是任何来源写过的原文，不该作为别名留在任何人名下。"""
    connection.execute("DELETE FROM entity_alias WHERE entity_id=? AND instr(alias,?)>0",
                       (entity_id, DAMAGE))
    return int(connection.execute("SELECT changes()").fetchone()[0])


def _damaged(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT id,kind,canonical_name FROM entity WHERE instr(canonical_name,?)>0 ORDER BY kind,id",
        (DAMAGE,)).fetchall()


def _codes(connection: sqlite3.Connection, entity_id: int) -> list[str]:
    return sorted({str(row[0]) for row in connection.execute(
        "SELECT DISTINCT a.code FROM asset_entity ae JOIN asset a ON a.id=ae.asset_id"
        " WHERE ae.entity_id=? AND a.code IS NOT NULL AND a.code<>''", (entity_id,))})


def _evidence(index: dict[str, list[Path]], codes: list[str], kind: str) -> tuple[set[str], list[Path]]:
    """挂在这些番号上的快照给出的名字，连同读过的那几份快照路径。"""
    names: set[str] = set()
    used: list[Path] = []
    for code in codes:
        for path in index.get(_key(code), ()):
            payload = _snapshot(path)
            if payload is None:
                continue
            used.append(path)
            names |= names_in(payload, kind)
    return names, used


def _global_names(index: dict[str, list[Path]], kinds: tuple[str, ...]) -> dict[str, set[str]]:
    """全库每个种类出现过的干净名字。读一趟目录，三类一起收。"""
    found: dict[str, set[str]] = {kind: set() for kind in kinds}
    for paths in index.values():
        for path in paths:
            payload = _snapshot(path)
            if payload is None:
                continue
            for kind in kinds:
                found[kind] |= names_in(payload, kind)
    return found


def collect(connection: sqlite3.Connection, snapshots: Path) -> list[dict[str, object]]:
    """逐条判定损坏的规范名怎么处置：改名、合并，或留给人。"""
    connection.row_factory = sqlite3.Row
    index = snapshot_index(snapshots)
    global_names: dict[str, set[str]] = {}
    rows: list[dict[str, object]] = []
    for entity in _damaged(connection):
        entity_id, kind = int(entity["id"]), str(entity["kind"])
        current = str(entity["canonical_name"] or "")
        if kind not in NAME_FIELDS:
            # 只有这三类在来源快照里有对应的名字字段；别的种类坏了没有取证路径。
            rows.append({"entity_id": entity_id, "kind": kind, "current": current, "target": "",
                         "assets": "", "codes": "", "snapshots": "", "action": SKIP,
                         "reason": "unsupported-kind", "conflict_entity_id": ""})
            continue
        codes = _codes(connection, entity_id)
        names, used = _evidence(index, codes, kind)
        scope = "same-code"
        if not codes:
            # 没挂任何作品就没有同番号旁证，只能退到全库找唯一图像；这时的结论只够改名，
            # 不够判合并——同名的另一条实体与它没有任何共同作品，凭什么说是同一人。
            if not global_names:
                global_names.update(_global_names(index, tuple(NAME_FIELDS)))
            names, used = global_names[kind], []
            scope = "global"
        candidates = {name for name in names if mangled(name) == current}
        row: dict[str, object] = {
            "entity_id": entity_id, "kind": kind, "current": current, "target": "",
            "assets": connection.execute(
                "SELECT count(*) FROM asset_entity WHERE entity_id=?", (entity_id,)).fetchone()[0],
            "codes": " ".join(codes), "snapshots": " | ".join(str(path) for path in used),
            "action": SKIP, "reason": "", "conflict_entity_id": "",
        }
        rows.append(row)
        if not candidates:
            row["reason"] = "no-codes-no-global-match" if not codes else "missing-evidence"
            continue
        if len(candidates) > 1:
            row["target"] = " | ".join(sorted(candidates))
            row["reason"] = "ambiguous-evidence"
            continue
        target = next(iter(candidates))
        row["target"] = target
        clash = _clash(connection, kind, entity_id, target)
        if clash is None:
            row["action"], row["reason"] = RENAME, f"verified-{scope}"
        elif scope == "same-code":
            # 同一番号上两个人名各立了一条实体：重采时新建的那条接管了作品，旧的留在原地。
            row["action"], row["conflict_entity_id"] = MERGE, clash
            row["reason"] = "same-code-credits-target"
        else:
            row["conflict_entity_id"] = clash
            row["reason"] = "clash-needs-review"
    return rows


def _apply_rename(connection: sqlite3.Connection, row: dict, now: str,
                  counts: Counter) -> None:
    entity_id, kind = int(row["entity_id"]), str(row["kind"])
    old, new = str(row["current"]), str(row["target"])
    if _clash(connection, kind, entity_id, new) is not None:
        counts["clash"] += 1
        return
    connection.execute(
        "UPDATE entity SET canonical_name=?,normalized_name=?,updated_at=? WHERE id=?",
        (new, normalize_entity_name(new), now, entity_id))
    counts["renamed"] += int(connection.execute("SELECT changes()").fetchone()[0])
    counts["aliases_dropped"] += _dropped_aliases(connection, entity_id)
    counts["flat_rewritten"] += rewrite_flat_projection(connection, kind, entity_id, old, new)


def _merge_target(connection: sqlite3.Connection, row: dict) -> int | None:
    """计划里那条合并目标还在、名字还被它占着，才认；否则整行作废。

    重新算一遍占用而不直接用计划里的 id：`collect` 到写入之间账本可能已经变过，这时
    按旧 id 合过去就是合错了人。
    """
    recorded = str(row.get("conflict_entity_id") or "").strip()
    target = _clash(connection, str(row["kind"]), int(row["entity_id"]), str(row["target"]))
    if target is None or (recorded and str(target) != recorded):
        return None
    return target


def _apply_merge(connection: sqlite3.Connection, row: dict, now: str, counts: Counter) -> None:
    entity_id, kind = int(row["entity_id"]), str(row["kind"])
    old = str(row["current"])
    target = _merge_target(connection, row)
    if target is None:
        counts["clash"] += 1
        return
    kept = connection.execute(
        "SELECT canonical_name FROM entity WHERE id=?", (target,)).fetchone()
    if kept is None:
        counts["clash"] += 1
        return
    moved = merge_entity(connection, target_id=target, source_id=entity_id,
                         source_name=old, alias_source=MERGE_ALIAS_SOURCE, now=now)
    counts["merged"] += 1
    for key in ("assets", "aliases", "refs", "links", "terms", "dropped_refs"):
        counts[key] += int(moved[key])
    counts["aliases_dropped"] += _dropped_aliases(connection, target)
    # 扁平投影跟的是保留方的规范名，不是「当初对上的那个写法」：对上的常常是它的别名，
    # 留下别名会让卡片写着别名、资料页写着规范名。
    counts["flat_rewritten"] += rewrite_flat_projection(
        connection, kind, target, old, str(kept[0]))


def apply_rows(connection: sqlite3.Connection, rows: list[dict[str, object]], now: str) -> Counter:
    """按计划改名与合并。现名已变的整行跳过，冲突的只计数不硬改。"""
    counts: Counter = Counter()
    for row in rows:
        action = str(row["action"])
        if action not in (RENAME, MERGE):
            continue
        entity_id, kind = int(row["entity_id"]), str(row["kind"])
        found = connection.execute(
            "SELECT canonical_name FROM entity WHERE id=? AND kind=?", (entity_id, kind)).fetchone()
        if found is None or found[0] != str(row["current"]):
            counts["stale"] += 1
            continue
        if action == RENAME:
            _apply_rename(connection, row, now, counts)
        else:
            _apply_merge(connection, row, now, counts)
    return counts
