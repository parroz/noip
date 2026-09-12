# GoDaddy dynamic DNS updater

This small, dependency-free Python program keeps the `A` records for
`gluglu.app`, `pandora.gluglu.app`, and `auth.gluglu.app` synchronized with the
public IPv4 address of the machine running it. It is intended to run from cron
on a Debian host behind NAT.

The updater:

- discovers the public IPv4 address through an outside HTTPS service, with a
  second provider as fallback;
- reads the current record from GoDaddy before every change;
- creates the record when it is missing, updates it only when the address has
  changed, and preserves an existing record's TTL;
- refuses to guess if multiple matching A records exist;
- reads the GoDaddy PAT from a root-only file and never logs it.

## Credential

Create a GoDaddy Personal Access Token with only these scopes:

- `domains.domain:read`
- `domains.dns:update`

Store the raw token as the only line of `/etc/noip/godaddy.pat`. Never put it in
this repository or pass it on the command line.

An interactive command that does not place the token in shell history is:

```sh
sudo install -d -o root -g root -m 700 /etc/noip
sudo bash -c 'umask 077; read -rsp "GoDaddy PAT: " token; printf "\n"; printf "%s\n" "$token" > /etc/noip/godaddy.pat'
```

## Install on Debian

Clone the repository under `/srv/noip`, add the token as shown above, then run:

```sh
cd /srv/noip
sudo ./deploy/install.sh
sudo /usr/bin/python3 /srv/noip/noip.py
```

To inspect the public address and planned DNS action without writing to GoDaddy:

```sh
sudo /usr/bin/python3 /srv/noip/noip.py --dry-run
```

The installer creates `/etc/cron.d/noip-godaddy` with a default five-minute
schedule. To choose another schedule, reinstall with a five-field cron value:

```sh
sudo ./deploy/install.sh --schedule '*/10 * * * *'
```

Record names can also be selected explicitly by repeating `--record`. Omitting
the option installs the three defaults (`@`, `pandora`, and `auth`):

```sh
sudo ./deploy/install.sh --record @ --record pandora --record auth
```

Cron sends updater output to syslog under the tag `noip-godaddy`. On systems
using journald, inspect recent entries with:

```sh
journalctl -t noip-godaddy
```

The minimum permitted GoDaddy TTL is 600 seconds. This is independent of the
cron polling interval.

## Test

No network calls or credentials are needed for the test suite:

```sh
python3 -m unittest discover -s tests -v
```
