import asyncio
import logging
import os
import yaml
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from agent.proactive import Signal, SignalSource, ProactiveEngine

logger = logging.getLogger("brain.agent.core.fs_watcher")

class KestrelFSEventHandler(FileSystemEventHandler):
    def __init__(self, engine: ProactiveEngine):
        super().__init__()
        self.engine = engine
        
        # We need the asyncio loop from the main thread to enqueue signals
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def _emit(self, event_type: str, event):
        if event.is_directory:
            return

        path = event.src_path

        # Ignore directory-based noise
        noise_dirs = ['.git', '__pycache__', 'node_modules', '.idea', '.vscode']
        if any(f"/{x}/" in path or path.endswith(f"/{x}") for x in noise_dirs):
            return

        # Ignore file-based noise (e.g. macOS metadata, compiled artifacts, editor swaps)
        basename = os.path.basename(path)
        noise_files = {'.DS_Store', 'Thumbs.db', 'desktop.ini'}
        noise_suffixes = ('.pyc', '.pyo', '.swp', '.swo', '.tmp')
        if basename in noise_files or basename.endswith(noise_suffixes):
            return

        signal = Signal(
            source=SignalSource.SYSTEM,
            source_id="fs_watcher",
            title=f"File {event_type}",
            body=f"File {path} was {event_type}d.",
            severity="info",
            metadata={"path": path, "event_type": event_type}
        )
        
        # Enqueue to engine safely from thread
        if self.engine and self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self.engine.process_signal(signal), self._loop
            )
        else:
            logger.debug(f"FS signal dropped (no event loop): {event_type} {path}")

    def on_modified(self, event):
        self._emit("modified", event)

    def on_created(self, event):
        self._emit("created", event)

    def on_deleted(self, event):
        self._emit("deleted", event)


class KestrelFSWatcher:
    """
    Observer component that monitors configured paths on the local filesystem
    and emits proactive signals.
    """
    def __init__(self, engine: ProactiveEngine = None):
        self.engine = engine
        self.observer = Observer()
        self.handler = KestrelFSEventHandler(engine)
        self.watchlist_file = Path(os.path.expanduser("~/.kestrel/watchlist/paths.yml"))
        self._watched_paths = set()
        self._running = False

    def load_watchlist(self) -> set[str]:
        paths = set()
        if self.watchlist_file.exists():
            try:
                with open(self.watchlist_file, 'r') as f:
                    data = yaml.safe_load(f)
                    if data and isinstance(data, dict):
                        raw_paths = data.get("paths", [])
                        for p in raw_paths:
                            expanded = os.path.expanduser(p)
                            if os.path.exists(expanded):
                                paths.add(expanded)
            except Exception as e:
                logger.error(f"Error loading watchlist: {e}")
                
        # Always watch kestrel workspaces and context
        home_dir = os.path.expanduser("~/.kestrel")
        
        ensure_dirs = [
             os.path.join(home_dir, "memory"),
             os.path.join(home_dir) # Just watch the whole ~/.kestrel to get WORKSPACE.md etc
        ]
        
        for d in ensure_dirs:
            if os.path.exists(d):
                paths.add(d)
            
        return paths

    def start(self):
        if self._running:
            return
            
        paths = self.load_watchlist()
        
        # Deduplicate: skip any path that is a subdirectory of an already-queued
        # path (watchdog recursive=True covers all descendants automatically).
        # Processing shorter paths first guarantees parents are added before children.
        to_watch = set()
        for p in sorted(paths, key=len):
            if not any(p.startswith(w + os.sep) for w in to_watch):
                to_watch.add(p)
        
        if not to_watch:
            logger.info("No valid paths in watchlist. FS Watcher idle.")
            return

        for path in to_watch:
            try:
                self.observer.schedule(self.handler, path, recursive=True)
                self._watched_paths.add(path)
            except Exception as e:
                logger.warning(f"Failed to watch path {path}: {e}")
            
        if self._watched_paths:
            self.observer.start()
            self._running = True
            logger.info(f"Kestrel FS Watcher started, watching {len(self._watched_paths)} root directories.")

    def stop(self):
        if self._running:
            self.observer.stop()
            self.observer.join()
            self._running = False
            logger.info("Kestrel FS Watcher stopped.")
