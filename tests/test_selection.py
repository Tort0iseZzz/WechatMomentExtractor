"""验证手动筛选在刷新与重新启动后能恢复，且文件中不保存评论正文。"""
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from wechatauto.app import demo_feeds
from wechatauto.selection import SelectionStore

class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'selections.json'
        self.feed = demo_feeds()[0]

    def tearDown(self): self.temp.cleanup()

    def test_restore_after_restart_and_comment_reordering(self):
        store = SelectionStore(self.path)
        store.save(self.feed, {0}, {'exclude_replies': True, 'keyword': '散步'})
        restored = SelectionStore(self.path)
        self.assertEqual(restored.restore(self.feed), {0})
        self.assertTrue(restored.filters(self.feed)['exclude_replies'])
        reordered = deepcopy(self.feed)
        reordered['comments'].reverse()
        self.assertEqual(restored.restore(reordered), {2})
        self.assertNotIn(self.feed['comments'][0]['content'], self.path.read_text(encoding='utf-8'))

    def test_separate_moments_and_clear_saved_state(self):
        store = SelectionStore(self.path)
        store.save(self.feed, {0})
        other = deepcopy(self.feed)
        other['id'] = 'different'
        self.assertEqual(store.restore(other), set())
        store.save(self.feed, set(), {'exclude_replies': False})
        self.assertEqual(SelectionStore(self.path).restore(self.feed), set())

    def test_missing_ids_use_fingerprint(self):
        self.feed['comments'][0]['comment_id'] = ''
        store = SelectionStore(self.path)
        store.save(self.feed, {0})
        self.assertEqual(SelectionStore(self.path).restore(self.feed), {0})

    def test_failed_atomic_save_keeps_previous_state(self):
        store = SelectionStore(self.path)
        store.save(self.feed, {0})
        with patch('wechatauto.selection.os.replace', side_effect=OSError('locked')):
            with self.assertRaises(OSError): store.save(self.feed, {1})
        self.assertEqual(SelectionStore(self.path).restore(self.feed), {0})
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

if __name__ == '__main__': unittest.main()
