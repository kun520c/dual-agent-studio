"""Launch the local Studio client; mock-only submission or --read-only history."""
import json
import os
import time
if os.name == 'nt':
    _flags = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')
    if '--disable-gpu' not in _flags and '--disable-direct-composition' not in _flags:
        os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = (_flags + ' --disable-direct-composition').strip()
STARTED=time.perf_counter()
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent))
sys.path.insert(0,str(ROOT/'python'))
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
# QApplication must exist before Qt WebEngine is imported. The main window stays
# hidden until the real interface reports that it is interactive.
QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
_app=QApplication(sys.argv)
_app.setFont(QFont('Microsoft YaHei UI', 10))
_qapp_ms=round((time.perf_counter()-STARTED)*1000, 1)
from desktop_validation.app import main
_imported_ms=round((time.perf_counter()-STARTED)*1000, 1)
os.environ['DAS_STARTUP_MARKS']=json.dumps({'qapp':_qapp_ms,'app_import':_imported_ms})
if __name__=='__main__':raise SystemExit(main(STARTED))
