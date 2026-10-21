# M0 Check 8: Routing Provider Credentials and Budget Enforcement

**Purpose.** Routing goes through a provider abstraction (ADR-0006): Waze via a
Home Assistant relay, and Google Routes under a $10/month **app-enforced** cap
with an $8 warning to Morgan. This check prepares both providers and proves the
spending controls before any real call.

## Part A: Google Cloud (Routes API)

1. In the Google Cloud console, select or create a project dedicated to Family
   Logistics. Confirm a billing account is attached (Routes API requires it).
2. Enable **Routes API** only.
3. Create an API key and restrict it:
   - **API restrictions**: Routes API only.
   - **Application restrictions**: IP addresses, set to the Docker host's public
     egress address (the WAN address your firewall NATs the Docker host to;
     find it with `curl -s https://ifconfig.me` run on the Docker host). Record only that the
     restriction is set, not the address.
4. **Budgets and alerts**: create a budget for the project of $10/month with
   alert thresholds at $8 (80%) and $10 (100%), notifying Morgan.
   Google budgets **alert only; they do not stop spending**. The app-side
   counter (`FL_ROUTES_MONTHLY_BUDGET_USD=10`) is the hard stop.
5. Optional: set a per-day quota on `ComputeRoutes` requests in **APIs and
   services, Quotas** as an additional ceiling.
6. Store the key in the secret store as `FL_GOOGLE_ROUTES_API_KEY`
   of the deployment. Never in
   Git, never in a chat or ticket.
7. Smoke test from the Docker host with a route between two public city landmarks
   (never a home, school pickup point or any family address), and confirm a
   200 response and a duration:

   ```bash
   curl -s -X POST https://routes.googleapis.com/directions/v2:computeRoutes \
     -H 'Content-Type: application/json' \
     -H "X-Goog-Api-Key: ${FL_GOOGLE_ROUTES_API_KEY}" \
     -H 'X-Goog-FieldMask: routes.duration' \
     -d '{"origin":{"address":"<public landmark A>"},"destination":{"address":"<public landmark B>"},"travelMode":"DRIVE"}'
   ```

   Run with the key in the environment only (`read -rs FL_GOOGLE_ROUTES_API_KEY; export FL_GOOGLE_ROUTES_API_KEY`),
   then `unset` it.
8. Confirm the same request from a host that is **not** the Docker host is refused
   (proves the IP restriction).

## Part B: app-side cap (verified in M3, planned here)

Record the pricing tier used to convert calls to dollars (Routes "Basic" or
"Advanced" SKU depending on the field mask and waypoint optimisation) so the
app's counter can price each call conservatively. M3 must demonstrate with the
`static` provider and a fake price table that:

- at $8 cumulative, one warning goes to `fl-ops` (Morgan);
- at $10, discretionary calls stop, non-routing validation continues, and trips
  without a recent estimate are marked travel-feasibility `incomplete`.

## Part C: Waze relay presence

From check 3: is `waze_travel_time` installed and its action available? If not,
install the integration in Home Assistant (no account needed) and record the
region setting. The relay automation itself is built in M3.

## Pass criteria

- Key restricted to Routes API and Docker host egress; refused elsewhere.
- Budget with $8 and $10 alerts exists and notifies Morgan.
- Smoke test returns a duration from the Docker host.
- Waze action availability is known.

## Record

Section 8 of [RESULTS-TEMPLATE.md](RESULTS-TEMPLATE.md). Never record the key,
the project's billing details or the egress IP.
