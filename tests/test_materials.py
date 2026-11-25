import unittest

from materials import list_feeds, read_feed

FIELDS = {
    'entrances': ('entrance', 'site', 'fault_zone', 'max_sessions', 'opening_chain', 'opening_generation', 'allow_groups'),
    'cohorts': ('group', 'site', 'minimum_reconnect', 'min_zones', 'latched_chains', 'session_cap_min'),
    'chains': ('chain', 'valid_from', 'expires'),
    'operations': ('at', 'kind', 'entrance', 'group', 'site', 'generation', 'value'),
    'sessions': ('session_set', 'group', 'entrance', 'chain', 'begin_min', 'end_notice_min', 'count'),
}


class MaterialTest(unittest.TestCase):
    def test_feed_fields(self):
        self.assertEqual(list_feeds(), sorted(FIELDS))
        for name, fields in FIELDS.items():
            with self.subTest(feed=name):
                records = list(read_feed(name))
                self.assertTrue(records)
                self.assertTrue(all(set(row) == set(fields) for row in records))
