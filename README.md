# nautilus-taildrop

Nautilus extension that integrates with
[taildrop](https://tailscale.com/kb/1106/taildrop/).

Adds a context menu item to Nautilus so you can send and receive files seamlessly across your Tailscale devices.

As everyone loves a demo, there is one
[here.](https://www.youtube.com/watch?v=KXvxQX_CKx4)

## Prerequisites

Make sure the Nautilus Python bindings are installed on your system:

- **Ubuntu / Debian**:
  ```bash
  sudo apt install python3-nautilus
  ```
- **Fedora**:
  ```bash
  sudo dnf install nautilus-python
  ```
- **Arch Linux**:
  ```bash
  sudo pacman -S python-nautilus
  ```

Ensure your user is configured as a Tailscale operator to allow file operations without root:
```bash
tailscale up --operator=$USER
```

## Installation

You can install the extension using `make`:

```bash
make install
make restart
```

Or manually:

```bash
mkdir -p ~/.local/share/nautilus-python/extensions/
cp taildrop.py ~/.local/share/nautilus-python/extensions/
nautilus -q
```

## Uninstallation

```bash
make uninstall
make restart
```

## License

MIT
