"""Cross-platform per-device daemon primitives.

Internal library code intended for mechanical extraction to
``embedded_bridge.daemon`` once stable. The module layout here mirrors
its future home so extraction is ``git mv`` plus an import-path rewrite.

PPK2-specific types must NOT cross into this package — inputs are plain
``str`` (serial), ``Path``, and ``dict``. Parametrising constants
(``STATE_DIR_NAME``, ``LABEL_PREFIX``, ``_program_args``) live at module
top so a consumer can override them after import.
"""
