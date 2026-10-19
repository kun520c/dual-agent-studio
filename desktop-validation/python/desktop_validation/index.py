"""Disposable bounded JSONL indexes. Never writes coordinator artifacts."""
from array import array
from collections import OrderedDict
import json
from pathlib import Path
from .files import opened_under_root

MAX_FILE = 8 * 1024 * 1024

def signature(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

class JsonlIndex:
    def __init__(self, root, max_bytes=2*1024*1024, max_entries=8):
        self.root=root
        self.entries = OrderedDict()
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.scans = 0
        self.hits = 0

    @property
    def bytes(self):
        return sum(v['cost'] for v in self.entries.values())

    def get(self, path: Path):
        if not path.is_file():
            self.entries.pop(path, None)
            return {'offsets': array('Q'), 'invalid': 0, 'usage': {}, 'agents': {}, 'signature': None}
        stamp = signature(path.stat())
        if stamp[2] > MAX_FILE:
            raise ValueError('历史文件超过 8 MiB 读取上限')
        cached = self.entries.get(path)
        if cached and cached['signature'] == stamp:
            self.hits += 1
            self.entries.move_to_end(path)
            return cached
        offsets = array('Q'); invalid = 0; usage = {}; agents = {}
        with path.open('rb') as stream:
            opened_under_root(stream,self.root)
            before = signature(__import__('os').fstat(stream.fileno()))
            # Windows Python 3.13 stat/fstat report different ctime semantics.
            # Identity, size and mtime agree; path ctime still invalidates cache.
            if before[:4] != stamp[:4]: raise ValueError('历史文件正在替换，请重试')
            while stream.tell() < before[2]:
                position = stream.tell()
                line = stream.readline(MAX_FILE+1)
                if stream.tell() > before[2]: raise ValueError('历史文件正在追加，请重试')
                if not line.strip(): continue
                try:
                    item = json.loads(line.decode('utf-8-sig'))
                    if not isinstance(item, dict): raise ValueError('record')
                except (ValueError, UnicodeError, RecursionError):
                    invalid += 1
                    continue
                offsets.append(position)
                for field in ('input_tokens', 'output_tokens'):
                    value = item.get(field)
                    if type(value) is int and value >= 0: usage[field] = usage.get(field, 0) + value
                executor = str(item.get('sender') or item.get('executor') or '')[:100]
                if executor:
                    info = agents.setdefault(executor, {'saved': 0, 'invoked': 0})
                    info['saved'] += 1
                    info['invoked'] += int(item.get('invoked') is True)
            if signature(__import__('os').fstat(stream.fileno())) != before or signature(path.stat()) != stamp:
                raise ValueError('历史文件读取期间发生变化，请重试')
        cost=len(offsets)*8+len(json.dumps({'usage':usage,'agents':agents},ensure_ascii=False).encode('utf8'))+256
        entry = {'offsets': offsets, 'signature': stamp, 'invalid': invalid, 'usage': usage, 'agents': agents, 'cost':cost}
        self.scans += 1
        self.entries.pop(path, None)
        if cost <= self.max_bytes:
            self.entries[path] = entry
            while self.bytes > self.max_bytes or len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)
        return entry

    def read_page(self, path, offset, limit, redact, max_page):
        entry = self.get(path); rows = []; used = 0
        if not entry['signature']: return rows, 0, entry
        with path.open('rb') as stream:
            opened_under_root(stream,self.root)
            if signature(__import__('os').fstat(stream.fileno()))[:4] != entry['signature'][:4]:
                raise ValueError('历史索引已失效，请重试')
            for position in entry['offsets'][offset:offset+limit]:
                stream.seek(position)
                raw = stream.readline(MAX_FILE+1).decode('utf-8-sig')
                item = json.loads(raw)
                encoded = json.dumps(redact(item), ensure_ascii=False)
                size = len(encoded.encode('utf8'))
                if size > max_page and not rows: raise ValueError('单条历史记录超过页面读取上限')
                if used + size > max_page: break
                # Validate original content before redaction changes its hash.
                if item.get('content_sha256'):
                    import hashlib
                    if hashlib.sha256(str(item.get('content', '')).encode('utf8')).hexdigest() != item['content_sha256']:
                        sanitized = json.loads(encoded); sanitized['_integrityMismatch'] = True
                    else: sanitized = json.loads(encoded)
                else: sanitized = json.loads(encoded)
                rows.append(sanitized); used += size
            if signature(__import__('os').fstat(stream.fileno()))[:4] != entry['signature'][:4] or signature(path.stat()) != entry['signature']:
                self.entries.pop(path, None)
                raise ValueError('历史索引已失效，请重试')
        return rows, len(entry['offsets']), entry
