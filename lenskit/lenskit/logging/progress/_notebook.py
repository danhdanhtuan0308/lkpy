"""
Jupyter notebook progress support.
"""

from __future__ import annotations

from time import perf_counter

import ipywidgets as widgets
from IPython.display import display

from ._base import Progress

__all__ = ["JupyterProgress"]


class JupyterProgress(Progress):
    """
    Progress logging to Jupyter notebook widgets.
    """

    widget: widgets.IntProgress
    box: widgets.HBox
    total: int | None
    current: int
    _last_update: float = 0
    _field_format: str | None = None

    def __init__(
        self,
        label: str | None,
        total: int | None,
        fields: dict[str, str | None],
    ):
        self.current = 0
        self.total = total
        if total:
            self.widget = widgets.IntProgress(value=0, min=0, max=total, step=1)
        else:
            self.widget = widgets.IntProgress(value=1, min=0, max=1, step=1)
            self.widget.bar_style = "info"

        pieces = []
        if label:
            pieces.append(widgets.Label(value=label))
        pieces.append(self.widget)
        self.box = widgets.HBox(pieces)
        display(self.box)

        if fields:
            self._field_format = ", ".join(
                [f"{name}: {fs or None}" for (name, fs) in fields.items()]
            )

    def update(self, advance: int = 1, **kwargs: float | int | str):
        """
        Update the progress bar.
        """
        self.current += advance
        now = perf_counter()
        if now - self._last_update >= 0.1 or (self.total and self.current >= self.total):
            self.widget.value = self.current
            self._last_update = now
        # if self._field_format:
        #     self.tqdm.set_postfix_str(self._field_format.format(kwargs))

    def finish(self):
        """
        Finish and clean up this progress bar.  If the progresss bar is used as
        a context manager, this is automatically called on context exit.
        """
        self.box.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.finish()
