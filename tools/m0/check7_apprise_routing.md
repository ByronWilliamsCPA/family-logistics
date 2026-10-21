# M0 Check 7: Apprise Routing and Recipients

**Purpose.** Recipient routing lives in Apprise, not in Family Logistics; the app
only chooses tags. Before any family-facing message is sent we must prove that
each Family Logistics tag reaches exactly the intended people and channels.

## Final tag set

| Tag | App setting | Message | Recipients | Channel |
|-----|-------------|---------|------------|---------|
| `fl-report` | `FL_APPRISE_TAG_REPORT` | Weekly and daily report | Morgan and Riley | e-mail (HTML) |
| `fl-status` | `FL_APPRISE_TAG_STATUS` | Assessment complete (clear / attention required / incomplete) | Morgan and Riley | short push |
| `fl-warning` | `FL_APPRISE_TAG_WARNING` | New validation errors; "could not verify" for an imminent ride | Morgan and Riley | push |
| `fl-urgent` | `FL_APPRISE_TAG_URGENT` | Critical issue for an imminent ride; bypasses quiet hours | Morgan and Riley | urgent push |
| `fl-ops` | `FL_APPRISE_TAG_OPS` | Service health, $8 routing budget warning | Morgan only | push |
| `fl-shadow` | `FL_APPRISE_TAG_SHADOW` | All traffic while `FL_NOTIFICATION_MODE=shadow` | Morgan only | e-mail and push |

Signal and SMS are future options only and must not be reachable from any
`fl-*` tag.

## Rule

Family Logistics **never** sends to shared or generic severity tags (for
example `info`, `warning`, `high`, `critical`, `all`, `email`, `push`, `sms`,
`signal`). Those tags may already fan out to operations channels, SMS or public
topics that must never carry children's schedules. The app sends only to the
six `fl-*` tags, and each tag is routed in Apprise to private channels only.

## Steps

Set `APPRISE_URL` to your Apprise API base URL before running the commands
below (the value comes from the private deployment repository; do not record
it in this repository).

1. Review the Apprise configuration in the private deployment repository and
   list, for each existing tag, which channels it reaches. Confirm who receives
   the existing e-mail and push channels today, and whether a private push
   channel exists for each parent. Note whether the API requires
   authentication.
2. In the private deployment repository, add URLs carrying only `fl-*` tags: an
   e-mail route with both parents as recipients (`fl-report`), push routes for
   both parents (`fl-status`, `fl-warning`, `fl-urgent`), and admin-parent-only
   routes (`fl-ops`, `fl-shadow`). Recipient addresses go in the deployment's
   secret store, never in Git. Confirm no `fl-*` tag is attached to an SMS,
   Signal or public-topic URL.
3. Send one test message per tag with an obviously fake body from the home
   network:

   ```bash
   : "${APPRISE_URL:?set APPRISE_URL to your Apprise API base URL}"
   for tag in fl-report fl-status fl-warning fl-urgent fl-ops fl-shadow; do
     curl -s -X POST "${APPRISE_URL}/notify/${tag}" -d "body=FL M0 test ${tag}"
   done
   ```

4. Each parent confirms receipt of exactly the expected tags (Riley must
   receive nothing from `fl-ops` or `fl-shadow`), and that no SMS, Signal or
   public-topic message arrived.
5. Send an HTML test body to `fl-report` (`-d format=html`) and confirm it
   renders on both parents' mail clients.

## Pass criteria

- Each `fl-*` tag reaches exactly the recipients and channels in the table.
- No `fl-*` tag reaches SMS, Signal or a public topic.
- A both-parents e-mail route exists; HTML renders for both.

## Record

Section 7 of [RESULTS-TEMPLATE.md](RESULTS-TEMPLATE.md): tag names, channel types
and recipient roles (for example "both parents"). No addresses, numbers or topic
names.
