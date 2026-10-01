"""按稳定评论标识自动保存手动筛选，不保存评论正文。"""
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path

def feed_key(feed):
    value = json.dumps([feed['username'], feed['id'], feed['tid']], ensure_ascii=False)
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

def comment_keys(comments):
    result, seen = [], Counter()
    for comment in comments:
        ident = next((str(comment.get(name)) for name in ('comment_64id', 'comment_id') if str(comment.get(name) or '') not in {'', '0'}), '')
        if ident: token = 'id:' + ident
        else:
            value = json.dumps([comment.get(name) for name in ('username', 'create_time', 'content', 'ref_comment_id')], ensure_ascii=False)
            token = 'hash:' + hashlib.sha256(value.encode('utf-8')).hexdigest()
        result.append(token + ':' + str(seen[token]))
        seen[token] += 1
    return result

class SelectionStore:
    def __init__(self, path):
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.data = {}
            for key, value in data.items():
                record = {'excluded': value, 'filters': {}} if isinstance(value, list) else value
                if not isinstance(record, dict) or not isinstance(record.get('excluded'), list): continue
                if all(isinstance(token, str) for token in record['excluded']): self.data[key] = record
        except (OSError, ValueError, AttributeError): self.data = {}

    def restore(self, feed):
        excluded = set(self.data.get(feed_key(feed), {}).get('excluded', []))
        return {index for index, token in enumerate(comment_keys(feed['comments'])) if token in excluded}

    def filters(self, feed):
        value = self.data.get(feed_key(feed), {}).get('filters', {})
        if not isinstance(value, dict): return {}
        return {key: value[key] for key in ('keyword', 'author', 'since', 'until', 'exclude_replies') if key in value
                and isinstance(value[key], bool if key == 'exclude_replies' else str)}

    def save(self, feed, excluded, criteria=None):
        keys = comment_keys(feed['comments'])
        data = dict(self.data)
        key = feed_key(feed)
        tokens = [keys[index] for index in sorted(excluded) if 0 <= index < len(keys)]
        criteria = criteria if criteria is not None else self.filters(feed)
        if tokens or any(criteria.values()): data[key] = {'excluded': tokens, 'filters': criteria}
        else: data.pop(key, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.path.parent, suffix='.tmp', delete=False) as stream:
                temp = stream.name
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            self.data = data
        finally:
            if temp and os.path.exists(temp): os.unlink(temp)
