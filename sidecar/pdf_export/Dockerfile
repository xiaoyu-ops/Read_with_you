FROM awwaawwa/pdfmathtranslate-next@sha256:c737d5342c9220a56026733f3a42182581bb4d8e5052b133e3326babffea109a

RUN groupadd --gid 10001 pdfexport \
    && useradd --uid 10001 --gid 10001 --home-dir /work/home --no-create-home --shell /usr/sbin/nologin pdfexport \
    && mkdir -p /opt/pdf-export-cache \
    && cp -a /root/.cache/. /opt/pdf-export-cache/ \
    && chown -R 10001:10001 /opt/pdf-export-cache \
    && chmod -R u=rX,g=rX,o= /opt/pdf-export-cache

COPY --chown=10001:10001 sidecar/pdf_export/ /pet-sidecar/

USER 10001:10001
