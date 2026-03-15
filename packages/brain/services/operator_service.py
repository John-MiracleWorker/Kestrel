from __future__ import annotations

from .base import BaseServicerMixin
from .operator_service_memory import OperatorMemoryMixin
from .operator_service_queries import OperatorQueryMixin
from .operator_service_tasks import OperatorTaskMixin


class OperatorServicerMixin(
    OperatorTaskMixin,
    OperatorMemoryMixin,
    OperatorQueryMixin,
    BaseServicerMixin,
):
    pass
