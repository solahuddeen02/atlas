# Security policy

## Reporting a vulnerability

**Please report vulnerabilities privately, through GitHub. Don't open a public issue, pull request or discussion.**

1. Go to the repository's **Security** tab.
2. Choose **Report a vulnerability**. This uses GitHub's private vulnerability reporting, so only the maintainer sees it.
3. Describe what you found:
   - which part is affected (`atlas/`, `deploy/`, `legacy/`, a workflow…) and which commit;
   - how to reproduce it, and what an attacker gains;
   - any fix or mitigation you suggest.

Please don't include real credentials, personal data or other people's systems in a report. Test only against an instance you run yourself.

## What to expect

Atlas is maintained by one person, so there is no guaranteed response time. As a guide:

- You'll get an acknowledgement, usually within a few days.
- Once the issue is confirmed, the fix is worked on privately and released as soon as it's ready.
- With your permission, you'll be credited in the advisory.

Please give the fix a chance to ship before you disclose the issue publicly.

## Supported versions

Atlas has **no releases yet** (it's pre-1.0; see [README](README.md#versioning)). Only the `main` branch is supported, and fixes land there.

| Part | Status |
|---|---|
| `main`: the new platform (`atlas/`, `modules/`, `deploy/`) | Supported |
| `legacy/` (the frozen Drive/Photos app) | Security fixes only |

Once releases start, this table will list which ones get fixes.

## Scope notes

- **How an instance is exposed** (tunnel, reverse proxy, VPN, firewall) is the operator's responsibility. [docs/SELF_HOSTING.md](docs/SELF_HOSTING.md) and [ADR-003](docs/adr/003-internet-exposure.md) describe the supported setups. Something that is only exploitable because an instance ignores that guidance is a documentation issue, not a vulnerability; it's still welcome as a normal issue.
- **Secrets** belong in the secrets directory, never in the repo. If you find a secret committed to this repository, report it privately as above.
