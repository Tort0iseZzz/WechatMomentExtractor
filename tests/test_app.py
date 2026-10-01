"""验证界面勾选、实际导出路径和小窗口布局。"""
import tempfile
import tkinter as tk
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from wechatauto.app import App
from wechatauto.backup import load_backup

class AppTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, persist_selection=False)
        self.app.show_demo()
        self.app.feed_tree.selection_set('0')
        self.app.select_feed()
        self.root.update()

    def tearDown(self): self.app.close()

    def test_reply_filter_manual_choice_survives_filter_changes(self):
        app = self.app
        app.exclude_replies.set(True)
        app.search_comments()
        app.toggle_comment('0')
        self.assertEqual([c['comment_id'] for c in app.selected_comment_feed()['comments']], ['103'])
        app.vars['comment_keyword'].set('突然')
        app.search_comments()
        self.assertEqual(len(app.selected_comment_feed()['comments']), 1)
        app.vars['comment_keyword'].set('')
        app.exclude_replies.set(False)
        app.search_comments()
        self.assertEqual([c['comment_id'] for c in app.selected_comment_feed()['comments']], ['102', '103'])
        app.clear_comments()
        self.assertEqual(len(app.selected_comment_feed()['comments']), 3)

    def test_manual_choices_belong_to_each_moment(self):
        app = self.app
        second = deepcopy(app.feeds[0])
        second['id'], second['tid'] = 'second', 'second'
        app.feeds.append(second)
        app.search()
        app.feed_tree.selection_set('0')
        app.select_feed()
        app.toggle_comment('0')
        app.feed_tree.selection_set('1')
        app.select_feed()
        self.assertEqual(len(app.selected_comment_feed()['comments']), 3)
        app.feed_tree.selection_set('0')
        app.select_feed()
        self.assertEqual(len(app.selected_comment_feed()['comments']), 2)

    def test_export_applies_current_filters_and_keeps_full_export(self):
        app = self.app
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'filtered.json'
            app.vars['comment_keyword'].set('突然')
            with patch('wechatauto.app.filedialog.asksaveasfilename', return_value=str(path)): app.export_filtered()
            filtered = load_backup(path)
            self.assertEqual(filtered['comment_count'], 1)
            self.assertEqual(filtered['export_scope'], 'filtered_comments')
            self.assertNotIn('raw_xml', filtered['moments'][0])
            full_path = Path(directory) / 'full.json'
            with patch('wechatauto.app.filedialog.asksaveasfilename', return_value=str(full_path)): app.export_selected()
            self.assertEqual(load_backup(full_path)['comment_count'], 3)
            self.assertEqual(len(app.feeds[0]['comments']), 3)

    def test_no_selected_comments_does_not_open_save_dialog(self):
        self.app.select_all_comments(False)
        with patch('wechatauto.app.messagebox.showinfo') as notice, patch('wechatauto.app.filedialog.asksaveasfilename') as save:
            self.app.export_filtered()
        notice.assert_called_once()
        save.assert_not_called()

    def test_small_window_buttons_fit_and_content_can_scroll(self):
        self.root.deiconify()
        self.root.geometry('640x480')
        self.root.update()
        app = self.app
        left, top = self.root.winfo_rootx(), self.root.winfo_rooty()
        right, bottom = left + self.root.winfo_width(), top + self.root.winfo_height()
        for button in app.export_buttons:
            self.assertGreaterEqual(button.winfo_rootx(), left)
            self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), right)
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), bottom)
        self.assertGreater(app.content.winfo_height(), app.canvas.winfo_height())
        for _, cells in app.forms:
            self.assertTrue(all(cell.winfo_ismapped() for cell in cells))
            self.assertTrue(all(cell.winfo_rootx() + cell.winfo_width() <= right for cell in cells))
        app.canvas.yview_moveto(1)
        self.root.update()
        self.assertAlmostEqual(app.canvas.yview()[1], 1)

    def test_large_picker_full_text_and_immediate_sync(self):
        app = self.app
        content = '第一行完整评论\n第二行长内容：' + '较长的内容用于验证自动换行。' * 20
        app.feeds[0]['comments'][0]['content'] = content
        self.root.deiconify()
        self.root.update()
        app.open_manual_picker()
        self.root.update()
        picker = app.manual_picker
        self.assertEqual(picker.cards[0].get('1.0', 'end-1c'), content)
        self.assertGreater(int(picker.cards[0].cget('height')), 1)
        self.assertGreaterEqual(int(picker.cards[0].cget('spacing3')), 6)
        picker.content.winfo_children()[0].winfo_children()[0].invoke()
        self.assertEqual(len(app.selected_comment_feed()['comments']), 2)
        picker.close()
        app.open_manual_picker()
        self.assertFalse(app.manual_picker.checks[0].get())
        app.manual_picker.close()

    def test_picker_filters_and_batch_selection_sync(self):
        app = self.app
        app.open_manual_picker()
        picker = app.manual_picker
        app.vars['comment_author'].set('朋友')
        picker.apply_filters()
        self.assertEqual(len(picker.cards), 1)
        app.select_all_comments(False)
        self.assertFalse(picker.checks[1].get())
        self.assertEqual(app.selected_comment_feed()['comments'], [])
        app.clear_comments()
        self.assertEqual(len(picker.cards), 3)
        self.assertTrue(all(value.get() for value in picker.checks.values()))
        picker.close()

    def test_app_restores_saved_filters_and_manual_selection(self):
        with tempfile.TemporaryDirectory() as directory, patch('wechatauto.app.data_directory', return_value=Path(directory)):
            self.app.close()
            self.root = tk.Tk()
            self.root.withdraw()
            self.app = App(self.root)
            self.app.show_demo()
            self.app.feed_tree.selection_set('0')
            self.app.select_feed()
            self.app.exclude_replies.set(True)
            self.app.search_comments()
            self.app.toggle_comment('0')
            self.app.close()
            self.root = tk.Tk()
            self.root.withdraw()
            self.app = App(self.root)
            self.app.show_demo()
            self.app.feed_tree.selection_set('0')
            self.app.select_feed()
            self.assertTrue(self.app.exclude_replies.get())
            self.assertEqual([comment['comment_id'] for comment in self.app.selected_comment_feed()['comments']], ['103'])

if __name__ == '__main__': unittest.main()
