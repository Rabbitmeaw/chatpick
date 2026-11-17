import unittest

from grouppulse.layouts import DEFAULT_LAYOUT, LAYOUTS


class LayoutTests(unittest.TestCase):
    def test_current_eleven_selectable_layouts(self):
        ids = [layout['id'] for layout in LAYOUTS]
        self.assertEqual(len(ids), 11)
        self.assertEqual(len(set(ids)), 11)
        self.assertEqual(sum(key.startswith('external:') for key in ids), 5)
        self.assertEqual(DEFAULT_LAYOUT, 'v1')
        self.assertNotIn('v2', ids)
        self.assertEqual(len({layout['style'] for layout in LAYOUTS}), 11)
        self.assertEqual(sum(layout['platform'] == 'mobile' for layout in LAYOUTS), 6)
        self.assertEqual(sum(layout['platform'] == 'desktop' for layout in LAYOUTS), 5)
        forbidden = ('wex' + 'in', 'wei' + 'xin', 'we' + 'chat', '\u5fae\u4fe1')
        for layout in LAYOUTS:
            display = (layout['name'] + layout['description']).lower()
            self.assertFalse(any(word in display for word in forbidden))
        self.assertEqual(next(item['name'] for item in LAYOUTS if item['id'] == 'mobile-feed'), '轻量信息流')

    def test_every_layout_preserves_report_sections(self):
        for layout in LAYOUTS:
            with self.subTest(layout=layout['name']):
                self.assertCountEqual(layout['order'], ['hero', 'topics', 'actions', 'important'])
                self.assertIn(layout['platform'], ('mobile', 'desktop'))
                self.assertTrue(layout['name'] and layout['description'])


if __name__ == '__main__':
    unittest.main()
