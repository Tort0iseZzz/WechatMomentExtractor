"""独立大窗口，以完整评论卡片进行手动筛选。"""
import tkinter as tk
from tkinter import ttk
from .moment import is_reply

class ManualPicker:
    def __init__(self, app, feed):
        self.app, self.feed, self.key = app, feed, app._feed_key(feed)
        self.cards, self.checks, self.metadata, self.fit_timer = [], {}, [], None
        self.window = tk.Toplevel(app.root)
        self.window.title('手动筛选评论')
        self.window.transient(app.root)
        width, height = min(1120, self.window.winfo_screenwidth() - 80), min(860, self.window.winfo_screenheight() - 100)
        self.window.geometry(f'{width}x{height}')
        self.window.minsize(640, 480)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.bind('<Escape>', lambda event: self.close())
        self.window.bind('<MouseWheel>', self._wheel, add='+')
        outer = ttk.Frame(self.window, padding=12)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='手动筛选评论', font=('Microsoft YaHei UI', 17, 'bold')).pack(anchor='w')
        self.info = ttk.Label(outer, text=f'{feed["nickname"]} · {app._time(feed["create_time"])}', wraplength=950)
        self.info.pack(fill='x', pady=(3, 8))
        filters = ttk.LabelFrame(outer, text='筛选条件与主界面同步', padding=8)
        filters.pack(fill='x', pady=(0, 8))
        self.fields = []
        for label, name in [('评论关键词', 'comment_keyword'), ('评论者', 'comment_author'),
                            ('起始日期', 'comment_since'), ('结束日期', 'comment_until')]:
            cell = ttk.Frame(filters)
            ttk.Label(cell, text=label, width=10).pack(side='left')
            entry = ttk.Entry(cell, textvariable=app.vars[name], width=12)
            entry.pack(side='left', fill='x', expand=True, padx=(0, 6))
            entry.bind('<Return>', lambda event: self.apply_filters())
            self.fields.append(cell)
        self.filter_frame = filters
        options = ttk.Frame(outer)
        options.pack(fill='x', pady=(0, 8))
        ttk.Checkbutton(options, text='不包括全部回复消息', variable=app.exclude_replies, command=self.apply_filters).pack(side='left')
        ttk.Button(options, text='应用筛选', command=self.apply_filters).pack(side='left', padx=6)
        ttk.Button(options, text='重置筛选与勾选', command=app.clear_comments).pack(side='left')
        footer = ttk.Frame(outer)
        footer.pack(side='bottom', fill='x', pady=(8, 0))
        self.stats = ttk.Label(footer, textvariable=app.comment_stats, wraplength=950)
        self.stats.pack(fill='x')
        self.saved = ttk.Label(footer, textvariable=app.selection_save_status, foreground='#167565', wraplength=950)
        self.saved.pack(fill='x', pady=(3, 6))
        buttons = ttk.Frame(footer)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='勾选当前结果', command=lambda: app.select_all_comments(True)).pack(side='left')
        ttk.Button(buttons, text='取消当前勾选', command=lambda: app.select_all_comments(False)).pack(side='left', padx=6)
        ttk.Button(buttons, text='关闭（已自动保存）', command=self.close).pack(side='right')
        viewport = ttk.Frame(outer)
        viewport.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(viewport, background='#edf1f4', highlightthickness=0, yscrollincrement=24)
        scroll = ttk.Scrollbar(viewport, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.content = tk.Frame(self.canvas, background='#edf1f4')
        self.item = self.canvas.create_window((0, 0), window=self.content, anchor='nw')
        self.content.bind('<Configure>', lambda event: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', self._resize)
        self.refresh()
        self.window.grab_set()

    def apply_filters(self):
        if self.app._selected() is not self.feed: self.close(); return
        self.app.search_comments()

    def _resize(self, event):
        width = event.width
        self.canvas.itemconfigure(self.item, width=width)
        columns = 2 if width >= 760 else 1
        for column in (0, 1): self.filter_frame.columnconfigure(column, weight=1 if column < columns else 0)
        for index, cell in enumerate(self.fields): cell.grid(row=index // columns, column=index % columns, sticky='ew', pady=3)
        for label in (self.info, self.stats, self.saved): label.configure(wraplength=max(280, width - 20))
        for label in self.metadata: label.configure(wraplength=max(240, width - 100))
        self._schedule_fit()

    def _schedule_fit(self):
        if self.fit_timer is not None: self.window.after_cancel(self.fit_timer)
        self.fit_timer = self.window.after_idle(self._fit_texts)

    def _fit_texts(self):
        self.fit_timer = None
        for text in self.cards:
            lines = text.count('1.0', 'end', 'displaylines')
            text.configure(height=max(1, lines[0] if lines else 1))

    def _wheel(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), 'units')
        return 'break'

    def refresh(self):
        if self.app._selected() is not self.feed: self.close(); return
        position = self.canvas.yview()[0]
        for child in self.content.winfo_children(): child.destroy()
        self.cards, self.checks, self.metadata = [], {}, []
        excluded = self.app._excluded(self.feed)
        for index, comment in zip(self.app.current_indices, self.app.current_comments):
            card = tk.Frame(self.content, background='white', padx=12, pady=12, highlightbackground='#d8e0e5', highlightthickness=1)
            card.pack(fill='x', padx=8, pady=(0, 12))
            checked = tk.BooleanVar(master=self.window, value=index not in excluded)
            self.checks[index] = checked
            check = tk.Checkbutton(card, variable=checked, command=lambda i=index: self.app.set_comment_checked(i, self.checks[i].get()),
                                   font=('Microsoft YaHei UI', 14), background='white', activebackground='white', anchor='n')
            check.pack(side='left', anchor='n', padx=(0, 8))
            body = tk.Frame(card, background='white')
            body.pack(side='left', fill='both', expand=True)
            flags = ' · 回复' if is_reply(comment) else ''
            if comment.get('deleted'): flags += ' · 已删除'
            metadata = tk.Label(body, text=f'{self.app._time(comment["create_time"])} · {comment["nickname"] or comment["username"]}{flags}',
                                font=('Microsoft YaHei UI', 11), background='white', foreground='#596571', anchor='w', justify='left',
                                wraplength=max(240, self.canvas.winfo_width() - 100))
            metadata.pack(fill='x', pady=(0, 6))
            self.metadata.append(metadata)
            text = tk.Text(body, height=1, width=1, wrap='word', font=('Microsoft YaHei UI', 12), spacing1=4, spacing3=6,
                           background='white', foreground='#17242d', relief='flat', padx=2, pady=0)
            text.insert('1.0', comment['content'])
            text.configure(state='disabled')
            text.pack(fill='x')
            text.bind('<MouseWheel>', self._wheel)
            self.cards.append(text)
        if not self.cards:
            tk.Label(self.content, text='没有符合条件的评论，请调整筛选条件。', background='#edf1f4', pady=30).pack(fill='x')
        self.canvas.yview_moveto(position)
        self._schedule_fit()

    def sync_checks(self):
        excluded = self.app._excluded(self.feed)
        for index, checked in self.checks.items(): checked.set(index not in excluded)

    def close(self):
        if self.fit_timer is not None: self.window.after_cancel(self.fit_timer)
        self.app.manual_picker = None
        self.window.grab_release()
        self.window.destroy()
