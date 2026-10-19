"""Independent trusted assertions for the B2.2-A isolated Python fixture."""
from pathlib import Path
import runpy
import sys

workspace=Path(sys.argv[1]).resolve()
module=runpy.run_path(str(workspace/'calculator.py'))
add=module['add']
assert add(2,3)==5
assert add(-7,4)==-3
assert add(0,0)==0
print('Trusted Python calculator: 3 independent assertions PASS')
