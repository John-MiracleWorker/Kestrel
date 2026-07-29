# Kestrel browser-validation image

This image supplies the `/opt/kestrel/browser-validate` contract consumed by
`browser.validate`. It is based on an immutable multi-architecture Playwright
image and pins the matching Playwright and axe-core packages.

Build and qualify it locally:

```bash
docker build -t kestrel-browser-validation:local containers/browser-validation
docker run --rm \
  --network=none \
  --read-only \
  --cap-drop=ALL \
  --security-opt=no-new-privileges:true \
  --pids-limit=64 \
  --memory=1g \
  --cpus=1 \
  --ipc=none \
  --user=65534:65534 \
  --tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m \
  kestrel-browser-validation:local \
  /opt/kestrel/browser-validate --self-test
```

Push the qualified image to a trusted registry and configure Kestrel with the
resulting `name@sha256:<digest>` reference. A mutable tag is never accepted at
runtime. Project source is mounted by Kestrel as a private, read-only snapshot;
the project start command and browser run inside the same networkless
container. Any non-local request must match an exact deterministic fixture.
