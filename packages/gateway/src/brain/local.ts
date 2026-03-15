import path from 'path';
import { getKestrelHome } from '../utils/paths';
import { DaemonControlTransport, type ControlTransport } from './local-transport';
import { LocalGatewayStateStore } from './local-store';

export * from './local-transport';
export * from './local-types';
export * from './local-store';

let sharedLocalStore: LocalGatewayStateStore | null = null;

export function getLocalGatewayStateStore(): LocalGatewayStateStore {
    if (!sharedLocalStore) {
        sharedLocalStore = new LocalGatewayStateStore();
    }
    return sharedLocalStore;
}

let sharedControlTransport: ControlTransport | null = null;

export function getLocalControlTransport(): ControlTransport {
    if (!sharedControlTransport) {
        sharedControlTransport = new DaemonControlTransport();
    }
    return sharedControlTransport;
}

export function daemonTransportEndpoint(): { kind: 'unix' | 'tcp'; address: string } {
    const defaultControlPort = parseInt(process.env.KESTREL_CONTROL_PORT || '8749', 10);
    const defaultControlHost = process.env.KESTREL_CONTROL_HOST || '127.0.0.1';
    if (process.platform === 'win32') {
        return {
            kind: 'tcp',
            address: `${defaultControlHost}:${defaultControlPort}`,
        };
    }
    return {
        kind: 'unix',
        address: path.join(getKestrelHome(), 'run', 'control.sock'),
    };
}
