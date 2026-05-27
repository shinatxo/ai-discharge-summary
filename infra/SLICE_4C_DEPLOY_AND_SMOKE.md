# Slice 4c — deploy + smoke test

> S3 (private, OAC) + CloudFront single-distribution / two-origin in front of
> the slice-4b HTTP API. Default `*.cloudfront.net` hostname (no custom
> domain yet). After this slice, the SPA, the API, and every viewer header
> are served from one origin → no CORS, one TLS cert, one URL.

Run every command from `ai-discharge-summary/` unless stated otherwise. All
of this runs on your machine (the AWS CLI is not available from the sandbox).

---

## 0. Pre-flight

You should have, locally:

- `aws` CLI v2, logged in as the `Shina` user, default region `eu-west-2`.
- `node` + `npm` (already used for slice 4b).
- `ui-spa/.env.local` filled in from slice 4b (we'll re-use the same values).
- A clean working tree (`git status` reports nothing dirty in `infra/` or
  `ui-spa/` apart from the slice-4c files we're about to add).

Verify the audit stack is healthy:

```bash
aws cloudformation describe-stacks \
  --stack-name discharge-audit \
  --region eu-west-2 \
  --query 'Stacks[0].StackStatus' --output text
```

Expect `CREATE_COMPLETE` or `UPDATE_COMPLETE`.

---

## 1. Redeploy `discharge-audit` to publish the new Exports

**What:** the audit-stack template gained three Output changes (`HttpApiId`
now exports; `HttpApiDomain` is brand-new and exports). Nothing else changed.

**How:**

```bash
cd infra
aws cloudformation package \
  --template-file template.yaml \
  --s3-bucket aws-sam-cli-managed-default-samclisourcebucket-xufyv0yd8kla \
  --output-template-file packaged.yaml

aws cloudformation deploy \
  --template-file packaged.yaml \
  --stack-name discharge-audit \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides Environment=demo \
  --region eu-west-2
```

**Why:** even though no resources change, CFN still walks the template and
materialises the Export entries. Without this step, the slice-4c
`Fn::ImportValue` calls will fail with `No export named
discharge-audit-HttpApiDomain found`.

Verify the exports are live:

```bash
aws cloudformation list-exports \
  --region eu-west-2 \
  --query "Exports[?starts_with(Name, 'discharge-audit-')].Name" \
  --output table
```

Expect to see `discharge-audit-HttpApiId` and `discharge-audit-HttpApiDomain`.

---

## 2. Deploy `discharge-web`

**What:** brand-new stack — S3 bucket, OAC, security-headers policy,
CloudFront distribution with two origins.

**How:**

```bash
# Still in infra/ — no `package` step needed because there's no Lambda code
# to upload. CloudFront, S3, OAC and the response-headers policy are all
# inline resources.
aws cloudformation deploy \
  --template-file web-template.yaml \
  --stack-name discharge-web \
  --parameter-overrides Environment=demo AuditStackName=discharge-audit \
  --region eu-west-2
```

**Why the first deploy is slow:** the CloudFront distribution itself is
fast to create (~30s in the API), but **propagating the config to every edge
PoP takes 5–15 minutes**. The CFN stack will go to `CREATE_COMPLETE` only
once propagation finishes. This is unavoidable and the whole reason we put
this in a separate stack — you do NOT want every backend iteration paying
this cost.

While you wait, in another terminal:

```bash
watch -n 30 'aws cloudformation describe-stacks \
  --stack-name discharge-web --region eu-west-2 \
  --query "Stacks[0].StackStatus" --output text'
```

When it flips to `CREATE_COMPLETE`, grab the outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name discharge-web --region eu-west-2 \
  --query 'Stacks[0].Outputs' --output table
```

Note down:
- `SpaBucketName` — the S3 bucket you'll sync into.
- `DistributionDomain` — the `xxxxxxxx.cloudfront.net` hostname.
- `DistributionId` — used later for cache invalidations.

---

## 3. Build the SPA for production

**What:** Vite produces a hashed, minified bundle in `ui-spa/dist/` using the
values in `.env.production`.

**How:**

```bash
cd ../ui-spa

# Copy the User Pool + Client id from .env.local into .env.production.
# (We can't symlink because Vite reads the file at build time.)
# If you'd rather, edit .env.production directly.
echo ""                                                               >> .env.production
echo "# Auto-copied from .env.local — same values for prod build."    >> .env.production
grep VITE_USER_POOL_ID         .env.local                             >> .env.production
grep VITE_USER_POOL_CLIENT_ID  .env.local                             >> .env.production

# Sanity-check before building.
cat .env.production

npm install                # idempotent if already done
npm run build              # tsc -b && vite build → ui-spa/dist/
```

**Why:** Vite loads env files in this priority order:
`.env.[mode].local > .env.[mode] > .env.local > .env`.
When `mode = production` (which `vite build` sets), `.env.production` wins
over `.env.local`. The intentional consequence: in dev the SPA hits the full
`*.execute-api.*` URL; in the prod build it uses relative paths. Same source
file, no `if (process.env...)` branches.

After the build, `ui-spa/dist/` should contain:
- `index.html` (a few KB)
- `assets/*-<hash>.js` and `assets/*-<hash>.css`
- maybe `vite.svg` or other static files

Check `index.html` references the hashed assets (no `?dev` or `?import`
query strings — those would indicate a dev-mode build snuck through):

```bash
grep -E 'src=|href=' dist/index.html
```

---

## 4. Sync the SPA to S3

**What:** copy `dist/` into the SPA bucket, deleting any objects that no
longer exist locally.

**How:**

```bash
SPA_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name discharge-web --region eu-west-2 \
  --query "Stacks[0].Outputs[?OutputKey=='SpaBucketName'].OutputValue" \
  --output text)
echo "$SPA_BUCKET"   # sanity-check it's the bucket you expect

aws s3 sync dist/ "s3://${SPA_BUCKET}/" \
  --delete \
  --cache-control "public, max-age=31536000, immutable" \
  --exclude "index.html" \
  --region eu-west-2

# index.html separately, with a very short TTL — it's the entry point and
# must update fast when you re-deploy the SPA.
aws s3 cp dist/index.html "s3://${SPA_BUCKET}/index.html" \
  --cache-control "no-cache, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --region eu-west-2
```

**Why two passes?** Vite hashes asset filenames (`app-a1b2c3.js`), so a new
build = new filenames. Old objects can sit on the long TTL safely;
`index.html` is the only file whose URL is stable across deploys, so it
needs a short TTL or every viewer would keep loading the old bundle. The
canonical pattern: long-cache everything except the bootstrap HTML.

`--delete` removes objects no longer in `dist/` — without it, old bundles
accumulate forever and slowly bloat the bucket. With versioning enabled on
the bucket (we did), deleted objects become *delete markers* rather than
hard-deleted, so accidental wipes are recoverable.

---

## 5. (Optional) Force-invalidate index.html

If you want viewers who hit the edge in the last minute to immediately see
the new SPA without waiting for the no-cache header to clear:

```bash
DIST_ID=$(aws cloudformation describe-stacks \
  --stack-name discharge-web --region eu-west-2 \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" \
  --output text)

aws cloudfront create-invalidation \
  --distribution-id "$DIST_ID" \
  --paths "/index.html"
```

**Why this is mostly unnecessary:** because we set `Cache-Control: no-cache`
on `index.html` itself, CloudFront revalidates every viewer hit anyway.
Invalidation is a hammer reserved for emergencies (e.g. you accidentally
shipped credentials).

CloudFront invalidations: the first 1,000 paths per month are free; after
that, $0.005 per path. Wildcard paths (`/assets/*`) count as one path each.
SAA-C03 trivia: invalidation latency is "a few minutes" not instant.

---

## 6. Smoke test

The CloudFront distribution URL is what the user sees. Open it in a browser:

```
https://<DistributionDomain>/
```

### 6.1 — SPA loads, login works

| Check | Pass criteria |
|---|---|
| HTTPS without warning | viewer cert is the AWS-managed `*.cloudfront.net` wildcard |
| SPA renders the Amplify Authenticator | sign-in form appears |
| Sign in as the slice-4a/4b test user | redirected to the in-app generate form |

### 6.2 — Same-origin requests (open DevTools → Network)

| Check | Pass criteria |
|---|---|
| Submit a job | `POST /generate` appears in Network, **NOT** `POST https://*.execute-api.*` |
| Response status | `202 Accepted` |
| **No `OPTIONS` preflight before the POST** | confirms same-origin — proves the SPA is calling the SAME hostname it was served from |
| Response includes `x-content-type-options: nosniff` and `strict-transport-security` | response-headers policy fires |
| Polling | `GET /generations/{id}` returns `pending` then `complete` |
| Output tabs render | summary, GP letter, patient version all visible |

### 6.3 — Audit + ledger still intact

```bash
USER_SUB=$(aws cognito-idp admin-get-user \
  --user-pool-id <YOUR_POOL_ID> \
  --username <YOUR_TEST_USER_EMAIL> \
  --region eu-west-2 \
  --query "UserAttributes[?Name=='sub'].Value" --output text)

# NOTE: DynamoDB attribute names are case-sensitive. The audit table's
# partition + sort keys are UPPERCASE `PK` / `SK` (see template.yaml
# KeySchema). Using lowercase `pk` fails with `ValidationException:
# Query condition missed key schema element: PK`.
aws dynamodb query \
  --table-name discharge-audit-audit \
  --key-condition-expression "PK = :pk" \
  --expression-attribute-values "{\":pk\":{\"S\":\"USER#${USER_SUB}\"}}" \
  --region eu-west-2 \
  --query "Items[?starts_with(SK.S, 'GEN#')] | [0]" \
  --output json
```

| Check | Pass criteria |
|---|---|
| Audit row exists for the job | `user_sub` matches the JWT `sub` (anti-spoof intact) |
| `status` is `complete` | worker finished cleanly |
| No PHI in the audit row | hashes only — `input_sha256`, `output_sha256.*` |
| WORM ledger picked up the events | `aws s3 ls s3://<LedgerBucket>/ledger/year=2026/.../ --recursive \| tail -5` shows fresh objects since the job started |

### 6.4 — Behaviour evaluation order (sanity)

Open these URLs in the browser to confirm the path patterns route correctly:

| URL | Expected | Why |
|---|---|---|
| `/` | SPA loads | default behaviour → S3 → `/index.html` (DefaultRootObject) |
| `/anything-nonexistent` | SPA loads (not a CF error) | S3 404 → CustomErrorResponses rewrites to `/index.html` 200 |
| `/generate` (GET in browser) | API returns `{"error":"method not allowed"}` or 401 (depending on whether you're signed in) | path pattern hit → API origin (NOT the S3 rewrite — proves cache behaviours win over default) |
| `/generations/anything` | API returns 401 (no Bearer) | path pattern hit → API origin |

This is the easiest way to confirm cache behaviours are ordered correctly
(specific patterns before default). If you see the SPA at `/generate`, the
cache-behaviour wiring is wrong.

### 6.5 — Negative: bucket is genuinely private

```bash
# Direct S3 URL should be denied (not 404 — denied).
curl -sI "https://${SPA_BUCKET}.s3.eu-west-2.amazonaws.com/index.html"
```

Expect `HTTP/1.1 403 Forbidden`. If you see 200, the bucket policy or PAB is
wrong; back-track to step 2.

---

## 7. Tear-down (only if you need to)

```bash
# Empty the bucket first — CFN won't delete a non-empty bucket.
aws s3 rm "s3://${SPA_BUCKET}/" --recursive --region eu-west-2

# Versioned bucket: also delete all versions + delete markers
aws s3api delete-objects \
  --bucket "$SPA_BUCKET" --region eu-west-2 \
  --delete "$(aws s3api list-object-versions \
              --bucket "$SPA_BUCKET" --region eu-west-2 \
              --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' \
              --output json)"
aws s3api delete-objects \
  --bucket "$SPA_BUCKET" --region eu-west-2 \
  --delete "$(aws s3api list-object-versions \
              --bucket "$SPA_BUCKET" --region eu-west-2 \
              --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' \
              --output json)" || true

aws cloudformation delete-stack \
  --stack-name discharge-web --region eu-west-2
```

CloudFront `DELETE_IN_PROGRESS` also takes 10-15 min — the distribution must
be disabled and propagated before it can be removed. The bucket itself has
`DeletionPolicy: Retain`, so even if the stack delete fails, the SPA is
recoverable.

---

## Findings to record after the run

Use the same template as slice 4b — note in `docs/ADR-phase1.md` (ADR-006)
and the Notion Phase 2 record:

- **First-deploy CloudFront propagation time** (the actual minutes).
- **TTFB from a UK IP vs an EU IP** (open DevTools → Network → Timing; the
  edge-PoP latency story is the SAA-C03-relevant data point).
- **Any path that didn't route as expected** (the behaviour-evaluation
  order is easy to get wrong; document the fix if you find one).
- **CORS check: was a preflight issued?** (it should NOT be, since same-origin)
- **Response headers received** (HSTS, CSP, nosniff — confirms the policy
  attached to both API behaviours).
