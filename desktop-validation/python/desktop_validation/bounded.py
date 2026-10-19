class BoundedLog(list):
    """JSON-serializable diagnostic ring. Never retains unbounded messages."""
    def __init__(self, maxlen):
        super().__init__();self.maxlen=maxlen
    def append(self, value):
        super().append(value)
        if len(self)>self.maxlen:del self[:-self.maxlen]
