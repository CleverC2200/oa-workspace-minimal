"""OA finance preaudit orchestration."""

__all__ = ["collect_case"]


def __getattr__(name: str) -> object:
    if name != "collect_case":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from .case_bundle import collect_case

    globals()[name] = collect_case
    return collect_case
