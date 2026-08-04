"""Package-aware entry point for the frozen Kestrel desktop sidecar."""

from __future__ import annotations

import sys
from collections.abc import Sequence

PROVIDER_HTTP_WORKER_ARGUMENT = "--kestrel-provider-http-worker-v1"


def _run_desktop_sidecar(arguments: list[str]) -> int:
    from nested_memvid_agent.desktop_sidecar import main

    main(arguments)
    return 0


def _run_provider_http_worker() -> int:
    from nested_memvid_agent.llm.provider_http_worker import main

    return main()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == [PROVIDER_HTTP_WORKER_ARGUMENT]:
        return _run_provider_http_worker()
    if PROVIDER_HTTP_WORKER_ARGUMENT in arguments:
        raise ValueError("unsupported frozen sidecar arguments")
    return _run_desktop_sidecar(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
