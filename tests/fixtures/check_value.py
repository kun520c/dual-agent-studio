"""可信验证示例：只比较调用方预定的文件内容，不读取模型输出。"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: check_value.py FILE EXPECTED", file=sys.stderr)
        return 2
    actual = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
    if actual != sys.argv[2]:
        print(f"expected {sys.argv[2]} but got {actual}", file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
