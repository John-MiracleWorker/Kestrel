from collections import deque


class Widget:
    def render(self) -> str:
        return helper("ready")


def helper(value: str) -> str:
    queue = deque([value])
    return queue.popleft()
