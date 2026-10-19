"""Trusted isolated fixture check, outside every mock-writable workspace."""
from pathlib import Path
import sys

workspace = Path(sys.argv[1]).resolve()
content = (workspace / 'value.txt').read_text(encoding='utf8').splitlines()
ok = len(content) == 2 and content[0] == '5' and content[1].startswith('任务：studio-')
print('可信测试：value=5，存在本次模拟任务标识；' + ('PASS' if ok else 'FAIL'))
raise SystemExit(0 if ok else 1)
