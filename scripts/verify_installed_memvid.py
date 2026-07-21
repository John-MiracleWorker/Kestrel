from __future__ import annotations

import argparse
import json
from pathlib import Path


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exercise the installed Kestrel wheel against a real Memvid v2 container."
    )
    parser.add_argument("--memory-dir", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    args = parser.parse_args()

    import memvid_sdk

    import nested_memvid_agent
    from nested_memvid_agent.backends.memvid_backend import MemvidBackend
    from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord

    package_path = Path(nested_memvid_agent.__file__).resolve(strict=True)
    source_package = (args.source_root / "src" / "nested_memvid_agent").resolve()
    if _is_relative_to(package_path, source_package):
        raise RuntimeError(f"source checkout shadowed installed wheel: {package_path}")
    if not callable(memvid_sdk.create) or not callable(memvid_sdk.use):
        raise RuntimeError("installed memvid_sdk does not expose the Memvid v2 API")

    args.memory_dir.mkdir(parents=True, exist_ok=False)
    container = args.memory_dir / "episodic.mv2"
    backend = MemvidBackend(path=container, layer=MemoryLayer.EPISODIC)
    backend.open()
    try:
        record_id = backend.put(
            MemoryRecord(
                id="installed-wheel-memvid-smoke",
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.EVENT,
                title="Installed wheel Memvid smoke",
                content="The downloaded Kestrel wheel can persist and retrieve Memvid v2 data.",
                confidence=1.0,
            )
        )
        backend.seal()
        if not backend.verify():
            raise RuntimeError("Memvid verification failed before reopen")
    finally:
        backend.close()

    reopened = MemvidBackend(path=container, layer=MemoryLayer.EPISODIC)
    reopened.open()
    try:
        if not reopened.verify():
            raise RuntimeError("Memvid verification failed after reopen")
        hits = reopened.find("downloaded Kestrel wheel Memvid", k=5)
        if not any(hit.record.id == record_id for hit in hits):
            raise RuntimeError("Memvid record was not retrieved after reopen")
    finally:
        reopened.close()

    containers = sorted(path.name for path in args.memory_dir.glob("*.mv2"))
    if containers != ["episodic.mv2"]:
        raise RuntimeError(f"unexpected Memvid v2 container layout: {containers}")
    print(
        json.dumps(
            {
                "container": str(container),
                "package_path": str(package_path),
                "record_id": record_id,
                "verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
