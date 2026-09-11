# AI HOT independent recovery scheduler

This TypeScript subproject is the Phase G1 Cloudflare Workers control-plane
scheduler. It has no AI HOT retrieval, candidate, snapshot-data, Actions
dispatch, consumer, Pages, or Gmail logic. Its only mutation is an authenticated
GitHub Contents API update of:

`Ninaix0217/aihot-data-bridge:aihot-scheduler-control/.aihot-control/trigger.json`

The module Worker derives the report date from the nominal Cloudflare
`controller.scheduledTime` in `Asia/Shanghai`. Actual execution time is used
only for `requested_at` and schedule-lag observability. A deterministic request
ID makes a repeated delivery of the same nominal occurrence a no-op when that
request is already current on the control branch.

## Safe committed state

`wrangler.jsonc` contains `triggers.crons: []`. Phase G1 does not deploy or
enable the proposed `25 5 * * *` production cron.

## Local validation

```text
npm ci
npm run typecheck
npm test
```

The test suite uses Cloudflare's official Workers Vitest integration and
`createScheduledController()` to supply an explicit scheduled time. No public
HTTP handler is exposed.

## Deployment credential gate

Create and install a GitHub App only on `Ninaix0217/aihot-data-bridge`, with
repository Contents permission set to Read and write and all unrelated
permissions disabled. Do not enable a webhook receiver.

Configure these Worker bindings before any real deployment:

- `GITHUB_APP_CLIENT_ID` — ordinary configuration
- `GITHUB_INSTALLATION_ID` — ordinary configuration
- `GITHUB_APP_PRIVATE_KEY` — Cloudflare secret binding only

Put the private key directly into Cloudflare with:

```text
npx wrangler secret put GITHUB_APP_PRIVATE_KEY
```

Never paste or commit the PEM, a Cloudflare API token, an App JWT, or an
installation token. Installation tokens are freshly requested for each
invocation and restricted to this repository with `contents: write`.

Probe deployments may set `DEPLOYMENT_MODE=probe` and an explicit
`PROBE_TARGET_REPORT_DATE`. Production mode rejects this override. A temporary
probe cron must be removed after evidence is collected; recurring recovery is
not part of Phase G1.
