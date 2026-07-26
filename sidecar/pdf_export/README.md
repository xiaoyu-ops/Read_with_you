# Pet PDF export sidecar

This optional service converts an uploaded English PDF into a monolingual,
watermarked Simplified Chinese PDF. It is isolated behind the Compose
`pdf-export` profile and has no host port.

The sidecar receives only an internal bearer token and an internal OpenAI-like
base URL. Pet's backend resolves the real translation model and provider
through LiteLLM; upstream API keys are never passed into this container.

Start only after setting `PEINIDU_PDF_EXPORT_INTERNAL_TOKEN`:

```bash
docker compose --profile pdf-export up -d pdf-export
```

The completed T13 deployment target runs this optional export service on the
developer MacBook, not on the 3.8 GiB VPS. With the source backend already
listening on `127.0.0.1:8000`, put the shared internal token in the ignored
0600 root `.env`, set the backend sidecar URL to `http://127.0.0.1:8091`, and
start the fixed, loopback-only local container:

```bash
chmod 600 .env
./scripts/start_local_pdf_export_sidecar.sh
```

The script rebuilds the disclosed wrapper over the pinned upstream image,
uses a 4 GiB / 2 CPU limit and a tmpfs work area, and never passes Provider
credentials into the sidecar. The VPS must keep this feature disabled unless a
future deployment independently satisfies the 8 GiB production safety gate.

Run the read-only runtime probe:

```bash
python scripts/verify_pdf_export_sidecar.py --runtime
```

See [THIRD_PARTY.md](./THIRD_PARTY.md) for the pinned version, license, and
corresponding source link.
