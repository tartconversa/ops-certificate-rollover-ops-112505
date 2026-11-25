"""命令行端到端回归：init → ingest → status/plan/audit/receipt。"""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from rotation.cli import main

ROOT = Path(__file__).resolve().parent.parent
MATERIALS = ROOT / "materials"


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "cli.db")

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            main(["--db", self.db, *argv])
        return out.getvalue()

    def test_full_flow(self):
        self.run_cli("init", "--materials", str(MATERIALS))
        self.run_cli("ingest", "--operations", str(MATERIALS / "operations.jsonl"),
                     "--sessions", str(MATERIALS / "sessions.jsonl"))
        status = self.run_cli("status")
        self.assertIn("t=61", status)
        self.assertIn("legal", status)
        # 迟到的 legal 探针被拒并可见
        self.assertIn("代次已被替换", status)
        plan = self.run_cli("plan")
        self.assertIn("探针", plan)
        audit = self.run_cli("audit", "--group", "legal")
        self.assertIn("代次10", audit)
        self.assertIn("发布准备", audit)
        # 重启后（同一状态库）继续接收回执
        self.run_cli("receipt", "--json",
                     '{"at":62,"kind":"probe","entrance":"n2","group":"legal",'
                     '"site":"north","generation":11,"value":"new:ok"}')
        status = self.run_cli("status")
        self.assertIn("t=62", status)
        group = self.run_cli("group", "legal", "--site", "north")
        self.assertIn("信任版本 v3", group)
        drain = self.run_cli("check-drain", "--entrance", "s1")
        self.assertIn("可安全排空", drain)
        what_if = self.run_cli("what-if", "--group", "assist", "--site",
                               "south", "--extra", "1")
        self.assertIn("不可行", what_if)

    def test_configure_custom_inputs(self):
        # 允许录入不同的到期日、信任覆盖和业务群
        self.run_cli("configure", "chain", "c1", "--valid-from", "0",
                     "--expires", "90")
        self.run_cli("configure", "entrance", "x1", "--site", "east",
                     "--zone", "z1", "--max-sessions", "20",
                     "--allow-groups", "vip", "--opening-chain", "c1",
                     "--opening-generation", "1")
        self.run_cli("configure", "cohort", "vip", "--site", "east",
                     "--minimum-reconnect", "5", "--min-zones", "1",
                     "--session-cap", "30", "--latched", "c1")
        status = self.run_cli("status")
        self.assertIn("east", status)
        self.assertIn("vip", status)


if __name__ == "__main__":
    unittest.main()
