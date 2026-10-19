import type {Run,Task} from './bridge';

export function shouldPollRun(id:string,run:Run|null,firstPage:Task[],pageReadPending=false,force=false):boolean{
  const active=(task:Task)=>task.id===id&&['running','verifying','reviewing','repairing'].includes(task.state);
  return Boolean(id&&!pageReadPending&&(force||(run&&active(run.task))||firstPage.some(active)));
}

// Resolve the persisted opaque ID directly, independent of list pagination.
// A missing/corrupt selection remains selected and produces a visible error.
export async function restoreSelection(items:Task[],id:string,load:(id:string)=>Promise<Run>):Promise<{items:Task[];id:string;error:string}>{
  if(!id)return {items,id:items.find(t=>t.outcome.passed)?.id||items[0]?.id||'',error:''};
  if(items.some(t=>t.id===id))return {items,id,error:''};
  try{const run=await load(id);if(run.taskId!==id||run.task.id!==id)throw Error('恢复任务 ID 不一致');return {items:[...items,run.task],id,error:''};}
  catch(error){return {items,id,error:`原先选择的任务无法恢复：${String(error)}。已保留选择，请刷新或手动选择其他任务。`};}
}

export const MESSAGE_PAGE_SIZE=100;
export const EVENT_PAGE_SIZE=200;
export function tailOffset(total:number,size:number):number{
  return Math.floor(Math.max(0,total-1)/size)*size;
}

// A bounded second read follows the newest complete page. If a writer crosses
// another page boundary meanwhile, the next refresh catches it without a loop.
export async function readTimelinePage(
  load:(id:string,m:number,e:number)=>Promise<Run>,id:string,m:number,e:number,
  follow:boolean,current:()=>boolean=()=>true,
):Promise<Run>{
  const page=await load(id,m,e);
  if(!follow||!current())return page;
  const message=tailOffset(page.messageTotal,MESSAGE_PAGE_SIZE);
  const event=tailOffset(page.eventTotal,EVENT_PAGE_SIZE);
  return message===page.messageOffset&&event===page.eventOffset?page:load(id,message,event);
}
