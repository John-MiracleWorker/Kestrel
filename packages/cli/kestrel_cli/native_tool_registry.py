from __future__ import annotations

from . import native_tool_registry_custom as _native_tool_registry_custom

globals().update({name: value for name, value in vars(_native_tool_registry_custom).items() if not name.startswith("__")})

class NativeToolRegistry(
    NativeToolRegistryCustomMixin,
    NativeToolRegistryHandlersMixin,
    NativeToolRegistryCore,
):
    pass
