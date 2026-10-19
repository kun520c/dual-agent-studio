export interface Project { id:string; name:string; workspace:string; enabled:boolean; registered:boolean;isDefault?:boolean;configError?:string;revision?:string }
export interface Posture { states:string[]; label:string; nextStep:string; executionGrantedByUi:boolean }
export interface ProjectReadout { verification:{type:string;summary:string}[]; worktree:{enabled?:boolean;summary?:string;base_ref?:string;root?:string;path?:string}; budget:Budget }
export interface Outcome { kind:string; label:string; passed:boolean; reason:string; tone:string }
export interface Task { id:string; projectId:string; taskId:string; title:string; goal:string; state:string; outcome:Outcome; started:string; finished:string; mode:string; executionMode?:string; modelCalls:number;callKind?:string }
export interface PreflightItem { code:string; title:string; status:string; detail:string }
export interface Preflight { ready:boolean; mode:string; realModelCalls:number; items:PreflightItem[] }
export interface UsageReport {cli_launches:number|null;cli_launch_limit:number|null;totals:Record<string,{known:number|null;complete:boolean}>;rounds:({round:number;executor:string;role:string;simulation:boolean}&Record<string,unknown>)[]}
export interface Run { taskId:string; task:Task; messages:Record<string,unknown>[]; events:Record<string,unknown>[]; messageTotal:number; eventTotal:number; messageOffset:number; eventOffset:number; permission:Record<string,unknown>; usage:Record<string,number>;usageReport?:UsageReport;usageRecorded:Record<string,boolean>; integrityWarnings:string[]; agents:Record<string,{saved:number;invoked:number}>;verificationSummary:Record<string,unknown>[];verificationTotal:number;resultSummary:Record<string,unknown>;hasResult:boolean }
export interface DiffLine { old:string; next:string; kind:'same'|'add'|'remove'|'hunk'; text:string }
export interface DiffFile { id:string; path:string; added:number; removed:number; total:number; source?:string }
export interface Evidence { taskId:string;kind:string; files?:DiffFile[]; lines?:DiffLine[]; offset?:number; total?:number; source:string; data?:unknown; truncated?:boolean;present:boolean;invalidRecords?:number;snapshot?:boolean }
type Callback=(value:string)=>void;
export interface AccessMode { allowed:boolean; reason:string; code?:string }
export interface AccessCheck { code:string; title:string; status:string; detail:string; action:string }
export interface AccessReport { projectId:string; realModelCalls:number; signerInstalled:boolean; modes:Record<string,AccessMode>; authorization:{present:boolean;status:string;detail:string;signer?:string;mode?:string;expires?:number|null;maxTasks?:number|null;projectSha256?:string}; cli:{codex:{status:string;detail:string;version?:string};cursor:{status:string;detail:string;version?:string}}; account:{status:string;detail:string}; unsigned:Record<string,unknown>; export:{relative:string;bytes:number}|null; nextSteps:string[]; checklist?:AccessCheck[] }
export interface WorkspaceReport { workspace:string; normalized:string; accepted:boolean; errors:string[]; warnings:string[]; duplicate:string|null; git:{available:boolean;branch:string;changes:string;error:string;summary?:string}; executionGranted:boolean }
export interface Retired { retired:boolean; projectId:string; workspacePreserved:string; historyPreserved:boolean; sourceDeleted:boolean }
interface NativeBridge {getProject:(request:string,cb:Callback)=>void;saveProject:(request:string,cb:Callback)=>void;setProjectEnabled:(request:string,cb:Callback)=>void;setDefaultProject:(request:string,cb:Callback)=>void;inspectAccess:(request:string,cb:Callback)=>void;inspectWorkspace:(request:string,cb:Callback)=>void;retireProject:(request:string,cb:Callback)=>void;createRehearsal:(request:string,cb:Callback)=>void;findTask:(task:string,cb:Callback)=>void}
export interface Budget {modelCalls:number;rounds:number;repairs:number;runSeconds:number;stepSeconds:number}
export interface TaskRequest {projectId:string;title:string;goal:string;templateId:string;mode?:string;executionMode?:string;budget?:Budget}
export interface Defaults {projectId:string;mode:string;templates:{id:string;label:string}[];budget:Budget;verification:{type:string;label:string}[];participants:{id:string;executor:string;role:string}[];worktree:Record<string,unknown>;workspace:string;assignment:string}
export interface ProjectDetails {config:Record<string,unknown>;revision:string;git:{available:boolean;branch:string;changes:string;error:string;summary?:string};execution:{allowed:boolean;mode:string;reason:string;modes?:Record<string,{allowed:boolean;reason:string}>};posture?:Posture;readout?:ProjectReadout;isDefault:boolean;rehearsal?:{projectId:string;created:boolean;reset:boolean;drifted:boolean;workspace:string}}
export interface Submission {requestId:string;scheduleId:string;taskId:string;projectId:string;state:string;duplicate:boolean;mode:string}
export interface Schedule {schedule_id:string;project_id:string;task_id:string;title:string;state:string;detail:string;stop_reason:string;updated_at:string;mode:string;cancellationRequested?:boolean}
interface NativeBridge { listProjects:(cb:Callback)=>void; listTasks:(project:string,offset:number,limit:number,cb:Callback)=>void; loadTask:(id:string,messageOffset:number,eventOffset:number,cb:Callback)=>void; loadEvidence:(id:string,kind:string,artifact:string,offset:number,limit:number,cb:Callback)=>void; getTaskDefaults:(request:string,cb:Callback)=>void;validateTask:(request:string,cb:Callback)=>void;preflightTask:(request:string,cb:Callback)=>void;submitTask:(request:string,cb:Callback)=>void;listSchedules:(request:string,cb:Callback)=>void;cancelTask:(request:string,cb:Callback)=>void }
declare global { interface Window { __desktopPrefs?:Record<string,string>;QWebChannel:new(transport:unknown,callback:(channel:{objects:{history:NativeBridge}})=>void)=>void; __channelReceive?:(message:unknown)=>void; __studioReady?:boolean; __studioShell?:boolean; __studioSnapshot?:()=>unknown; __readerMethods?:string[]; __imeProbe?:unknown[]; __pollRuns?:number; __projectFetches?:number; __overviewFetches?:number; __inspectFetches?:number; __taskTrace?:string[]; __studioTransport?:{send:(data:string)=>void;onmessage?:((event:{data:unknown})=>void)} } }
let native:NativeBridge;
export async function connect():Promise<void>{
  await new Promise<void>((resolve,reject)=>{
    const timeout=setTimeout(()=>reject(new Error('只读桥接连接超时')),10000);
    const transport={send:(data:string)=>console.info('DAS_CHANNEL '+data),onmessage:undefined as ((event:{data:unknown})=>void)|undefined};
    window.__studioTransport=transport;
    window.__channelReceive=(message:unknown)=>transport.onmessage?.({data:message});
    new window.QWebChannel(transport,channel=>{native=channel.objects.history;window.__readerMethods=Object.keys(native).filter(k=>['listProjects','listTasks','loadTask','loadEvidence','getTaskDefaults','validateTask','preflightTask','submitTask','listSchedules','cancelTask','getProject','saveProject','setProjectEnabled','setDefaultProject','inspectAccess','inspectWorkspace','retireProject','createRehearsal','findTask'].includes(k));clearTimeout(timeout);resolve();});
  });
}
function invoke<T>(call:(cb:Callback)=>void):Promise<T>{return new Promise((resolve,reject)=>{const timeout=setTimeout(()=>reject(new Error('桥接响应超时；提交重试将保留原请求 ID')),15000);try{call(value=>{clearTimeout(timeout);try{const response=JSON.parse(value);if(!response.ok)reject(new Error(`${response.code||'BRIDGE_ERROR'}：${response.error}`));else resolve(response.data as T);}catch(e){reject(e);}});}catch(e){clearTimeout(timeout);reject(e);}});}
function remember<T>(bucket:Map<string,Promise<T>>,key:string,load:()=>Promise<T>):Promise<T>{const current=bucket.get(key);if(current)return current;const pending=load().catch(error=>{bucket.delete(key);throw error;});bucket.set(key,pending);return pending;}
const projectReads=new Map<string,Promise<ProjectDetails>>();
const accessReads=new Map<string,Promise<AccessReport>>();
const defaultReads=new Map<string,Promise<Defaults>>();
function forgetProject(id=''){if(id){projectReads.delete(id);accessReads.delete(id);for(const key of [...defaultReads.keys()])if(key.startsWith(id+'\n'))defaultReads.delete(key);}else{projectReads.clear();accessReads.clear();defaultReads.clear();}}
export const api={
  invalidateProject:(projectId:string)=>forgetProject(projectId),
  writable:()=>typeof native?.submitTask==='function',
  defaults:(projectId:string,executionMode='mock')=>remember(defaultReads,projectId+'\n'+executionMode,()=>invoke<Defaults>(cb=>native.getTaskDefaults(JSON.stringify({projectId,executionMode}),cb))),
  validate:(request:TaskRequest)=>invoke<{valid:boolean}>(cb=>native.validateTask(JSON.stringify(request),cb)),
  preflight:(request:TaskRequest)=>invoke<Preflight>(cb=>native.preflightTask(JSON.stringify(request),cb)),
  submit:(request:TaskRequest&{requestId:string})=>invoke<Submission>(cb=>native.submitTask(JSON.stringify(request),cb)),
  schedules:(scheduleId='',offset=0)=>invoke<{items:Schedule[];total:number;hostPid:number}>(cb=>native.listSchedules(JSON.stringify({scheduleId,offset}),cb)),
  project:(projectId:string)=>remember(projectReads,projectId,()=>invoke<ProjectDetails>(cb=>native.getProject(JSON.stringify({projectId}),cb))),
  inspect:(projectId:string,exportRequest=false)=>exportRequest?invoke<AccessReport>(cb=>native.inspectAccess(JSON.stringify({projectId,export:true}),cb)):remember(accessReads,projectId,()=>invoke<AccessReport>(cb=>native.inspectAccess(JSON.stringify({projectId,export:false}),cb))),
  saveProject:(config:Record<string,unknown>,create:boolean,revision?:string)=>invoke<ProjectDetails>(cb=>native.saveProject(JSON.stringify({config,create,revision}),cb)).then(value=>{forgetProject(String(config.project_id||''));return value;}),
  enableProject:(projectId:string,enabled:boolean,revision:string)=>invoke<ProjectDetails>(cb=>native.setProjectEnabled(JSON.stringify({projectId,enabled,revision}),cb)).then(value=>{forgetProject(projectId);return value;}),
  defaultProject:(projectId:string)=>invoke<{defaultProjectId:string}>(cb=>native.setDefaultProject(JSON.stringify({projectId}),cb)),
  inspectWorkspace:(workspace:string,projectId='',create=true)=>invoke<WorkspaceReport>(cb=>native.inspectWorkspace(JSON.stringify({workspace,...(projectId?{projectId}:{}),create}),cb)),
  retireProject:(projectId:string,revision:string)=>invoke<Retired>(cb=>native.retireProject(JSON.stringify({projectId,revision,confirm:'retire-registration-only'}),cb)).then(value=>{forgetProject(projectId);return value;}),
  createRehearsal:(reset=false)=>invoke<ProjectDetails>(cb=>native.createRehearsal(JSON.stringify({reset}),cb)).then(value=>{forgetProject();return value;}),
  findTask:(task:string)=>invoke<{task:Task|null}>(cb=>native.findTask(task,cb)),
  cancel:(scheduleId:string)=>invoke<{state:string;detail:string}>(cb=>native.cancelTask(JSON.stringify({scheduleId}),cb)),
  projects:()=>{forgetProject();return invoke<Project[]>(cb=>native.listProjects(cb));},
  tasks:(project='',offset=0,limit=100)=>invoke<{items:Task[];total:number}>(cb=>native.listTasks(project,offset,limit,cb)),
  task:(id:string,messageOffset=0,eventOffset=0)=>invoke<Run>(cb=>native.loadTask(id,messageOffset,eventOffset,cb)),
  evidence:(id:string,kind:string,artifact='',offset=0,limit=300)=>invoke<Evidence>(cb=>native.loadEvidence(id,kind,artifact,offset,limit,cb)),
};
