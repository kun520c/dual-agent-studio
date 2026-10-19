from __future__ import annotations
import json
from pathlib import Path
from urllib.parse import unquote
from PySide6.QtCore import QByteArray,QBuffer,QIODevice,QUrl
from PySide6.QtWebEngineCore import QWebEngineUrlScheme,QWebEngineUrlSchemeHandler,QWebEngineUrlRequestJob,QWebEngineUrlRequestInterceptor
from PySide6.QtWebChannel import QWebChannelAbstractTransport
from .bridge import METHODS,BUSINESS_METHODS,MANAGEMENT_METHODS
from .reader import ReadDispatcher
from .bounded import BoundedLog

ORIGIN='studio://app'
CHANNEL_SCRIPT='qrc:///qtwebchannel/qwebchannel.js'

def register_scheme():
    scheme=QWebEngineUrlScheme(b'studio')
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.HostAndPort)
    scheme.setDefaultPort(80)
    scheme.setFlags(QWebEngineUrlScheme.Flag.SecureScheme|QWebEngineUrlScheme.Flag.LocalScheme|QWebEngineUrlScheme.Flag.CorsEnabled|QWebEngineUrlScheme.Flag.FetchApiAllowed)
    QWebEngineUrlScheme.registerScheme(scheme)

def allowed_url(url:QUrl,channel_script=True)->bool:
    if channel_script and url.toString()==CHANNEL_SCRIPT:return True
    return url.scheme()=='studio' and url.host()=='app' and url.port(-1) in {-1,80} and not url.userName() and not url.password()

class LocalAssets(QWebEngineUrlSchemeHandler):
    def __init__(self,root:Path,parent=None):
        super().__init__(parent);self.root=root.resolve();self.requests=BoundedLog(200)
        self.files={('/'+str(path.relative_to(self.root)).replace('\\','/')):path for path in self.root.rglob('*') if path.is_file() and path.suffix in {'.html','.js','.css','.svg'}}

    def requestStarted(self,job):
        url=job.requestUrl();path=unquote(url.path());path='/index.html' if path=='/' else path
        self.requests.append(url.toString())
        source=self.files.get(path)
        if not allowed_url(url,False) or job.requestMethod()!=b'GET' or source is None or not source.resolve().is_relative_to(self.root) or source.is_symlink():
            job.fail(QWebEngineUrlRequestJob.Error.RequestDenied);return
        types={'.html':b'text/html','.css':b'text/css','.js':b'application/javascript','.svg':b'image/svg+xml'}
        buffer=QBuffer(job);buffer.setData(QByteArray(source.read_bytes()));buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        job.reply(types[source.suffix],buffer)

class LocalOnlyRequests(QWebEngineUrlRequestInterceptor):
    def __init__(self,parent=None):super().__init__(parent);self.blocked=BoundedLog(100)
    def interceptRequest(self,info):
        if not allowed_url(info.requestUrl()):self.blocked.append(info.requestUrl().toString());info.block(True)

class RestrictedTransport(QWebChannelAbstractTransport):
    """QWebChannel over an origin-bound carrier with a native method allowlist.

    Filters init metadata AND incoming invocation/property packets. Inherited
    QObject deleteLater/objectName operations never become callable by the page.
    """
    def __init__(self,page,bridge,parent=None,async_reads=False):
        super().__init__(parent);self.page=page;self.blocked=BoundedLog(100);meta=bridge.metaObject()
        self.allowed={i:bytes(meta.method(i).name()).decode() for i in range(meta.methodCount()) if bytes(meta.method(i).name()).decode() in METHODS+BUSINESS_METHODS+MANAGEMENT_METHODS}
        self.methods=set(self.allowed.values())
        self.reader=ReadDispatcher(bridge,self._reply,self) if async_reads else None

    def _reply(self,request,value):
        if allowed_url(self.page.url(),False):self.sendMessage({'type':10,'id':request['id'],'data':value})

    def sendMessage(self,message):
        packet=dict(message)
        if packet.get('type')==10 and isinstance(packet.get('data'),dict):
            pass
        data=packet.get('data')
        if packet.get('type')==10 or packet.get('type')==3:
            if isinstance(data,dict) and isinstance(data.get('history'),dict):
                obj=data['history'];obj['methods']=[m for m in obj.get('methods',[]) if m[0] in self.methods];obj['properties']=[];obj['signals']=[]
        encoded=json.dumps(packet,ensure_ascii=False,separators=(',',':'))
        script='window.__channelReceive && window.__channelReceive('+encoded+')'
        runner=getattr(self.page,'run_guarded',None)
        if runner:runner(script)
        else:self.page.runJavaScript(script)

    def receive(self,raw:str)->bool:
        try:
            if not allowed_url(self.page.url(),False) or len(raw.encode('utf8'))>16384:raise ValueError('origin/size')
            packet=json.loads(raw)
            if not isinstance(packet,dict):raise ValueError('packet')
            kind=packet.get('type')
            if kind not in {3,4,6}:raise ValueError('type')
            if kind==6:
                method=packet.get('method')
                name=self.allowed.get(method) if type(method) is int else method if isinstance(method,str) and method in self.methods else None
                if packet.get('object')!='history' or name not in self.methods:raise ValueError('method')
                args=packet.get('args',[])
                expected={'listProjects':0,'listTasks':3,'loadTask':3,'loadEvidence':5,'findTask':1,**{m:1 for m in BUSINESS_METHODS+MANAGEMENT_METHODS}}[name]
                if not isinstance(args,list) or len(args)!=expected:raise ValueError('args')
                if any(isinstance(a,str) and (len(a.encode('utf8'))>12000 if name in BUSINESS_METHODS+MANAGEMENT_METHODS else len(a)>300) for a in args):raise ValueError('string')
                types={'listProjects':(),'listTasks':(str,int,int),'loadTask':(str,int,int),'loadEvidence':(str,str,str,int,int),'findTask':(str,),**{m:(str,) for m in BUSINESS_METHODS+MANAGEMENT_METHODS}}[name]
                if any(type(arg) is not kind for arg,kind in zip(args,types)):raise ValueError('arg types')
                if type(packet.get('id')) is not int or packet['id']<0:raise ValueError('request id')
                if self.reader:
                    self.reader.submit(packet,name);return True
            self.messageReceived.emit(packet,self)
            return True
        except (ValueError,TypeError,KeyError,RecursionError) as error:
            details={'reason':str(error)[:300]}
            if 'packet' in locals() and isinstance(packet,dict):details.update(type=packet.get('type'),method=packet.get('method'))
            self.blocked.append(details);return False
