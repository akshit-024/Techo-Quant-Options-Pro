"""Fail-closed execution errors with stable API codes."""


class ExecutionError(RuntimeError):
    code = "EXECUTION_ERROR"


class ExecutionBlockedError(ExecutionError):
    code = "EXECUTION_BLOCKED"


class PlanValidationError(ExecutionError):
    code = "INVALID_PLAN"


class DuplicateSignalError(ExecutionError):
    code = "DUPLICATE_SIGNAL"


class DuplicatePositionError(ExecutionError):
    code = "DUPLICATE_POSITION"


class ApprovalError(ExecutionError):
    code = "INVALID_APPROVAL"


class KillSwitchError(ExecutionError):
    code = "KILL_SWITCH_ACTIVE"


class LiveExecutionDisabledError(ExecutionError):
    code = "LIVE_EXECUTION_DISABLED"


class ExecutionUncertainError(ExecutionError):
    code = "EXECUTION_UNCERTAIN"

