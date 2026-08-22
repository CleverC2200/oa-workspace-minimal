"""OA finance preaudit deep module."""

__all__ = ["FlowEngine"]


def __getattr__(name: str) -> object:
    if name != "FlowEngine":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from .flow_engine import FlowEngine

    globals()[name] = FlowEngine
    return FlowEngine
