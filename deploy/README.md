# Deploying the recorder

The recorder's job is to be running. Its longest outage - four days on a
laptop - cost more data than every bug in this project combined, including a
whole weekend of college football. That is what this directory is for.

## What you need

A small Linux box. The recorder uses about 10% of one core and well under a
gigabyte of RAM; the binding constraint is disk, at roughly **18 GB a month**
with dedup off and 20 book levels.

    Hetzner CX22 (Ashburn, VA)   ~EUR 3.79/mo   40 GB   ~2 months of tape
      + a 100 GB volume          ~EUR 4.40/mo           ~7 months

Ashburn keeps latency to the venue low. The volume is resizable later, which
a bigger instance is not.

## Steps

1. Create the box (Ubuntu 24.04) and note its IP.

2. Run the setup script from this machine:

       ssh root@HOST 'bash -s' < deploy/setup.sh

   It installs Python, creates a `poly` user, clones the repo, builds the
   venv, installs the systemd unit, sets up log rotation and the disk guard.

3. Copy your credentials. These are never in git:

       scp .env root@HOST:/home/poly/Polymarket/.env
       ssh root@HOST 'chown poly:poly /home/poly/Polymarket/.env && \
                      chmod 600 /home/poly/Polymarket/.env'

4. Start it:

       ssh root@HOST 'systemctl start polymarket && systemctl status polymarket'

## Checking on it

    ssh root@HOST 'journalctl -u polymarket -n 50'
    ssh root@HOST 'tail -f /home/poly/Polymarket/logs/run-$(date -u +%F).log'
    ssh root@HOST 'df -h /home/poly/Polymarket/data'

## Getting the data back

    rsync -avz --progress root@HOST:/home/poly/Polymarket/data/ ./data-server/

## What the pieces do

- `polymarket.service` restarts always, with no start limit, because a venue
  outage can fail many attempts in a row and giving up is the one behaviour
  we cannot afford. It stops with SIGTERM and waits 60s, which is what lets
  the recorder flush its gzip tapes rather than tear them.
- `diskguard.sh` runs every 15 minutes and deletes the oldest tapes when free
  space drops under 15%. A full disk does not stop the recorder - writes just
  fail and the data is silently lost - so something has to watch for it.
- Log rotation keeps 14 days compressed.

## Before you trust it

The recorder has a paper-trading mode and a real-money mode. Check
`LS_REAL_NEW_ENTRIES` in `bot/config.py` before starting on a new box.
