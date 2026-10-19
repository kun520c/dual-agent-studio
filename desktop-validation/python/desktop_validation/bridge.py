from __future__ import annotations
import json
from .bounded import BoundedLog
from PySide6.QtCore import QObject,Slot
from .store import HistoryStore
from .project_api import ProjectService, PROJECT_METHODS

METHODS=('listProjects','listTasks','loadTask','loadEvidence','findTask')
BUSINESS_METHODS=('getTaskDefaults','validateTask','preflightTask','submitTask','listSchedules','cancelTask')
MANAGEMENT_METHODS=PROJECT_METHODS

class ReadOnlyBridge(QObject):
    def __init__(self,store:HistoryStore):
        super().__init__();self.store=store;self.calls=BoundedLog(200)

    def _reply(self,method,action):
        self.calls.append(method)
        try:
            payload=json.dumps({'ok':True,'data':action()},ensure_ascii=False)
            if len(payload.encode('utf8'))>2*1024*1024:raise ValueError('单次返回超过 2 MiB 上限')
            return payload
        except (ValueError,OSError,TypeError,KeyError,OverflowError) as error:
            return json.dumps({'ok':False,'error':str(error)[:300],'code':getattr(error,'code','READ_ERROR')},ensure_ascii=False)

    @Slot(result=str)
    def listProjects(self):return self._reply('listProjects',self.store.list_projects)

    @Slot(str,int,int,result=str)
    def listTasks(self,project,offset,limit):return self._reply('listTasks',lambda:self.store.list_tasks(project,offset,limit))

    @Slot(str,int,int,result=str)
    def loadTask(self,identifier,message_offset,event_offset):return self._reply('loadTask',lambda:self.store.load_task(identifier,message_offset,event_offset))

    @Slot(str,str,str,int,int,result=str)
    def loadEvidence(self,identifier,kind,artifact,offset,limit):return self._reply('loadEvidence',lambda:self.store.load_evidence(identifier,kind,artifact,offset,limit))

    @Slot(str,result=str)
    def findTask(self,task):return self._reply('findTask',lambda:self.store.find_task(task))

class StudioBridge(ReadOnlyBridge):
    def __init__(self, store, client):
        super().__init__(store); self.client=client
        self.projects=ProjectService(store, getattr(client,'policy',None))

    @Slot(result=str)
    def listProjects(self):
        return self._reply('listProjects', self.projects.list_rows)

    @Slot(str,result=str)
    def getProject(self,request):return self._reply('getProject',lambda:self.projects.get(request))

    @Slot(str,result=str)
    def inspectAccess(self,request):return self._reply('inspectAccess',lambda:self.projects.inspect_access(request))

    @Slot(str,result=str)
    def saveProject(self,request):return self._reply('saveProject',lambda:self.projects.save(request))

    @Slot(str,result=str)
    def setProjectEnabled(self,request):return self._reply('setProjectEnabled',lambda:self.projects.enabled(request))

    @Slot(str,result=str)
    def setDefaultProject(self,request):return self._reply('setDefaultProject',lambda:self.projects.set_default(request))

    @Slot(str,result=str)
    def inspectWorkspace(self,request):return self._reply('inspectWorkspace',lambda:self.projects.inspect_workspace(request))

    @Slot(str,result=str)
    def retireProject(self,request):return self._reply('retireProject',lambda:self.projects.retire(request))

    @Slot(str,result=str)
    def createRehearsal(self,request):return self._reply('createRehearsal',lambda:self.projects.create_rehearsal(request))

    @Slot(str,result=str)
    def getTaskDefaults(self,request):return self._reply('getTaskDefaults',lambda:self.client.call('getTaskDefaults',request))

    @Slot(str,result=str)
    def validateTask(self,request):return self._reply('validateTask',lambda:self.client.call('validateTask',request))

    @Slot(str,result=str)
    def preflightTask(self,request):return self._reply('preflightTask',lambda:self.client.call('preflightTask',request))

    @Slot(str,result=str)
    def submitTask(self,request):return self._reply('submitTask',lambda:self.client.call('submitTask',request))

    @Slot(str,result=str)
    def listSchedules(self,request):return self._reply('listSchedules',lambda:self.client.call('listSchedules',request))

    @Slot(str,result=str)
    def cancelTask(self,request):return self._reply('cancelTask',lambda:self.client.call('cancelTask',request))
