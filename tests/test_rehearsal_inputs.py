import unittest

from materials import read_feed


class InputsTest(unittest.TestCase):
    def test_chain_generations_and_shared_sessions(self):
        entries = {r["entrance"]: r for r in read_feed("entrances")}
        chains = {r["chain"]: r for r in read_feed("chains")}
        sessions = list(read_feed("sessions"))
        for row in sessions:
            self.assertIn(row["group"], entries[row["entrance"]]["allow_groups"])
            cert = chains[row["chain"]]
            self.assertLessEqual(cert["valid_from"], row["begin_min"])
            self.assertLess(row["begin_min"], cert["expires"])
        observations = list(read_feed("operations"))
        change = next(r for r in observations if r["kind"] == "served" and r["entrance"] == "n4")
        probe = next(r for r in observations if r["kind"] == "probe" and r["value"] == "old:ok")
        self.assertGreater(probe["at"], change["at"])
        self.assertLess(probe["generation"], change["generation"])
        self.assertTrue(any(r["end_notice_min"] > chains["old"]["expires"] for r in sessions if r["chain"] == "old"))

