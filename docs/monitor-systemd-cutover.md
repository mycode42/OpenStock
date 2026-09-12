# Node monitor systemd cutover

The Node monitor lives in `scripts/monitor.js`; its maintainable core is in
`lib/monitor.js`. Both files and all runtime data are owned by `openclaw`.
`scripts/monitor.sh` remains unchanged and available as the rollback target.

Only the one-time root-owned systemd drop-in requires sudo. Future monitor code,
tests, logs, cache, and database maintenance do not.

## Verification

Run from `/opt/openstock`:

```sh
npm test
node --check lib/monitor.js
node --check scripts/monitor.js
```

Tests use temporary SQLite databases and mocked `fetch`; they do not call Yahoo,
the FX provider, or Telegram.

## Exact cutover commands

Stop the timer so it cannot race the manual proof, inspect the current units, and
install the reviewed narrow drop-in rather than replacing the existing unit:

```sh
sudo systemctl stop openstock-monitor.timer
sudo systemctl cat openstock-monitor.service
sudo systemctl cat openstock-monitor.timer
cat /opt/openstock/ops/systemd/openstock-monitor-node.conf
sudo install -D -m 0644 /opt/openstock/ops/systemd/openstock-monitor-node.conf /etc/systemd/system/openstock-monitor.service.d/10-node-monitor.conf
sudo systemctl daemon-reload
sudo systemctl start openstock-monitor.service
sudo systemctl status --no-pager -l openstock-monitor.service
sudo journalctl -u openstock-monitor.service -n 80 --no-pager
```

Confirm that the observed run ends with `Monitoring cycle complete (exit=0)`.
Exit `1` means one stock, Telegram delivery, or the equity snapshot failed;
exit `3` means no usable FX rate was available. Investigate before restarting
the timer.

After a successful observed run:

```sh
sudo systemctl start openstock-monitor.timer
sudo systemctl status --no-pager openstock-monitor.timer
```

## Exact rollback commands

```sh
sudo systemctl stop openstock-monitor.timer
sudo rm /etc/systemd/system/openstock-monitor.service.d/10-node-monitor.conf
sudo systemctl daemon-reload
sudo systemctl start openstock-monitor.service
sudo systemctl status --no-pager -l openstock-monitor.service
sudo systemctl start openstock-monitor.timer
```

The original service then resumes `/opt/openstock/scripts/monitor.sh`.
