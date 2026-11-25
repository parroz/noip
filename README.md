# Cloudflare dynamic DNS updater

This small, dependency-free Python program keeps the `A` record for
`daemon.gluglu.app` synchronized with the public IPv4 address of the machine
running it. It is intended to run from cron on a Debian host behind NAT.

Only `daemon` carries the address. The services on the same host are `CNAME`
records that point at it, so they follow it automatically and the updater never
touches them:

| Name                   | Type  | Target              |
|------------------------|-------|---------------------|
| `daemon.gluglu.app`    | A     | updated by noip.py  |
| `pandora.gluglu.app`   | CNAME | `daemon.gluglu.app` |
| `auth.gluglu.app`      | CNAME | `daemon.gluglu.app` |
| `classquiz.gluglu.app` | CNAME | `daemon.gluglu.app` |
| `console.gluglu.app`   | CNAME | `daemon.gluglu.app` |
| `pulso.gluglu.app`     | CNAME | `daemon.gluglu.app` |

The apex `gluglu.app` deliberately has no address record, so the bare domain
does not reveal the home connection; its MX and TXT records for mail remain.
To publish a new service, add a DNS-only `CNAME` to `daemon.gluglu.app` in the
Cloudflare dashboard.

The updater:

- discovers the public IPv4 address through an outside HTTPS service, with a
  second provider as fallback;
- reads the current record from Cloudflare before every change;
- creates the record when it is missing and updates only its address when it
  has changed, so an existing record's TTL, proxy status and comment are kept;
- refuses to guess if multiple matching A records exist;
- reads the Cloudflare API token from a root-only file and never logs it.

## Moving the zone from GoDaddy

The domain can stay registered at GoDaddy; only its DNS moves to Cloudflare.

1. Add `gluglu.app` to Cloudflare (the free plan is enough). Cloudflare scans
   the existing records, but its scan can miss some (here it missed `pandora`,
   the three CNAMEs and the SPF include TXT). Compare every name against the
   old provider before switching.
2. At GoDaddy, replace the domain's nameservers with the two Cloudflare
   assigns, and wait until Cloudflare reports the zone as active.
3. Install this version as described below. The installer removes the old
   `/etc/cron.d/noip-godaddy` job.
4. Delete `/etc/noip/godaddy.pat` and revoke the GoDaddy token.

## Credential

Create a Cloudflare API token (My Profile → API Tokens → Create Token) from the
**Edit zone DNS** template, restricted to:

- Permissions: `Zone` → `DNS` → `Edit`
- Zone Resources: `Include` → `Specific zone` → `gluglu.app`

Use a scoped API token, not the Global API Key. Store the raw token as the only
line of `/etc/noip/cloudflare.token`. Never put it in this repository or pass it
on the command line.

An interactive command that does not place the token in shell history is:

```sh
sudo install -d -o root -g root -m 700 /etc/noip
sudo bash -c 'umask 077; read -rsp "Cloudflare API token: " token; printf "\n"; printf "%s\n" "$token" > /etc/noip/cloudflare.token'
```

## Install on Debian

Clone the repository under `/srv/noip`, add the token as shown above, then run:

```sh
cd /srv/noip
sudo ./deploy/install.sh
sudo /usr/bin/python3 /srv/noip/noip.py
```

To inspect the public address and planned DNS action without writing to
Cloudflare:

```sh
sudo /usr/bin/python3 /srv/noip/noip.py --dry-run
```

The installer creates `/etc/cron.d/noip-cloudflare` with a default five-minute
schedule. To choose another schedule, reinstall with a five-field cron value:

```sh
sudo ./deploy/install.sh --schedule '*/10 * * * *'
```

Record names can also be selected explicitly by repeating `--record`. Omitting
the option installs the default, `daemon`:

```sh
sudo ./deploy/install.sh --record daemon
```

Cron sends updater output to syslog under the tag `noip-cloudflare`. On systems
using journald, inspect recent entries with:

```sh
journalctl -t noip-cloudflare
```

## TTL and proxying

Records the updater creates use Cloudflare's automatic TTL (`--ttl 1`) by
default; any value from 60 to 86400 seconds is also accepted. New records are
DNS-only unless `--proxied` is given, in which case Cloudflare's proxy (the
orange cloud) is enabled and the TTL is always automatic. Both options only
affect records the updater creates: when it updates an existing record it
changes the address and nothing else, so proxy status set in the dashboard is
preserved.

## Test

No network calls or credentials are needed for the test suite:

```sh
python3 -m unittest discover -s tests -v
```
