"""轮换演练引擎规则测试。

前一类用例基于 materials/ 的演练资料做端到端核对；
后一类用合成场景逐条钉死证据、容量与生命周期规则。
"""
import tempfile
import unittest
from pathlib import Path

from rotation import Engine, audit_group

ROOT = Path(__file__).resolve().parent.parent
MATERIALS = ROOT / "materials"


def read_jsonl(path):
    import json
    with open(path, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def materials_engine(directory):
    engine = Engine.open(Path(directory) / "drill.db")
    for row in read_jsonl(MATERIALS / "chains.jsonl"):
        engine.configure_chain(row["chain"], row["valid_from"], row["expires"], since=0)
    for row in read_jsonl(MATERIALS / "entrances.jsonl"):
        engine.configure_entrance(row["entrance"], row["site"], row["fault_zone"],
                                  row["max_sessions"], row["allow_groups"],
                                  row["opening_chain"], row["opening_generation"],
                                  since=0)
    for row in read_jsonl(MATERIALS / "cohorts.jsonl"):
        engine.configure_cohort(row["group"], row["site"], row["minimum_reconnect"],
                                row["min_zones"], row["session_cap_min"],
                                row["latched_chains"], since=0)
    engine.ingest_sessions(read_jsonl(MATERIALS / "sessions.jsonl"))
    engine.ingest_receipts(read_jsonl(MATERIALS / "operations.jsonl"))
    return engine


class TempEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def open_engine(self):
        return materials_engine(self.tmp.name)


class MaterialsScenarioTest(TempEngineTest):
    def test_final_status(self):
        eng = self.open_engine()
        self.assertEqual(eng.clock, 61)
        st = eng.status()
        north = st["sites"]["north"]["cohorts"]
        self.assertEqual(north["assist"]["state"], "合规")
        self.assertEqual(north["assist"]["zones_covered"], 2)
        # legal 切到信任版本 v3 后缺少 n2 的新探针，故障域覆盖不足
        self.assertEqual(north["legal"]["state"], "不足")
        self.assertEqual(north["legal"]["zones_covered"], 1)
        self.assertEqual(north["media"]["state"], "合规")
        south = st["sites"]["south"]["cohorts"]
        self.assertEqual(south["assist"]["state"], "合规")
        self.assertEqual(south["media"]["state"], "合规")
        eng.close()

    def test_late_probe_for_replaced_generation_rejected(self):
        eng = self.open_engine()
        late = [r for r in eng.rejected if r["kind"] == "probe"]
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["payload"]["generation"], 10)
        self.assertEqual(late[0]["at"], 52)
        self.assertIn("替换", late[0]["reason"])
        # 迟到成功没有让旧代次重新生效
        self.assertEqual(eng._current_gen_at("n4", 61), 12)
        eng.close()

    def test_opening_observation_and_unknown_generation(self):
        eng = self.open_engine()
        # 开局观察：初始链上的同站点信任覆盖
        self.assertTrue(eng.path_valid("assist", "north", "n1", 5)[0])
        # n2 在 23 仍靠开局代次10 服务；代次11 已同步(22)但未生效，不计
        self.assertEqual(eng._current_gen_at("n2", 23), 10)
        self.assertEqual(eng.served_log["n2"], [(0, 10), (25, 11)])
        self.assertEqual(eng.status(23)["pending"],
                         [{"entrance": "n2", "generation": 11, "chain": "new",
                           "state": "已同步待生效"}])
        # served(25) 之后还需该代次探针：assist 41 才有
        self.assertFalse(eng.path_valid("assist", "north", "n2", 30)[0])
        self.assertTrue(eng.path_valid("assist", "north", "n2", 41)[0])
        eng.close()

    def test_chain_expiry_blocks_new_handshakes_only(self):
        eng = self.open_engine()
        # old 链 54 到期：s1 之后禁止新握手
        self.assertTrue(eng.path_valid("assist", "south", "s1", 53)[0])
        self.assertFalse(eng.path_valid("assist", "south", "s1", 54)[0])
        # 但存续会话维持到回报/业务时限：g5 在 old 链上维持到 61
        self.assertEqual(eng.occupancy("n4", 54), 19)
        self.assertEqual(eng.occupancy("n4", 60), 19)
        self.assertEqual(eng.occupancy("n4", 61), 0)
        eng.close()

    def test_draining_entrance_not_counted(self):
        eng = self.open_engine()
        # n1 于 27 提交排空：不再计入新会话容量，但占用仍按回报扣除
        self.assertFalse(eng.path_valid("legal", "north", "n1", 27)[0])
        self.assertEqual(eng.occupancy("n1", 27), 37)  # g1 18 + g2 19
        self.assertEqual(eng.occupancy("n1", 29), 18)  # g2 结束
        eng.close()

    def test_trust_version_scopes_evidence(self):
        eng = self.open_engine()
        # legal 在 42 的探针按信任版本 v2 记录；v3(57) 后该证据不再适用
        self.assertTrue(eng.path_valid("legal", "north", "n2", 56)[0])
        self.assertFalse(eng.path_valid("legal", "north", "n2", 58)[0])
        # n4 代次12 在 58 的探针按 v3 记录，renewed 在 v3 内
        self.assertTrue(eng.path_valid("legal", "north", "n4", 58)[0])
        eng.close()

    def test_offline_and_restore(self):
        eng = self.open_engine()
        self.assertFalse(eng.path_valid("assist", "north", "n3", 50)[0])
        self.assertTrue(eng.path_valid("assist", "north", "n3", 60)[0])
        # 离线期间 media@north 无合规路径，但 g5 可维持到 61
        st = eng.status(50)
        media = [u for u in st["unguaranteed"]
                 if u["group"] == "media" and u["site"] == "north"]
        self.assertEqual(media[0]["state"], "无合规路径")
        self.assertEqual(media[0]["maintainable"][0]["ends_at"], 61)
        eng.close()

    def test_shared_capacity_and_joint_feasibility(self):
        eng = self.open_engine()
        # 33–44：n2 新代次尚无探针（暗），n1 排空中，可用 40 远低于联合需求 83
        rep = eng.site_analysis("north", 33)
        self.assertFalse(rep["joint_feasible"])
        self.assertEqual(rep["total_minimum"], 83)
        self.assertEqual(rep["total_free"], 40)
        # 探针补齐、会话结束后恢复
        self.assertTrue(eng.site_analysis("north", 61)["joint_feasible"])
        eng.close()

    def test_zone_coverage_requires_allocatable_capacity(self):
        eng = self.open_engine()
        # t=50：n3 离线，assist 只剩 rack-b 一个域有实际容量
        north = eng.site_analysis("north", 50)["cohorts"]
        self.assertEqual(north["assist"]["zones_covered"], 1)
        self.assertEqual(north["assist"]["zone_capacity"], {"rack-b": 23})
        eng.close()

    def test_migration_headroom_what_if(self):
        eng = self.open_engine()
        # t=61 南站：old 链已到期，只剩 s2 的 50 路，assist 与 media 恰好分满
        self.assertTrue(eng.check_shift("assist", "south", 0, 61)["joint_feasible"])
        # assist 加速切换多占 1 路，media 的最低量就不保
        result = eng.check_shift("assist", "south", 1, 61)
        self.assertFalse(result["joint_feasible"])
        self.assertLess(result["cohorts"]["media"]["guaranteed"], 22)
        eng.close()

    def test_persistence_across_restart(self):
        eng = self.open_engine()
        before = eng.status()
        db = Path(self.tmp.name) / "drill.db"
        eng.close()
        # 控制程序重启：存续会话与已生效入口延续
        eng2 = Engine.open(db)
        self.assertEqual(eng2.status(), before)
        # 继续接收回执：legal 补打 n2 探针后恢复合规
        eng2.ingest_receipts([{"at": 62, "kind": "probe", "entrance": "n2",
                               "group": "legal", "site": "north",
                               "generation": 11, "value": "new:ok"}])
        north = eng2.status()["sites"]["north"]["cohorts"]
        self.assertEqual(north["legal"]["state"], "合规")
        eng2.close()

    def test_audit_milestones_for_n4_legal(self):
        eng = self.open_engine()
        rows = [r for r in audit_group(eng, "legal")
                if r["entrance"] == "n4" and r["old_generation"] == 10]
        self.assertEqual(len(rows), 1)
        m = rows[0]["milestones"]
        self.assertEqual(m["publish_ready"], 43)   # sync 代次12
        self.assertEqual(m["allow_traffic"], 58)   # legal 信任+探针齐备
        self.assertEqual(m["replaced_at"], 49)     # 实际替换早于允许接流
        self.assertEqual(m["actual_end"], 61)      # g5 维持到 61
        self.assertIsNone(m["drain_allowed"])      # 覆盖始终不足，无安全排空点
        self.assertTrue(any("不存在" in f for f in rows[0]["flags"]))
        self.assertTrue(any("早于该群允许接流" in f for f in rows[0]["flags"]))
        eng.close()

    def test_audit_drain_only_episode(self):
        eng = self.open_engine()
        rows = [r for r in audit_group(eng, "assist")
                if r["entrance"] == "n1"]
        self.assertEqual(len(rows), 1)
        m = rows[0]["milestones"]
        self.assertEqual(m["drained_at"], 27)
        self.assertEqual(m["actual_end"], 44)
        # 安全窗口 [16,24]：27 的排空提交已晚于窗口
        self.assertIn((16, 24), rows[0]["safe_windows"])
        eng.close()


class SyntheticRuleTest(unittest.TestCase):
    """合成场景：逐条钉死证据与生命周期规则。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.eng = Engine.open(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.eng.close)
        self.eng.configure_chain("old", 0, 100, since=0)
        self.eng.configure_chain("new", 10, 200, since=0)
        self.eng.configure_entrance("e1", "s", "z1", 10, ["g"], "old", 1, since=0)
        self.eng.configure_cohort("g", "s", 5, 1, 40, ["old", "new"], since=0)

    def receipt(self, **row):
        self.eng.ingest_receipts([row])

    def test_served_chain_mismatch_rejected(self):
        self.receipt(at=20, kind="sync", entrance="e1", generation=2, value="new")
        self.receipt(at=21, kind="served", entrance="e1", generation=2, value="old")
        self.assertEqual(len(self.eng.rejected), 1)
        self.assertIn("不符", self.eng.rejected[0]["reason"])
        self.assertEqual(self.eng._current_gen_at("e1", 30), 1)

    def test_probe_direction_rejected(self):
        self.receipt(at=20, kind="served", entrance="e1", generation=2, value="new")
        # 群无此入口路由权限
        self.receipt(at=21, kind="probe", entrance="e1", generation=2,
                     group="other", site="s", value="new:ok")
        # 群在此站点无登记
        self.eng.configure_entrance("e2", "s2", "z2", 10, ["g"], "old", 1, since=0)
        self.receipt(at=22, kind="probe", entrance="e2", generation=1,
                     group="g", site="s2", value="old:ok")
        reasons = [r["reason"] for r in self.eng.rejected]
        self.assertEqual(sum("方向不符" in r for r in reasons), 2)

    def test_probe_ok_with_untrusted_chain_rejected(self):
        self.eng.configure_cohort("h", "s", 5, 1, 40, ["old"], since=0)
        self.eng.configure_entrance("e3", "s", "z3", 10, ["h"], "old", 1, since=0)
        self.receipt(at=20, kind="served", entrance="e3", generation=2, value="new")
        self.receipt(at=21, kind="probe", entrance="e3", generation=2,
                     group="h", site="s", value="new:ok")
        self.assertEqual(len(self.eng.rejected), 1)
        self.assertIn("信任", self.eng.rejected[0]["reason"])
        # 同情形报 failed 则与信任一致，予以记录但不构成证据
        self.receipt(at=22, kind="probe", entrance="e3", generation=2,
                     group="h", site="s", value="new:failed")
        self.assertEqual(len(self.eng.rejected), 1)
        self.assertFalse(self.eng.path_valid("h", "s", "e3", 30)[0])

    def test_probe_before_served_rejected(self):
        self.receipt(at=20, kind="sync", entrance="e1", generation=2, value="new")
        self.receipt(at=21, kind="probe", entrance="e1", generation=2,
                     group="g", site="s", value="new:ok")
        self.assertEqual(len(self.eng.rejected), 1)
        self.assertIn("尚未生效", self.eng.rejected[0]["reason"])

    def test_late_sync_and_stale_trust_rejected(self):
        self.receipt(at=20, kind="served", entrance="e1", generation=2, value="new")
        self.receipt(at=30, kind="sync", entrance="e1", generation=1, value="old")
        self.receipt(at=31, kind="trust", group="g", site="s", generation=2,
                     value=["new"])
        self.receipt(at=32, kind="trust", group="g", site="s", generation=2,
                     value=["old", "new"])
        kinds = [r["kind"] for r in self.eng.rejected]
        self.assertEqual(kinds, ["sync", "trust"])

    def test_drain_only_current_generation(self):
        self.receipt(at=20, kind="served", entrance="e1", generation=2, value="new")
        self.receipt(at=21, kind="drain", entrance="e1", generation=1)
        self.assertEqual(len(self.eng.rejected), 1)
        self.receipt(at=22, kind="drain", entrance="e1", generation=2)
        self.assertFalse(self.eng.path_valid("g", "s", "e1", 23)[0])

    def test_session_business_cap_beats_late_notice(self):
        self.eng.ingest_sessions([{"session_set": "x1", "group": "g",
                                   "entrance": "e1", "chain": "old",
                                   "begin_min": 10, "end_notice_min": 80,
                                   "count": 3}])
        # 业务时限 40：10+40=50 即扣除，迟到的 80 回报不延长占用
        self.assertEqual(self.eng.occupancy("e1", 49), 3)
        self.assertEqual(self.eng.occupancy("e1", 50), 0)

    def test_end_notice_can_arrive_after_drain(self):
        self.eng.ingest_sessions([{"session_set": "x1", "group": "g",
                                   "entrance": "e1", "chain": "old",
                                   "begin_min": 10, "end_notice_min": None,
                                   "count": 3}])
        self.receipt(at=20, kind="drain", entrance="e1", generation=1)
        self.assertEqual(self.eng.occupancy("e1", 30), 3)
        # 排空以后才到的终止回报照常扣除
        self.eng.ingest_sessions([{"session_set": "x1", "group": "g",
                                   "entrance": "e1", "chain": "old",
                                   "begin_min": 10, "end_notice_min": 35,
                                   "count": 3}])
        self.assertEqual(self.eng.occupancy("e1", 34), 3)
        self.assertEqual(self.eng.occupancy("e1", 35), 0)

    def test_duplicate_receipts_ignored(self):
        row = {"at": 20, "kind": "sync", "entrance": "e1", "generation": 2,
               "value": "new"}
        self.assertEqual(self.eng.ingest_receipts([row])["new"], 1)
        self.assertEqual(self.eng.ingest_receipts([row])["duplicates"], 1)

    def test_expired_chain_no_new_handshake_even_below_minimum(self):
        # 唯一路径的链到期后，即使低于最低容量也不放行新握手
        self.eng.configure_chain("old", 0, 30, since=0)
        self.assertTrue(self.eng.path_valid("g", "s", "e1", 29)[0])
        ok, reason = self.eng.path_valid("g", "s", "e1", 30)
        self.assertFalse(ok)
        self.assertIn("到期", reason)
        rep = self.eng.site_analysis("s", 30)
        self.assertEqual(rep["cohorts"]["g"]["state"], "无合规路径")


if __name__ == "__main__":
    unittest.main()
