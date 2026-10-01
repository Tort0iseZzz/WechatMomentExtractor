"""本地朋友圈 XML 解析与只读查询，保留未知时间和原始 XML。"""
from __future__ import annotations
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8))

def timestamp_text(value):
    if not value: return None
    try: return datetime.fromtimestamp(int(value), SHANGHAI).isoformat()
    except (ValueError, TypeError, OSError, OverflowError): return None

def date_bound(value, end=False):
    """日期筛选包含结束日；空字符串表示不限。"""
    if not value.strip(): return None
    day = datetime.strptime(value.strip(), '%Y-%m-%d').replace(tzinfo=SHANGHAI)
    return int((day + timedelta(days=1)).timestamp()) - 1 if end else int(day.timestamp())

def _text(node, name, default=''):
    found = node.find(name)
    return ''.join(found.itertext()) if found is not None else default

def _number(value):
    try: return int(value)
    except (ValueError, TypeError): return None

def parse_feed(xml, fallback_user='', tid='', nick=None):
    """实体与 CDATA 交给 XML 解析器处理，不能用正则吞掉正文。"""
    if isinstance(xml, bytes): xml = xml.decode('utf-8-sig', errors='strict')
    root = ET.fromstring(xml)
    timeline = root if root.tag == 'TimelineObject' else root.find('.//TimelineObject')
    if timeline is None: timeline = root
    def field(name):
        value = _text(timeline, name)
        return value if value else _text(root, name)
    author = field('username') or fallback_user
    resolve = nick or (lambda user: user)
    comments, likes = [], []
    for tag, target in [('comment_user_list', comments), ('like_user_list', likes)]:
        container = root.find('.//' + tag)
        if container is None: continue
        for node in container.findall('user_comment'):
            user = _text(node, 'username')
            created = _number(_text(node, 'create_time'))
            target.append({'username': user, 'nickname': _text(node, 'nickname') or resolve(user),
                           'content': _text(node, 'content'), 'create_time': created or None, 'published_at': timestamp_text(created),
                           'comment_id': _text(node, 'comment_id'), 'ref_comment_id': _text(node, 'ref_comment_id'),
                           'comment_64id': _text(node, 'comment_64id'), 'ref_comment_64id': _text(node, 'ref_comment_64id'),
                           'ref_username': _text(node, 'ref_username'),
                           'type': _number(_text(node, 'type')), 'deleted': _number(_text(node, 'b_deleted'))})
    created = _number(field('createTime'))
    expected = None
    for name in ('commentCount', 'comment_count', 'comment_user_count', 'commentUserCount'):
        value = _text(root, name) or _text(timeline, name)
        if value != '': expected = _number(value); break
    warnings = []
    if not created: warnings.append('朋友圈发布时间缺失')
    if any(not c['create_time'] for c in comments): warnings.append('部分评论发布时间缺失，保留为 null')
    if expected is not None and expected > len(comments): warnings.append(f'本地声明 {expected} 条评论，仅缓存 {len(comments)} 条')
    return {'id': field('id') or str(tid), 'tid': str(tid), 'username': author, 'nickname': field('nickname') or resolve(author),
            'text': field('contentDesc'), 'create_time': created or None, 'published_at': timestamp_text(created),
            'comments': comments, 'likes': likes, 'declared_comment_count': expected, 'cached_comment_count': len(comments),
            'warnings': warnings, 'raw_xml': xml}

class MomentDB:
    """只查询 SnsTimeLine，不更改微信数据库或进程。"""
    def __init__(self, db=None, sqlite_path=None):
        self.db, self.sqlite_path = db, sqlite_path
        self.nicknames = {}
        self.warnings = []

    def _nick(self, user):
        if not user: return ''
        if user not in self.nicknames:
            try: self.nicknames[user] = self.db.get_nickname(user) if self.db else user
            except Exception: self.nicknames[user] = user
        return self.nicknames[user]

    def _open_sns(self):
        if self.sqlite_path:
            conn = sqlite3.connect(Path(self.sqlite_path).resolve().as_uri() + '?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            return conn
        for rel, path, _ in self.db._db_files:
            if Path(path).name == 'sns.db': return self.db._open(rel)
        raise RuntimeError('未找到 sns.db，请先在 PC 微信打开目标朋友圈，再重新读取。')

    def get_moments(self, username=None):
        self.warnings = []
        conn = self._open_sns()
        try:
            sql = 'SELECT tid, user_name, content FROM SnsTimeLine'
            rows = conn.execute(sql + (' WHERE user_name=?' if username else '') + ' ORDER BY tid DESC', (username,) if username else ()).fetchall()
        finally: conn.close()
        result = []
        for row in rows:
            try: result.append(parse_feed(row['content'], row['user_name'], row['tid'], self._nick))
            except (ET.ParseError, UnicodeError, TypeError, ValueError) as exc:
                raise RuntimeError(f'朋友圈 tid={row["tid"]} 无法解析，读取已停止，避免导出时遗漏内容：{exc}') from exc
        if self.db:
            self.warnings = list(dict.fromkeys(self.db.snapshot_warnings))
            for feed in result: feed['warnings'].extend(self.warnings)
        return sorted(result, key=lambda item: (item['create_time'] or 0, item['tid']), reverse=True)

def filter_feeds(feeds, keyword='', author='', since=None, until=None, username=None):
    keyword, author = keyword.casefold().strip(), author.casefold().strip()
    return [f for f in feeds if (not username or f['username'] == username)
            and (not keyword or keyword in f['text'].casefold() or keyword in f['id'])
            and (not author or author in (f['nickname'] + ' ' + f['username']).casefold())
            and (since is None or (f['create_time'] is not None and f['create_time'] >= since))
            and (until is None or (f['create_time'] is not None and f['create_time'] <= until))]

def is_reply(comment):
    """0 是微信的无回复标记，不能把非空字符串 '0' 当成回复。"""
    return any(str(comment.get(field) or '').strip() not in {'', '0'} for field in ('ref_comment_id', 'ref_comment_64id', 'ref_username'))

def filter_comments(comments, keyword='', author='', since=None, until=None, exclude_replies=False):
    return [c for c in comments if (not keyword or keyword.casefold() in c['content'].casefold())
            and (not exclude_replies or not is_reply(c))
            and (not author or author.casefold() in (c['nickname'] + ' ' + c['username']).casefold())
            and (since is None or (c['create_time'] is not None and c['create_time'] >= since))
            and (until is None or (c['create_time'] is not None and c['create_time'] <= until))]
