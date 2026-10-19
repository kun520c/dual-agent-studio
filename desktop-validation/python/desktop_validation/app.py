from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import sys
import time
import traceback
def _prefer_stable_surface():
    # DirectComposition updates the window, clip and surface on different
    # frames. Maximizing then shows the previous viewport in the corner and
    # black everywhere else. GPU raster stays on; only that presenter is off.
    if os.name != 'nt':
        return
    flags = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')
    if '--disable-gpu' in flags or '--disable-direct-composition' in flags:
        return
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = (flags + ' --disable-direct-composition').strip()


_prefer_stable_surface()
from .bounded import BoundedLog
from PySide6 import __version__ as qt_version
from PySide6.QtCore import Qt,QTimer,QUrl,QSettings,Signal,QStandardPaths,QEvent,QObject
from PySide6.QtGui import QFont, QColor, QPalette
from PySide6.QtWidgets import QApplication,QMainWindow,QPushButton,QLabel,QVBoxLayout,QWidget,QMessageBox
from PySide6.QtWebEngineCore import QWebEnginePage,QWebEngineProfile,QWebEngineSettings,QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel
from .store import HistoryStore
from .bridge import ReadOnlyBridge,StudioBridge
from .task_host import HostClient
from .security import register_scheme,allowed_url,LocalAssets,LocalOnlyRequests,RestrictedTransport,ORIGIN
from .metrics import memory

VALIDATION=Path(__file__).resolve().parents[2]
ROOT=VALIDATION.parent
register_scheme()
# Automatic reloads stay outside the termination slot and stop after this cap.
# Qt documents renderProcessTerminated as the recovery signal and warns that
# runJavaScript callbacks must not use a page that is going away.
MAX_CONSECUTIVE_RECOVERY=2
MAX_SESSION_RECOVERY=3

def deployment_marker():
    path=ROOT/'deployment.json'
    if not path.is_file():return None
    try:data=json.loads(path.read_text(encoding='utf8'))
    except (OSError,ValueError):return {'version':'0.8.0','invalid':True}
    return data if isinstance(data,dict) else {'version':'0.8.0','invalid':True}

GUI_VERSION='0.14.1'

def product_version():
    marker=deployment_marker()
    if isinstance(marker,dict) and not marker.get('invalid'):
        version=marker.get('version')
        if isinstance(version,str) and version.strip():return version.strip()
    return GUI_VERSION

def window_title():
    return f'Dual Agent Studio · GUI v{product_version()} 内部测试版'

def runtime_problems():
    problems=[]
    if not (VALIDATION/'frontend'/'dist'/'index.html').is_file():problems.append('缺少前端页面 frontend/dist/index.html')
    base=Path(__import__('PySide6').__file__).resolve().parent
    for rel in ('QtWebEngineProcess.exe','resources/icudtl.dat','resources/qtwebengine_resources.pak','plugins/platforms/qwindows.dll'):
        if not (base/rel).is_file():problems.append('缺少 Qt 运行文件 '+rel)
    return problems

class _ShutdownFilter(QObject):
    """Stop the page before Qt handles Quit.

    QApplication.quit() while a QWebEnginePage is still alive does not return
    from exec() on Windows. Closing the page first lets the process exit and
    leaves the detached task host running.
    """
    def __init__(self, window):
        super().__init__(window)
        self._window = window

    def eventFilter(self, watched, event):
        window = self._window
        if event.type() == QEvent.Type.Quit and window is not None and not window._closed:
            window._finish_close()
        return False

class SafePage(QWebEnginePage):
    uiReady=Signal(dict)
    shellReady=Signal(dict)
    def __init__(self,profile,parent=None):
        super().__init__(profile,parent);self.transport=None;self.errors=BoundedLog(100);self.blocked_navigation=BoundedLog(100)
        self.permissionRequested.connect(lambda permission:permission.deny())
        self.fileSystemAccessRequested.connect(lambda request:request.reject())
        self.fullScreenRequested.connect(lambda request:request.reject())

    def acceptNavigationRequest(self,url,kind,is_main):
        ok=allowed_url(url,False) and (not is_main or url.path() in {'/','/index.html'})
        if not ok:self.blocked_navigation.append(url.toString())
        return ok

    def createWindow(self,kind):return None
    def chooseFiles(self,mode,old,mime):return []

    def javaScriptConsoleMessage(self,level,message,line,source):
        if message.startswith('DAS_CHANNEL '):
            if self.transport:self.transport.receive(message[12:])
        elif message.startswith('DAS_THEME '):
            theme=message[9:].strip()
            parent=self.parent()
            window=parent.window() if parent is not None else None
            apply=getattr(window,'_apply_page_background',None)
            if theme in {'light','dark'} and apply:apply(theme)
        elif message.startswith('DAS_SHELL '):
            try:self.shellReady.emit(json.loads(message[10:]))
            except json.JSONDecodeError:self.errors.append('Malformed shell readiness record')
        elif message.startswith('DAS_READY '):
            try:self.uiReady.emit(json.loads(message[10:]))
            except json.JSONDecodeError:self.errors.append('Malformed UI readiness record')
        elif level==QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            self.errors.append({'message':message[:500],'line':line,'source':source})

class DesktopWindow(QMainWindow):
    ready=Signal(dict)
    def __init__(self,store:HistoryStore,state_root:Path,size=(1440,900),started=None,restore=True,client=None):
        super().__init__();self.started=started or time.perf_counter();self.metrics={};self.crashes=BoundedLog(100);self.recovery_count=0;self._closing=False;self._closed=False
        self._renderer_alive=False;self._accept_deferred=False;self._deferred_scripts=[];self._recovery_pending=False;self._recovery_total=0;self._recovery_exhausted=False
        self._timers=set();self.phases={};self._shell_visible=False;self._defer_present=False;self._failure_shown=False
        self._close_timer=QTimer(self);self._close_timer.setSingleShot(True);self._close_timer.timeout.connect(self._finish_close)
        self.setWindowTitle(window_title())
        self.resize(*size);self.setMinimumSize(960,640)
        state_root=state_root.resolve()
        state_root.mkdir(parents=True,exist_ok=True)
        self.state_root=state_root
        problems=runtime_problems()
        if problems:
            (state_root/'startup-diagnostic.json').write_text(json.dumps({'problems':problems},ensure_ascii=False,indent=2),encoding='utf8')
            raise RuntimeError('启动资源不完整：'+'；'.join(problems))
        self.settings=QSettings(str(state_root/'window.ini'),QSettings.Format.IniFormat)
        if restore and self.settings.value('geometry'):self.restoreGeometry(self.settings.value('geometry'))
        self.profile=QWebEngineProfile('studio-v03',self);self._mark('profile')
        self.profile.setPersistentStoragePath(str(state_root/'storage'))
        self.profile.setCachePath(str(state_root/'cache'))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self.profile.setHttpCacheMaximumSize(8*1024*1024)
        self.interceptor=LocalOnlyRequests(self.profile);self.profile.setUrlRequestInterceptor(self.interceptor)
        self.assets=LocalAssets(VALIDATION/'frontend'/'dist',self.profile);self.profile.installUrlSchemeHandler(b'studio',self.assets)
        self.profile.downloadRequested.connect(lambda item:item.cancel())
        self.web=QWebEngineView(self);self.page=SafePage(self.profile,self.web);self.web.setPage(self.page);self._mark('page')
        self._resize_trace=os.environ.get('DAS_RESIZE_TRACE') or ''
        self._load_count=0
        self._view_token=id(self.web)
        self._channel_token=0
        prefs={key:str(self.settings.value('reader/'+key)) for key in ('theme','reading','task','project','panel','panelWidth','sidebarWidth','panelWidthChosen') if self.settings.contains('reader/'+key)}
        prefs['version']=product_version()
        bootstrap=QWebEngineScript();bootstrap.setName('studio-display-preferences');bootstrap.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation);bootstrap.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld);bootstrap.setRunsOnSubFrames(False)
        bootstrap.setSourceCode('window.__desktopPrefs='+json.dumps(prefs,ensure_ascii=False)+';');self.page.scripts().insert(bootstrap)
        def first_document_loaded(_):
            # Native close preferences seed a new process once. Renderer
            # recovery in this process must retain its newer localStorage state.
            if not self._is_open():return
            self.page.scripts().remove(bootstrap)
            self.page.loadFinished.disconnect(first_document_loaded)
        self.page.loadFinished.connect(first_document_loaded)
        self.web.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setCentralWidget(self.web)
        config=self.page.settings()
        for attribute in (QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows,QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard,QWebEngineSettings.WebAttribute.JavascriptCanPaste,QWebEngineSettings.WebAttribute.AllowRunningInsecureContent):config.setAttribute(attribute,False)
        config.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled,True)
        # The custom local scheme needs its own local JS/CSS modules. Arbitrary
        # file/qrc access is still rejected by the native request allowlist.
        config.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,True)
        config.setAttribute(QWebEngineSettings.WebAttribute.ErrorPageEnabled,False)
        self.bridge=StudioBridge(store,client) if client else ReadOnlyBridge(store);self.channel=QWebChannel(self)
        self.channel.registerObject('history',self.bridge);self._channel_token=id(self.channel)
        # Same page, channel, and transport are reused after reload. The new
        # document constructs QWebChannel again; destroying the C++ channel here
        # would race the termination callback.
        self.page.run_guarded=self.run_page_script
        self.transport=RestrictedTransport(self.page,self.bridge,self.channel,async_reads=True);self.channel.connectTo(self.transport);self.page.transport=self.transport;self._mark('bridge')
        self.page.loadStarted.connect(self._document_load_started)
        self.page.loadFinished.connect(self._document_load_finished)
        self.page.uiReady.connect(self._ui_ready)
        self.page.shellReady.connect(self._shell_ready)
        self.page.renderProcessTerminated.connect(self._renderer_ended)
        self.recovery=QWidget(self);layout=QVBoxLayout(self.recovery)
        self.recovery.setStyleSheet('background:#f4f1ea;color:#1c1b19;padding:16px;')
        self.recovery_label=QLabel('页面渲染进程已退出。真实任务与历史记录未被改变。')
        self.recovery_label.setWordWrap(True);layout.addWidget(self.recovery_label)
        retry=QPushButton('重新加载页面');retry.clicked.connect(self.reload_local);layout.addWidget(retry)
        self.recovery.hide();self.load_started=time.perf_counter()
        theme=str(self.settings.value('reader/theme') or 'light')
        self._apply_page_background('dark' if theme=='dark' else 'light')
        self.web.load(QUrl(ORIGIN+'/index.html'))
        self._later(20000,self._reveal_if_unready)
        application=QApplication.instance()
        if application is not None:
            self._shutdown_filter=_ShutdownFilter(self)
            application.installEventFilter(self._shutdown_filter)
        self._mark('ctor_done')
        self._trace('constructed', loads=0)

    def _place_overlay(self, widget):
        web=getattr(self,'web',None)
        if widget is None or web is None or not widget.isVisible():return
        widget.setGeometry(web.geometry());widget.raise_()

    def _apply_page_background(self, theme):
        # Chromium clears resized tiles to the page background. The default is
        # black, so a size change flashes black before the next composite.
        color=QColor('#141413' if theme=='dark' else '#FAFAF9')
        page=getattr(self,'page',None)
        if page is not None:page.setBackgroundColor(color)
        web=getattr(self,'web',None)
        palette=self.palette();palette.setColor(QPalette.ColorRole.Window, color);self.setPalette(palette);self.setAutoFillBackground(True)
        if web is None:return
        web_palette=web.palette();web_palette.setColor(QPalette.ColorRole.Window, color);web.setPalette(web_palette);web.setAutoFillBackground(True)

    def showEvent(self, event):
        super().showEvent(event)
        theme=str(self.settings.value('reader/theme') or 'light')
        self._apply_page_background('dark' if theme=='dark' else 'light')
        self._place_overlay(getattr(self,'startup_label',None))

    def _trace(self, event, **extra):
        path=getattr(self,'_resize_trace',None)
        if not path:return
        row={'event':event,'t':round(time.perf_counter()-self.started,4),'view':getattr(self,'_view_token',0),'channel':getattr(self,'_channel_token',0),'loads':getattr(self,'_load_count',0),'w':self.width(),'h':self.height(),'state':int(self.windowState().value)}
        row.update(extra)
        try:
            with open(path,'a',encoding='utf8') as handle:handle.write(json.dumps(row,ensure_ascii=False)+'\n')
        except OSError:pass

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type()==QEvent.Type.WindowStateChange:self._trace('window-state')

    def resizeEvent(self, event):
        started=time.perf_counter()
        super().resizeEvent(event)
        self._place_overlay(getattr(self,'startup_label',None))
        recovery=getattr(self,'recovery',None)
        if recovery is not None and recovery.isVisible():self._place_overlay(recovery)
        if getattr(self,'_resize_trace',None):self._trace('resize', ms=round((time.perf_counter()-started)*1000,3))

    def _set_startup(self, text):
        label=getattr(self,'startup_label',None)
        if label is None:return
        label.setText(text);label.show();self._place_overlay(label)

    def _hide_startup(self):
        label=getattr(self,'startup_label',None)
        if label is not None:label.hide()

    def _mark(self, name):
        self.phases[name]=round((time.perf_counter()-self.started)*1000,1)

    def _is_open(self):return not self._closing and not self._closed

    def _later(self,ms,action):
        if not self._is_open():return
        timer=QTimer(self);timer.setSingleShot(True);self._timers.add(timer)
        def fired():
            self._timers.discard(timer);timer.deleteLater()
            if self._is_open():action()
        timer.timeout.connect(fired);timer.start(ms)

    def _document_load_started(self):
        # A new document invalidates replies aimed at the previous renderer.
        self._load_count=getattr(self,'_load_count',0)+1
        self._trace('load-started')
        self._renderer_alive=False
        self._accept_deferred=self._is_open()
        self._deferred_scripts.clear()
        reader=getattr(getattr(self,'transport',None),'reader',None)
        if reader:reader.invalidate()

    def _document_load_finished(self,ok):
        if not self._is_open():return
        if not ok:
            self._renderer_alive=False;self._accept_deferred=False;self._deferred_scripts.clear()
            self._show_failure('页面没有载入。任务没有被启动。可以关闭窗口后重试。')
            return
        pid_reader=getattr(self.page,'renderProcessPid',None)
        if pid_reader is not None:
            try:pid=int(pid_reader())
            except (RuntimeError,TypeError,ValueError,OSError):pid=0
            if pid<=0:
                self._renderer_alive=False;self._accept_deferred=False;self._deferred_scripts.clear();return
        self._renderer_alive=True;self._accept_deferred=False
        self._mark('load_finished')
        self.recovery.hide()
        pending=self._deferred_scripts;self._deferred_scripts=[]
        for script in pending:
            if not self._is_open() or not self._renderer_alive:break
            try:self.page.runJavaScript(script)
            except RuntimeError:
                self._renderer_alive=False;break

    def run_page_script(self,script,callback=None):
        """Run JavaScript only while this page's renderer is alive.

        Channel replies that arrive during a live load are held until
        loadFinished. Replies that arrive after termination are dropped so
        they cannot touch a dead render process or the next document.
        """
        if not self._is_open() or not self._renderer_alive:
            if callback is None and self._accept_deferred and len(self._deferred_scripts)<32:
                self._deferred_scripts.append(script)
            return False
        pid_reader=getattr(self.page,'renderProcessPid',None)
        if pid_reader is not None:
            try:pid=int(pid_reader())
            except (RuntimeError,TypeError,ValueError,OSError):pid=0
            if pid<=0:
                self._renderer_alive=False;return False
        try:
            if callback is None:self.page.runJavaScript(script)
            else:self.page.runJavaScript(script,callback)
        except RuntimeError:
            self._renderer_alive=False;return False
        return True

    def present_when_ready(self):
        """Let Qt WebEngine render, then reveal the real window on the shell signal."""
        if self._shell_visible:
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
            if self.windowOpacity()<1:self.setWindowOpacity(1)
            if not self.isVisible():self.show()
            return
        self._defer_present=True
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowOpacity(0)
        self.show()
        self._mark('hidden_show')

    def _shell_ready(self, payload):
        if not self._is_open() or self._shell_visible:return
        if not isinstance(payload, dict) or payload.get('interactive') is not True:return
        self._shell_visible=True
        self._mark('interactive')
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        if not self.isVisible():self.show()
        if self.windowOpacity()<1:self.setWindowOpacity(1)
        self.raise_();self.activateWindow()
        self.phases['shown']=self.phases.get('interactive')
        self._trace('shell-ready', url=self.page.url().toString())
        try:(self.state_root/'startup-phases.json').write_text(json.dumps(self.phases,ensure_ascii=False,indent=2),encoding='utf8')
        except OSError:pass

    def _reveal_if_unready(self):
        if not self._is_open() or self._shell_visible:return
        self._diagnose_if_unready()
        log=self.state_root/'startup-diagnostic.json'
        self._show_failure('界面没有在限定时间内准备好。任务没有被启动。\n诊断日志：'+str(log))

    def _show_failure(self, text):
        if not self._is_open() or self._failure_shown:return
        self._failure_shown=True
        self._mark('failure')
        try:(self.state_root/'startup-phases.json').write_text(json.dumps(self.phases,ensure_ascii=False,indent=2),encoding='utf8')
        except OSError:pass
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        if self.windowOpacity()<1:self.setWindowOpacity(1)
        if not self.isVisible():self.show()
        self.raise_();self.activateWindow()
        QMessageBox.critical(self, window_title(), text)

    def _diagnose_if_unready(self):
        if not self._is_open() or self.metrics:return
        info={'js_errors':list(self.page.errors),'assets':list(self.assets.requests),'blocked':list(self.interceptor.blocked),'channel_rejected':list(self.transport.blocked),'calls':list(self.bridge.calls),'url':self.page.url().toString(),'renderer_alive':self._renderer_alive}
        def save(raw):
            if not self._is_open():return
            try:info['document']=json.loads(raw or '{}')
            except (TypeError,ValueError):info['document']={}
            (self.state_root/'startup-diagnostic.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf8')
        if not self.run_page_script("JSON.stringify({qweb:typeof QWebChannel,rootChildren:document.getElementById('root')?.childElementCount,scripts:Array.from(document.scripts).map(s=>s.src),sheets:document.styleSheets.length})",save):
            info['document']={'blocked':'renderer script gate'}
            (self.state_root/'startup-diagnostic.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf8')

    def _note_recovery_success(self):
        self.recovery.hide();self.recovery_count=0
        if self._recovery_total<MAX_SESSION_RECOVERY:self._recovery_exhausted=False

    def _ui_ready(self,payload):
        if not self._is_open():return
        self._note_recovery_success()
        self._hide_startup()
        if 'ready' in self.phases:return
        self._mark('ready')
        self._trace('ui-ready', url=self.page.url().toString())
        if os.environ.get('DAS_RESIZE_LAB')=='open-task':
            def open_task():
                self.run_page_script("document.querySelector('.nav-task')?.click(); 'opened'", lambda _raw: self._trace('task-open'))
                self.run_page_script("(()=>{const c=document.createElement('canvas');const gl=c.getContext('webgl');if(!gl)return 'no-webgl';const d=gl.getExtension('WEBGL_debug_renderer_info');return d?String(gl.getParameter(d.UNMASKED_RENDERER_WEBGL)):'webgl';})()", lambda raw: self._trace('gpu', renderer=raw))
            self._later(400, open_task)
        try:(self.state_root/'startup-phases.json').write_text(json.dumps(self.phases,ensure_ascii=False,indent=2),encoding='utf8')
        except OSError:pass
        screen=self.screen()
        self.metrics.update({'pid':__import__('os').getpid(),'platform':platform.platform(),'python':platform.python_version(),'qt':qt_version,'startup_to_interactive_ms':self.phases.get('interactive'),'startup_to_data_ms':(time.perf_counter()-self.started)*1000,'page_to_data_ms':(time.perf_counter()-self.load_started)*1000,'phases':dict(self.phases),'window_client':[self.width(),self.height()],'device_pixel_ratio':self.devicePixelRatioF(),'screen_device_pixel_ratio':screen.devicePixelRatio(),'logical_dpi':screen.logicalDotsPerInch(),'physical_dpi':screen.physicalDotsPerInch(),'ui':payload,'resource_requests':list(self.assets.requests),'blocked_navigation':list(self.page.blocked_navigation),'blocked_resource_requests':list(self.interceptor.blocked),'bridge_calls':list(self.bridge.calls),'execution_gate':'default_mock_deny_real'})
        self.ready.emit(dict(self.metrics))

    def _show_recovery(self):
        if getattr(self,'recovery_label',None) is not None:
            self.recovery_label.setText('页面渲染进程已退出，自动恢复已停止。真实任务与历史记录未被改变。可以手动重新加载。' if self._recovery_exhausted else '页面渲染进程已退出。真实任务与历史记录未被改变。')
        recovery=getattr(self,'recovery',None);web=getattr(self,'web',None)
        if recovery is None or web is None:return
        if not self.isVisible():self.show()
        if self.windowOpacity()<1:self.setWindowOpacity(1)
        self._hide_startup()
        recovery.setGeometry(web.geometry());recovery.show();recovery.raise_()

    def _schedule_recovery(self):
        if not self._is_open() or self._recovery_pending:return
        if self._recovery_exhausted or self.recovery_count>=MAX_CONSECUTIVE_RECOVERY or self._recovery_total>=MAX_SESSION_RECOVERY:
            self._recovery_exhausted=True;self._show_recovery();return
        self.recovery_count+=1;self._recovery_total+=1;self._recovery_pending=True
        def fire():
            self._recovery_pending=False
            self.reload_local()
        self._later(500,fire)

    def _renderer_ended(self,status,exit_code):
        # No QWebEnginePage navigation or JavaScript in this slot. Reload is a
        # later event-loop turn, and it stops after the explicit cap.
        self._renderer_alive=False;self._accept_deferred=False
        deferred=getattr(self,'_deferred_scripts',None)
        if deferred is not None:deferred.clear()
        self.crashes.append({'status':getattr(status,'name',str(status)),'exit_code':exit_code,'at':time.perf_counter()})
        reader=getattr(getattr(self,'transport',None),'reader',None)
        if reader:reader.invalidate()
        DesktopWindow._record_renderer_state(self)
        if not self._is_open():return
        self._show_recovery();self._schedule_recovery()

    def _record_renderer_state(self):
        root=getattr(self,'state_root',None)
        if root is None:return
        try:
            root.mkdir(parents=True,exist_ok=True)
            (root/'renderer-recovery.json').write_text(json.dumps({
                'crashes':list(self.crashes),'consecutive':self.recovery_count,'session':self._recovery_total,
                'exhausted':self._recovery_exhausted,'renderer_alive':self._renderer_alive,
            },ensure_ascii=False,indent=2),encoding='utf8')
        except OSError:pass

    def reload_local(self):
        if not self._is_open():return
        self.load_started=time.perf_counter();self.web.load(QUrl(ORIGIN+'/index.html'))

    def snapshot(self,callback):
        if not self._is_open():return
        def received(raw):
            if not self._is_open():return
            try:value=json.loads(raw or '{}')
            except (TypeError,ValueError):return
            callback(value)
        self.run_page_script('JSON.stringify(window.__studioSnapshot ? window.__studioSnapshot() : {})',received)

    def closeEvent(self,event):
        if self._closed:event.accept();return
        event.ignore()
        if self._closing:return
        self._closing=True
        for timer in self._timers:timer.stop();timer.deleteLater()
        self._timers.clear()
        # Read the known local UI snapshot; no browser-callable write method.
        # Chromium localStorage can still have pending disk writes at exit.
        def saved(snapshot):
            if self._closed:return
            values={'theme':snapshot.get('theme'),'reading':snapshot.get('reading'),'task':snapshot.get('taskId'),'project':snapshot.get('projectId'),'panel':str(snapshot.get('panel',True)).lower()}
            for key,value in values.items():
                if not isinstance(value,str) or len(value)>300:continue
                if key=='theme' and value not in {'light','dark'}:continue
                if key=='reading' and value not in {'concise','standard','detailed'}:continue
                if key=='panel' and value not in {'true','false'}:continue
                if key=='task' and value and value not in self.bridge.store._runs:continue
                if key=='project' and value and value not in self.bridge.store._projects and value!='__archive__':continue
                self.settings.setValue('reader/'+key,value)
            for key,low,high in (('panelWidth',240,720),('sidebarWidth',200,360)):
                try:number=int(round(float(snapshot.get(key))))
                except (TypeError,ValueError):continue
                if low<=number<=high:self.settings.setValue('reader/'+key,str(number))
            if snapshot.get('panelWidthChosen') in {'1','true',True}:self.settings.setValue('reader/panelWidthChosen','1')
            self._finish_close()
        def received(raw):
            if self._closed:return
            if not isinstance(raw,str) or len(raw.encode('utf8'))>8192:self._finish_close();return
            try:values=json.loads(raw)
            except (ValueError,TypeError):self._finish_close();return
            if isinstance(values,dict):saved(values)
            else:self._finish_close()
        script='JSON.stringify((()=>{const s=window.__studioSnapshot?.()||{};return {theme:s.theme,reading:s.reading,taskId:s.taskId,projectId:s.projectId,panel:s.panel,panelWidth:s.panelWidth,sidebarWidth:s.sidebarWidth,panelWidthChosen:s.panelWidthChosen};})())'
        # Closing has already begun, so the normal page-script gate would drop this read.
        started=False
        if self._renderer_alive:
            try:
                self.page.runJavaScript(script,received)
                started=True
            except RuntimeError:
                self._renderer_alive=False
        if not started:self._finish_close()
        self._close_timer.start(1000)

    def _finish_close(self):
        if self._closed:return
        self._closed=True;self._renderer_alive=False;self._accept_deferred=False;self._deferred_scripts.clear()
        self._close_timer.stop()
        try:self.page.renderProcessTerminated.disconnect(self._renderer_ended)
        except (RuntimeError,TypeError):pass
        self.settings.setValue('geometry',self.saveGeometry());self.settings.sync()
        self.transport.reader.shutdown()
        self.web.stop();self.close()

def main(started=None):
    origin=started or time.perf_counter()
    parser=argparse.ArgumentParser();parser.add_argument('--width',type=int,default=1440);parser.add_argument('--height',type=int,default=900);parser.add_argument('--state',type=Path);parser.add_argument('--measure',type=Path);parser.add_argument('--ime-monitor',type=Path);parser.add_argument('--auto-close',type=float,default=0);parser.add_argument('--no-restore',action='store_true');parser.add_argument('--read-only',action='store_true');args=parser.parse_args()
    app=QApplication.instance()
    if app is None:
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        app=QApplication(sys.argv);app.setFont(QFont('Microsoft YaHei UI',10))
    qapp_ms=round((time.perf_counter()-origin)*1000,1)
    marker=deployment_marker()
    if marker:
        app.setApplicationName('Dual Agent Studio');app.setOrganizationName('DualAgentStudio')
    else:
        app.setApplicationName('Dual Agent Studio v0.3 read-only validation')
    # Chromium opens databases and caches: keep them outside Git/coordinator
    # workspaces so their file locks cannot interfere with source protection.
    # A portable deployment keeps that state beside the install, not in AppData.
    state=args.state or ((ROOT/'state') if marker else Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))/'validation-state')
    def report(etype,value,tb):
        text=''.join(traceback.format_exception(etype,value,tb))[-4000:]
        try:
            state.mkdir(parents=True,exist_ok=True)
            (state/'startup-diagnostic.json').write_text(json.dumps({'uncaught':text},ensure_ascii=False,indent=2),encoding='utf8')
        except OSError:pass
        sys.__excepthook__(etype,value,tb)
    sys.excepthook=report
    store=HistoryStore(ROOT/'projects',ROOT/'runs')
    store_ms=round((time.perf_counter()-origin)*1000,1)
    client=None if args.read_only else HostClient(ROOT/'projects',ROOT/'runs',VALIDATION/'mock-policy.json')
    try:window=DesktopWindow(store,state,(args.width,args.height),origin,not args.no_restore,client=client)
    except RuntimeError as exc:
        text=str(exc)
        print(text,file=sys.stderr)
        QMessageBox.critical(None, 'Dual Agent Studio', text+'\n\n诊断已写入启动日志。')
        return 3
    window.phases['qapp']=qapp_ms
    window.phases['store']=store_ms
    try:
        early=json.loads(os.environ.get('DAS_STARTUP_MARKS') or '{}')
    except json.JSONDecodeError:
        early={}
    if isinstance(early, dict):
        for key, value in early.items():
            if isinstance(value, (int, float)): window.phases[key]=value
    if args.ime_monitor:
        monitor=QTimer(window)
        def save_ime(snapshot):
            if not window._is_open():return
            args.ime_monitor.parent.mkdir(parents=True,exist_ok=True)
            args.ime_monitor.write_text(json.dumps({'draft':snapshot.get('draft'),'events':snapshot.get('imeProbe'),'input_locale':app.inputMethod().locale().name()},ensure_ascii=False,indent=2),encoding='utf8')
        monitor.timeout.connect(lambda:window.snapshot(save_ime));monitor.start(700)
    if args.measure:
        def measured(data):
            def collect():
                if not window._is_open():return
                data.update(memory=memory(),crashes=window.crashes,js_errors=list(window.page.errors))
                window.snapshot(lambda snapshot:save(data,snapshot))
            def save(data,snapshot):
                if not window._is_open():return
                data['snapshot']=snapshot;args.measure.parent.mkdir(parents=True,exist_ok=True);args.measure.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
                window.web.grab().save(str(args.measure.with_suffix('.png')))
                if args.auto_close:window._later(int(args.auto_close*1000),window.close)
            window._later(1000,collect)
        window.ready.connect(measured)
    window.present_when_ready()
    return app.exec()
