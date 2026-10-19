"""Test-only process bootstrap. Offline public key cannot approve real mode."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from v05_support import install
index=sys.argv.index('--offline-key');key=sys.argv[index+1];del sys.argv[index:index+2]
install(key)
from desktop_validation.task_host import main
raise SystemExit(main())
