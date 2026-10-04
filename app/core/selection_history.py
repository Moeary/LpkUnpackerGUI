"""Bounded gesture history, independent of model edits and dirty state."""


class SelectionHistory:
    def __init__(self, limit=100):
        self.limit = limit
        self.reset()

    def reset(self, state=((), None)):
        self.current = state
        self.past = []
        self.future = []

    def record(self, state):
        if state == self.current:
            return
        self.past.append(self.current)
        self.past = self.past[-self.limit:]
        self.current = state
        self.future.clear()

    def undo(self):
        if not self.past:
            return None
        self.future.append(self.current)
        self.current = self.past.pop()
        return self.current

    def redo(self):
        if not self.future:
            return None
        self.past.append(self.current)
        self.current = self.future.pop()
        return self.current
