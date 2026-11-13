import os
import sys

# "python -m cutvelvet" puts the current folder first on Python's import path. When that folder is a project (which
# can come from anyone), a json.py or numpy/ lying in it must not be imported in place of the real module.
if sys.path and sys.path[0] in ('', os.getcwd()) and not os.path.isfile(os.path.join(os.getcwd(), 'cutvelvet', '__main__.py')):
    del sys.path[0]

from .cli import main  # noqa: E402
main()
