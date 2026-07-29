FROM mcr.microsoft.com/playwright:v1.62.0-noble@sha256:baed2032d533817f3dbe6425de795788430ba345e819a1201337009ba17c9d07

ENV NODE_ENV=production \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

WORKDIR /opt/kestrel
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \
    && npm cache clean --force \
    && chmod -R a=rX /opt/kestrel

COPY browser-validate.mjs /opt/kestrel/browser-validate
RUN chmod 0555 /opt/kestrel/browser-validate

WORKDIR /extension

LABEL org.opencontainers.image.title="Kestrel contained browser validation" \
      org.opencontainers.image.description="Networkless Playwright, DOM, screenshot, and accessibility evidence runner" \
      org.opencontainers.image.licenses="Apache-2.0"

CMD ["/opt/kestrel/browser-validate", "--self-test"]
