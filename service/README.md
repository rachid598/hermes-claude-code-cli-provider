# Managed service support

This directory contains optional service templates for keeping the local Claude Code CLI shim alive across logout/reboot.

- Linux: `systemd --user` unit installed by `scripts/install-service.sh`.
- macOS: `launchd` LaunchAgent installed by `scripts/install-service.sh`.

The scripts are opt-in and do not run during plugin install. They template paths from the current checkout / plugin location and the selected `HERMES_HOME`.

Useful checks after installation:

```bash
# Linux
systemctl --user status claude-code-cli-shim
journalctl --user -u claude-code-cli-shim -n 50

# macOS
launchctl print gui/$(id -u)/com.hermes.claude-code-cli-shim
tail -n 50 "${HERMES_HOME:-$HOME/.hermes}/logs/claude-code-cli-shim.err.log"
```
