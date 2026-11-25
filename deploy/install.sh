#!/bin/sh
set -eu

schedule='*/5 * * * *'
zone='gluglu.app'
records=''
ttl='1'
proxied=''
token_file='/etc/noip/cloudflare.token'
cron_file='/etc/cron.d/noip-cloudflare'
legacy_cron_file='/etc/cron.d/noip-godaddy'

usage() {
    printf '%s\n' "Usage: sudo $0 [--schedule '*/5 * * * *'] [--zone gluglu.app] [--record NAME ...] [--ttl 1] [--proxied]"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --schedule) schedule=$2; shift 2 ;;
        --zone) zone=$2; shift 2 ;;
        --record) records="$records $2"; shift 2 ;;
        --ttl) ttl=$2; shift 2 ;;
        --proxied) proxied=' --proxied'; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Run this installer as root.' >&2
    exit 1
fi

set -f
set -- $schedule
if [ "$#" -ne 5 ]; then
    printf '%s\n' 'The cron schedule must contain exactly five fields.' >&2
    exit 2
fi

case "$zone$records$ttl$schedule" in
    *"
"*) printf '%s\n' 'Configuration values must not contain newlines.' >&2; exit 2 ;;
esac

case "$ttl" in
    *[!0-9]*|'') printf '%s\n' 'TTL must be an integer.' >&2; exit 2 ;;
esac
if [ "$ttl" -ne 1 ] && { [ "$ttl" -lt 60 ] || [ "$ttl" -gt 86400 ]; }; then
    printf '%s\n' 'TTL must be 1 (automatic) or between 60 and 86400.' >&2
    exit 2
fi
case "$zone" in
    *[!A-Za-z0-9.-]*|'') printf '%s\n' 'Zone contains unsupported characters.' >&2; exit 2 ;;
esac
if [ -z "$records" ]; then
    records=' daemon'
fi
record_args=''
for record in $records; do
    case "$record" in
        *[!A-Za-z0-9_.*@-]*|'') printf '%s\n' 'Record contains unsupported characters.' >&2; exit 2 ;;
    esac
    record_args="$record_args --record $record"
done

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
install -d -o root -g root -m 700 /etc/noip
if [ ! -e "$token_file" ]; then
    install -o root -g root -m 600 /dev/null "$token_file"
fi
chown root:root "$token_file"
chmod 600 "$token_file"
chmod 755 "$project_dir/noip.py"

cron_tmp=$(mktemp)
trap 'rm -f "$cron_tmp"' EXIT HUP INT TERM
{
    printf '%s\n' 'SHELL=/bin/sh'
    printf '%s\n' 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    printf '%s\n' 'MAILTO=""'
    printf '\n%s root /usr/bin/python3 %s/noip.py --zone %s%s --ttl %s%s --token-file %s 2>&1 | /usr/bin/logger -t noip-cloudflare\n' \
        "$schedule" "$project_dir" "$zone" "$record_args" "$ttl" "$proxied" "$token_file"
} > "$cron_tmp"
install -o root -g root -m 644 "$cron_tmp" "$cron_file"

printf 'Installed cron job in %s (%s).\n' "$cron_file" "$schedule"
if [ -e "$legacy_cron_file" ]; then
    rm -f "$legacy_cron_file"
    printf 'Removed the old GoDaddy cron job %s.\n' "$legacy_cron_file"
fi
if [ ! -s "$token_file" ]; then
    printf 'Token file %s is empty; add the API token before running the updater.\n' "$token_file"
fi
