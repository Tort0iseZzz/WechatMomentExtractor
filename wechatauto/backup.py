"""JSON 快照原子保存与离线载入。"""
import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from .moment import SHANGHAI

LIMITATION = '仅包含此电脑已缓存的朋友圈及评论，未验证服务端完整性。若有遗漏，请在 PC 微信打开目标朋友圈及评论区后重新读取。'

def make_backup(feeds, account='', source='local_wechat_db'):
    return {'schema_version': 1, 'exported_at': datetime.now(SHANGHAI).isoformat(), 'timezone': 'Asia/Shanghai',
            'source': source, 'account': account, 'completeness': 'local_cache_only', 'limitation': LIMITATION,
            'moment_count': len(feeds), 'comment_count': sum(len(f['comments']) for f in feeds), 'moments': feeds}

def filtered_feed(feed, comments, criteria=None):
    """筛选导出不携带包含全部评论的原始 XML，也不修改原始快照。"""
    result = deepcopy({key: value for key, value in feed.items() if key != 'raw_xml'})
    result['comments'] = deepcopy(comments)
    result['export_scope'] = 'filtered_comments'
    result['exported_comment_count'] = len(comments)
    result['source_comment_count'] = len(feed['comments'])
    result['comment_selection'] = deepcopy(criteria or {})
    result['raw_xml_omitted'] = True
    return result

def save_backup(path, backup, protected_root=None):
    path = Path(path).resolve()
    if protected_root and path.is_relative_to(Path(protected_root).resolve()):
        raise ValueError('请将备份保存到微信数据目录之外。')
    if path.suffix.lower() != '.json': raise ValueError('备份文件必须使用 .json 扩展名。')
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, suffix='.tmp', delete=False) as stream:
            temp = stream.name
            json.dump(backup, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp and os.path.exists(temp): os.unlink(temp)

def load_backup(path):
    with open(path, encoding='utf-8-sig') as stream: backup = json.load(stream)
    if not isinstance(backup, dict) or backup.get('schema_version') != 1 or not isinstance(backup.get('moments'), list):
        raise ValueError('不是本工具生成的 JSON 备份（schema_version=1）。')
    for feed in backup['moments']:
        if not isinstance(feed, dict): raise ValueError('朋友圈记录格式错误。')
        for field in ('id', 'tid', 'username', 'nickname', 'text'):
            if not isinstance(feed.get(field), str): raise ValueError(f'朋友圈字段 {field} 格式错误。')
        if feed.get('create_time') is not None and type(feed['create_time']) is not int: raise ValueError('朋友圈时间格式错误。')
        if not isinstance(feed.get('comments'), list): raise ValueError('评论列表格式错误。')
        for comment in feed['comments']:
            if not isinstance(comment, dict): raise ValueError('评论格式错误。')
            for field in ('username', 'nickname', 'content', 'comment_id', 'ref_comment_id'):
                if not isinstance(comment.get(field), str): raise ValueError(f'评论字段 {field} 格式错误。')
            if comment.get('create_time') is not None and type(comment['create_time']) is not int: raise ValueError('评论时间格式错误。')
    return backup
