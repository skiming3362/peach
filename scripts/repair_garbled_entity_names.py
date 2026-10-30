#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""采集桥 GBK／UTF-8 错配期间写坏的实体规范名，按同番号快照还原。

2026-10-25 到 10-27 之间，Windows 冻结运行时里的 amane 采集桥把 stdout 按 cp936 发出去，
父进程按 UTF-8 收，于是日文字全落成 U+FFFD 写进了 `entity.canonical_name`。通道本身
已在 `tools/amane-bridge/bridge.py` 与 `metadata_amane.py` 修好（提交 4271dece、
8ec95b15），本脚本只清它留在账本里的存量：1704 条实体里 173 条，首页名册可见 91 条。

判据是逐字节的，不是相似的：`snapshot.encode('gbk').decode('utf-8')` 必须逐字等于账本
里那个损坏名。U+FFFD 吃掉所有非法字节，所以一个损坏名可能对应多个原名，光比对这个
映射不够——证据还必须来自**挂在这个实体自己番号上**的快照：同一部作品上的名字，就是
这个人／这家厂牌／这个系列。同番号给不出唯一结论时交人工，不猜。

三种处置：

- 改名（`rename`）：唯一原名且没被别的实体占用。目标用**日文原名**——本地没有中日
  映射表，先消掉乱码是这一步的目的；`scripts/localize_performer_names.py` 的
  `canonical-jp` 一层之后仍能把日文原名换成本地化中文。
- 合并（`merge`）：唯一原名已被另一条同类实体占用，且证据来自同一番号。那是重采时
  新建的干净实体接管了作品、旧的留在原地，两条是同一身份。合并不可逆。
- 留给人（`skip`）：多候选、缺证据、只有全库对像的撞名。原因写在 `reason` 列。

损坏名不留别名：它不是任何来源写过的原文，按它搜不到东西，留着只污染检索。旧值留在
审计 CSV 和备份里，要撤回就照 CSV 反着改。

默认只写审计 CSV；`--apply` 才写 ledger，且必须同时给 `--backup`。改名与合并都撤不
回来，`--apply` 前先确认这批计划已经过目。

剩下的硬骨头——快照自己在同一个窗口里被写坏、又没有任何同番号干净实体可对——自动取证
取不到证。这类可以人工定名：给 `--reviewed-csv`（列为 `entity_id,name`），人给的名字
仍要过同一条逐字节判据，对不上的记 `reviewed-name-does-not-match` 留在审计里。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from peach.config import GENERATED_DIR, SOURCES_DIR   # noqa: E402
from peach.garbled_name_repair import (   # noqa: E402
    FIELDS, TITLE_FIELDS, apply_rows, apply_titles, collect, collect_titles,
)
from peach.review_csv import read_rows, write_rows   # noqa: E402
from peach.scripting import (   # noqa: E402
    add_ledger_write_args, counts_of, open_for_write, verify_after_write,
)


def _reviewed(path: Path | None) -> dict[int, str]:
    """人定下的名字：`entity_id,name` 两列。文件没给就是空。"""
    if path is None:
        return {}
    found: dict[int, str] = {}
    for row in read_rows(path, missing_ok=False):
        raw = str(row.get("entity_id") or "").strip()
        name = str(row.get("name") or "").strip()
        if not raw.isdigit() or not name:
            continue
        found[int(raw)] = name
    return found

EXTRA_COUNTS = {
    "damaged_names": "SELECT count(*) FROM entity WHERE instr(canonical_name, '\ufffd')>0",
    "damaged_titles": "SELECT count(*) FROM asset WHERE instr(COALESCE(catalog_title,''), '\ufffd')>0",
    "entities": "SELECT count(*) FROM entity",
    "aliases": "SELECT count(*) FROM entity_alias",
    "performer_links": "SELECT count(*) FROM asset_entity WHERE role='performer'",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按同番号快照还原被 GBK／UTF-8 错配写坏的实体规范名")
    add_ledger_write_args(parser)
    parser.add_argument("--snapshots", type=Path, default=SOURCES_DIR / "library-metadata",
                        help="来源快照目录")
    parser.add_argument("--audit-csv", type=Path,
                        default=GENERATED_DIR / "garbled-entity-name-repair.csv")
    parser.add_argument("--reviewed-csv", type=Path, default=None,
                        help="人工定名，两列 entity_id,name；仍按逐字节判据校验")
    parser.add_argument("--titles", action="store_true",
                        help="同时修 asset.catalog_title（同一条判据，同番号快照反算原件）")
    parser.add_argument("--title-audit-csv", type=Path,
                        default=GENERATED_DIR / "garbled-title-repair.csv")
    return parser


def _report_titles(rows: list[dict[str, object]], path: Path) -> None:
    counts = Counter(str(row["action"]) for row in rows)
    print(f"损坏作品标题 {len(rows)} 条，审计 CSV：{path}")
    print("  动作分布：", dict(counts))
    print("  跳过原因：", dict(Counter(str(row["reason"]) for row in rows
                                    if row["action"] == "skip")))
    for row in rows:
        if row["action"] != "skip":
            print(f"    [{row['action']}] asset #{row['asset_id']} {row['code']} "
                  f"{row['current']!r} -> {row['target']!r}  {row['reason']}"
                  f"（来源 {row['sources']}）")


def run(args: argparse.Namespace) -> int:
    connection = open_for_write(args)
    try:
        rows = collect(connection, args.snapshots, _reviewed(args.reviewed_csv))
        write_rows(args.audit_csv, FIELDS, rows, atomic=True, fill_missing=True)
        counts = Counter(str(row["action"]) for row in rows)
        print(f"损坏规范名 {len(rows)} 条，审计 CSV：{args.audit_csv}")
        print("  动作分布：", dict(counts))
        print("  跳过原因：", dict(Counter(str(row["reason"]) for row in rows
                                        if row["action"] == "skip")))
        for row in rows:
            if row["action"] != "skip":
                extra = f" -> #{row['conflict_entity_id']}" if row["action"] == "merge" else ""
                if row["action"] == "split":
                    extra = f"  {row['split_plan']}"
                print(f"    [{row['action']}] {row['kind']} #{row['entity_id']} "
                      f"{row['current']!r} -> {row['target']!r}{extra}  {row['reason']}")
        title_rows: list[dict[str, object]] = []
        if args.titles:
            title_rows = collect_titles(connection, args.snapshots)
            write_rows(args.title_audit_csv, TITLE_FIELDS, title_rows,
                       atomic=True, fill_missing=True)
            _report_titles(title_rows, args.title_audit_csv)
        if not args.apply:
            print("  未写 ledger（加 --apply --backup 才写）")
            return 0

        print(f"  已备份到 {args.backup}")
        before = counts_of(connection, EXTRA_COUNTS)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with connection:
            connection.execute("PRAGMA foreign_keys=ON")
            result = apply_rows(connection, rows, stamp)
            if title_rows:
                result.update(apply_titles(connection, title_rows))
        after = counts_of(connection, EXTRA_COUNTS)
        integrity, violations = verify_after_write(connection)
        print("  写入结果：", dict(result))
        for key in before:
            mark = "" if before[key] == after[key] else "  <-- 变化"
            print(f"    {key}: {before[key]} -> {after[key]}{mark}")
        print(f"  integrity_check={integrity}；foreign_key_check={violations}")
        return 1 if integrity != "ok" or violations else 0
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
