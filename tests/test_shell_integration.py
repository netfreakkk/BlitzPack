"""Unit tests for Windows Explorer shell context menu integration."""

from __future__ import annotations

import sys

import pytest

if sys.platform != "win32":
    pytest.skip("Windows Explorer Shell integration requires Windows", allow_module_level=True)

from blitzpack.shell_integration import (
    is_shell_context_menu_registered,
    register_shell_context_menu,
    unregister_shell_context_menu,
)


def test_shell_context_menu_registration_cycle():
    # 1. Register context menu
    registered = register_shell_context_menu()
    assert registered is True
    assert is_shell_context_menu_registered() is True

    # 2. Unregister context menu
    unregistered = unregister_shell_context_menu()
    assert unregistered is True
    assert is_shell_context_menu_registered() is False
