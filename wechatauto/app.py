"""朋友圈只读桌面工具；所有微信读取在后台线程中完成。"""
from __future__ import annotations
import argparse
import ctypes
import io
import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from .backup import LIMITATION, filtered_feed, load_backup, make_backup, save_backup
from .moment import MomentDB, SHANGHAI, date_bound, filter_comments, filter_feeds, is_reply, parse_feed, timestamp_text
from .selection import SelectionStore

def data_directory():
    override = os.environ.get('MOMENT_EXTRACTOR_DATA_DIR')
    return Path(override) if override else Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'WechatMomentExtractor'

def demo_feeds():
    """演示记录不读取微信，亦不冒充真实数据。"""
    xml = '''<SnsDataItem><id>9000000000000000001</id><username>wxid_demo</username><nickname>演示用户</nickname>
    <createTime>1790812800</createTime><contentDesc><![CDATA[废话条 · 日常记录\n在这里持续记录自己的想法。]]></contentDesc>
    <commentCount>3</commentCount><comment_user_list>
    <user_comment><username>wxid_demo</username><nickname>演示用户</nickname><content><![CDATA[今天散步，看到了很好看的晚霞。]]></content>
    <create_time>1790848800</create_time><comment_id>101</comment_id><b_deleted>0</b_deleted></user_comment>
    <user_comment><username>wxid_friend</username><nickname>朋友</nickname><content>秋天真好 &amp; 希望天气继续晴朗</content>
    <create_time>1790850600</create_time><comment_id>102</comment_id><ref_comment_id>101</ref_comment_id><b_deleted>0</b_deleted></user_comment>
    <user_comment><username>wxid_demo</username><nickname>演示用户</nickname><content>突然想到一个点子：每天留下一句话。</content>
    <create_time>1790851200</create_time><comment_id>103</comment_id><b_deleted>0</b_deleted></user_comment>
    </comment_user_list></SnsDataItem>'''
    return [parse_feed(xml, tid='42')]

class App:
    def __init__(self, root, persist_selection=True):
        self.root = root
        self.root.title('废话条 · 朋友圈只读备份')
        self.root.geometry('940x760')
        self.root.minsize(640, 480)
        self.feeds, self.visible, self.reader = [], [], None
        self.account, self.source, self.protected_root = '', 'local_wechat_db', None
        self.events, self.busy = queue.Queue(), False
        self.closed, self.poll_timer = False, None
        self.vars = {name: tk.StringVar() for name in ('directory', 'account', 'keyword', 'author', 'since', 'until',
                                                     'comment_keyword', 'comment_author', 'comment_since', 'comment_until')}
        try:
            preferences = json.loads((data_directory() / 'preferences.json').read_text(encoding='utf-8'))
            if isinstance(preferences.get('directory'), str): self.vars['directory'].set(preferences['directory'])
            self.last_account = preferences.get('account', '')
        except (OSError, ValueError, AttributeError): self.last_account = ''
        self.only_self = tk.BooleanVar(value=True)
        self.exclude_replies = tk.BooleanVar(value=False)
        self.manual_exclusions, self.current_comments, self.current_indices = {}, [], []
        self.manual_picker = None
        self.active_filter_key = None
        self.applied_comment_criteria = {}
        self.selection_store = SelectionStore(data_directory() / 'comment_selections.json') if persist_selection else None
        self.selection_save_status = tk.StringVar(value='勾选变化自动保存，关闭窗口后仍然保留。')
        self.comment_stats = tk.StringVar(value='请选择朋友圈，再筛选和勾选评论。')
        self.status = tk.StringVar(value='请登录 PC 微信，在微信中打开目标朋友圈，然后点击「扫描账号」。')
        self.summary = tk.StringVar(value='选择一条朋友圈，核对正文与日期后导出。')
        self._build()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_timer = self.root.after(100, self._poll)

    def _build(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10))
        style.configure('Treeview', rowheight=29)
        style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 10, 'bold'))
        style.configure('Export.TButton', background='#167565', foreground='white', font=('Microsoft YaHei UI', 11, 'bold'), padding=(10, 7))
        style.map('Export.TButton', background=[('active', '#105c50'), ('pressed', '#0a493f')])
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill='both', expand=True)
        self.wrap_labels, self.forms, self.action_grids = [], [], []
        status_label = ttk.Label(outer, textvariable=self.status, wraplength=900)
        status_label.pack(side='bottom', fill='x', pady=(6, 0))
        self.wrap_labels.append(status_label)
        export = ttk.LabelFrame(outer, text='04  导出 JSON', padding=8)
        export.pack(side='bottom', fill='x', pady=(8, 0))
        export.columnconfigure((0, 1), weight=1)
        stats = ttk.Label(export, textvariable=self.comment_stats, wraplength=880)
        stats.grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 5))
        self.wrap_labels.append(stats)
        self.export_buttons = []
        for row, column, span, title, command, button_style in [
            (1, 0, 2, '导出筛选的评论', self.export_filtered, 'Export.TButton'),
            (2, 0, 1, '导出整条朋友圈及完整评论区', self.export_selected, 'TButton'),
            (2, 1, 1, '导出朋友圈搜索结果', self.export_visible, 'TButton')]:
            button = ttk.Button(export, text=title, command=command, style=button_style)
            button.grid(row=row, column=column, columnspan=span, sticky='ew', padx=2, pady=3)
            self.export_buttons.append(button)
        ttk.Label(outer, text='废话条 / 朋友圈备份', font=('Microsoft YaHei UI', 17, 'bold')).pack(anchor='w', pady=(0, 6))
        viewport = ttk.Frame(outer)
        viewport.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(viewport, highlightthickness=0, background=style.lookup('TFrame', 'background'))
        scrollbar = ttk.Scrollbar(viewport, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.content = ttk.Frame(self.canvas)
        self.canvas_item = self.canvas.create_window((0, 0), window=self.content, anchor='nw')
        self.content.bind('<Configure>', lambda event: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', self._resize)
        self.root.bind_all('<MouseWheel>', self._wheel, add='+')
        connect = self._section('01  读取微信或打开备份')
        directory = ttk.Frame(connect)
        directory.pack(fill='x')
        ttk.Label(directory, text='数据目录').pack(side='left')
        ttk.Entry(directory, textvariable=self.vars['directory'], width=12).pack(side='left', fill='x', expand=True, padx=6)
        self._button(directory, '选择目录', self.choose_directory)
        account = ttk.Frame(connect)
        account.pack(fill='x', pady=6)
        ttk.Label(account, text='微信账号').pack(side='left')
        self.account_box = ttk.Combobox(account, textvariable=self.vars['account'], state='readonly', width=16)
        self.account_box.pack(side='left', fill='x', expand=True, padx=6)
        self._button(account, '扫描账号', self.scan_accounts)
        self._button(account, '读取 / 刷新', self.connect)
        actions = ttk.Frame(connect)
        actions.pack(fill='x')
        self._button(actions, '打开 JSON 备份', self.open_json)
        self._button(actions, '查看演示', self.show_demo)
        warning = ttk.Label(connect, text=LIMITATION, foreground='#8b5b18', wraplength=880)
        warning.pack(fill='x', pady=(8, 0))
        self.wrap_labels.append(warning)
        choose = self._section('02  查找并确认朋友圈')
        self._form(choose, [('正文关键词 / ID', 'keyword'), ('发布者', 'author'), ('起始日期', 'since'), ('结束日期', 'until')])
        actions = ttk.Frame(choose)
        actions.pack(fill='x', pady=(4, 8))
        self._button(actions, '查找', self.search)
        self._button(actions, '清空', self.clear_search)
        ttk.Checkbutton(actions, text='仅我的朋友圈', variable=self.only_self, command=self.search).pack(side='left', padx=8)
        ttk.Label(choose, text='日期格式：YYYY-MM-DD', foreground='#555555').pack(anchor='w', pady=(0, 5))
        feed_frame = ttk.Frame(choose)
        feed_frame.pack(fill='x')
        self.feed_tree = self._tree(feed_frame, [('time', '发布时间', 150), ('author', '发布者', 100), ('text', '朋友圈正文', 400), ('count', '评论数', 70)])
        self.feed_tree.configure(height=3)
        self.feed_tree.bind('<<TreeviewSelect>>', self.select_feed)
        summary = ttk.Label(choose, textvariable=self.summary, wraplength=880)
        summary.pack(fill='x', pady=(8, 4))
        self.wrap_labels.append(summary)
        self.body = self._text_box(choose, 3)
        comments_section = self._section('03  筛选评论，再逐条勾选')
        self._form(comments_section, [('评论关键词', 'comment_keyword'), ('评论者', 'comment_author'),
                                      ('起始日期', 'comment_since'), ('结束日期', 'comment_until')])
        ttk.Checkbutton(comments_section, text='不包括全部回复消息', variable=self.exclude_replies, command=self.search_comments).pack(anchor='w', pady=5)
        actions = ttk.Frame(comments_section)
        actions.pack(fill='x', pady=(0, 6))
        buttons = [ttk.Button(actions, text=title, command=command) for title, command in [
            ('筛选评论', self.search_comments), ('重置筛选与勾选', self.clear_comments),
            ('勾选当前结果', lambda: self.select_all_comments(True)), ('取消当前勾选', lambda: self.select_all_comments(False))]]
        self.action_grids.append((actions, buttons))
        ttk.Button(comments_section, text='手动筛选（大窗口）', command=self.open_manual_picker, style='Export.TButton').pack(fill='x', pady=(2, 8))
        ttk.Label(comments_section, text='点击「导出」列切换勾选；也可选中评论后按空格。', foreground='#555555').pack(anchor='w', pady=(0, 5))
        comments = ttk.Frame(comments_section)
        comments.pack(fill='x')
        self.comment_tree = self._tree(comments, [('include', '导出', 50), ('time', '评论时间', 150), ('author', '评论者', 100),
                                                 ('content', '评论内容', 400), ('reply', '回复评论 ID', 95)])
        self.comment_tree.configure(height=5)
        self.comment_tree.tag_configure('excluded', foreground='#8a8a8a')
        self.comment_tree.bind('<<TreeviewSelect>>', self.select_comment)
        self.comment_tree.bind('<Button-1>', self._comment_click, add='+')
        self.comment_tree.bind('<space>', self._comment_space)
        self.comment_body = self._text_box(comments_section, 3)
        self.root.bind('<Control-f>', lambda event: self.search())
        self.root.bind('<F5>', lambda event: self.connect())

    def _section(self, title):
        frame = ttk.LabelFrame(self.content, text=title, padding=8)
        frame.pack(fill='x', padx=(0, 6), pady=(0, 10))
        return frame

    def _form(self, parent, fields):
        frame = ttk.Frame(parent)
        frame.pack(fill='x')
        cells = []
        for label, name in fields:
            cell = ttk.Frame(frame)
            ttk.Label(cell, text=label, width=14).pack(side='left')
            entry = ttk.Entry(cell, textvariable=self.vars[name], width=12)
            entry.pack(side='left', fill='x', expand=True, padx=(0, 8))
            entry.bind('<Return>', lambda event, field=name: self.search_comments() if field.startswith('comment_') else self.search())
            cells.append(cell)
        self.forms.append((frame, cells))

    def _resize(self, event):
        width = event.width
        self.canvas.itemconfigure(self.canvas_item, width=width)
        columns = 2 if width >= 760 else 1
        for frame, cells in self.forms:
            for column in (0, 1): frame.columnconfigure(column, weight=1 if column < columns else 0)
            for index, cell in enumerate(cells): cell.grid(row=index // columns, column=index % columns, sticky='ew', pady=3)
        for label in self.wrap_labels: label.configure(wraplength=max(280, width - 36))
        for frame, buttons in self.action_grids:
            count = 4 if width >= 760 else 2
            for column in range(4): frame.columnconfigure(column, weight=1 if column < count else 0)
            for index, button in enumerate(buttons): button.grid(row=index // count, column=index % count, sticky='ew', padx=2, pady=2)
        for index, button in enumerate(self.export_buttons[1:]):
            button.grid(row=2 if width >= 760 else 2 + index, column=index if width >= 760 else 0, columnspan=1 if width >= 760 else 2)

    def _wheel(self, event):
        if isinstance(event.widget, (ttk.Treeview, tk.Text)): return
        widget = event.widget
        while widget is not None:
            if widget == self.content or widget == self.canvas:
                self.canvas.yview_scroll(-int(event.delta / 120), 'units')
                return 'break'
            widget = getattr(widget, 'master', None)

    def _text_box(self, parent, height):
        frame = ttk.Frame(parent)
        frame.pack(fill='x', pady=(6, 0))
        text = tk.Text(frame, height=height, wrap='word', font=('Microsoft YaHei UI', 10), background='#f8fafc', relief='flat')
        scroll = ttk.Scrollbar(frame, orient='vertical', command=text.yview)
        text.configure(yscrollcommand=scroll.set, state='disabled')
        scroll.pack(side='right', fill='y')
        text.pack(side='left', fill='x', expand=True)
        return text

    def _button(self, parent, text, command):
        ttk.Button(parent, text=text, command=command).pack(side='left', padx=3)

    def _tree(self, parent, columns):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        tree = ttk.Treeview(parent, columns=[c[0] for c in columns], show='headings', selectmode='browse', height=4)
        for name, label, width in columns:
            tree.heading(name, text=label)
            tree.column(name, width=width, minwidth=60, stretch=name in {'text', 'content'})
        vertical = ttk.Scrollbar(parent, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(parent, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        return tree

    def _background(self, job, done):
        if self.busy: return
        self.busy = True
        self.status.set('正在读取，请稍候；首次提取密钥可能需要较长时间。')
        def worker():
            try: self.events.put((done, job(), None))
            except Exception as exc:
                logging.exception('读取失败')
                self.events.put((done, None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        if self.closed: return
        try:
            while True:
                done, result, error = self.events.get_nowait()
                self.busy = False
                if error:
                    self.status.set('读取失败：' + error)
                    messagebox.showerror('读取失败', error, parent=self.root)
                else: done(result)
        except queue.Empty: pass
        self.poll_timer = self.root.after(100, self._poll)

    def close(self):
        self.closed = True
        if self.manual_picker is not None: self.manual_picker.close()
        if self.poll_timer is not None: self.root.after_cancel(self.poll_timer)
        self.root.unbind_all('<MouseWheel>')
        self.root.destroy()

    def choose_directory(self):
        if self.busy: return
        path = filedialog.askdirectory(title='选择 xwechat_files 或账号目录', parent=self.root)
        if path: self.vars['directory'].set(path); self.scan_accounts()

    def scan_accounts(self):
        if self.busy: return
        directory = self.vars['directory'].get().strip()
        def job():
            from .db import auto_detect_db_dir, list_accounts
            path = Path(directory) if directory else Path(auto_detect_db_dir() or '')
            if (path / 'db_storage').is_dir(): path = path.parent
            return str(path), list_accounts(str(path))
        def done(result):
            directory, accounts = result
            self.vars['directory'].set(directory)
            names = [a['account'] for a in accounts]
            self.account_box.configure(values=names)
            self.vars['account'].set(self.last_account if self.last_account in names else names[0] if names else '')
            self.status.set(f'找到 {len(names)} 个账号目录，请确认账号后点击「读取 / 刷新」。' if names else '未找到账号目录，请手动选择微信设置中显示的 xwechat_files 目录。')
        self._background(job, done)

    def connect(self):
        if self.busy: return
        directory, account = self.vars['directory'].get().strip(), self.vars['account'].get().strip()
        if not directory or not account:
            messagebox.showinfo('先选择账号', '请先扫描账号，并确认要读取的账号。', parent=self.root)
            return
        def job():
            from .db import WeChatDB
            workdir = data_directory() / 'cache' / account
            workdir.mkdir(parents=True, exist_ok=True)
            db = WeChatDB(db_dir=directory, account=account, workdir=str(workdir))
            reader = MomentDB(db)
            return reader, reader.get_moments(), db.wxid
        def done(result):
            self.reader, self.feeds, self.account = result
            self.manual_exclusions.clear()
            self.source, self.protected_root = 'local_wechat_db', directory
            try:
                data_directory().mkdir(parents=True, exist_ok=True)
                (data_directory() / 'preferences.json').write_text(json.dumps({'directory': directory, 'account': account}, ensure_ascii=False), encoding='utf-8')
            except OSError: pass
            self.search()
        self._background(job, done)

    def _bounds(self, prefix=''):
        since = date_bound(self.vars[prefix + 'since'].get())
        until = date_bound(self.vars[prefix + 'until'].get(), end=True)
        if since is not None and until is not None and since > until: raise ValueError('起始日期不能晚于结束日期。')
        return since, until

    def search(self):
        try: since, until = self._bounds()
        except ValueError:
            messagebox.showerror('日期格式错误', '请使用 YYYY-MM-DD，起始日期不能晚于结束日期。', parent=self.root)
            return
        if self.manual_picker is not None: self.manual_picker.close()
        self.visible = filter_feeds(self.feeds, self.vars['keyword'].get(), self.vars['author'].get(), since, until,
                                    self.account if self.only_self.get() and self.account else None)
        self.feed_tree.delete(*self.feed_tree.get_children())
        self.comment_tree.delete(*self.comment_tree.get_children())
        self.current_comments, self.current_indices = [], []
        self.comment_stats.set('请选择朋友圈，再筛选和勾选评论。')
        self._set_text(self.body, '')
        self._set_text(self.comment_body, '')
        self.summary.set('选择一条朋友圈，核对正文与日期后导出。')
        for index, feed in enumerate(self.visible):
            self.feed_tree.insert('', 'end', iid=str(index), values=(self._time(feed['create_time']), feed['nickname'] or feed['username'],
                                                                   feed['text'].replace('\n', ' '), len(feed['comments'])))
        label = '演示数据' if self.source == 'demo' else '离线备份' if self.source == 'json_backup' else '本地缓存'
        self.status.set(f'{label} · 已读取 {len(self.feeds)} 条朋友圈，匹配 {len(self.visible)} 条。时间均为北京时间。')

    def clear_search(self):
        for name in ('keyword', 'author', 'since', 'until'): self.vars[name].set('')
        self.search()

    def _selected(self):
        selected = self.feed_tree.selection()
        return self.visible[int(selected[0])] if selected else None

    def select_feed(self, event=None):
        feed = self._selected()
        if feed is None: return
        key = self._feed_key(feed)
        if self.active_filter_key != key:
            criteria = self.selection_store.filters(feed) if self.selection_store else {}
            for name in ('keyword', 'author', 'since', 'until'): self.vars['comment_' + name].set(criteria.get(name, ''))
            self.exclude_replies.set(criteria.get('exclude_replies', False))
            self.active_filter_key = key
        self._set_text(self.body, feed['text'])
        self.search_comments()

    def search_comments(self):
        feed = self._selected()
        if feed is None: return
        try: since, until = self._bounds('comment_')
        except ValueError:
            messagebox.showerror('日期格式错误', '请使用 YYYY-MM-DD，起始日期不能晚于结束日期。', parent=self.root)
            return
        matches = filter_comments(feed['comments'], self.vars['comment_keyword'].get(), self.vars['comment_author'].get(), since, until,
                                  exclude_replies=self.exclude_replies.get())
        matched_objects = {id(comment) for comment in matches}
        self.applied_comment_criteria[self._feed_key(feed)] = self._comment_criteria()
        rows = sorted(((index, comment) for index, comment in enumerate(feed['comments']) if id(comment) in matched_objects),
                      key=lambda row: row[1]['create_time'] or 0)
        self.current_indices = [row[0] for row in rows]
        self.current_comments = [row[1] for row in rows]
        self.comment_tree.delete(*self.comment_tree.get_children())
        self._set_text(self.comment_body, '')
        for index, comment in enumerate(self.current_comments):
            deleted = '[已删除] ' if comment.get('deleted') else ''
            checked = self.current_indices[index] not in self._excluded(feed)
            reply = '↳ ' if is_reply(comment) else ''
            self.comment_tree.insert('', 'end', iid=str(index), tags=() if checked else ('excluded',),
                                     values=('☑' if checked else '☐', self._time(comment['create_time']), comment['nickname'] or comment['username'],
                                             deleted + reply + comment['content'].replace('\n', ' '), comment['ref_comment_id']))
        notes = '；'.join(feed.get('warnings', []))
        self.summary.set(f'{feed["nickname"]} · {self._time(feed["create_time"])} · ID {feed["id"]} · 评论 {len(self.current_comments)}/{len(feed["comments"])}'
                         + (' · ' + notes if notes else ''))
        self._selection_stats()
        self._save_selection(feed)
        if self.manual_picker is not None: self.manual_picker.refresh()
        return True

    @staticmethod
    def _feed_key(feed): return (feed['username'], feed['id'], feed['tid'])

    def _excluded(self, feed):
        key = self._feed_key(feed)
        if key not in self.manual_exclusions:
            self.manual_exclusions[key] = self.selection_store.restore(feed) if self.selection_store else set()
        return self.manual_exclusions[key]

    def _save_selection(self, feed):
        try:
            criteria = self.applied_comment_criteria.get(self._feed_key(feed), self._comment_criteria())
            if self.selection_store: self.selection_store.save(feed, self._excluded(feed), criteria)
            self.selection_save_status.set('选择已自动保存。关闭窗口后可直接导出。')
        except OSError as exc:
            logging.exception('手动筛选自动保存失败')
            self.selection_save_status.set(f'本地自动保存失败：{exc}；本次选择已同步到主界面。')
        if self.manual_picker is not None: self.manual_picker.sync_checks()

    def open_manual_picker(self):
        if self.busy: return
        if self.manual_picker is not None: self.manual_picker.window.lift(); return
        feed = self._selected()
        if feed is None:
            messagebox.showinfo('选择朋友圈', '请先选择朋友圈，再打开手动筛选窗口。', parent=self.root)
            return
        if self.search_comments() is not True: return
        from .manual_picker import ManualPicker
        self.manual_picker = ManualPicker(self, feed)

    def _selection_stats(self):
        feed = self._selected()
        if feed is None: return
        excluded = self._excluded(feed)
        selected = sum(index not in excluded for index in self.current_indices)
        self.comment_stats.set(f'已载入 {len(feed["comments"])} 条 · 筛选命中 {len(self.current_comments)} 条 · 勾选导出 {selected} 条')

    def toggle_comment(self, row):
        feed = self._selected()
        if feed is None or not self.comment_tree.exists(str(row)): return
        original_index = self.current_indices[int(row)]
        self.set_comment_checked(original_index, original_index in self._excluded(feed))

    def set_comment_checked(self, original_index, checked):
        feed = self._selected()
        if feed is None or not 0 <= original_index < len(feed['comments']): return
        excluded = self._excluded(feed)
        if checked: excluded.discard(original_index)
        else: excluded.add(original_index)
        if original_index in self.current_indices:
            row = str(self.current_indices.index(original_index))
            values = list(self.comment_tree.item(row, 'values'))
            values[0] = '☑' if checked else '☐'
            self.comment_tree.item(row, values=values, tags=() if checked else ('excluded',))
        self._selection_stats()
        self._save_selection(feed)

    def _comment_click(self, event):
        row = self.comment_tree.identify_row(event.y)
        if row and self.comment_tree.identify_column(event.x) == '#1' and self.comment_tree.identify_region(event.x, event.y) == 'cell':
            self.toggle_comment(row)

    def _comment_space(self, event):
        selected = self.comment_tree.selection()
        if selected: self.toggle_comment(selected[0])
        return 'break'

    def select_all_comments(self, checked):
        feed = self._selected()
        if feed is None: return
        excluded = self._excluded(feed)
        if checked: excluded.difference_update(self.current_indices)
        else: excluded.update(self.current_indices)
        for row in self.comment_tree.get_children():
            values = list(self.comment_tree.item(row, 'values'))
            values[0] = '☑' if checked else '☐'
            self.comment_tree.item(row, values=values, tags=() if checked else ('excluded',))
        self._selection_stats()
        self._save_selection(feed)

    def clear_comments(self):
        for name in ('comment_keyword', 'comment_author', 'comment_since', 'comment_until'): self.vars[name].set('')
        self.exclude_replies.set(False)
        feed = self._selected()
        if feed is not None:
            self._excluded(feed).clear()
        self.search_comments()

    def select_comment(self, event=None):
        selected = self.comment_tree.selection()
        if not selected: return
        comment = self.current_comments[int(selected[0])]
        self._set_text(self.comment_body, f'{comment["nickname"]} ({comment["username"]}) · {self._time(comment["create_time"])}\n{comment["content"]}')

    @staticmethod
    def _set_text(widget, value):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', value)
        widget.configure(state='disabled')

    @staticmethod
    def _time(value):
        text = timestamp_text(value)
        return text[:19].replace('T', ' ') if text else '时间缺失'

    def show_demo(self):
        if self.busy: return
        self.reader, self.protected_root = None, None
        self.feeds, self.account, self.source = demo_feeds(), 'wxid_demo', 'demo'
        self.manual_exclusions.clear()
        self.clear_search()

    def open_json(self):
        if self.busy: return
        path = filedialog.askopenfilename(title='打开朋友圈 JSON 备份', filetypes=[('JSON 备份', '*.json')], parent=self.root)
        if not path: return
        def done(backup):
            self.reader, self.protected_root = None, None
            self.feeds, self.account, self.source = backup['moments'], backup.get('account', ''), 'json_backup'
            self.manual_exclusions.clear()
            self.clear_search()
        self._background(lambda: load_backup(path), done)

    def export_selected(self):
        if self.busy: return
        feed = self._selected()
        if feed is None:
            messagebox.showinfo('选择朋友圈', '请先点击要备份的朋友圈，核对正文、发布者和日期。', parent=self.root)
            return
        self._export([feed])

    def export_visible(self):
        if self.busy: return
        if not self.visible:
            messagebox.showinfo('没有匹配内容', '请先读取或打开备份，并调整朋友圈筛选条件。', parent=self.root)
            return
        self._export(self.visible)

    def selected_comment_feed(self):
        """仅保留当前命中的勾选评论，供界面导出和测试共用。"""
        feed = self._selected()
        if feed is None: return None
        excluded = self._excluded(feed)
        comments = [comment for index, comment in zip(self.current_indices, self.current_comments) if index not in excluded]
        criteria = self._comment_criteria()
        criteria['manual_excluded_count'] = sum(index in excluded for index in self.current_indices)
        return filtered_feed(feed, comments, criteria)

    def _comment_criteria(self):
        criteria = {name: self.vars['comment_' + name].get().strip() for name in ('keyword', 'author', 'since', 'until')}
        criteria['exclude_replies'] = self.exclude_replies.get()
        return criteria

    def export_filtered(self):
        if self.busy: return
        if self._selected() is None:
            messagebox.showinfo('选择朋友圈', '请先选择朋友圈，再筛选和勾选评论。', parent=self.root)
            return
        if self.search_comments() is not True: return
        feed = self.selected_comment_feed()
        if not feed['comments']:
            messagebox.showinfo('没有勾选评论', '请调整筛选条件，或勾选至少一条评论。', parent=self.root)
            return
        self._export([feed], filtered=True)

    def _export(self, feeds, filtered=False):
        filename = ('moments_filtered_' if filtered else 'moments_') + datetime.now(SHANGHAI).strftime('%Y%m%d_%H%M%S') + '.json'
        title = '保存筛选导出（仅包含勾选评论）' if filtered else '保存备份（包含全部缓存评论，不受评论筛选影响）'
        path = filedialog.asksaveasfilename(title=title,
                                          initialfile=filename, defaultextension='.json', filetypes=[('JSON 备份', '*.json')], parent=self.root)
        if not path: return
        try:
            backup = make_backup(feeds, self.account, self.source)
            if filtered: backup['export_scope'] = 'filtered_comments'
            save_backup(path, backup, self.protected_root)
        except (OSError, ValueError) as exc:
            messagebox.showerror('保存失败', str(exc), parent=self.root)
            return
        self.status.set(f'已保存 {len(feeds)} 条朋友圈、{backup["comment_count"]} 条评论：{path}')

def smoke_test(output, backup_path=None):
    """验证真实 Tk 控件、筛选和 JSON 回读；不会连接微信。"""
    from .db import WeChatDB
    root = tk.Tk()
    root.withdraw()
    app = App(root, persist_selection=False)
    app.show_demo()
    root.update()
    assert len(app.feed_tree.get_children()) == 1
    app.feed_tree.selection_set('0')
    app.select_feed()
    assert len(app.comment_tree.get_children()) == 3
    app.vars['comment_author'].set('朋友')
    app.search_comments()
    assert len(app.comment_tree.get_children()) == 1
    app.clear_comments()
    app.exclude_replies.set(True)
    app.search_comments()
    assert len(app.comment_tree.get_children()) == 2
    app.toggle_comment('0')
    selected = app.selected_comment_feed()
    assert len(selected['comments']) == 1 and selected['comments'][0]['comment_id'] == '103'
    assert 'raw_xml' not in selected and len(app.feeds[0]['comments']) == 3
    app.search_comments()
    assert len(app.selected_comment_feed()['comments']) == 1
    app.select_all_comments(False)
    assert not app.selected_comment_feed()['comments']
    app.select_all_comments(True)
    assert len(app.selected_comment_feed()['comments']) == 2
    app.clear_comments()
    demo_backup_path = Path(output).with_suffix('.json')
    save_backup(demo_backup_path, make_backup(app.feeds, app.account, 'demo'))
    assert len(load_backup(demo_backup_path)['moments'][0]['comments']) == 3
    app.vars['keyword'].set('不存在的关键词')
    app.search()
    assert not app.feed_tree.get_children()
    real_comments, real_nonreply_comments = None, None
    if backup_path:
        loaded = load_backup(backup_path)
        app.feeds, app.account, app.source = loaded['moments'], loaded.get('account', ''), 'json_backup'
        app.clear_search()
        app.clear_comments()
        assert app.feed_tree.get_children()
        app.feed_tree.selection_set('0')
        app.select_feed()
        real_comments = len(app.visible[0]['comments'])
        assert len(app.comment_tree.get_children()) == real_comments
        app.exclude_replies.set(True)
        app.search_comments()
        real_nonreply_comments = len(app.selected_comment_feed()['comments'])
        assert real_nonreply_comments == len(filter_comments(app.visible[0]['comments'], exclude_replies=True))
        if app.current_comments:
            app.open_manual_picker()
            picker = app.manual_picker
            assert len(picker.checks) == real_nonreply_comments
            first_index = app.current_indices[0]
            app.set_comment_checked(first_index, False)
            assert not picker.checks[first_index].get()
            picker.close()
            assert len(app.selected_comment_feed()['comments']) == real_nonreply_comments - 1
    app.close()
    Path(output).write_text(json.dumps({'ok': True, 'tk': True, 'db_import': True, 'comment_filter': True,
                                       'manual_selection': True, 'filtered_export': True, 'json_roundtrip': True,
                                       'large_manual_picker': True,
                                       'opened_backup_comment_count': real_comments, 'nonreply_comment_count': real_nonreply_comments}), encoding='utf-8')

def main():
    parser = argparse.ArgumentParser(description='朋友圈只读查看与 JSON 备份')
    parser.add_argument('--demo', action='store_true', help='只显示演示数据')
    parser.add_argument('--open', dest='backup_path', help='打开 JSON 备份')
    parser.add_argument('--smoke-test', metavar='REPORT', help='离线启动自检并将结果写入文件')
    args = parser.parse_args()
    if sys.stdout is None: sys.stdout = io.StringIO()
    if sys.stderr is None: sys.stderr = io.StringIO()
    if args.smoke_test:
        try: smoke_test(args.smoke_test, args.backup_path)
        except Exception as exc:
            Path(args.smoke_test).write_text(json.dumps({'ok': False, 'error': str(exc)}), encoding='utf-8')
            raise SystemExit(1) from exc
        return
    workdir = data_directory()
    workdir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=workdir / 'app.log', level=logging.INFO, encoding='utf-8', format='%(asctime)s %(levelname)s %(message)s')
    try: ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError): pass
    root = tk.Tk()
    app = App(root)
    def report_callback(exc_type, exc, trace):
        logging.error('界面操作失败', exc_info=(exc_type, exc, trace))
        messagebox.showerror('操作失败', str(exc), parent=root)
    root.report_callback_exception = report_callback
    if args.demo: app.show_demo()
    elif args.backup_path:
        try:
            backup = load_backup(args.backup_path)
            app.feeds, app.account, app.source = backup['moments'], backup.get('account', ''), 'json_backup'
            app.search()
        except Exception as exc: messagebox.showerror('无法打开备份', str(exc), parent=root)
    root.mainloop()
