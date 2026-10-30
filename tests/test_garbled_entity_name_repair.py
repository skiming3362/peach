"""解码错配写坏的规范名修复：判据必须逐字节成立，证据必须来自同一番号。

这里守的是「什么才算证据」。三条退让都会把别人的名字改错：靠相似度猜、拿全库对像
代替同番号、把撞名直接当同一人。
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from peach import garbled_name_repair as module
from peach.migrations import upgrade

ROOT = Path(__file__).resolve().parents[1]

#: 这串日文按 GBK 编码再按 UTF-8 读回来就是下面那个损坏名。判据的整个立足点。
CLEAN = "夏目彩春"
DAMAGED = CLEAN.encode("gbk").decode("utf-8", errors="replace")


class Fixture(unittest.TestCase):
    """临时账本与快照目录；用例用它铺一条实体、它的作品和作品快照。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "ledger.db"
        self.snapshots = root / "snapshots"
        self.snapshots.mkdir()
        upgrade(self.db, ROOT / "migrations")
        self.con = sqlite3.connect(self.db)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys=ON")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def entity(self, entity_id: int, kind: str, name: str) -> int:
        self.con.execute(
            "INSERT INTO entity(id,kind,canonical_name,normalized_name,created_at,updated_at)"
            " VALUES(?,?,?,?,'2026-10-26T00:00:00Z','2026-10-26T00:00:00Z')",
            (entity_id, kind, name, module.normalize_entity_name(name)))
        return entity_id

    def asset(self, asset_id: int, code: str) -> int:
        self.con.execute(
            "INSERT INTO asset(id,location,path,name,medium,code) VALUES(?,?,?,?,'video',?)",
            (asset_id, "local", f"{asset_id}.mp4", f"{asset_id}.mp4", code))
        return asset_id

    def link(self, asset_id: int, entity_id: int, kind: str) -> None:
        self.con.execute(
            "INSERT OR IGNORE INTO asset_entity(asset_id,entity_id,role,source)"
            " VALUES(?,?,?,'javinizer:current')", (asset_id, entity_id, kind))

    def snapshot(self, code: str, source: str, **payload) -> Path:
        path = self.snapshots / f"{code}-{source}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def performer_snapshot(self, code: str, *names: str, source: str = "javdb") -> Path:
        return self.snapshot(code, source, actresses=[
            {"japanese_name": name} for name in names])

    def rows(self) -> dict[int, dict]:
        return {int(row["entity_id"]): row for row in module.collect(self.con, self.snapshots)}


class MangledTests(unittest.TestCase):
    def test_the_mapping_reproduces_the_damaged_name_byte_for_byte(self):
        self.assertEqual(module.mangled(CLEAN), DAMAGED)

    def test_a_name_that_cannot_travel_that_channel_maps_to_nothing(self):
        """emoji 与生僻字编不进 GBK：它不可能是账本里那个损坏名的来源。"""
        self.assertIsNone(module.mangled("😀"))
        self.assertIsNone(module.mangled("𠮷野家"))

    def test_a_clean_ascii_name_is_its_own_image(self):
        self.assertEqual(module.mangled("MOODYZ"), "MOODYZ")


class CollectTests(Fixture):
    def test_a_unique_name_on_the_same_code_is_renamed(self):
        self.entity(1, "performer", DAMAGED)
        self.asset(10, "MIDE-044")
        self.link(10, 1, "performer")
        self.performer_snapshot("MIDE-044", CLEAN)
        row = self.rows()[1]
        self.assertEqual((row["action"], row["target"], row["reason"]),
                         ("rename", CLEAN, "verified-same-code"))

    def test_a_snapshot_of_another_code_is_not_evidence(self):
        """同一个名字出现在别的番号上，说明不了这条实体是谁。"""
        self.entity(1, "performer", DAMAGED)
        self.asset(10, "MIDE-044")
        self.link(10, 1, "performer")
        self.performer_snapshot("MIDE-999", CLEAN)
        row = self.rows()[1]
        self.assertEqual((row["action"], row["reason"]), ("skip", "missing-evidence"))

    def test_an_orphan_falls_back_to_a_unique_global_match(self):
        self.entity(1, "series", "朝を迎えても".encode("gbk").decode("utf-8", errors="replace"))
        self.snapshot("ABC-001", "makers", series="朝を迎えても")
        row = self.rows()[1]
        self.assertEqual((row["action"], row["reason"]), ("rename", "verified-global"))

    def test_an_orphan_with_two_global_matches_is_left_alone(self):
        """`輪●計画` 与 `輪姦計画` 折成同一个损坏名：这是这条路唯一不能自动的地方。"""
        damaged = "輪姦計画".encode("gbk").decode("utf-8", errors="replace")
        self.entity(1, "series", damaged)
        self.snapshot("ABC-001", "makers", series="輪姦計画")
        self.snapshot("ABC-002", "makers", series="輪●計画")
        row = self.rows()[1]
        self.assertEqual((row["action"], row["reason"]), ("skip", "ambiguous-evidence"))
        self.assertIn("輪●計画", row["target"])

    def test_a_damaged_name_in_the_snapshot_is_not_evidence(self):
        self.entity(1, "performer", DAMAGED)
        self.asset(10, "MIDE-044")
        self.link(10, 1, "performer")
        self.performer_snapshot("MIDE-044", CLEAN.replace("彩", "\ufffd"))
        self.assertEqual(self.rows()[1]["reason"], "missing-evidence")

    def test_the_same_code_seen_from_two_sources_is_still_one_answer(self):
        self.entity(1, "performer", DAMAGED)
        self.asset(10, "MIDE-044")
        self.link(10, 1, "performer")
        self.performer_snapshot("MIDE-044", CLEAN, source="javdb")
        self.performer_snapshot("MIDE-044", CLEAN, source="r18dev")
        self.assertEqual(self.rows()[1]["action"], "rename")

    def test_a_javinizer_snapshot_wrapped_in_result_is_read(self):
        self.entity(1, "performer", DAMAGED)
        self.asset(10, "MIDE-044")
        self.link(10, 1, "performer")
        self.snapshot("MIDE-044", "javinizer",
                      result={"actresses": [{"name": CLEAN}]})
        self.assertEqual(self.rows()[1]["target"], CLEAN)

    def test_studio_names_are_plain_strings_in_the_snapshot(self):
        """厂牌名在快照里是字符串，女优名才是对象列表。当列表读会一个都取不到。"""
        damaged = "ダスッ！".encode("gbk").decode("utf-8", errors="replace")
        self.entity(1, "studio", damaged)
        self.asset(10, "DASD-866")
        self.link(10, 1, "studio")
        self.snapshot("DASD-866", "makers", maker="ダスッ！")
        self.assertEqual(self.rows()[1]["action"], "rename")

    def test_an_unsupported_kind_is_reported_not_guessed(self):
        self.entity(1, "tag", DAMAGED)
        row = self.rows()[1]
        self.assertEqual((row["action"], row["reason"]), ("skip", "unsupported-kind"))


class MergeTests(Fixture):
    def setUp(self):
        super().setUp()
        # 同一个人：重采时新建的干净实体接管了作品，旧的损坏实体留在原地。
        self.entity(1, "performer", DAMAGED)
        self.entity(2, "performer", CLEAN)
        self.asset(10, "MIDE-044")
        self.link(10, 1, "performer")
        self.performer_snapshot("MIDE-044", CLEAN)

    def test_a_name_taken_by_a_same_code_entity_becomes_a_merge(self):
        row = self.rows()[1]
        self.assertEqual((row["action"], int(row["conflict_entity_id"])),
                         ("merge", 2))

    def test_the_damaged_entity_disappears_and_its_assets_move(self):
        with self.con:
            counts = module.apply_rows(self.con, module.collect(self.con, self.snapshots),
                                      "2026-10-30T00:00:00Z")
        self.assertEqual(counts["merged"], 1)
        self.assertEqual(self.con.execute(
            "SELECT count(*) FROM entity WHERE id=1").fetchone()[0], 0)
        self.assertEqual(self.con.execute(
            "SELECT entity_id FROM asset_entity WHERE asset_id=10").fetchone()[0], 2)

    def test_the_damaged_name_is_not_kept_as_an_alias(self):
        """损坏名不是任何来源写过的原文：留着它只污染检索。"""
        with self.con:
            module.apply_rows(self.con, module.collect(self.con, self.snapshots),
                              "2026-10-30T00:00:00Z")
        aliases = [row[0] for row in self.con.execute("SELECT alias FROM entity_alias")]
        self.assertFalse([alias for alias in aliases if module.DAMAGE in alias])

    def test_the_tombstone_points_at_the_surviving_entity(self):
        with self.con:
            module.apply_rows(self.con, module.collect(self.con, self.snapshots),
                              "2026-10-30T00:00:00Z")
        self.assertEqual(self.con.execute(
            "SELECT target_id FROM entity_redirect WHERE old_id=1").fetchone()[0], 2)


class RenameTests(Fixture):
    def setUp(self):
        super().setUp()
        self.entity(1, "performer", DAMAGED)
        self.asset(10, "MIDE-044")
        self.link(10, 1, "performer")
        self.performer_snapshot("MIDE-044", CLEAN)

    def apply(self):
        with self.con:
            return module.apply_rows(self.con, module.collect(self.con, self.snapshots),
                                     "2026-10-30T00:00:00Z")

    def test_the_canonical_and_normalized_name_both_change(self):
        self.assertEqual(self.apply()["renamed"], 1)
        row = self.con.execute(
            "SELECT canonical_name,normalized_name FROM entity WHERE id=1").fetchone()
        self.assertEqual(tuple(row), (CLEAN, module.normalize_entity_name(CLEAN)))

    def test_the_flat_projection_follows(self):
        """只改实体不改 `演员:` 标签，卡片上还写着旧名、按旧名也照样查得到。"""
        self.con.execute(
            "INSERT INTO asset_tag(asset_id,tag,confidence,source) VALUES(10,?,0.9,'auto:javdb')",
            (f"演员:{DAMAGED}",))
        self.assertEqual(self.apply()["flat_rewritten"], 1)
        tags = [row[0] for row in self.con.execute("SELECT tag FROM asset_tag")]
        self.assertEqual(tags, [f"演员:{CLEAN}"])

    def test_a_row_whose_current_name_moved_on_is_skipped(self):
        """`collect` 到写入之间账本可能已经变过；按计划硬改就是改错了人。"""
        rows = module.collect(self.con, self.snapshots)
        with self.con:
            self.con.execute("UPDATE entity SET canonical_name='别人' WHERE id=1")
        with self.con:
            counts = module.apply_rows(self.con, rows, "2026-10-30T00:00:00Z")
        self.assertEqual(counts["stale"], 1)
        self.assertEqual(self.con.execute(
            "SELECT canonical_name FROM entity WHERE id=1").fetchone()[0], "别人")

    def test_a_row_that_became_a_clash_is_not_written(self):
        rows = module.collect(self.con, self.snapshots)
        with self.con:
            self.entity(9, "performer", CLEAN)
        with self.con:
            counts = module.apply_rows(self.con, rows, "2026-10-30T00:00:00Z")
        self.assertEqual(counts["clash"], 1)
        self.assertEqual(self.con.execute(
            "SELECT canonical_name FROM entity WHERE id=1").fetchone()[0], DAMAGED)


class SplitTests(Fixture):
    """一个损坏名对着两个人：GBK 双字节塌进 UTF-8 的替换字符，两个人的作品并成了一条。"""

    #: 两个不同的日文名，编成 GBK 再按 UTF-8 读回来是同一个损坏名。
    FIRST = "菊乃らん"
    SECOND = "弓乃りむ"

    def setUp(self):
        super().setUp()
        self.assertEqual(
            module.mangled(self.FIRST), module.mangled(self.SECOND),
            "这组名字必须塌成同一个损坏串，用例才成立")
        self.shared = module.mangled(self.FIRST)
        self.entity(1, "performer", self.shared)
        self.asset(10, "JUFE-493")
        self.asset(11, "DASS-253")
        self.link(10, 1, "performer")
        self.link(11, 1, "performer")
        self.performer_snapshot("JUFE-493", self.FIRST)
        self.performer_snapshot("DASS-253", self.SECOND)

    def apply(self):
        with self.con:
            return module.apply_rows(self.con, module.collect(self.con, self.snapshots),
                                     "2026-10-30T00:00:00Z")

    def test_each_work_goes_to_the_person_its_own_snapshot_names(self):
        rows = self.rows()
        self.assertEqual(rows[1]["action"], module.SPLIT)
        self.assertEqual(self.apply()["split"], 1)
        found = dict(self.con.execute(
            "SELECT a.code,e.canonical_name FROM asset_entity ae"
            " JOIN asset a ON a.id=ae.asset_id JOIN entity e ON e.id=ae.entity_id"
            " WHERE ae.role='performer'"))
        self.assertEqual(found, {"JUFE-493": self.FIRST, "DASS-253": self.SECOND})

    def test_a_target_that_does_not_exist_yet_is_created(self):
        self.apply()
        names = sorted(row[0] for row in self.con.execute(
            "SELECT canonical_name FROM entity WHERE kind='performer'"))
        self.assertEqual(names, sorted([self.FIRST, self.SECOND]))

    def test_the_damaged_entity_is_gone(self):
        self.apply()
        self.assertIsNone(self.con.execute("SELECT id FROM entity WHERE id=1").fetchone())

    def test_a_tombstone_points_at_one_of_the_people(self):
        self.apply()
        row = self.con.execute("SELECT target_id FROM entity_redirect WHERE old_id=1").fetchone()
        self.assertIsNotNone(row)
        self.assertIn(self.con.execute(
            "SELECT canonical_name FROM entity WHERE id=?", (row[0],)).fetchone()[0],
            (self.FIRST, self.SECOND))

    def test_the_damaged_actor_tag_is_rewritten_per_asset(self):
        for asset_id in (10, 11):
            self.con.execute(
                "INSERT INTO asset_tag(asset_id,tag,confidence,source) VALUES(?,?,0.9,'auto:javdb')",
                (asset_id, f"演员:{self.shared}"))
        self.apply()
        tags = dict(self.con.execute(
            "SELECT asset_id,tag FROM asset_tag"))
        self.assertEqual(tags, {10: f"演员:{self.FIRST}", 11: f"演员:{self.SECOND}"})

    def test_a_code_that_names_nobody_blocks_the_whole_split(self):
        """有一个番号指不出人，就整条不拆——宁可留在复核里，也不能把作品挂到别人名下。"""
        self.asset(12, "UNKNOWN-001")
        self.link(12, 1, "performer")
        self.assertEqual(self.rows()[1]["action"], module.SKIP)
        self.assertEqual(self.rows()[1]["reason"], "ambiguous-evidence")

    def test_a_code_naming_both_people_blocks_the_whole_split(self):
        self.performer_snapshot("JUFE-493", self.FIRST, self.SECOND)
        self.assertEqual(self.rows()[1]["action"], module.SKIP)


class MetadataCodeTests(Fixture):
    """孤儿实体没有作品可挂，但它建的时候是照某份快照建的，metadata 记着那份快照的番号。"""

    def test_the_entity_metadata_supplies_the_code(self):
        self.entity(1, "performer", DAMAGED)
        self.con.execute(
            "UPDATE entity SET metadata_json=? WHERE id=1",
            (json.dumps({"content_id": "MIDE894",
                         "raw_snapshot": "D:\\sources\\MIDE-894-makers.json"}),))
        self.performer_snapshot("MIDE-894", CLEAN, source="makers")
        rows = self.rows()
        self.assertEqual(rows[1]["action"], module.RENAME)
        self.assertEqual(rows[1]["target"], CLEAN)

    def test_the_two_writings_of_one_code_are_not_two_codes(self):
        """`JUFE-493` 与元数据里的 `JUFE493` 是同一部作品，别当成两条番号去各取一次证。"""
        self.entity(1, "performer", DAMAGED)
        self.asset(10, "MIDE-894")
        self.link(10, 1, "performer")
        self.con.execute("UPDATE entity SET metadata_json=? WHERE id=1",
                         (json.dumps({"content_id": "MIDE894"}),))
        self.assertEqual(module._codes(self.con, 1), ["MIDE-894"])

    def test_a_ledger_sibling_with_that_name_is_merged_into_it(self):
        """快照也坏了的时候，账本里别的干净实体就是字典：唯一命中只能并过去。"""
        self.entity(1, "performer", DAMAGED)
        self.asset(10, "MIDE-894")
        self.link(10, 1, "performer")
        self.snapshot("MIDE-894", "makers", actresses=[{"japanese_name": DAMAGED}])
        self.entity(2, "performer", CLEAN)
        rows = self.rows()
        self.assertEqual(rows[1]["action"], module.MERGE)
        self.assertEqual(rows[1]["reason"], "ledger-dictionary")
        self.assertEqual(rows[1]["conflict_entity_id"], 2)


class SiblingTests(Fixture):
    """重采同一部作品会写出两条实体：旧的留着坏名字，新的已经有干净名字。"""

    def setUp(self):
        super().setUp()
        self.entity(1, "series", "FETISH" + module.DAMAGE * 2 + "BODY")
        self.asset(10, "MIAD-495")
        self.link(10, 1, "series")
        self.snapshot("MIAD-495", "makers", series="FETISH" + module.DAMAGE * 2 + "BODY")

    def provider(self, entity_id: int, provider: str) -> None:
        """记下这条实体的来源链；和它原有的番号并存，不覆盖。"""
        row = self.con.execute("SELECT metadata_json FROM entity WHERE id=?",
                               (entity_id,)).fetchone()
        metadata = json.loads(row[0] or "{}")
        metadata["provider"] = provider
        self.con.execute("UPDATE entity SET metadata_json=? WHERE id=?",
                         (json.dumps(metadata), entity_id))

    def clean(self, entity_id: int, name: str, code: str = "MIAD-495") -> None:
        """同一部作品上另一条名字干净的实体：它建的时候记着这部作品的番号。"""
        self.entity(entity_id, "series", name)
        self.con.execute("UPDATE entity SET metadata_json=? WHERE id=?",
                         (json.dumps({"content_id": code}), entity_id))

    def test_a_clean_entity_on_the_same_code_is_merged_into(self):
        """名字反算不出来（`FETISH BODY` 编回去不是那个坏串），但同番号的干净实体只有一个。"""
        self.clean(2, "FETISH BODY")
        rows = self.rows()
        self.assertEqual(rows[1]["action"], module.MERGE)
        self.assertEqual(rows[1]["reason"], "same-code-clean-sibling")
        self.assertEqual(rows[1]["target"], "FETISH BODY")
        self.assertEqual(rows[1]["conflict_entity_id"], 2)

    def test_two_clean_entities_on_the_same_code_are_resolved_by_source(self):
        """makers 的未打码原名与 r18dev 的打码写法各占一条实体，按来源链认自己那条。"""
        self.clean(2, "輪●計画")
        self.clean(3, "輪姦計画")
        self.provider(1, "amane-makers")
        self.provider(2, "javinizer-go")
        self.provider(3, "amane-makers")
        rows = self.rows()
        self.assertEqual(rows[1]["action"], module.MERGE)
        self.assertEqual(rows[1]["reason"], "same-code-clean-sibling")
        self.assertEqual(rows[1]["conflict_entity_id"], 3)

    def test_two_clean_entities_from_the_same_source_are_left_alone(self):
        self.clean(2, "輪●計画")
        self.clean(3, "輪姦計画")
        self.provider(1, "amane-makers")
        self.provider(2, "amane-makers")
        self.provider(3, "amane-makers")
        rows = self.rows()
        self.assertEqual(rows[1]["action"], module.SKIP)
        self.assertEqual(rows[1]["reason"], "ambiguous-sibling")

    def test_a_clean_entity_that_already_moved_on_is_still_the_one(self):
        """那条实体改过名，但它还挂着这部作品：合并过去，投影跟保留方的现名。"""
        self.clean(2, "FETISH BODY")
        self.con.execute("UPDATE entity SET canonical_name='別名',normalized_name='別名'"
                         " WHERE id=2")
        rows = self.rows()
        self.assertEqual(rows[1]["action"], module.MERGE)
        self.assertEqual(rows[1]["reason"], "same-code-clean-sibling")
        self.assertEqual(rows[1]["conflict_entity_id"], 2)
        self.assertEqual(rows[1]["target"], "別名")

    def test_a_clean_entity_without_that_code_is_not_evidence(self):
        self.clean(2, "FETISH BODY", code="OTHER-001")
        rows = self.rows()
        self.assertEqual(rows[1]["action"], module.SKIP)
        self.assertEqual(rows[1]["reason"], "missing-evidence")


class ReviewedTests(Fixture):
    """快照自己也坏了、又没有同番号干净实体可对：这时只能人看过之后定名字。"""

    def setUp(self):
        super().setUp()
        self.entity(1, "series", DAMAGED)
        self.asset(10, "MIDE-044")
        self.link(10, 1, "series")

    def rows(self, reviewed):
        return module.collect(self.con, self.snapshots, reviewed)

    def test_a_reviewed_name_that_passes_the_byte_check_is_renamed(self):
        self.assertEqual(self.rows({1: CLEAN})[0]["action"], module.RENAME)
        self.assertEqual(self.rows({1: CLEAN})[0]["reason"], "reviewed")
        self.assertEqual(self.rows({1: CLEAN})[0]["target"], CLEAN)

    def test_a_reviewed_name_is_still_written_by_apply(self):
        with self.con:
            module.apply_rows(self.con, self.rows({1: CLEAN}), "2026-10-30T00:00:00Z")
        self.assertEqual(self.con.execute(
            "SELECT canonical_name FROM entity WHERE id=1").fetchone()[0], CLEAN)

    def test_a_reviewed_name_that_does_not_match_is_not_written(self):
        """人定的是「哪个名字」，不是「怎么绕过验证」：对不上坏串就留在复核里。"""
        rows = self.rows({1: "别人的名字"})
        self.assertEqual(rows[0]["action"], module.SKIP)
        self.assertEqual(rows[0]["reason"], "reviewed-name-does-not-match")
        with self.con:
            counts = module.apply_rows(self.con, rows, "2026-10-30T00:00:00Z")
        self.assertEqual(counts["renamed"], 0)
        self.assertEqual(self.con.execute(
            "SELECT canonical_name FROM entity WHERE id=1").fetchone()[0], DAMAGED)

    def test_a_reviewed_name_already_held_by_a_sibling_is_merged(self):
        self.entity(2, "series", CLEAN)
        row = self.rows({1: CLEAN})[0]
        self.assertEqual(row["action"], module.MERGE)
        self.assertEqual(row["reason"], "reviewed")
        self.assertEqual(row["conflict_entity_id"], 2)

    def test_an_entity_without_a_reviewed_name_still_goes_through_evidence(self):
        """没给人名的照旧走自动取证，不能因为传了复核件就整批跳过。"""
        row = self.rows({})[0]
        self.assertEqual(row["action"], module.SKIP)
        self.assertEqual(row["reason"], "missing-evidence")


#: 标题比实体名多一种破坏方式：全角空格被折成半角后，按字节判据就对不上了。
TITLE_CLEAN = "しんゆう\u3000-友達でも恋人でもセフレでもない２人-"
TITLE_DAMAGED = TITLE_CLEAN.encode("gbk").decode("utf-8", errors="replace")


class TitleTests(Fixture):
    """作品标题走同一条错配通道：判据一样，但一个番号一部作品，只能换不能合。"""

    def setUp(self):
        super().setUp()
        self.asset(10, "MIMK-176")
        self.con.execute("UPDATE asset SET catalog_title=? WHERE id=10", (TITLE_DAMAGED,))
        self.con.commit()

    def title_rows(self) -> dict[int, dict]:
        return {int(row["asset_id"]): row
                for row in module.collect_titles(self.con, self.snapshots)}

    def test_a_unique_title_on_the_same_code_is_replaced(self):
        self.snapshot("MIMK-176", "makers", title=TITLE_CLEAN)
        row = self.title_rows()[10]
        self.assertEqual(row["action"], module.REPLACE)
        self.assertEqual(row["target"], TITLE_CLEAN)

    def test_the_full_width_space_survives_the_byte_check(self):
        """全角空格是原文的一部分：折成半角就不再逐字节相等，这条判据就失效了。"""
        self.snapshot("MIMK-176", "makers", title=TITLE_CLEAN)
        self.assertEqual(self.title_rows()[10]["target"], TITLE_CLEAN)
        self.assertIn("\u3000", self.title_rows()[10]["target"])

    def test_a_title_from_another_code_is_not_evidence(self):
        self.snapshot("OTHER-001", "makers", title=TITLE_CLEAN)
        row = self.title_rows()[10]
        self.assertEqual(row["action"], module.SKIP)
        self.assertEqual(row["reason"], "missing-evidence")

    def test_a_damaged_title_in_the_snapshot_is_not_evidence(self):
        self.snapshot("MIMK-176", "makers", title=TITLE_DAMAGED)
        self.assertEqual(self.title_rows()[10]["action"], module.SKIP)

    def test_two_different_originals_are_left_alone(self):
        """两个原名都能反算成坏串时，这条判据在这份数据上不再唯一，不做选择。

        真实来源里确实会这样：同一部作品的打码写法与未打码写法，按 GBK→UTF-8 反算后
        是同一个串。这时选哪个都是猜。
        """
        plain, masked = "輪姦計画", "輪●計画"
        self.assertEqual(module.mangled(plain), module.mangled(masked))
        self.con.execute("UPDATE asset SET catalog_title=? WHERE id=10",
                         (module.mangled(plain),))
        self.con.commit()
        self.snapshot("MIMK-176", "makers", title=plain)
        self.snapshot("MIMK-176", "r18dev", title=masked)
        row = self.title_rows()[10]
        self.assertEqual(row["action"], module.SKIP)
        self.assertEqual(row["reason"], "ambiguous-evidence")

    def test_apply_writes_the_title_and_records_the_owner(self):
        self.snapshot("MIMK-176", "makers", title=TITLE_CLEAN)
        with self.con:
            counts = module.apply_titles(self.con, module.collect_titles(self.con, self.snapshots))
        self.assertEqual(counts["replaced"], 1)
        stored = self.con.execute("SELECT catalog_title FROM asset WHERE id=10").fetchone()[0]
        self.assertEqual(stored, TITLE_CLEAN)

    def test_apply_skips_a_title_that_moved_on(self):
        """计划是照着收集时的值算的：值一变，算出来的原名就不再对应这一行。"""
        self.snapshot("MIMK-176", "makers", title=TITLE_CLEAN)
        rows = module.collect_titles(self.con, self.snapshots)
        with self.con:
            self.con.execute("UPDATE asset SET catalog_title='别人的标题' WHERE id=10")
        with self.con:
            counts = module.apply_titles(self.con, rows)
        self.assertEqual(counts["stale"], 1)
        self.assertEqual(self.con.execute(
            "SELECT catalog_title FROM asset WHERE id=10").fetchone()[0], "别人的标题")

    def test_a_user_written_title_is_refused(self):
        """标题是真相字段：用户手填的值，脚本照样改不动。"""
        self.snapshot("MIMK-176", "makers", title=TITLE_CLEAN)
        self.con.execute(
            "UPDATE asset SET field_owners=? WHERE id=10",
            (json.dumps({"catalog_title": "user:manual"}),))
        self.con.commit()
        with self.con:
            counts = module.apply_titles(self.con, module.collect_titles(self.con, self.snapshots))
        self.assertEqual(counts["refused"], 1)
        self.assertEqual(self.con.execute(
            "SELECT catalog_title FROM asset WHERE id=10").fetchone()[0], TITLE_DAMAGED)


if __name__ == "__main__":
    unittest.main()
