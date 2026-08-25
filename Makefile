.PHONY: install uninstall deinstall restart

EXT_DIR := $(HOME)/.local/share/nautilus-python/extensions

install:
	mkdir -p $(EXT_DIR)
	cp taildrop.py $(EXT_DIR)/
	@echo "Installation successful. Restart Nautilus with: make restart"

uninstall:
	rm -f $(EXT_DIR)/taildrop.py
	@echo "Uninstallation complete."

deinstall: uninstall

restart:
	-nautilus -q
	@echo "Nautilus restarted."
