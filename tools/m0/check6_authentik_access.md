# M0 Check 6: Authentik Accounts and the `family-logistics` Group

**Purpose.** The registry web UI and reports sit behind the deployment's
single sign-on proxy (for example Traefik forward auth with Authentik). Access
is limited to an identity-provider group `family-logistics` with Morgan and
Riley as editors. Both parents' accounts must be verified or created before M4.

## Prerequisites

- Authentik admin access.
- Knowledge of how the site manages Authentik objects. If groups, applications
  and providers are managed as code (for example OpenTofu), create the group
  through a pull request in the private deployment repository rather than by
  hand in the UI.

## Steps

1. In Authentik, **Directory, Users**: search for Riley.
   - If found: confirm the account is active, has a verified e-mail, and has
     MFA enrolled per the site's policy.
   - If not found: create (or enroll) the account through the site's normal
     process. Do not record the e-mail address in Git or in the results file.
2. Check whether a `family-logistics` group already exists.
3. Create the group `family-logistics` (in OpenTofu if Authentik RBAC is managed
   there; otherwise in the UI and note it for later codification). Members:
   Morgan, Riley.
4. The Authentik application and proxy provider for the registry UI hostname
   are created in the deploy pull request in the private deployment
   repository, bound to this group. Nothing else is needed in M0.
5. Have Riley sign in to Authentik once from their phone on the home network to
   confirm credentials and MFA work.

## Pass criteria

- Both parents have active Authentik accounts with MFA.
- The `family-logistics` group exists with exactly Morgan and Riley.
- How the group is managed (OpenTofu or UI) is recorded.

## Record

Section 6 of [RESULTS-TEMPLATE.md](RESULTS-TEMPLATE.md): yes/no answers and the
management method only.
