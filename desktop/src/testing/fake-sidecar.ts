import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import type {
  RetainedSidecarChild,
  SidecarSpawnRequest
} from "../main/sidecar-supervisor";

export class FakeSidecarChild
  extends EventEmitter
  implements RetainedSidecarChild
{
  readonly stdout = new PassThrough();
  readonly stderr = new PassThrough();
  exitCode: number | null = null;
  signalCode: NodeJS.Signals | null = null;
  readonly killSignals: Array<NodeJS.Signals | number | undefined> = [];

  constructor(readonly pid: number) {
    super();
  }

  kill(signal?: NodeJS.Signals | number): boolean {
    this.killSignals.push(signal);
    return true;
  }

  exit(code: number, signal: NodeJS.Signals | null = null): void {
    if (this.exitCode !== null || this.signalCode !== null) {
      return;
    }
    this.exitCode = code;
    this.signalCode = signal;
    this.emit("exit", code, signal);
  }
}

export class FakeSidecarSpawner {
  readonly requests: SidecarSpawnRequest[] = [];
  readonly children: FakeSidecarChild[] = [];

  spawn = (request: SidecarSpawnRequest): FakeSidecarChild => {
    this.requests.push(request);
    const child = new FakeSidecarChild(9100 + this.children.length);
    this.children.push(child);
    return child;
  };
}
