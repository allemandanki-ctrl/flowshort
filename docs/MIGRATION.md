# Migration: Postiz -> Ayrshare + Zernio

FlowShort no longer talks to a self-hosted Postiz instance. It now calls
Ayrshare (Snapchat) and Zernio (TikTok/YouTube/Instagram/Facebook) directly —
both handle the actual publishing AND server-side scheduling themselves, so
FlowShort just generates videos, uploads them, and fires scheduled posts.
No daemon, no VPS.

## 1. Get your API keys

| Env var | Where to find it |
|---|---|
| `AYRSHARE_API_KEY` | Ayrshare dashboard -> API Key (top of the dashboard after login) |
| `ZERNIO_A_API_KEY` | Zernio **compte frère** dashboard -> API Keys -> create key |
| `ZERNIO_B_API_KEY` | Zernio **compte principal** dashboard -> API Keys -> create key |

Add all three to `.env` (copy `.env.example` if you don't have one yet).

## 2. Get Zernio accountIds

Zernio's `accountId` is the specific connected social channel (tiktok,
youtube, instagram, facebook) inside a workspace — NOT the API key itself.
Once each Zernio account has TikTok/YouTube (account A) or Instagram/Facebook
(account B) connected in their dashboard, list them:

```
python schedule_batch.py --list-accounts zernio_a
python schedule_batch.py --list-accounts zernio_b
```

Copy each printed `accountId` into `profiles.yaml` under the matching
platform's `account_id` field.

Ayrshare has no equivalent step — its API key already maps to whatever
social accounts you've linked in the Ayrshare dashboard.

## 3. Run the dry-run

Confirm the plan (platform, provider, datetime) without publishing anything:

```
python schedule_batch.py --file schedule.csv --dry-run
```

or, for a single real test post per provider (asks for confirmation, fires a
REAL scheduled post ~10 min out):

```
python test_publishers.py snapchat
python test_publishers.py tiktok
python test_publishers.py instagram
```

## 4. Judgment calls made during this migration

- **Zernio `accountId` placement**: the user's spec for `profiles.yaml` didn't
  include a per-platform accountId field. Added `account_id: "..."` under
  each Zernio-backed platform entry (empty placeholder in `profiles.yaml`,
  `REPLACE_...` in `profiles.example.yaml`), discoverable via
  `schedule_batch.py --list-accounts <provider>` — mirrors how the old
  Postiz setup pointed at `--list --profile`.
- **`app.py`'s `mode` field renamed** from `"postiz"` to `"schedule"` (not
  `"auto"`) to avoid colliding with the pre-existing, unrelated
  `format_mode="auto"` concept (the 1/3-format1/1/3-plain/1/3-hook video mix).
  Both now coexist without ambiguity: `mode` picks the publish path
  (manual/schedule), `format_mode` picks the video format mix.
- **Aggregated per-video result**: a single video can now fan out to up to 3
  different provider accounts (ayrshare_main for snapchat, zernio_a for
  tiktok+youtube, zernio_b for instagram+facebook). `modules/publish_config.py`
  wraps the per-platform `PublishResult`s into one `AggregateResult` per video
  (status = created/partial/error) so `app.py`'s existing per-video logging
  loop needed only a small diff instead of a rewrite.
- **Dedup log kept**, renamed `state/postiz_schedule_log.json` ->
  `state/publish_schedule_log.json` — same idea (skip if this exact
  video+platforms+time combo was already scheduled), now keyed across
  providers instead of Postiz integrations.
- **Ayrshare video upload**: uses the large-media presigned flow
  (`GET /media/uploadUrl` -> `PUT` -> `accessUrl`, valid 30 days) rather than
  the 30MB-capped direct `/media/upload` endpoint, since generated videos can
  exceed that and there's no server to fall back to hosting them.
- **`app.py` has no dry-run toggle in the web UI** — dry-run is only wired
  through `schedule_batch.py --dry-run` (CLI). Adding a UI checkbox + thread
  the flag through `_generate_batch`/`_schedule_post` would be a bigger,
  separate change to the Flask app than this migration's scope.
- Multi-brand support (`modules/users.py`) predates this migration and isn't
  part of the user's original spec, but is real, load-bearing functionality —
  kept intact. Each brand still gets its own `profiles.yaml`; new brands
  scaffold with brand-scoped env var placeholders
  (`AYRSHARE_API_KEY_<BRAND>`, `ZERNIO_API_KEY_<BRAND>`) since a second brand
  would need its own Ayrshare/Zernio accounts, not resetworth's.

## 5. Decommission checklist — ONLY after all `test_publishers.py` runs above succeed for real

- [ ] Confirm at least one real post landed on Snapchat (Ayrshare), TikTok/YouTube (Zernio A), and Instagram/Facebook (Zernio B)
- [ ] Stop the Postiz Docker Compose stack on the Hetzner VPS
- [ ] Cancel/destroy the Hetzner VPS
- [ ] Remove the Cloudflare Tunnel pointing at the old `app.publishloop.uk`
- [ ] (Optional) Deregister/delete the `publishloop.uk` domain if nothing else uses it
