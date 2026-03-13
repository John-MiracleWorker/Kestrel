import fs from 'fs';
import os from 'os';
import path from 'path';

function expandHome(inputPath: string): string {
    if (!inputPath.startsWith('~')) {
        return inputPath;
    }
    return path.join(os.homedir(), inputPath.slice(1).replace(/^[\\/]+/, ''));
}

export function getKestrelHome(): string {
    const configured = process.env.KESTREL_HOME?.trim();
    return path.resolve(expandHome(configured || path.join('~', '.kestrel')));
}

export function ensureKestrelStateDir(): string {
    const stateDir = path.join(getKestrelHome(), 'state');
    fs.mkdirSync(stateDir, { recursive: true });
    return stateDir;
}

export function getGatewayStateFile(name: string): string {
    return path.join(ensureKestrelStateDir(), name);
}

export function getMediaDir(): string {
    const explicit = process.env.MEDIA_DIR?.trim();
    const mediaDir = explicit
        ? path.resolve(expandHome(explicit))
        : path.join(getKestrelHome(), 'artifacts', 'media');
    fs.mkdirSync(mediaDir, { recursive: true });
    return mediaDir;
}
