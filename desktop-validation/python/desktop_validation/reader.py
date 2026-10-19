"""One read worker, at most eight pending reads. No execution capability."""
from .read_pool import BoundedReadPool
from collections import deque
import json
import threading
from PySide6.QtCore import QObject, Signal, Qt

def failure(message):
    return json.dumps({'ok':False,'error':message},ensure_ascii=False)

class ReadDispatcher(QObject):
    completed = Signal(object)

    def __init__(self, bridge, deliver, parent=None):
        super().__init__(parent)
        self.bridge = bridge; self.deliver = deliver
        self.pool = BoundedReadPool(capacity=8)
        self.pending = {}; self.latest = {}; self.epoch = 0; self.serial = 0
        self.closed = False; self.peak_pending = 0; self.worker_threads = deque(maxlen=20)
        self.selected_task = None
        self._completion_lock = threading.RLock()
        self.completed.connect(self._finished,Qt.ConnectionType.QueuedConnection)

    def invalidate(self):
        self.epoch += 1
        self.selected_task = None
        self.latest.clear()
        for future, *_ in list(self.pending.values()): future.cancel()

    def submit(self, packet, method):
        if self.closed: return
        args = packet.get('args', [])
        # Independent evidence kinds can coexist; newer pages replace older pages.
        key = (method, args[0]) if method=='loadTask' else (method, packet['id']) if method in {'loadEvidence','submitTask','cancelTask','validateTask','preflightTask','getTaskDefaults','getProject','saveProject','setProjectEnabled','setDefaultProject','findTask','inspectAccess','inspectWorkspace','retireProject','createRehearsal'} else (method,)
        if method=='loadTask' and args[0]!=self.selected_task:
            self.selected_task=args[0]
            for serial, (future, old_key, epoch, old_packet) in list(self.pending.items()):
                if old_key[0] in {'loadTask','loadEvidence'} and old_packet.get('args',[None])[0]!=args[0] and future.cancel():
                    self.pending.pop(serial,None)
                    if self.latest.get(old_key)==serial:self.latest.pop(old_key,None)
                    if epoch==self.epoch:self.deliver(old_packet,failure('读取已被新选择替代'))
        for serial, (future, old_key, epoch, old_packet) in list(self.pending.items()):
            if old_key==key and future.cancel():
                self.pending.pop(serial,None)
                if self.latest.get(old_key)==serial:self.latest.pop(old_key,None)
                if epoch==self.epoch:self.deliver(old_packet,failure('读取已被新选择替代'))
        if len(self.pending)>=8:
            self.deliver(packet,failure('只读队列已满，请稍后重试'));return
        self.serial += 1; serial=self.serial; epoch=self.epoch
        self.latest[key]=serial
        def work():
            self.worker_threads.append(threading.get_ident())
            return getattr(self.bridge,method)(*args)
        future=self.pool.submit(work)
        self.pending[serial]=(future,key,epoch,packet)
        self.peak_pending=max(self.peak_pending,len(self.pending))
        def done(completed):
            try: value=completed.result()
            except Exception as error: value=failure('读取取消' if completed.cancelled() else str(error)[:300])
            self._post_completion((serial,value))
        future.add_done_callback(done)

    def _post_completion(self, result):
        # Serialize the closed check and queued emission with shutdown. Once
        # shutdown returns, a worker cannot touch the Qt signal again.
        with self._completion_lock:
            if self.closed:
                return
            try:
                self.completed.emit(result)
            except RuntimeError:
                self.closed = True

    def _finished(self, result):
        serial,value=result; entry=self.pending.pop(serial,None)
        if entry is None or self.closed:return
        _,key,epoch,packet=entry
        if epoch!=self.epoch:return
        if self.latest.get(key)!=serial:value=failure('读取已被新选择替代')
        else:self.latest.pop(key,None)
        if key[0] in {'loadTask','loadEvidence'} and self.selected_task is not None and packet['args'][0]!=self.selected_task:value=failure('读取已被新选择替代')
        self.deliver(packet,value)

    def shutdown(self):
        with self._completion_lock:
            if self.closed:return
            self.closed=True;self.invalidate()
            self.pool.shutdown(wait=False,cancel_futures=True)
            # Running work completes in the pool, without retaining the page's
            # Future/packet registrations or emitting after this close gate.
            self.pending.clear();self.latest.clear()
