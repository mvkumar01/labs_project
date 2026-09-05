# Live administration and order routing

## Operator workflow

The administration page is `/live/admin` on the deployed Labs domain. It is
visible only to live administrators. Normal accounts can read only their own
readiness and egress information on `/live/` and `/live/readiness`.

1. Create a named route with the authenticated proxy URL and its one or two
   expected public IPs. The URL is encrypted with the existing `LABS_CRED_KEY`
   and is never rendered again. Blank URL fields preserve the stored secret.
   For initial migration, the optional import checkbox reads the existing
   deployment proxy on the server without displaying or retyping its secret.
2. Verify egress. This makes one HTTPS request to api.ipify.org through the proxy
   and consumes one request from the route budget. It sends no broker credentials.
3. Connect/authenticate the intended broker account. In Kite/SmartAPI, whitelist its route IPs.
   Labs cannot edit the broker's own allowlist. A successful probe observes one
   provider-selected IP; it does not prove every possible egress IP is configured.
4. Disarm the affected connection and resolve open positions, pending orders,
   and mismatches. Assign the route to that connection and attest that the broker
   allowlist was configured. Different accounts may use different proxy endpoints.
5. Connect the broker, inspect readiness, arm dry-run, then arm live when ready.

The UI also edits daily loss caps and lot count within the existing phase cap
(one lot), grants/revokes delegated administrators, and shows settings and
transport audit records with IST timestamps. Strategy choice remains in each
user's existing Configure page. Audit tables show the latest 100 records;
older records remain in the database.

## Routing and limits

- `proxy_routes` contains encrypted endpoints, expected IPs, revisions, enabled
  state, probe results, and request budgets.
- `live_proxy_assignments` binds a user/connection to a specific confirmed route
  revision. There is no automatic fallback to the old environment proxy.
- `live/brokers/order_transport.py` handles entry, exit, modification and
  cancellation HTTP requests. Both adapters send entries/exits through it.
  Modification/cancellation additionally require a broker order ID owned by the
  connection. No web route submits orders, and no new strategy is introduced.
- Each request uses its own `requests.Session` with `trust_env=False`, explicit
  proxies, certificate verification, bounded timeouts, and redirects disabled.
  It does not mutate SDK clients or process-wide proxy environment variables.
  Auth, quotes, positions, funds and order-status reads retain the existing direct
  broker paths. There are no automatic transport retries.
- SQLite `BEGIN IMMEDIATE` reserves quota before transmission, across workers
  and processes. Requests, including failed/uncertain requests and probes, count
  against the route. Each account also has conservative limits across assignments.
  Route limits are capped at 5 requests/sec, 100/minute and 1000/day. Account limits
  are 5/sec, 100/minute and 1000 per rolling 24 hours.
- Daily and monthly route windows use the IST calendar. This is a local request
  budget, not a provider billing meter. Configure budgets from the provider's
  remaining allowance, especially if other applications share the subscription;
  external requests and provider byte-based billing are not tracked here.
- An exit/cancellation reserve stops entries and modifications before consuming
  the last daily/monthly budget. Hard exhausted budgets still block all requests;
  rate-limit capacity is not reserved by priority.
- A missing, disabled, unverified or unconfirmed route blocks orders. Rotating
  any route setting creates a new revision, clears its probe, and requires fresh
  assignments. Rotation/assignment cannot proceed on armed, open or unresolved
  connections. Creating a new route and moving idle accounts individually permits
  staged rotation without changing other accounts.
- An uncertain or interrupted request blocks further requests for that connection,
  including new intent IDs. Do not retry or clear it blindly. Reconcile the broker
  order book and actual position with the order ledger first. This release exposes
  these requests in the audit UI but deliberately has no unchecked "clear" button.

## Broker switching

Visiting/selecting a broker or starting Kite login no longer changes the active
broker. Successful authentication and an atomic idle-state check precede selection.
The persisted selection overrides stale browser-session selections. All other
connections must be disarmed and free of unresolved positions/orders before a
switch. An explicit "Use this connected broker" action supports already-connected
accounts. Kite callbacks require an expiring, single-use session state value.

Credential edits and connection identity updates commit together. Identity changes
invalidate the existing proxy assignment. Access-token-only refreshes preserve it.
The runner refreshes cached adapters when credentials change. Arming and disarming
write mode and armed state together to avoid partial transitions.

## PA rollout

No new always-on task is required. This change needs a coordinated web reload and
restart of the existing `pa_live_runner.py` task after route configuration.

Do not deploy by hot-pulling into a running trading process. Outside trading,
disarm accounts, resolve open/pending orders, stop the live runner, and take a
consistent SQLite backup of `storage/live.db` plus the existing private configuration.
Inspect the PA working tree and deploy the reviewed commit while retaining private
files and existing runtime data. Do not stash/remove untracked credentials blindly.

After deployment, the existing app startup creates the new tables idempotently.
Grant the first administrator once from the PA shell:

```bash
cd /home/mvkumar01/labs_project
python3 tools/bootstrap_live_admin.py vipin
```

The bootstrap refuses to run if an administrator already exists. Alternatively,
`LIVE_ADMIN_USER_IDS` may contain comma-separated immutable user IDs in the private
environment; usernames do not grant admin access. Further delegation happens in
the UI. The normal web/runner environment loader supports this key and
`LIVE_PROXY_ALLOWED_HOSTS` (exact extra proxy hostnames). QuotaGuard subdomains are
approved by default; arbitrary proxy hosts, private IP targets and SOCKS URLs are
not accepted in this version.

Reload the web app, create/verify routes, and explicitly assign each existing
connection. Existing environment proxy settings are not silently migrated or
assumed to be broker-whitelisted. The old runner decision ABI is rejected by the
new web app. Restart the runner and check the fresh
`alpha-v2.11b-live-v2-routing` heartbeat, route readiness, selected broker, and kill
state before rearming each account. Connected disarmed accounts now receive a
heartbeat without enrolling their strategy or issuing orders.

For rollback, stop the live runner and disarm before reverting code. Do not discard
post-deployment order ledger/audit rows or restore an old database over new trades.
The additive routing tables may stay in place while code is rolled back.

## Validation and sources

Tests use temporary databases and mocked HTTP responses. They cover account
isolation, route revision invalidation, concurrent quota reservations, exit reserve,
no direct fallback, no retry after uncertainty, both broker request formats,
credential changes, OAuth callback state, CSRF/admin access and existing exit safety.
UI inspection uses a temporary local database with fake credentials and live orders
disabled. No test submits a production order.

- [Kite order API](https://kite.trade/docs/connect/v3/orders/)
- [Kite limits](https://kite.trade/docs/connect/v3/exceptions/)
- [Kite login and redirect parameters](https://www.kite.trade/docs/connect/v3/user/)
- [SmartAPI order rate limits](https://smartapi.angelone.in/docs/Portfolio)
- [Angel SDK request implementation](https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartConnect.py)

Follow-up improvements: provider quota/billing integration, external broker
allowlist verification where an API exists, and a reconciliation workflow that
links uncertain requests to broker execution evidence. These must not infer fills
or flatten state from an operator's unchecked button click.
