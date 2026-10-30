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
    "conflict_entity_id", "split_plan",
)

RENAME, MERGE, SKIP, SPLIT = "rename", "merge", "skip", "split"
#: 旧名不是原文、是通道故障的产物：它不留别名（按它搜不到，留着只污染检索），旧值只留在
#: 审计 CSV 与备份里。
RENAME_ALIAS_SOURCE = "script:repair_garbled_entity_names"
MERGE_ALIAS_SOURCE = "merge:repair-garbled-entity-names"
SPLIT_SOURCE = "script:split-garbled-entity-names"


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


def _occupant(connection: sqlite3.Connection, kind: str, name: str) -> int | None:
    """这个名字对应哪条同类实体：规范名优先，别名次之，都没有返回 ``None``。"""
    key = normalize_entity_name(name)
    taken = connection.execute(
        "SELECT id FROM entity WHERE kind=? AND normalized_name=? LIMIT 1", (kind, key)).fetchone()
    if taken:
        return int(taken[0])
    alias = connection.execute(
        "SELECT a.entity_id FROM entity_alias a JOIN entity e ON e.id=a.entity_id"
        " WHERE e.kind=? AND a.normalized_alias=? ORDER BY a.entity_id LIMIT 1", (kind, key)).fetchone()
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
    """这个实体能关联到的番号：账本里挂的作品，加上它自己 metadata 记的番号。

    只读 ``asset_entity`` 是不够的：这些损坏实体大多是当年「一条候选建一条实体」留下的，
    有 70 条一条作品都没挂上，但它们建的时候是照某份快照建的，``metadata_json`` 里留着
    ``content_id``／``provider_id``／``raw_snapshot``——那份快照往往还是好的。
    """
    codes = {str(row[0]) for row in connection.execute(
        "SELECT DISTINCT a.code FROM asset_entity ae JOIN asset a ON a.id=ae.asset_id"
        " WHERE ae.entity_id=? AND a.code IS NOT NULL AND a.code<>''", (entity_id,))}
    codes |= _metadata_codes(connection, entity_id)
    # `JUFE-493` 与元数据里的 `JUFE493` 是同一部作品：按归一化键去重，别当两条番号。
    unique: dict[str, str] = {}
    for code in sorted(codes):
        unique.setdefault(_key(code), code)
    return sorted(unique.values())


def _metadata_codes(connection: sqlite3.Connection, entity_id: int) -> set[str]:
    row = connection.execute(
        "SELECT metadata_json FROM entity WHERE id=?", (entity_id,)).fetchone()
    if row is None:
        return set()
    try:
        metadata = json.loads(str(row[0] or "{}"))
    except ValueError:
        return set()
    if not isinstance(metadata, dict):
        return set()
    codes = set()
    for field in ("content_id", "provider_id"):
        value = metadata.get(field)
        if isinstance(value, str) and value.strip():
            codes.add(value.strip())
    snapshot = metadata.get("raw_snapshot")
    if isinstance(snapshot, str) and snapshot.strip():
        code, separator, _source = Path(snapshot).name.rpartition("-")
        if separator and code:
            codes.add(code)
    return codes


def _evidence(index: dict[str, list[Path]], codes: list[str], kind: str) -> tuple[dict[str, set[str]], list[Path]]:
    """按番号分别取证：每个番号上的快照给出哪些名字，连同读过的那几份快照路径。

    分开保留而不是并成一个集合，是因为同一个损坏名可能对应多个原名（见 ``collect``）：
    要知道是哪个番号说的是谁，才能把作品分回各自的人。
    """
    per_code: dict[str, set[str]] = {}
    used: list[Path] = []
    for code in codes:
        found: set[str] = set()
        for path in index.get(_key(code), ()):
            payload = _snapshot(path)
            if payload is None:
                continue
            used.append(path)
            found |= names_in(payload, kind)
        per_code[code] = found
    return per_code, used


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


def _ledger_names(connection: sqlite3.Connection) -> dict[str, list[tuple[int, str]]]:
    """账本里各条同类实体的干净规范名，连 id 一起。

    来源快照自己在同一个编码错配窗口里被写坏时（快照里每个名字都带 U+FFFD），快照取证
    就无从下手；但账本里别的实体还留着干净的名字。把它们的名字按同一条错配反向算一遍，
    能对上损坏名的就是候选项——这是用账本给自己当字典。
    """
    found: dict[str, list[tuple[int, str]]] = {}
    for row in connection.execute(
        "SELECT id,kind,canonical_name FROM entity WHERE instr(canonical_name,?)<=0"
        " ORDER BY kind,id", (DAMAGE,)
    ):
        found.setdefault(str(row[1]), []).append((int(row[0]), str(row[2])))
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
        per_code, used = _evidence(index, codes, kind)
        names = {name for found in per_code.values() for name in found}
        scope = "same-code"
        if not codes:
            # 没挂任何作品就没有同番号旁证，只能退到全库找唯一图像；这时的结论只够改名，
            # 不够判合并——同名的另一条实体与它没有任何共同作品，凭什么说是同一人。
            if not global_names:
                global_names.update(_global_names(index, tuple(NAME_FIELDS)))
            names, used = global_names[kind], []
            scope = "global"
        candidates = {name for name in names if mangled(name) == current}
        ledger = _ledger_names(connection).get(kind, [])
        ledger_hits = [name for _id, name in ledger if mangled(name) == current]
        row: dict[str, object] = {
            "entity_id": entity_id, "kind": kind, "current": current, "target": "",
            "assets": connection.execute(
                "SELECT count(*) FROM asset_entity WHERE entity_id=?", (entity_id,)).fetchone()[0],
            "codes": " ".join(codes), "snapshots": " | ".join(str(path) for path in used),
            "action": SKIP, "reason": "", "conflict_entity_id": "", "split_plan": "",
        }
        rows.append(row)
        if not candidates:
            if len(ledger_hits) == 1:
                # 快照自己也坏了，只能拿账本里别的干净实体当字典：唯一命中就是它，
                # 而且那条实体本来就在，所以是合并过去而不是改名。
                target = ledger_hits[0]
                clash = _clash(connection, kind, entity_id, target)
                if clash is not None:
                    row["target"] = target
                    row["action"], row["conflict_entity_id"] = MERGE, clash
                    row["reason"] = "ledger-dictionary"
                    continue
            row["reason"] = "no-codes-no-global-match" if not codes else "missing-evidence"
            continue
        if len(candidates) > 1:
            plan = (_split_plan(connection, kind, entity_id, codes, per_code, current)
                    if scope == "same-code" else None)
            if plan is not None:
                row["action"], row["reason"] = SPLIT, "one-name-many-people"
                row["target"] = " | ".join(sorted(candidates))
                row["split_plan"] = _format_split(plan)
                continue
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


def _split_plan(connection: sqlite3.Connection, kind: str, entity_id: int, codes: list[str],
                per_code: dict[str, set[str]], current: str) -> list[tuple[str, str, int | None]] | None:
    """一个损坏名对上多个原名时，按番号把作品分回各自的人。

    GBK 是双字节、UTF-8 是变长：写出去再按 UTF-8 读回来，不同的原文可以塌成同一串
    U+FFFD——`菊乃らん` 与 `弓乃りむ` 就是同一个损坏名。这不是名字写错，是两个人的作品
    被并进了同一条实体，改名改不动它，只能拆。

    每个番号必须各自指出**唯一**的人，且这个人不能就是没被拆开的那条；只要有一个番号
    指不出人、或指到两处，整条都不拆——宁可留在复核里，也不能把作品挂到别人名下。
    """
    assignments: list[tuple[str, str, int | None]] = []
    seen_names: set[str] = set()
    for code in codes:
        found = per_code.get(code) or set()
        hit = {name for name in found if mangled(name) == current}
        if len(hit) != 1:
            return None
        name = next(iter(hit))
        owner = _occupant(connection, kind, name)
        if owner == entity_id:
            return None
        if name in seen_names and owner is None:
            # 同一个新名字要新建两次，说明取证本身没把它们分开。
            return None
        seen_names.add(name)
        assignments.append((code, name, owner))
    if len(seen_names) < 2:
        return None
    return assignments


def _format_split(plan: list[tuple[str, str, int | None]]) -> str:
    return "; ".join(f"{code}|{name}|{'' if owner is None else owner}" for code, name, owner in plan)


def _parse_split(value: object) -> list[tuple[str, str, int | None]]:
    plan: list[tuple[str, str, int | None]] = []
    for chunk in str(value or "").split(";"):
        parts = chunk.strip().split("|")
        if len(parts) != 3 or not parts[0].strip() or not parts[1].strip():
            continue
        owner = parts[2].strip()
        plan.append((parts[0].strip(), parts[1].strip(), int(owner) if owner else None))
    return plan


def _apply_split(connection: sqlite3.Connection, row: dict, now: str, counts: Counter) -> None:
    """把每个番号上的作品改挂到它该属的人身上，再删掉这条串名实体。

    目标人已存在就挂过去；不存在就当场建一条。全部挂完原实体若已无作品，直接删除——
    它记的那个损坏名本来就不是任何人的名字。用 ``entity_redirect`` 留墓碑：万一还有链接
    指着它，能指回正确的人。
    """
    entity_id, kind = int(row["entity_id"]), str(row["kind"])
    old = str(row["current"])
    plan = _parse_split(row.get("split_plan"))
    if not plan:
        counts["clash"] += 1
        return
    landed: list[int] = []
    for code, name, _recorded in plan:
        owner = _occupant(connection, kind, name)
        if owner is None:
            owner = _create_entity(connection, kind, name, now)
            counts["created"] += 1
        if owner == entity_id:
            # 新名字落回这条串名实体自己，说明它根本没被拆开。
            counts["clash"] += 1
            return
        assets = [int(item[0]) for item in connection.execute(
            "SELECT DISTINCT ae.asset_id FROM asset_entity ae JOIN asset a ON a.id=ae.asset_id"
            " WHERE ae.entity_id=? AND a.code=?", (entity_id, code))]
        for asset_id in assets:
            moved = connection.execute(
                "UPDATE OR IGNORE asset_entity SET entity_id=? WHERE asset_id=? AND entity_id=?",
                (owner, asset_id, entity_id)).rowcount
            counts["assets"] += int(moved or 0)
            # 扁平投影里那条 `演员:<损坏名>` 是照着这条串名实体写的，按资产逐条改成新名字。
            counts["flat_rewritten"] += _retag(connection, asset_id, old, name)
        landed.append(owner)
    if not landed:
        counts["clash"] += 1
        return
    counts["split"] += 1
    counts["aliases_dropped"] += _dropped_aliases(connection, entity_id)
    left = connection.execute(
        "SELECT count(*) FROM asset_entity WHERE entity_id=?", (entity_id,)).fetchone()[0]
    if left:
        # 还有作品没分出去（不该发生，但别把证据一起删掉）：只改名，等人工看。
        kept = connection.execute(
            "SELECT canonical_name FROM entity WHERE id=?", (landed[0],)).fetchone()
        if kept is not None:
            connection.execute(
                "UPDATE entity SET canonical_name=?,normalized_name=?,updated_at=? WHERE id=?",
                (str(kept[0]), normalize_entity_name(str(kept[0])), now, entity_id))
            counts["flat_rewritten"] += rewrite_flat_projection(
                connection, kind, entity_id, old, str(kept[0]))
        return
    connection.execute(
        "INSERT OR IGNORE INTO entity_redirect(old_id,target_id,source,merged_at) VALUES(?,?,?,?)",
        (entity_id, landed[0], SPLIT_SOURCE, now))
    connection.execute("DELETE FROM entity WHERE id=?", (entity_id,))
    counts["deleted"] += int(connection.execute("SELECT changes()").fetchone()[0])


def _create_entity(connection: sqlite3.Connection, kind: str, name: str, now: str) -> int:
    cursor = connection.execute(
        "INSERT INTO entity(kind,canonical_name,normalized_name,metadata_json,created_at,updated_at)"
        " VALUES(?,?,?,'{}',?,?)",
        (kind, name, normalize_entity_name(name), now, now))
    return int(cursor.lastrowid)


def _retag(connection: sqlite3.Connection, asset_id: int, old: str, new: str) -> int:
    old_tag, new_tag = f"演员:{old}", f"演员:{new}"
    connection.execute(
        "INSERT OR IGNORE INTO asset_tag(asset_id,tag,confidence,source) "
        "SELECT asset_id,?,confidence,source FROM asset_tag WHERE asset_id=? AND tag=?",
        (new_tag, asset_id, old_tag))
    connection.execute("DELETE FROM asset_tag WHERE asset_id=? AND tag=?", (asset_id, old_tag))
    return int(connection.execute("SELECT changes()").fetchone()[0])


def apply_rows(connection: sqlite3.Connection, rows: list[dict[str, object]], now: str) -> Counter:
    """按计划改名、合并、拆分。现名已变的整行跳过，冲突的只计数不硬改。"""
    counts: Counter = Counter()
    for row in rows:
        action = str(row["action"])
        if action not in (RENAME, MERGE, SPLIT):
            continue
        entity_id, kind = int(row["entity_id"]), str(row["kind"])
        found = connection.execute(
            "SELECT canonical_name FROM entity WHERE id=? AND kind=?", (entity_id, kind)).fetchone()
        if found is None or found[0] != str(row["current"]):
            counts["stale"] += 1
            continue
        if action == RENAME:
            _apply_rename(connection, row, now, counts)
        elif action == MERGE:
            _apply_merge(connection, row, now, counts)
        else:
            _apply_split(connection, row, now, counts)
    return counts
