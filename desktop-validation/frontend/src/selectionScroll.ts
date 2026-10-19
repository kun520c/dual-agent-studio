import {useEffect, useRef} from 'react'

/** Qt's clipped WebEngine viewport does not scroll nested readers while selecting.
 * Keep native selection and mouse handling; only fill the missing edge scroll.
 */
export function useSelectionScroll(onReading:()=>void) {
 const notify=useRef(onReading);notify.current=onReading
 useEffect(()=>{
  if(!/QtWebEngine/i.test(navigator.userAgent))return
  let reader:HTMLElement|null=null,frame=0,x=0,y=0,last=0,previousTop=0
  const stop=()=>{reader=null;cancelAnimationFrame(frame);frame=0;last=0}
  const tick=(now:number)=>{
   if(!reader||!reader.isConnected){stop();return}
   const selection=window.getSelection(),rect=reader.getBoundingClientRect()
   const elapsed=last?Math.min(40,now-last):16;last=now
   if(selection&&!selection.isCollapsed&&reader.contains(selection.anchorNode)){
    notify.current()
    const edge=32
    const distance=y>rect.bottom-edge?Math.min(1,(y-rect.bottom+edge)/edge):y<rect.top+edge?-Math.min(1,(rect.top+edge-y)/edge):0
    // If Chromium is already scrolling, leave that frame entirely to it.
    const nativeMoved=Math.abs(reader.scrollTop-previousTop)>.5
    if(distance&&!nativeMoved){
     const before=reader.scrollTop
     reader.scrollTop+=(70+Math.abs(distance)*350)*Math.sign(distance)*elapsed/1000
     if(reader.scrollTop!==before){
      const point=document.caretRangeFromPoint(Math.max(rect.left+3,Math.min(rect.right-12,x)),Math.max(rect.top+3,Math.min(rect.bottom-3,y)))
      if(point&&reader.contains(point.startContainer))selection.extend(point.startContainer,point.startOffset)
     }
    }
   }
   previousTop=reader.scrollTop;frame=requestAnimationFrame(tick)
  }
  const down=(event:MouseEvent)=>{
   stop();if(event.button!==0||!(event.target instanceof Element)||event.target.closest('textarea,input,select,[role=separator]'))return
   let node=event.target.closest<HTMLElement>('.task-timeline,.evidence-content,.session-content,.code-diff')
   if(!node)return
   // Respect an explicitly scrollable inner evidence block when it is present.
   for(let child=event.target as HTMLElement;child&&child!==node;child=child.parentElement!){
    if(child.scrollHeight>child.clientHeight+2&&/auto|scroll/.test(getComputedStyle(child).overflowY)){node=child;break}
   }
   reader=node;x=event.clientX;y=event.clientY;previousTop=node.scrollTop
   frame=requestAnimationFrame(tick)
  }
  const move=(event:MouseEvent)=>{if(!reader)return;if(!(event.buttons&1)){stop();return}x=event.clientX;y=event.clientY}
  document.addEventListener('mousedown',down,true);window.addEventListener('mousemove',move,true)
  window.addEventListener('mouseup',stop,true);window.addEventListener('blur',stop);document.addEventListener('visibilitychange',stop)
  return()=>{stop();document.removeEventListener('mousedown',down,true);window.removeEventListener('mousemove',move,true);window.removeEventListener('mouseup',stop,true);window.removeEventListener('blur',stop);document.removeEventListener('visibilitychange',stop)}
 },[])
}
