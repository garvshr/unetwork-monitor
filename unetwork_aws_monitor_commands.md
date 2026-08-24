# AWS U Network Monitor — Quick Commands

## 1. Stop the monitor

```bash
sudo systemctl stop unetwork-monitor
```

## 2. Verify that it is stopped

```bash
sudo systemctl status unetwork-monitor --no-pager
```

You should see:

```text
Active: inactive (dead)
```

You can also run:

```bash
sudo systemctl is-active unetwork-monitor
```

Expected:

```text
inactive
```

## 3. Edit your file

Open the file you need to edit, for example:

```bash
nano config.json
```

### Save and exit nano

1. `Ctrl + O`
2. Press `Enter`
3. `Ctrl + X`

### Undo in nano

```text
Alt + U
```

> Note: `Alt + X` is not the normal nano undo command. Use `Alt + U` for Undo.

## 4. Check the file after editing

For `config.json`:

```bash
python3 -m json.tool config.json > /dev/null && echo "CONFIG OK"
```

Expected:

```text
CONFIG OK
```

## 5. Start the monitor again

```bash
sudo systemctl start unetwork-monitor
```

## 6. Check that the monitor is active

```bash
sudo systemctl status unetwork-monitor --no-pager
```

You should see:

```text
Active: active (running)
```

Quick check:

```bash
sudo systemctl is-active unetwork-monitor
```

Expected:

```text
active
```

## 7. Check live logs

```bash
tail -f ~/unetwork-monitor/monitor.log
```

Press `Ctrl + C` to stop watching the logs.

> `Ctrl + C` here only stops viewing the live logs. It does NOT stop the monitor service.

## 8. Check the previous 100 logs

```bash
tail -100 ~/unetwork-monitor/monitor.log
```

## 9. Useful: view the complete log page-by-page

```bash
less ~/unetwork-monitor/monitor.log
```

Controls inside `less`:

- `↑ / ↓` — move
- `Space` — next page
- `b` — previous page
- `g` — beginning
- `G` — end
- `q` — exit

## 10. Recommended quick workflow

```bash
sudo systemctl stop unetwork-monitor
sudo systemctl status unetwork-monitor --no-pager
nano config.json
# Save: Ctrl+O, Enter, Ctrl+X
# Undo in nano: Alt+U
python3 -m json.tool config.json > /dev/null && echo "CONFIG OK"
sudo systemctl start unetwork-monitor
sudo systemctl is-active unetwork-monitor
tail -100 ~/unetwork-monitor/monitor.log
tail -f ~/unetwork-monitor/monitor.log
```
