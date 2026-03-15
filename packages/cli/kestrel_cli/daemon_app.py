from __future__ import annotations

from . import daemon_tasks as _daemon_tasks

globals().update({name: value for name, value in vars(_daemon_tasks).items() if not name.startswith("__")})

class KestrelDaemon(
    KestrelDaemonTelegramIOMixin,
    KestrelDaemonTaskMixin,
    KestrelDaemonTelegramStateMixin,
    KestrelDaemonCore,
):
    pass

def datetime_now_hhmm() -> str:
    return time.strftime("%H:%M", time.localtime())


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def main() -> None:
    daemon = KestrelDaemon()
    await daemon.run()


if __name__ == "__main__":
    asyncio.run(main())
