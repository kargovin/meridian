"""The Summarization & Classification Platform (RFC A1).

The directory containing this package is named ``platform`` and must never gain an
``__init__.py``. That would make it a package shadowing the standard library's ``platform``
module, which SQLAlchemy and uvicorn both import — SQLAlchemy fails immediately with
``module 'platform' has no attribute 'python_implementation'``. As a plain directory it is
only a namespace portion, and a namespace portion loses to a stdlib module, so the name is
safe as long as it stays a plain directory.
"""
